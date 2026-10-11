"""The AOI-PEV graph.

cache ─HIT→ inspect ─analyse_only→ finish
  └─else→ analyse → observe ─┘   └→ plan → gate (interrupt) → execute → validate ─pass→ finish
                                                                   fix (once) ←fail┘
"""

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import tools
from .state import AAState

# run_stage(stage, state) -> {"output": str, "approaches": [...] (P only), "passed": bool (V only)}
# stage is one of A O I P E V, or FIX for the single auto-fix retry after a failed [V].
RunStage = Callable[[str, AAState], dict[str, Any]]


def build_graph(
    run_stage: RunStage,
    checkpointer=None,
    cache_check: Callable[[str], dict] = tools.cache_check,
    cache_stamp: Callable[[str], str] = tools.cache_stamp,
    create_branch: Callable[[str, str], str] = tools.create_branch,
):
    def cache(state: AAState) -> dict:
        return {"cache": cache_check(state["repo"]), "stage_log": ["cache"]}

    def stage(letter: str):
        def node(state: AAState) -> dict:
            result = run_stage(letter, state)
            update: dict[str, Any] = {"outputs": {letter: result["output"]}, "stage_log": [letter]}
            if letter == "P":
                update["approaches"] = result.get("approaches") or []
            if letter == "V":
                update["validation_ok"] = bool(result.get("passed"))
            return update

        return node

    def observe(state: AAState) -> dict:
        update = stage("O")(state)
        cache_stamp(state["repo"])  # record the scan deterministically, never by the model
        return update

    def gate(state: AAState) -> dict:
        ids = [a["id"] for a in state.get("approaches", [])]
        choice = interrupt({"approaches": state.get("approaches", []), "plan": state["outputs"]["P"]})
        choice = str(choice).strip().upper()
        if ids and choice not in ids:
            raise ValueError(f"unknown approach {choice!r}; expected one of {ids}")
        return {"chosen": choice, "stage_log": ["gate"]}

    def execute(state: AAState) -> dict:
        branch = {"branch_name": create_branch(state["repo"], state["task"])} if state.get("branch") else {}
        update = stage("E")({**state, **branch})
        return {**update, **branch}

    def fix(state: AAState) -> dict:
        result = run_stage("FIX", state)
        return {"outputs": {"FIX": result["output"]}, "stage_log": ["FIX"], "retry_used": True}

    def finish(state: AAState) -> dict:
        if state.get("analyse_only"):
            status = "analysed"
        else:
            status = "done" if state.get("validation_ok") else "handed_off"
        return {"status": status}

    g = StateGraph(AAState)
    g.add_node("cache", cache)
    g.add_node("analyse", stage("A"))
    g.add_node("observe", observe)
    g.add_node("inspect", stage("I"))
    g.add_node("plan", stage("P"))
    g.add_node("gate", gate)
    g.add_node("execute", execute)
    g.add_node("validate", stage("V"))
    g.add_node("fix", fix)
    g.add_node("finish", finish)

    g.add_edge(START, "cache")
    g.add_conditional_edges("cache", lambda s: "inspect" if s["cache"].get("status") == "HIT" else "analyse")
    g.add_edge("analyse", "observe")
    g.add_edge("observe", "inspect")
    g.add_conditional_edges("inspect", lambda s: "finish" if s.get("analyse_only") else "plan")
    g.add_edge("plan", "gate")
    g.add_edge("gate", "execute")
    g.add_edge("execute", "validate")
    g.add_conditional_edges(
        "validate",
        lambda s: "finish" if s.get("validation_ok") or s.get("retry_used") else "fix",
    )
    g.add_edge("fix", "validate")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)
