"""Graph routing with a stubbed stage runner: no LLM, no network."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from aa_agent import cli
from aa_agent.graph import build_graph

APPROACHES = [{"id": "A", "title": "inline", "files": ["a.js"], "recommended": True},
              {"id": "B", "title": "boundary", "files": ["b.js"]}]


class Stub:
    def __init__(self, validate_results=(True,)):
        self.calls: list[str] = []
        self.validate_results = list(validate_results)

    def __call__(self, stage, state):
        self.calls.append(stage)
        result = {"output": f"{stage} report"}
        if stage == "P":
            result["approaches"] = APPROACHES
        if stage == "V":
            result["passed"] = self.validate_results.pop(0)
        return result


def graph(stub, status="MISS", branches=None):
    return build_graph(
        stub,
        checkpointer=InMemorySaver(),
        cache_check=lambda repo: {"status": status, "reason": "test", "changed": []},
        cache_stamp=lambda repo: "abc",
        create_branch=lambda repo, task: branches.append(task) or "aa/x" if branches is not None else "aa/x",
    )


def start(**extra):
    return {"repo": ".", "task": "t", "outputs": {}, "stage_log": [], "retry_used": False, **extra}


CFG = {"configurable": {"thread_id": "t"}}


def test_miss_runs_analyse_and_observe_then_stops_at_gate():
    stub = Stub()
    g = graph(stub)
    g.invoke(start(), CFG)
    assert stub.calls == ["A", "O", "I", "P"]
    snap = g.get_state(CFG)
    assert snap.next == ("gate",)
    assert snap.interrupts[0].value["approaches"] == APPROACHES


def test_hit_skips_analyse_and_observe():
    stub = Stub()
    graph(stub, status="HIT").invoke(start(), CFG)
    assert stub.calls == ["I", "P"]


def test_analyse_only_ends_after_inspect():
    stub = Stub()
    g = graph(stub, status="HIT")
    g.invoke(start(analyse_only=True), CFG)
    assert stub.calls == ["I"]
    assert g.get_state(CFG).values["status"] == "analysed"


def test_resume_with_choice_executes_and_validates():
    stub, branches = Stub(), []
    g = graph(stub, branches=branches)
    g.invoke(start(branch=True), CFG)
    g.invoke(Command(resume="b"), CFG)
    values = g.get_state(CFG).values
    assert stub.calls == ["A", "O", "I", "P", "E", "V"]
    assert values["chosen"] == "B"
    assert values["branch_name"] == "aa/x" and branches == ["t"]
    assert values["status"] == "done"


def test_no_branch_unless_asked():
    stub, branches = Stub(), []
    g = graph(stub, branches=branches)
    g.invoke(start(), CFG)
    g.invoke(Command(resume="A"), CFG)
    assert branches == [] and "branch_name" not in g.get_state(CFG).values


def test_failed_validation_retries_exactly_once():
    stub = Stub(validate_results=(False, False))
    g = graph(stub)
    g.invoke(start(), CFG)
    g.invoke(Command(resume="A"), CFG)
    assert stub.calls[4:] == ["E", "V", "FIX", "V"]
    assert g.get_state(CFG).values["status"] == "handed_off"


def test_retry_that_passes_is_done():
    stub = Stub(validate_results=(False, True))
    g = graph(stub)
    g.invoke(start(), CFG)
    g.invoke(Command(resume="A"), CFG)
    assert g.get_state(CFG).values["status"] == "done"


def test_unknown_choice_is_rejected():
    g = graph(Stub())
    g.invoke(start(), CFG)
    with pytest.raises(ValueError, match="unknown approach"):
        g.invoke(Command(resume="Z"), CFG)


def test_cli_run_then_resume(tmp_path, monkeypatch, capsys):
    stub = Stub()
    db, plan = str(tmp_path / "a.sqlite"), tmp_path / "plan.md"
    base = ["--repo", str(tmp_path), "--db", db]

    # build_graph's defaults are bound at definition time, so stub the cache through a wrapper
    real = cli.build_graph
    monkeypatch.setattr(cli, "build_graph", lambda rs, checkpointer: real(
        rs, checkpointer, cache_check=lambda r: {"status": "HIT", "changed": []}, cache_stamp=lambda r: "abc"))

    assert cli.main(["run", "fix the bug", "--plan-out", str(plan), *base], run_stage=stub) == 0
    assert "status=awaiting_approval thread=fix-the-bug" in capsys.readouterr().out
    assert "| A | inline **(Recommended)** | a.js |" in plan.read_text()

    assert cli.main(["run", "fix the bug", *base], run_stage=stub) == 2  # thread exists
    assert cli.main(["resume", "--thread", "fix-the-bug", "--choice", "Q", *base], run_stage=stub) == 2
    assert cli.main(["resume", "--thread", "fix-the-bug", "--choice", "A", *base], run_stage=stub) == 0
    assert "status=done" in capsys.readouterr().out
    assert stub.calls == ["I", "P", "E", "V"]
