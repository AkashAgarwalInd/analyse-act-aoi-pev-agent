"""Repo-confined tools for the stage agents, plus the deterministic git/cache helpers the graph calls itself."""

import os
import re
import shlex
import signal
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


CORRECTIONS = "## Human corrections"


def cache_note(repo: str, stage: str, note: str) -> None:
    """Append a guided note on [A]/[O] to .aa/context.md, so a later cache HIT on this commit reads the
    correction instead of the findings it overrides. Writing the same note twice is a no-op."""
    target = Path(repo) / ".aa" / "context.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = target.read_text() if target.exists() else ""
    line = f"- after [{stage}]: {' '.join(note.split())}"
    if line in text.splitlines():
        return
    if CORRECTIONS not in text.splitlines():
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + CORRECTIONS + "\n\n"
    target.write_text(text.rstrip("\n") + "\n" + line + "\n")


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


def _changed(root: Path) -> dict[str, int]:
    """Files changed vs HEAD (tracked and untracked, minus .aa/) -> LOC, where LOC per file is
    max(added, deleted): a rewrite counts its size and a pure deletion isn't free."""
    spec = ["--", ".", ":(exclude).aa"]
    stats: dict[str, int] = {}
    out = _run(["git", "diff", "--numstat", "-z", "--no-renames", "HEAD", *spec], root).stdout
    for record in filter(None, out.split("\0")):
        path = record.split("\t", 2)[2]
        stats[path] = _numstat_loc(record)
    out = _run(["git", "ls-files", "-z", "--others", "--exclude-standard", *spec], root).stdout
    for path in filter(None, out.split("\0")):
        stats[path] = _lines(root / path)
    return stats


def _numstat_loc(record: str) -> int:
    """max(added, deleted) from one numstat record; binary files ("-") count 0."""
    added, deleted = (record.split("\t") + ["", ""])[:2]
    return max(int(added) if added.isdigit() else 0, int(deleted) if deleted.isdigit() else 0)


def _lines(file: Path) -> int:
    try:
        return len(file.read_text(errors="replace").splitlines())
    except OSError:
        return 0


def _blob(root: Path, path: str) -> str:
    """Store the file's current content in git's object database (never committed); "" if it doesn't exist."""
    if not (root / path).is_file():
        return ""
    return _run(["git", "hash-object", "-w", "--", path], root).stdout.strip()


def snapshot_dirty(repo: str) -> dict[str, str]:
    """Blob ids of the files already changed before [E], so [E]'s own edits to them can still be measured."""
    root = Path(repo)
    if not git_head(repo):
        return {}
    return {path: _blob(root, path) for path in _changed(root)}


def diff_stats(repo: str, baseline: dict[str, str]) -> tuple[list[str], int]:
    """Files [E] changed and their total LOC (see diff_by_file)."""
    stats = diff_by_file(repo, baseline)
    files = sorted(stats)
    return files, sum(stats[p] for p in files)


def diff_by_file(repo: str, baseline: dict[str, str]) -> dict[str, int]:
    """LOC per file [E] changed: changes vs HEAD, except that a file in `baseline` (dirty before [E])
    is measured against its baseline content instead, and left out only if [E] didn't touch it."""
    root = Path(repo)
    if not git_head(repo):
        return {}
    stats = _changed(root)
    for path, old in baseline.items():
        new = _blob(root, path)
        if old and _run(["git", "cat-file", "-e", old], root).returncode != 0:
            # baseline content isn't in this clone (e.g. a later CI job): count the whole file, never 0
            stats[path] = _lines(root / path)
            continue
        if new == old:
            stats.pop(path, None)
        elif old and new:
            stats[path] = _numstat_loc(_run(["git", "diff", "--numstat", old, new], root).stdout)
        elif new:
            stats[path] = _lines(root / path)
        else:  # [E] deleted a file that was dirty before
            stats[path] = len(_run(["git", "cat-file", "-p", old], root).stdout.splitlines())
    return stats


def diff_text(repo: str, baseline: dict[str, str]) -> str:
    """[E]'s changes for a human deciding at a drift pause: the same files as diff_by_file, each diffed against
    its content before [E] when it was already dirty (so the human's own earlier edits aren't shown as [E]'s)."""
    root = Path(repo)
    changed = sorted(diff_by_file(repo, baseline))
    untracked = set(filter(None, _run(
        ["git", "ls-files", "-z", "--others", "--exclude-standard", "--", ".", ":(exclude).aa"], root
    ).stdout.split("\0")))
    text, new = "", []
    for path in changed:
        old = baseline.get(path)
        if path not in baseline:
            if path in untracked:
                new.append(path)
            else:
                text += _run(["git", "diff", "HEAD", "--", path], root).stdout
        elif old and (blob := _blob(root, path)) and _run(["git", "cat-file", "-e", old], root).returncode == 0:
            body = _run(["git", "diff", old, blob], root).stdout
            hunks = body[body.index("\n@@") + 1:] if "\n@@" in body else ""  # its header names blob ids, not path
            text += f"--- {path} (before [E])\n+++ {path}\n{hunks}"
        else:
            text += f"{path}: changed, but its content before [E] can't be shown\n"
    if new:
        text += "\nnew files (untracked):\n" + "".join(f"  {path}\n" for path in new)
    return text or "(no changes)\n"


def _run_check_process(argv: list[str], cwd: Path, timeout: int) -> tuple[int | None, str]:
    """(exit code, stdout+stderr), or (None, "") on timeout. The check runs in its own process group, which is
    killed whole on timeout: `npm test` -> jest would otherwise outlive npm and keep running."""
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        return None, ""
    return proc.returncode, out + err


# --- model-facing tools ---


def safe_path(repo: Path, rel: str) -> Path:
    """Resolve rel inside repo; reject anything that escapes it or touches .git/."""
    root = repo.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path outside the repo: {rel}")
    if ".git" in (part.lower() for part in target.relative_to(root).parts):  # macOS paths ignore case
        raise ValueError(f"path inside .git: {rel}")
    return target


# The only .aa/ files the model writes. Everything else there (cache_meta.json, dirty_at_scan.txt,
# current_session.json, sessions/, agent.sqlite) belongs to the runner.
AA_MODEL_FILES = re.compile(r"context(\.[A-Za-z0-9_-]+)?\.md|schema_cache\.json")


def in_aa(repo: Path, target: Path) -> bool:
    rel = target.relative_to(repo.resolve()).parts
    return bool(rel) and rel[0].lower() == ".aa"


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
        """Write .aa/context.md, .aa/context.<package>.md or .aa/schema_cache.json. Never write secret values."""
        target = safe_path(repo, f".aa/{name}")
        if target.parent != repo.resolve() / ".aa" or not AA_MODEL_FILES.fullmatch(target.name):
            raise ValueError(f"only context.md, context.<package>.md and schema_cache.json can be written: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote .aa/{name}"

    def edit_file(path: str, old: str, new: str) -> str:
        """Replace the single exact occurrence of `old` with `new` in a file."""
        target = safe_path(repo, path)
        if in_aa(repo, target):
            raise ValueError(".aa/ is not editable in this stage")
        text = target.read_text()
        if text.count(old) != 1:
            raise ValueError(f"`old` occurs {text.count(old)} times in {path}; it must occur exactly once")
        target.write_text(text.replace(old, new))
        return f"edited {path}"

    def write_file(path: str, content: str) -> str:
        """Create or overwrite a file with `content`."""
        target = safe_path(repo, path)
        if in_aa(repo, target):
            raise ValueError(".aa/ is not editable in this stage")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {path}"

    allowed = checks or []

    def run_check(command: str) -> str:
        """Run a test/lint command. Only the commands the operator allowed with --check are permitted. You may
        append test files or test names (not options), optionally after a single "--"."""
        argv = shlex.split(command)
        base = next((shlex.split(c) for c in allowed if argv[: len(shlex.split(c))] == shlex.split(c)), None)
        extra = argv[len(base):] if base is not None else []
        if base is not None and extra[:1] == ["--"]:
            extra = extra[1:]
        bad = [a for a in extra if a.startswith(("-", "/", "~")) or ".." in a.split("/")]
        if base is not None and bad:
            raise ValueError(f"only test files or names can be appended, not options or outside paths: {bad}")
        if base is None:
            raise ValueError(
                f"command not allowed; allowed: {allowed or 'none (the operator passed no --check)'}. "
                "Report checks you could not run as manual."
            )
        code, output = _run_check_process(argv, repo, timeout=900)
        return "timed out after 900s" if code is None else _truncate(f"exit {code}\n{output}")

    funcs = [read_file, list_dir, grep, git]
    if write_aa:
        funcs.append(write_aa_file)
    if edit:
        funcs += [edit_file, write_file]
    if checks is not None:
        funcs.append(run_check)
    return [StructuredTool.from_function(f) for f in funcs]
