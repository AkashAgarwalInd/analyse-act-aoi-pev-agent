"""Repo-confined tools for the stage agents, plus the deterministic git/cache helpers the graph calls itself."""

import re
import shlex
import subprocess
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool

MAX_OUTPUT = 8000
GIT_READONLY = {"log", "show", "diff", "blame", "status", "ls-files", "rev-parse", "grep"}
# Allowlist, not a blocklist: options like --no-index, --contents, -f, -O or --output read or run things
# outside the repo, and new ones appear in every git release.
GIT_FLAGS = {
    "--", "-p", "--patch", "-s", "--stat", "--shortstat", "--name-only", "--name-status", "--oneline",
    "--graph", "--decorate", "--no-color", "--reverse", "--all", "--follow", "--first-parent", "--no-merges",
    "--cached", "--staged", "-w", "--ignore-all-space", "-b", "--short", "--porcelain", "--untracked-files",
    "-u", "--others", "--modified", "--deleted", "-i", "--ignore-case", "-E", "-F", "-I", "-l", "-n",
    "--line-number", "-e", "-S", "-G", "-L", "--abbrev-ref", "--verify", "-q", "--quiet", "--show-toplevel",
    "--word-diff", "--date=iso", "--date=short", "--date=relative",
}
GIT_VALUE_FLAGS = (
    "--format=", "--pretty=", "--since=", "--until=", "--after=", "--before=", "--author=", "--grep=",
    "--max-count=", "--unified=", "--abbrev=", "--untracked-files=", "--diff-filter=",
)


def skill_dir() -> Path:
    """The analyse-act skill: bundled into the wheel, or the repo copy in an editable install."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "skill", here.parents[2] / "skills" / "analyse-act"):
        if (candidate / "SKILL.md").is_file():
            return candidate
    raise FileNotFoundError("analyse-act skill not found; reinstall analyse-act-agent")


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n… truncated ({len(text)} chars)"


def _run(args: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


# --- deterministic helpers (called by graph nodes, not by the model) ---


def cache_check(repo: str) -> dict:
    """Run aa-cache.sh and parse its key=value output."""
    out = _run(["sh", str(skill_dir() / "scripts" / "aa-cache.sh"), repo], Path(repo)).stdout
    result: dict = {"changed": []}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key == "changed":
            result["changed"].append(value)
        elif key:
            result[key] = value
    return result


def cache_stamp(repo: str) -> str:
    out = _run(["sh", str(skill_dir() / "scripts" / "aa-cache.sh"), "--stamp", repo], Path(repo)).stdout
    return out.strip().removeprefix("stamped=")


def slug(task: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")
    return s[:limit].rstrip("-") or "task"


def create_branch(repo: str, task: str) -> str:
    """git switch -c aa/<slug>, appending -2, -3, … on a clash. Never commits."""
    base = f"aa/{slug(task)}"
    name, n = base, 1
    while _run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{name}"], Path(repo)).returncode == 0:
        n += 1
        name = f"{base}-{n}"
    proc = _run(["git", "switch", "-c", name], Path(repo))
    if proc.returncode != 0:
        raise RuntimeError(f"git switch -c {name} failed: {proc.stderr.strip()}")
    return name


def git_head(repo: str) -> str:
    proc = _run(["git", "rev-parse", "--verify", "-q", "HEAD"], Path(repo))
    return proc.stdout.strip() if proc.returncode == 0 else ""


def diff_stats(repo: str, exclude: list[str]) -> tuple[list[str], int]:
    """Files changed vs HEAD (tracked and untracked, minus .aa/ and `exclude`) and their LOC.

    LOC per file is max(added, deleted), so a rewrite counts its size and a pure deletion isn't free."""
    root = Path(repo)
    if not git_head(repo):
        return [], 0
    spec = ["--", ".", ":(exclude).aa"]
    stats: dict[str, int] = {}
    for line in _run(["git", "diff", "--numstat", "--no-renames", "HEAD", *spec], root).stdout.splitlines():
        added, deleted, path = line.split("\t", 2)
        stats[path] = max(int(added) if added.isdigit() else 0, int(deleted) if deleted.isdigit() else 0)
    for path in _run(["git", "ls-files", "--others", "--exclude-standard", *spec], root).stdout.splitlines():
        try:
            stats[path] = len((root / path).read_text(errors="replace").splitlines())
        except OSError:
            stats[path] = 0
    skip = set(exclude)
    files = sorted(p for p in stats if p not in skip)
    return files, sum(stats[p] for p in files)


# --- model-facing tools ---


def safe_path(repo: Path, rel: str) -> Path:
    """Resolve rel inside repo; reject anything that escapes it or touches .git/."""
    root = repo.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path outside the repo: {rel}")
    if ".git" in target.relative_to(root).parts:
        raise ValueError(f"path inside .git: {rel}")
    return target


def check_git_args(argv: list[str]) -> None:
    """Raise unless argv is an allowed read-only subcommand with allowlisted options and no outside paths."""
    if not argv or argv[0] not in GIT_READONLY:
        raise ValueError(f"git subcommand not allowed: {argv[:1]}")
    pattern_next = False  # the value after -e/-S/-G/-L is a pattern or range, not a path
    for arg in argv[1:]:
        if pattern_next:
            pattern_next = False
        elif arg.startswith("-"):
            if not (arg in GIT_FLAGS or arg.startswith(GIT_VALUE_FLAGS) or re.fullmatch(r"-\d+|-U\d+", arg)):
                raise ValueError(f"git option not allowed: {arg}")
            pattern_next = arg in ("-e", "-S", "-G", "-L")
        elif arg.startswith(("/", "~")) or ".." in arg.split("/"):
            raise ValueError(f"git argument outside the repo: {arg}")


def make_tools(repo: Path, *, write_aa: bool, edit: bool, checks: list[str] | None) -> list[BaseTool]:
    """Tools for one stage. Read tools always; .aa/ writes for A–P; source edits for E; checks for E/V.

    `checks` comes only from the operator's --check flags, never from .aa/ (which the model writes)."""

    def read_file(path: str, offset: int = 0, limit: int = 400) -> str:
        """Read a text file (repo-relative path), from line `offset`, at most `limit` lines."""
        lines = safe_path(repo, path).read_text(errors="replace").splitlines()
        body = "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(lines[offset : offset + limit], offset))
        return _truncate(body)

    def list_dir(path: str = ".") -> str:
        """List a directory (repo-relative). Directories end with /."""
        target = safe_path(repo, path)
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir() if p.name != ".git")
        return "\n".join(names)

    def grep(pattern: str, path: str = ".") -> str:
        """Search tracked and untracked files with git grep (extended regex). Returns file:line:text."""
        safe_path(repo, path)
        proc = _run(["git", "grep", "-n", "-I", "-E", "--untracked", "-e", pattern, "--", path], repo)
        return _truncate(proc.stdout) or "no matches"

    def git(argv: list[str]) -> str:
        """Run a read-only git command, e.g. ["log", "-5", "--oneline"]. Allowed: log show diff blame status
        ls-files rev-parse grep."""
        check_git_args(argv)
        proc = _run(["git", *argv], repo)
        return _truncate(proc.stdout + proc.stderr)

    def write_aa_file(name: str, content: str) -> str:
        """Write a file under .aa/ (e.g. "context.md", "schema_cache.json"). Never write secret values."""
        target = safe_path(repo, f".aa/{name}")
        if (repo.resolve() / ".aa") not in target.parents:
            raise ValueError(f"not under .aa/: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote .aa/{name}"

    def edit_file(path: str, old: str, new: str) -> str:
        """Replace the single exact occurrence of `old` with `new` in a file."""
        target = safe_path(repo, path)
        text = target.read_text()
        if text.count(old) != 1:
            raise ValueError(f"`old` occurs {text.count(old)} times in {path}; it must occur exactly once")
        target.write_text(text.replace(old, new))
        return f"edited {path}"

    def write_file(path: str, content: str) -> str:
        """Create or overwrite a file with `content`."""
        target = safe_path(repo, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {path}"

    allowed = checks or []

    def run_check(command: str) -> str:
        """Run a test/lint command. Only the commands the operator allowed with --check are permitted,
        optionally with extra arguments appended (e.g. a test file)."""
        argv = shlex.split(command)
        if not any(argv[: len(shlex.split(c))] == shlex.split(c) for c in allowed):
            raise ValueError(
                f"command not allowed; allowed: {allowed or 'none (the operator passed no --check)'}. "
                "Report checks you could not run as manual."
            )
        try:
            proc = _run(argv, repo, timeout=900)
        except subprocess.TimeoutExpired:
            return "timed out after 900s"
        return _truncate(f"exit {proc.returncode}\n{proc.stdout}{proc.stderr}")

    funcs = [read_file, list_dir, grep, git]
    if write_aa:
        funcs.append(write_aa_file)
    if edit:
        funcs += [edit_file, write_file]
    if checks is not None:
        funcs.append(run_check)
    return [StructuredTool.from_function(f) for f in funcs]
