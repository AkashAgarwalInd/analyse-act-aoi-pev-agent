"""Repo-confined tools for the stage agents, plus the deterministic git/cache helpers the graph calls itself."""

import json
import re
import shlex
import subprocess
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool

MAX_OUTPUT = 8000
GIT_READONLY = {"log", "show", "diff", "blame", "status", "ls-files", "rev-parse", "grep"}
GIT_FORBIDDEN_ARGS = ("--output", "--exec", "--ext-diff", "--textconv", "-c", "--config")


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


def allowed_checks(repo: Path, extra: list[str]) -> list[str]:
    """Test command from .aa/schema_cache.json plus any --check commands given on the CLI."""
    commands = list(extra)
    try:
        cached = json.loads((repo / ".aa" / "schema_cache.json").read_text())
        if cmd := cached.get("tests", {}).get("command"):
            commands.append(cmd)
    except (OSError, ValueError, AttributeError):
        pass
    return commands


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


def make_tools(repo: Path, *, write_aa: bool, edit: bool, checks: list[str] | None) -> list[BaseTool]:
    """Tools for one stage. Read tools always; .aa/ writes for A–P; source edits for E; checks for E/V."""

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
        if not argv or argv[0] not in GIT_READONLY:
            raise ValueError(f"git subcommand not allowed: {argv[:1]}")
        if any(a.startswith(GIT_FORBIDDEN_ARGS) for a in argv[1:]):
            raise ValueError("git option not allowed")
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
        """Run a test/lint command. Only the project's recorded commands are allowed, optionally with extra
        arguments appended (e.g. a test file)."""
        argv = shlex.split(command)
        if not any(argv[: len(shlex.split(c))] == shlex.split(c) for c in allowed):
            raise ValueError(f"command not allowed; allowed: {allowed or 'none recorded'}")
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
