"""`.aa/current_session.json` (skills/analyse-act/schemas/session.schema.json), shared with the interactive skill.

The runner writes it at guided stage pauses, at the [P] gate and at the end, so `/analyse-act --from <stage>`
can pick up a headless run.
Notes from --guided pauses go on the stage_log entry of the stage they followed, as the skill writes them.
`aa-agent run --from-session` goes the other way and starts at the [P] gate from a session the skill wrote.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

STATUS = {"awaiting_approval": "awaiting_approval", "analysed": "done", "done": "done", "handed_off": "handed_off"}
RISKS = {"Low", "Medium", "High"}


def summary_line(text: str) -> str:
    """The stage's 'Stage summary' line, else its first non-banner line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if "stage summary" in line.lower():
            return re.sub(r"^\W*stage summary\W*", "", line, flags=re.I)[:200] or line[:200]
    body = [line for line in lines if not line.startswith("━━")]
    return (body[0] if body else "")[:200]


def _approach(a: dict) -> dict:
    files = [str(f) for f in a.get("files") or []]
    out = {"id": str(a.get("id", "")), "title": str(a.get("title", "")), "files": files}
    if isinstance(a.get("est_loc"), int):
        out["est_loc"] = a["est_loc"]
    if a.get("risk") in RISKS:
        out["risk"] = a["risk"]
    for key in ("recommended", "exceeds_cap"):
        if isinstance(a.get(key), bool):
            out[key] = a[key]
    return out


def path(repo: str) -> Path:
    return Path(repo) / ".aa" / "current_session.json"


def read(repo: str) -> dict | None:
    try:
        return json.loads(path(repo).read_text())
    except (OSError, ValueError):
        return None


def write(state: dict, status: str) -> None:
    target = path(state["repo"])
    target.parent.mkdir(parents=True, exist_ok=True)
    old = read(state["repo"])
    if old and old.get("task") and old["task"] != state["task"]:
        archive = target.parent / "sessions"
        archive.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest, n = archive / f"{stamp}.json", 1
        while dest.exists():
            n += 1
            dest = archive / f"{stamp}-{n}.json"
        target.rename(dest)
    data: dict = {"task": state["task"], "status": STATUS.get(status, "in_progress")}
    if tier := state.get("tier") or state.get("tier_assigned"):
        data["tier"] = tier
    if state.get("approaches"):
        data["approaches"] = [_approach(a) for a in state["approaches"]]
    if head := (state.get("cache") or {}).get("head"):
        data["head"] = head  # the commit the plan was made on, so --from-session can refuse a moved HEAD
    for key in ("chosen", "plan_edit", "branch_name"):
        if state.get(key):
            data["branch" if key == "branch_name" else key] = state[key]
    log = [dict(entry) for entry in state.get("session_log", [])]
    for note in state.get("notes", []):
        if entry := next((e for e in reversed(log) if e.get("stage") == note["stage"]), None):
            entry["note"] = note["note"]
    data["stage_log"] = log
    target.write_text(json.dumps(data, indent=2) + "\n")
