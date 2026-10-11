"""Default stage runner: one Claude tool loop per stage, prompted with the skill's own reference files."""

import json
import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from . import tools
from .state import AAState

DEFAULT_MODEL = "claude-sonnet-5-5"
REFERENCES = {
    "A": ["analyse.md"],
    "DELTA": ["caching.md"],
    "O": ["observe.md"],
    "I": ["inspect.md"],
    "P": ["plan.md"],
    "E": ["execute.md"],
    "V": ["validate.md"],
    "FIX": ["execute.md", "validate.md"],
}
NAMES = {
    "DELTA": "DELTA UPDATE", "A": "ANALYSE", "O": "OBSERVE", "I": "INSPECT", "P": "PLAN",
    "E": "EXECUTE", "V": "VALIDATE", "FIX": "FIX",
}

HEADLESS = """You are running headless (CI); nobody can answer questions mid-stage.
- Do only the stage below, then reply with its report (banner, body, stage summary) and stop.
- Where the reference says to ask the human, make the conservative choice and state the assumption instead.
  The one exception is [P]: the graph itself stops there for approval.
- Never commit, push, or edit .gitignore. Don't write .aa/cache_meta.json or .aa/dirty_at_scan.txt:
  the runner stamps the cache. The runner's checkpoint replaces .aa/current_session.json; don't write it.
- If [E] would need to drift from the approved plan, stop editing and start the report with DRIFT."""

STAGE_TAIL = {
    "DELTA": "Cache DELTA: read .aa/context.md and .aa/schema_cache.json, rescan ONLY the changed files listed above "
    "(drop deleted ones), and patch the affected sections with write_aa_file. Don't rescan anything else.",
    "P": 'End with a ```json block: {"approaches": [{"id": "A", "title": "...", "files": ["..."], '
    '"est_loc": 10, "risk": "Low", "recommended": true}, ...]}',
    "V": 'End with a ```json block: {"passed": true|false} — true only if every check you ran passed.',
    "FIX": "[V] failed. Fix the failure once if your change caused it, re-run the failing check, then report. "
    "If the failure is environmental, change nothing and say so.",
}


def _rules(skill: Path) -> str:
    text = (skill / "SKILL.md").read_text()
    start = text.find("## Non-negotiable rules")
    return text[start:] if start >= 0 else ""


def system_prompt(stage: str) -> str:
    skill = tools.skill_dir()
    refs = "\n\n".join((skill / "references" / name).read_text() for name in REFERENCES[stage])
    return f"{HEADLESS}\n\n{_rules(skill)}\n\n{refs}"


def task_message(stage: str, state: AAState) -> str:
    cache = state.get("cache", {})
    parts = [
        f"Task: {state['task']}",
        f"Stage: [{stage}] {NAMES[stage]}",
        f"Cache: {cache.get('status', '?')} ({cache.get('reason', '')}); changed: {cache.get('changed') or 'none'}",
    ]
    if state.get("tier"):
        parts.append(f"Tier forced by the user: {state['tier']}")
    if stage in ("I", "P") and cache.get("status") == "HIT":
        parts.append("[A] and [O] were skipped on a cache HIT: read .aa/context.md and .aa/schema_cache.json.")
    if state.get("chosen"):
        parts.append(f"Approved approach: {state['chosen']}")
    if state.get("branch_name"):
        parts.append(f"Working branch (already created): {state['branch_name']}")
    for done, report in state.get("outputs", {}).items():
        parts.append(f"--- [{done}] report ---\n{report}")
    if stage in STAGE_TAIL:
        parts.append(STAGE_TAIL[stage])
    return "\n\n".join(parts)


def last_json_block(text: str) -> dict:
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    for block in reversed(blocks):
        try:
            return json.loads(block)
        except ValueError:
            continue
    return {}


def parse_approaches(text: str) -> list[dict]:
    approaches = last_json_block(text).get("approaches")
    if approaches:
        return approaches
    # fall back to the Decision Matrix headings: "[A] Normalise units ... (Recommended)"
    return [
        {"id": m[1], "title": m[2].strip(), "files": [], "recommended": "(Recommended)" in m[2]}
        for m in re.finditer(r"^\[([A-Z])\] (.+)$", text, re.M)
    ]


def tool_loop(llm, tool_list, system: str, human: str, max_steps: int) -> str:
    by_name = {t.name: t for t in tool_list}
    model = llm.bind_tools(tool_list) if tool_list else llm
    messages = [SystemMessage(system), HumanMessage(human)]
    for _ in range(max_steps):
        reply = model.invoke(messages)
        messages.append(reply)
        if not reply.tool_calls:
            return reply.text
        for call in reply.tool_calls:
            try:
                result = by_name[call["name"]].invoke(call["args"])
            except Exception as exc:  # report tool errors to the model instead of crashing the stage
                result = f"error: {exc}"
            messages.append(ToolMessage(str(result), tool_call_id=call["id"]))
    return f"⚠ stage stopped after {max_steps} tool steps without a final report"


def make_run_stage(model: str = DEFAULT_MODEL, max_steps: int = 40, llm=None):
    if llm is None:
        from langchain_anthropic import ChatAnthropic

        llm = ChatAnthropic(model=model, max_tokens=8192)

    def run_stage(stage: str, state: AAState) -> dict:
        repo = Path(state["repo"])
        checks = state.get("checks", []) if stage in ("E", "V", "FIX") else None
        stage_tools = tools.make_tools(
            repo, write_aa=stage in ("DELTA", "A", "O", "I", "P"), edit=stage in ("E", "FIX"), checks=checks
        )
        text = tool_loop(llm, stage_tools, system_prompt(stage), task_message(stage, state), max_steps)
        result: dict = {"output": text}
        if stage == "P":
            result["approaches"] = parse_approaches(text)
        if stage == "V":
            result["passed"] = last_json_block(text).get("passed") is True
        return result

    return run_stage
