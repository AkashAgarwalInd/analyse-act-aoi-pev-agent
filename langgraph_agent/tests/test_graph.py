"""Graph routing with a stubbed stage runner: no LLM, no network."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from aa_agent import cli, session
from aa_agent.graph import build_graph, stage_budget

APPROACHES = [{"id": "A", "title": "inline", "files": ["a.js"], "recommended": True},
              {"id": "B", "title": "boundary", "files": ["b.js"]}]


class Stub:
    def __init__(self, validate_results=(True,), approaches=APPROACHES, tokens=None, tier=None, drift=False):
        self.calls: list[str] = []
        self.states: dict[str, dict] = {}
        self.validate_results = list(validate_results)
        self.approaches = approaches
        self.tokens = tokens or {}
        self.tier = tier
        self.drift = drift

    def __call__(self, stage, state):
        self.calls.append(stage)
        self.states[stage] = dict(state)
        result = {"output": f"━━ [{stage}] ━━\n{stage} report", "tokens": self.tokens.get(stage, 0)}
        if stage == "I":
            result["tier"] = self.tier
        if stage == "P":
            result["approaches"] = self.approaches
        if stage == "E":
            result["drift"] = self.drift
        if stage == "V":
            result["passed"] = self.validate_results.pop(0)
        return result


def graph(stub, status="MISS", branches=None, reason="test", changed=(), stamps=None, diff=(["a.js"], 5),
          sessions=None):
    return build_graph(
        stub,
        checkpointer=InMemorySaver(),
        cache_check=lambda repo: {"status": status, "reason": reason, "changed": list(changed)},
        cache_stamp=lambda repo: stamps.append(repo) if stamps is not None else "abc",
        create_branch=lambda repo, task: branches.append(task) or "aa/x" if branches is not None else "aa/x",
        diff_stats=lambda repo, exclude: diff,
        write_session=lambda state, status: sessions.append(status) if sessions is not None else None,
    )


def start(**extra):
    return {"repo": "/nonexistent", "task": "t", "outputs": {}, "stage_log": [], "retry_used": False, **extra}


def pause(g):
    snap = g.get_state(CFG)
    return snap.interrupts[0].value if snap.interrupts else None


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


def test_delta_rescans_only_changed_files_then_stamps():
    stub, stamps = Stub(), []
    graph(stub, status="DELTA", reason="1-files-changed", changed=["src/a.js"], stamps=stamps).invoke(start(), CFG)
    assert stub.calls == ["DELTA", "I", "P"]
    assert stamps == ["/nonexistent"]


def test_delta_without_schema_cache_runs_observe_only():
    stub = Stub()
    graph(stub, status="DELTA", reason="no-schema-cache").invoke(start(), CFG)
    assert stub.calls == ["O", "I", "P"]


def test_delta_touching_a_manifest_is_a_full_rescan():
    stub = Stub()
    graph(stub, status="DELTA", reason="1-files-changed", changed=["web/package.json"]).invoke(start(), CFG)
    assert stub.calls[:2] == ["A", "O"]


def test_refresh_ignores_a_hit():
    stub = Stub()
    graph(stub, status="HIT").invoke(start(refresh=True), CFG)
    assert stub.calls[:2] == ["A", "O"]


def test_plan_without_approaches_hands_off_instead_of_gating():
    stub = Stub(approaches=[])
    g = graph(stub, status="HIT")
    g.invoke(start(), CFG)
    snap = g.get_state(CFG)
    assert snap.next == () and stub.calls == ["I", "P"]
    assert snap.values["status"] == "handed_off" and "no parseable approaches" in snap.values["note"]


def test_analyse_only_ends_after_inspect():
    stub = Stub()
    g = graph(stub, status="HIT")
    g.invoke(start(analyse_only=True), CFG)
    assert stub.calls == ["I"]
    assert g.get_state(CFG).values["status"] == "analysed"


def test_resume_with_choice_executes_and_validates():
    stub, branches = Stub(), []
    g = graph(stub, branches=branches, diff=(["b.js"], 5))
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


def test_over_budget_stage_pauses_and_can_continue():
    # tier M, cache HIT: [I] gets 20% + half of the skipped A/O shares = 37.5% of 60k = 22.5k; pause above 1.5×
    stub = Stub(tokens={"I": 40_000})
    g = graph(stub, status="HIT")
    g.invoke(start(), CFG)
    assert pause(g)["kind"] == "budget" and "stage I used ~40000" in pause(g)["reason"]
    assert stub.calls == ["I"]
    g.invoke(Command(resume="continue"), CFG)
    assert stub.calls == ["I", "P"] and pause(g)["kind"] == "plan"


def test_over_budget_stop_hands_off():
    g = graph(Stub(tokens={"A": 100_000}))
    g.invoke(start(), CFG)
    g.invoke(Command(resume="stop"), CFG)
    values = g.get_state(CFG).values
    assert values["status"] == "handed_off" and "over budget" in values["note"]


def test_under_budget_does_not_pause():
    stub = Stub(tokens={"I": 30_000})  # under 1.5 × 22.5k
    g = graph(stub, status="HIT")
    g.invoke(start(), CFG)
    assert pause(g)["kind"] == "plan"


def test_budget_uses_tier_from_inspect_and_moves_skipped_shares():
    assert stage_budget("I", {"outputs": {}, "tier_assigned": "S"}) == int(20_000 * 0.375)
    assert stage_budget("I", {"outputs": {"A": "", "O": ""}}) == int(60_000 * 0.2)
    assert stage_budget("FIX", {"outputs": {}, "tier": "L"}) == int(150_000 * 0.15)


def run_to_review(stub, **kw):
    g = graph(stub, status="HIT", **kw)
    g.invoke(start(), CFG)
    g.invoke(Command(resume="A"), CFG)
    return g


def test_unplanned_file_pauses_for_drift():
    stub = Stub()
    g = run_to_review(stub, diff=(["a.js", "c.js"], 10))
    assert pause(g)["kind"] == "drift" and "files not in the plan: c.js" in pause(g)["reason"]
    assert "V" not in stub.calls
    g.invoke(Command(resume="continue"), CFG)
    assert stub.calls[-1] == "V" and g.get_state(CFG).values["status"] == "done"


def test_drift_stop_skips_validate():
    stub = Stub(drift=True)
    g = run_to_review(stub)
    assert "reported DRIFT" in pause(g)["reason"]
    g.invoke(Command(resume="stop"), CFG)
    values = g.get_state(CFG).values
    assert "V" not in stub.calls and values["status"] == "handed_off" and "drift" in values["note"]


def test_loc_over_plan_and_tier_cap_pause():
    approaches = [{"id": "A", "title": "x", "files": ["a", "b", "c"], "est_loc": 10}]
    g = run_to_review(Stub(approaches=approaches, tier="S"), diff=(["a", "b", "c"], 20))
    reason = pause(g)["reason"]
    assert "20 LOC changed vs ~10 planned" in reason and "exceeds tier S cap: 3 files" in reason


def test_cap_flagged_in_plan_is_not_drift():
    approaches = [{"id": "A", "title": "x", "files": ["a", "b", "c"], "exceeds_cap": True}]
    g = run_to_review(Stub(approaches=approaches, tier="S"), diff=(["a", "b", "c"], 20))
    assert pause(g) is None and g.get_state(CFG).values["status"] == "done"


def test_plan_edit_reaches_execute_and_session_is_written():
    stub, sessions = Stub(), []
    g = graph(stub, status="HIT", sessions=sessions)
    g.invoke(start(), CFG)
    g.invoke(Command(resume={"choice": "a", "edit": "also log the expiry"}), CFG)
    assert stub.states["E"]["plan_edit"] == "also log the expiry"
    assert g.get_state(CFG).values["chosen"] == "A"
    assert sessions == ["awaiting_approval", "awaiting_approval", "done"]  # gate re-runs once on resume


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


def test_cli_resume_refuses_when_head_moved(tmp_path, monkeypatch, capsys):
    real = cli.build_graph
    monkeypatch.setattr(cli, "build_graph", lambda rs, checkpointer: real(
        rs, checkpointer, cache_check=lambda r: {"status": "HIT", "head": "a" * 40, "changed": []}))
    monkeypatch.setattr(cli.tools, "git_head", lambda repo: "b" * 40)
    stub = Stub()
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", *base], run_stage=stub) == 0
    assert cli.main(["resume", "--thread", "t", "--choice", "A", *base], run_stage=stub) == 2
    assert "HEAD moved" in capsys.readouterr().err
    assert stub.calls == ["I", "P"]


def test_session_round_trip_and_archive(tmp_path):
    state = {"repo": str(tmp_path), "task": "old", "approaches": [{"id": "A", "title": "x", "files": ["a"],
             "risk": "Huge", "est_loc": "ten"}], "session_log": [{"stage": "P", "at": "t", "summary": "s"}]}
    session.write(state, "awaiting_approval")
    data = session.read(str(tmp_path))
    assert data["status"] == "awaiting_approval"
    assert data["approaches"] == [{"id": "A", "title": "x", "files": ["a"]}]  # off-schema values dropped
    session.write({"repo": str(tmp_path), "task": "new"}, "analysed")
    assert session.read(str(tmp_path))["status"] == "done"
    assert [json_task(p) for p in (tmp_path / ".aa" / "sessions").iterdir()] == ["old"]


def json_task(path):
    import json
    return json.loads(path.read_text())["task"]


def test_cli_continues_an_interactive_session(tmp_path, capsys):
    session.write({"repo": str(tmp_path), "task": "fix it", "tier": "S", "approaches": APPROACHES,
                   "session_log": [{"stage": "P", "at": "t", "summary": "two approaches"}]}, "awaiting_approval")
    stub = Stub()
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "--from-session", *base], run_stage=stub) == 0
    assert "status=awaiting_approval thread=fix-it" in capsys.readouterr().out
    assert stub.calls == []
    assert cli.main(["resume", "--thread", "fix-it", "--choice", "A", "--edit", "keep it small", *base],
                    run_stage=stub) == 0
    assert stub.calls == ["E", "V"] and stub.states["E"]["plan_edit"] == "keep it small"
    data = session.read(str(tmp_path))
    assert data["status"] == "done" and data["chosen"] == "A" and data["plan_edit"] == "keep it small"
    assert [e["stage"] for e in data["stage_log"]] == ["P", "E", "V"]


def test_cli_from_session_rejects_other_task(tmp_path):
    session.write({"repo": str(tmp_path), "task": "fix it", "approaches": APPROACHES}, "awaiting_approval")
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "other task", "--from-session", *base], run_stage=Stub()) == 2


def test_cli_budget_pause_needs_a_decision(tmp_path, monkeypatch, capsys):
    real = cli.build_graph
    monkeypatch.setattr(cli, "build_graph", lambda rs, checkpointer: real(
        rs, checkpointer, cache_check=lambda r: {"status": "HIT", "changed": []}))
    stub = Stub(tokens={"I": 99_000})
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", *base], run_stage=stub) == 0
    assert "status=awaiting_budget_decision" in capsys.readouterr().out
    assert cli.main(["resume", "--thread", "t", "--choice", "A", *base], run_stage=stub) == 2
    assert cli.main(["resume", "--thread", "t", "--decision", "continue", *base], run_stage=stub) == 0
    assert "status=awaiting_approval" in capsys.readouterr().out


def test_written_session_matches_the_skill_schema(tmp_path):
    import json

    import jsonschema

    from aa_agent.tools import skill_dir

    schema = json.loads((skill_dir() / "schemas" / "session.schema.json").read_text())
    session.write({
        "repo": str(tmp_path), "task": "t", "tier_assigned": "M", "chosen": "A", "plan_edit": "e",
        "branch_name": "aa/t", "approaches": [{**APPROACHES[0], "risk": "Low", "est_loc": 5, "junk": 1}],
        "session_log": [{"stage": "V", "at": "2026-10-11T00:00:00Z", "summary": "ok"}],
    }, "handed_off")
    jsonschema.validate(session.read(str(tmp_path)), schema)
