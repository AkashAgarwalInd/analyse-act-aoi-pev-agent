"""The AOI-PEV graph.

cache ─HIT──────────────────────→ inspect ─analyse_only→ finish
  ├─DELTA──→ delta (rescan changed) ─┤        └→ plan ─no approaches→ finish (handed_off)
  ├─DELTA no-schema-cache → observe ─┤              └→ gate ⏸ → execute → review ⏸ → validate ─pass→ finish
  └─MISS / --refresh / manifest changed → analyse → observe ┘      review ⏸ ← fix (once) ←fail┘

Every LLM stage goes through `budget` (⏸ if the stage ran over 1.5× its token budget) before its next stage.
With --guided, [A], [O], DELTA and [I] then go through `guide` ⏸, where the human continues (optionally with a
note for the later stages; a note on [A]/[O] is also added to .aa/context.md, so the cache keeps it) or stops.
Guided adds pauses; it never replaces the [P] gate.
⏸ = interrupt(): the run stops and `aa-agent resume` continues it. Pause nodes are deterministic, because
LangGraph re-runs an interrupted node from the top when it resumes.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import session, tools
from .state import AAState

# run_stage(stage, state) -> {"output": str, "tokens": int, "approaches": [...] (P), "tier": str (I),
#                             "passed": bool (V), "drift": bool (E/FIX)}
# stage is one of A O I P E V, DELTA for a cache-delta rescan, or FIX for the single retry after a failed [V].
RunStage = Callable[[str, AAState], dict[str, Any]]

# A change to any of these invalidates the stack picture, so DELTA becomes a full rescan (references/caching.md).
MANIFESTS = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "go.mod", "Cargo.toml",
    "Gemfile", "pom.xml", "build.gradle", "build.gradle.kts", "composer.json",
}

# Tier table from SKILL.md: max files, max LOC, run token budget.
CAPS = {"S": (2, 50, 20_000), "M": (3, 150, 60_000), "L": (8, 400, 150_000)}
SHARES = {"A": 0.15, "O": 0.20, "I": 0.20, "P": 0.10, "E": 0.20, "V": 0.15}
SHARE_OF = {"DELTA": "O", "FIX": "V"}
OVER = 1.5
GUIDED_STAGES = ("A", "O", "DELTA", "I")  # --guided pauses after these; [P] is a gate in every mode


def route_cache(state: AAState) -> str:
    cache = state["cache"]
    status = cache.get("status")
    if state.get("refresh") or status not in ("HIT", "DELTA"):
        return "analyse"
    if status == "HIT":
        return "inspect"
    if cache.get("reason") == "no-schema-cache":
        return "observe"
    if any(PurePosixPath(f).name in MANIFESTS for f in cache.get("changed", [])):
        return "analyse"
    return "delta"


def tier_of(state: AAState) -> str:
    return state.get("tier") or state.get("tier_assigned") or "M"


def stage_budget(stage: str, state: AAState) -> int:
    """Run budget × stage share. Shares of skipped [A]/[O] move to [I] and [E], as in SKILL.md."""
    letter = SHARE_OF.get(stage, stage)
    share = SHARES[letter]
    if letter in ("I", "E"):
        ran = state.get("outputs", {})
        skipped = (0 if "A" in ran else SHARES["A"]) + (0 if "O" in ran or "DELTA" in ran else SHARES["O"])
        share += skipped / 2
    return int(CAPS[tier_of(state)][2] * share)


def clean_approaches(approaches: Any) -> list[dict]:
    """Approaches with each id as an uppercase string (what --choice and the prompts compare against).
    Items that aren't objects, have no id, or repeat an id are dropped."""
    out, seen = [], set()
    for a in approaches if isinstance(approaches, list) else []:
        if not isinstance(a, dict) or a.get("id") is None:
            continue
        aid = str(a["id"]).strip().upper()
        if aid and aid not in seen:
            seen.add(aid)
            out.append({**a, "id": aid})
    return out


def drift_reasons(state: AAState, files: list[str], loc: int) -> list[tuple[str, str]]:
    """(key, reason) for each way the [E] diff departs from the approved plan or the tier caps (empty = no
    drift). The key names the drift without its live counts, so a human who approved it isn't asked again
    when [FIX] only changes the numbers; a new unplanned file changes the key and is asked about."""
    reasons = []
    if state.get("drift_reported"):
        reasons.append(("reported", "[E] reported DRIFT: it needed something the plan didn't cover"))
    approach = next((a for a in state.get("approaches", []) if a.get("id") == state.get("chosen")), {})
    planned = set(approach.get("files") or [])
    if not planned and files:
        reasons.append((f"no-plan-files:{','.join(files)}",
                        f"the plan listed no files, so these can't be checked against it: {', '.join(files)}"))
    elif unplanned := sorted(set(files) - planned):
        reasons.append((f"unplanned:{','.join(unplanned)}", f"files not in the plan: {', '.join(unplanned)}"))
    est = approach.get("est_loc")
    if isinstance(est, int) and est > 0 and loc > OVER * est:
        reasons.append(("over-estimate", f"{loc} LOC changed vs ~{est} planned (over {OVER}×)"))
    max_files, max_loc, _ = CAPS[tier_of(state)]
    if not approach.get("exceeds_cap") and (len(files) > max_files or loc > max_loc):
        reasons.append(("over-cap",
            f"exceeds tier {tier_of(state)} cap: {len(files)} files / {loc} LOC (max {max_files} / {max_loc})"
        ))
    return reasons


def build_graph(
    run_stage: RunStage,
    checkpointer=None,
    cache_check: Callable[[str], dict] = tools.cache_check,
    cache_stamp: Callable[[str], str] = tools.cache_stamp,
    create_branch: Callable[[str, str], str] = tools.create_branch,
    snapshot: Callable[[str], dict[str, str]] = tools.snapshot_dirty,
    diff_stats: Callable[[str, dict[str, str]], tuple[list[str], int]] = tools.diff_stats,
    write_session: Callable[[AAState, str], None] = session.write,
    cache_note: Callable[[str, str, str], None] = tools.cache_note,
):
    def cache(state: AAState) -> dict:
        return {"cache": cache_check(state["repo"]), "stage_log": ["cache"]}

    def stage(letter: str, stamp: bool = False):
        def node(state: AAState) -> dict:
            result = run_stage(letter, state)
            if stamp:
                cache_stamp(state["repo"])  # record the scan deterministically, never by the model
            update: dict[str, Any] = {
                "outputs": {letter: result["output"]},
                "stage_log": [letter],
                "tokens": {letter: int(result.get("tokens") or 0)},
                "last_stage": letter,
                "session_log": [{
                    "stage": SHARE_OF.get(letter, letter),
                    "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "summary": session.summary_line(result["output"]),
                }],
            }
            if letter == "I" and result.get("tier") in CAPS and not state.get("tier"):
                update["tier_assigned"] = result["tier"]
            if letter == "P":
                update["approaches"] = clean_approaches(result.get("approaches"))
            if letter == "V":
                update["validation_ok"] = bool(result.get("passed"))
            if letter in ("E", "FIX"):
                update["drift_reported"] = bool(result.get("drift"))
            return update

        return node

    def budget(state: AAState) -> dict:
        letter = state["last_stage"]
        used, limit = state.get("tokens", {}).get(letter, 0), stage_budget(letter, state)
        if used <= OVER * limit or letter in state.get("budget_ok", []):
            return {}
        answer = interrupt({
            "kind": "budget",
            "reason": f"⚠ over budget: stage {letter} used ~{used} tok vs ~{limit} (tier {tier_of(state)})",
            "options": ["continue", "stop"],
        })
        if answer != "continue":
            return {"status": "handed_off", "note": f"stopped by the human after stage {letter} ran over budget"}
        return {"budget_ok": [*state.get("budget_ok", []), letter]}

    def next_node(state: AAState) -> str:
        if state.get("status"):
            return "finish"
        letter = state["last_stage"]
        if letter == "A":
            return "observe"
        if letter in ("O", "DELTA"):
            return "inspect"
        if letter == "I":
            return "finish" if state.get("analyse_only") else "plan"
        if letter == "P":
            return "gate" if state.get("approaches") else "finish"
        if letter == "V":
            return "finish" if state.get("validation_ok") or state.get("retry_used") else "fix"
        return "review"  # E and FIX: check the diff against the plan before validating

    def after_budget(state: AAState) -> str:
        if state.get("guided") and not state.get("status") and state["last_stage"] in GUIDED_STAGES:
            return "guide"
        return next_node(state)

    def guide(state: AAState) -> dict:
        letter = state["last_stage"]
        write_session(state, "in_progress")  # so the skill can pick up from here (/analyse-act --from <next>)
        answer = interrupt({
            "kind": "stage",
            "stage": letter,
            "reason": f"[{letter}] done: review it, add a note for the next stages, or stop",
            "options": ["continue", "stop"],
        })
        if not isinstance(answer, dict):
            answer = {"decision": answer}
        if answer.get("decision") != "continue":
            return {"status": "handed_off", "note": f"stopped by the human after stage {letter}"}
        note = str(answer.get("note") or "").strip()
        if not note:
            return {}
        # stage as the session logs it (DELTA is [O]'s rescan), so session.write can attach the note to that entry
        notes = [{"stage": SHARE_OF.get(letter, letter), "note": note}]
        if letter in ("A", "O", "DELTA"):  # their findings are cached, so the correction must be too
            cache_note(state["repo"], SHARE_OF.get(letter, letter), note)
        write_session({**state, "notes": [*state.get("notes", []), *notes]}, "in_progress")
        return {"notes": notes}

    def gate(state: AAState) -> dict:
        ids = [a["id"] for a in state.get("approaches", [])]
        if not ids:
            raise ValueError("no approaches to choose from")
        write_session(state, "awaiting_approval")
        answer = interrupt({"kind": "plan", "approaches": state["approaches"], "plan": state["outputs"].get("P", "")})
        if not isinstance(answer, dict):
            answer = {"choice": answer}
        choice, edit = answer.get("choice", ""), answer.get("edit") or ""
        choice = str(choice).strip().upper()
        if choice not in ids:
            raise ValueError(f"unknown approach {choice!r}; expected one of {ids}")
        return {"chosen": choice, "plan_edit": edit, "stage_log": ["gate"]}

    def execute(state: AAState) -> dict:
        branch = {"branch_name": create_branch(state["repo"], state["task"])} if state.get("branch") else {}
        baseline = snapshot(state["repo"])  # dirty before [E]: measured against this content, not HEAD
        update = stage("E")({**state, **branch})
        return {**update, **branch, "baseline_dirty": baseline}

    def review(state: AAState) -> dict:
        files, loc = diff_stats(state["repo"], state.get("baseline_dirty", {}))
        approved = state.get("drift_ok", [])
        new = [(key, why) for key, why in drift_reasons(state, files, loc) if key not in approved]
        stats = {"diff_files": files, "diff_loc": loc}
        if not new:  # nothing the human hasn't already approved (e.g. after [FIX] changed only the counts)
            return stats
        reason = "; ".join(why for _, why in new)
        answer = interrupt({"kind": "drift", "reason": reason, "options": ["continue", "stop"]})
        if answer != "continue":
            return {**stats, "status": "handed_off", "note": f"stopped at drift review: {reason}"}
        return {**stats, "drift_ok": [*approved, *(key for key, _ in new)]}

    def fix(state: AAState) -> dict:
        return {**stage("FIX")(state), "retry_used": True}

    def finish(state: AAState) -> dict:
        if state.get("status"):  # already decided at a pause
            result = {}
        elif state.get("analyse_only"):
            result = {"status": "analysed"}
        elif "P" in state.get("outputs", {}) and not state.get("approaches"):
            result = {"status": "handed_off", "note": "[P] produced no parseable approaches; nothing was executed"}
        else:
            result = {"status": "done" if state.get("validation_ok") else "handed_off"}
        final = {**state, **result}
        write_session(final, final["status"])
        return result

    g = StateGraph(AAState)
    g.add_node("cache", cache)
    g.add_node("analyse", stage("A"))
    g.add_node("delta", stage("DELTA", stamp=True))
    g.add_node("observe", stage("O", stamp=True))
    g.add_node("inspect", stage("I"))
    g.add_node("plan", stage("P"))
    g.add_node("budget", budget)
    g.add_node("guide", guide)
    g.add_node("gate", gate)
    g.add_node("execute", execute)
    g.add_node("review", review)
    g.add_node("validate", stage("V"))
    g.add_node("fix", fix)
    g.add_node("finish", finish)

    g.add_edge(START, "cache")
    g.add_conditional_edges("cache", route_cache)
    for node in ("analyse", "delta", "observe", "inspect", "plan", "execute", "validate", "fix"):
        g.add_edge(node, "budget")
    g.add_conditional_edges("budget", after_budget)
    g.add_conditional_edges("guide", next_node)
    g.add_edge("gate", "execute")
    g.add_conditional_edges("review", lambda s: "finish" if s.get("status") else "validate")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)
