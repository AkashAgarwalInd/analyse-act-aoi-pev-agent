"""Graph routing with a stubbed stage runner: no LLM, no network."""

import subprocess
from pathlib import Path

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
          sessions=None, cached_notes=None):
    return build_graph(
        stub,
        checkpointer=InMemorySaver(),
        cache_check=lambda repo: {"status": status, "reason": reason, "changed": list(changed)},
        cache_stamp=lambda repo: stamps.append(repo) if stamps is not None else "abc",
        create_branch=lambda repo, task: branches.append(task) or "aa/x" if branches is not None else "aa/x",
        snapshot=lambda repo: {},
        diff_stats=lambda repo, baseline: diff,
        write_session=lambda state, status: sessions.append(status) if sessions is not None else None,
        cache_note=lambda repo, stage, note: cached_notes.append((stage, note)) if cached_notes is not None else None,
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


def test_cli_edit_is_refused_at_a_decision_pause(tmp_path, monkeypatch, capsys):
    hit_cache(monkeypatch)
    stub = Stub(tokens={"I": 99_000})
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", *base], run_stage=stub) == 0
    assert cli.main(["resume", "--thread", "t", "--decision", "continue", "--edit", "x", *base],
                    run_stage=stub) == 2
    assert "--edit only applies at the [P] gate" in capsys.readouterr().err


def hit_cache(monkeypatch, head=""):
    real = cli.build_graph
    monkeypatch.setattr(cli, "build_graph", lambda rs, checkpointer: real(
        rs, checkpointer, cache_check=lambda r: {"status": "HIT", "head": head, "changed": []}))


def answers(*replies):
    queue = list(replies)
    return lambda question: queue.pop(0)


def test_cli_interactive_runs_to_the_end_in_one_call(tmp_path, monkeypatch, capsys):
    hit_cache(monkeypatch)
    stub = Stub()
    plan = tmp_path / "plan.md"
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    argv = ["run", "t", "-i", "--plan-out", str(plan), *base]
    assert cli.main(argv, run_stage=stub, ask=answers("e", "", "small")) == 0
    assert stub.calls == ["I", "P", "E", "V"] and stub.states["E"]["plan_edit"] == "small"
    assert "status=done" in capsys.readouterr().out
    assert plan.exists()


def test_cli_interactive_answers_a_budget_pause_then_the_plan(tmp_path, monkeypatch, capsys):
    hit_cache(monkeypatch)
    stub = Stub(tokens={"I": 99_000})
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", "-i", *base], run_stage=stub, ask=answers("c", "A")) == 0
    out = capsys.readouterr().out
    assert "over budget" in out and "status=done" in out
    assert stub.calls == ["I", "P", "E", "V"]


def test_cli_interactive_quit_leaves_the_thread_paused(tmp_path, monkeypatch, capsys):
    hit_cache(monkeypatch)
    stub = Stub()
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", "-i", *base], run_stage=stub, ask=answers("q")) == 0
    assert "aa-agent resume --thread t --choice" in capsys.readouterr().out
    assert cli.main(["resume", "--thread", "t", "-i", *base], run_stage=stub, ask=answers("A")) == 0
    out = capsys.readouterr().out
    assert "[P] " in out and "status=done" in out
    assert stub.calls == ["I", "P", "E", "V"]


def test_cli_interactive_is_off_without_a_terminal(tmp_path, monkeypatch, capsys):
    hit_cache(monkeypatch)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", "-i", *base], run_stage=Stub()) == 0
    captured = capsys.readouterr()
    assert "interactive off: stdin is not a terminal" in captured.err and "status=awaiting_approval" in captured.out


def test_cli_interactive_uses_input_in_a_terminal(tmp_path, monkeypatch, capsys):
    hit_cache(monkeypatch)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("builtins.input", answers("B"))
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", "-i", *base], run_stage=Stub()) == 0
    assert "status=done" in capsys.readouterr().out
    monkeypatch.setenv("CI", "true")
    assert cli.main(["run", "t2", "-i", "--thread", "t2", *base], run_stage=Stub()) == 0
    assert "status=awaiting_approval" in capsys.readouterr().out


def test_cli_interactive_resume_does_not_drop_an_edit(tmp_path, monkeypatch, capsys):
    hit_cache(monkeypatch)
    stub = Stub()
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", *base], run_stage=stub) == 0
    assert cli.main(["resume", "--thread", "t", "-i", "--edit", "x", *base], run_stage=stub, ask=answers()) == 2
    assert "pass --choice" in capsys.readouterr().err


def test_cli_interactive_from_session(tmp_path, capsys):
    session.write({"repo": str(tmp_path), "task": "fix it", "approaches": APPROACHES}, "awaiting_approval")
    stub = Stub()
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "--from-session", "-i", *base], run_stage=stub, ask=answers("")) == 0
    assert stub.calls == ["E", "V"] and session.read(str(tmp_path))["chosen"] == "A"


def test_cli_interactive_refuses_when_head_moved(tmp_path, monkeypatch, capsys):
    hit_cache(monkeypatch, head="a" * 40)
    monkeypatch.setattr(cli.tools, "git_head", lambda repo: "b" * 40)
    stub = Stub()
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", "-i", *base], run_stage=stub, ask=answers()) == 2  # refused before asking
    assert "HEAD moved" in capsys.readouterr().err
    assert stub.calls == ["I", "P"]


class Flaky(Stub):
    """Raises `error` the first time `stage` runs, like a crash or Ctrl-C in the middle of a stage."""

    def __init__(self, stage, error, **kw):
        super().__init__(**kw)
        self.fail_at, self.error = stage, error

    def __call__(self, stage, state):
        if stage == self.fail_at and self.error:
            error, self.error = self.error, None
            raise error
        return super().__call__(stage, state)


def test_cli_crashed_stage_can_be_retried(tmp_path, monkeypatch, capsys):
    hit_cache(monkeypatch)
    stub = Flaky("P", RuntimeError("API overloaded"))
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    with pytest.raises(RuntimeError):
        cli.main(["run", "t", *base], run_stage=stub)
    assert "\nstatus=interrupted thread=t\n" in capsys.readouterr().out  # CI reads the thread id from this
    assert cli.main(["status", "--thread", "t", *base]) == 1
    out = capsys.readouterr().out
    assert "stopped inside plan" in out and "\nstatus=interrupted thread=t\n" in out
    assert "aa-agent resume --thread t" in out
    assert cli.main(["resume", "--thread", "t", "--choice", "A", *base], run_stage=stub) == 2  # nothing to choose yet
    # the CI example passes empty --choice/--edit when only the thread id is given
    assert cli.main(["resume", "--thread", "t", "--choice", "", "--edit", "", *base], run_stage=stub) == 0
    assert "status=awaiting_approval" in capsys.readouterr().out
    assert stub.calls == ["I", "P"]  # [I] isn't re-run


def test_cli_ctrl_c_exits_130_and_can_be_retried(tmp_path, monkeypatch, capsys):
    hit_cache(monkeypatch)
    stub = Flaky("I", KeyboardInterrupt())
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", "-i", *base], run_stage=stub, ask=answers()) == 130
    assert "status=interrupted thread=t" in capsys.readouterr().out
    assert cli.main(["resume", "--thread", "t", "-i", *base], run_stage=stub, ask=answers("A")) == 0
    assert stub.calls == ["I", "P", "E", "V"]


def test_cli_does_not_retry_a_half_done_execute(tmp_path, monkeypatch, capsys):
    hit_cache(monkeypatch)
    stub = Flaky("E", RuntimeError("boom"))
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", *base], run_stage=stub) == 0
    with pytest.raises(RuntimeError):
        cli.main(["resume", "--thread", "t", "--choice", "A", *base], run_stage=stub)
    assert "stopped while editing" in capsys.readouterr().out
    assert cli.main(["resume", "--thread", "t", *base], run_stage=stub) == 2
    assert "stopped while editing" in capsys.readouterr().err
    assert stub.calls == ["I", "P"]


class EditsUnplannedFile(Stub):
    def __call__(self, stage, state):
        if stage == "E":
            (Path(state["repo"]) / "b.js").write_text("one\ntwo\n")
        return super().__call__(stage, state)


def test_cli_interactive_drift_shows_the_change_and_the_diff(tmp_path, monkeypatch, capsys):
    git = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)  # noqa: E731
    git("init", "-q")
    (tmp_path / "a.js").write_text("x\n")
    git("add", ".")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    hit_cache(monkeypatch)
    stub = EditsUnplannedFile()
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "db.sqlite")]
    assert cli.main(["run", "t", "-i", *base], run_stage=stub, ask=answers("A", "d", "c")) == 0
    out = capsys.readouterr().out
    assert "files not in the plan: b.js" in out and "  b.js  2 LOC" in out
    assert "new files (untracked):\n  b.js" in out and "status=done" in out


def test_cli_drift_pause_lists_the_change_without_interactive(tmp_path, monkeypatch, capsys):
    git = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)  # noqa: E731
    git("init", "-q")
    (tmp_path / "a.js").write_text("x\n")
    git("add", ".")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    hit_cache(monkeypatch)
    stub = EditsUnplannedFile()
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "db.sqlite")]
    assert cli.main(["run", "t", *base], run_stage=stub) == 0
    assert cli.main(["resume", "--thread", "t", "--choice", "A", *base], run_stage=stub) == 0
    out = capsys.readouterr().out
    assert "  b.js  2 LOC" in out and "status=awaiting_drift_decision thread=t" in out


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


def test_fix_is_reviewed_for_drift_too():
    # [FIX] grows the diff past the tier cap; before, only [E] was reviewed and this ended "done"
    stub = Stub(validate_results=(False, True))
    g = build_graph(stub, checkpointer=InMemorySaver(),
                    cache_check=lambda r: {"status": "HIT", "changed": []}, snapshot=lambda r: {},
                    diff_stats=lambda r, b: (["a.js", "x", "y", "z"], 900) if "FIX" in stub.calls else (["a.js"], 5),
                    write_session=lambda s, st: None)
    g.invoke(start(), CFG)
    g.invoke(Command(resume="A"), CFG)
    assert stub.calls[-1] == "FIX" and pause(g)["kind"] == "drift"
    assert "exceeds tier M cap: 4 files / 900 LOC" in pause(g)["reason"]
    g.invoke(Command(resume="stop"), CFG)
    assert g.get_state(CFG).values["status"] == "handed_off" and stub.calls.count("V") == 1


def test_approved_drift_is_not_asked_again_after_fix():
    stub = Stub(validate_results=(False, True))
    g = graph(stub, status="HIT", diff=(["a.js", "c.js"], 5))
    g.invoke(start(), CFG)
    g.invoke(Command(resume="A"), CFG)
    assert "c.js" in pause(g)["reason"]
    g.invoke(Command(resume="continue"), CFG)  # same diff after [FIX]: nothing new to ask about
    assert pause(g) is None and g.get_state(CFG).values["status"] == "done"
    assert stub.calls[-3:] == ["V", "FIX", "V"]


def test_plan_without_files_pauses_instead_of_skipping_the_check():
    g = run_to_review(Stub(approaches=[{"id": "A", "title": "x", "files": []}]), diff=(["a.js", "b.js"], 5))
    assert "the plan listed no files" in pause(g)["reason"]


def test_cli_refuses_session_whose_plan_was_already_chosen(tmp_path, capsys):
    session.write({"repo": str(tmp_path), "task": "t", "chosen": "A", "approaches": APPROACHES}, "in_progress")
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "--from-session", *base], run_stage=Stub()) == 2
    assert "already chosen" in capsys.readouterr().err


@pytest.mark.parametrize("raw,ids", [
    ([{"id": "a"}, {"id": " b "}], ["A", "B"]),
    ([{"id": 1}, {"title": "no id"}, "junk", {"id": "1"}], ["1"]),  # dropped: no id, not an object, repeat
    ({"id": "A"}, []),
])
def test_approach_ids_are_normalised(raw, ids):
    g = graph(Stub(approaches=raw), status="HIT")
    g.invoke(start(), CFG)
    assert [a["id"] for a in g.get_state(CFG).values["approaches"]] == ids
    if ids:
        g.invoke(Command(resume=ids[0].lower()), CFG)
        assert g.get_state(CFG).values["chosen"] == ids[0]


def test_approved_drift_is_not_asked_again_when_fix_changes_the_counts():
    stub = Stub(validate_results=(False, True), approaches=[{"id": "A", "title": "x", "files": ["a.js"],
                                                             "est_loc": 20}])
    g = build_graph(stub, checkpointer=InMemorySaver(),
                    cache_check=lambda r: {"status": "HIT", "changed": []}, snapshot=lambda r: {},
                    diff_stats=lambda r, b: (["a.js", "c.js"], 43 if "FIX" in stub.calls else 40),
                    write_session=lambda s, st: None)
    g.invoke(start(), CFG)
    g.invoke(Command(resume="A"), CFG)
    assert "40 LOC changed vs ~20" in pause(g)["reason"]
    g.invoke(Command(resume="continue"), CFG)
    assert pause(g) is None and g.get_state(CFG).values["status"] == "done"


def test_a_new_unplanned_file_after_fix_is_asked_about():
    stub = Stub(validate_results=(False, True))
    g = build_graph(stub, checkpointer=InMemorySaver(),
                    cache_check=lambda r: {"status": "HIT", "changed": []}, snapshot=lambda r: {},
                    diff_stats=lambda r, b: (["a.js", "c.js", "d.js"] if "FIX" in stub.calls else ["a.js", "c.js"], 5),
                    write_session=lambda s, st: None)
    g.invoke(start(), CFG)
    g.invoke(Command(resume="A"), CFG)
    g.invoke(Command(resume="continue"), CFG)
    assert stub.calls[-1] == "FIX" and "d.js" in pause(g)["reason"]


def test_from_session_refuses_when_head_moved_since_the_plan(tmp_path, monkeypatch, capsys):
    session.write({"repo": str(tmp_path), "task": "t", "approaches": APPROACHES, "cache": {"head": "a" * 40}},
                  "awaiting_approval")
    assert session.read(str(tmp_path))["head"] == "a" * 40
    monkeypatch.setattr(cli.tools, "git_head", lambda repo: "b" * 40)
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "--from-session", *base], run_stage=Stub()) == 2
    assert "HEAD moved" in capsys.readouterr().err


def test_from_session_seeds_the_assigned_tier_and_clean_ids(tmp_path, monkeypatch):
    session.write({"repo": str(tmp_path), "task": "t", "tier": "S", "approaches": APPROACHES}, "awaiting_approval")
    data = session.read(str(tmp_path))
    data["approaches"][0]["id"] = "a"
    session.path(str(tmp_path)).write_text(__import__("json").dumps(data))
    stub = Stub()
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "--from-session", *base], run_stage=stub) == 0
    assert cli.main(["resume", "--thread", "t", "--choice", "a", *base], run_stage=stub) == 0
    assert stub.states["E"]["tier_assigned"] == "S" and not stub.states["E"].get("tier")


def test_crash_handler_keeps_the_original_error_when_the_checkpoint_fails(tmp_path, monkeypatch, capsys):
    hit_cache(monkeypatch)
    broken = {"db": False}

    class LocksTheDb(Stub):
        def __call__(self, stage, state):
            broken["db"] = True
            raise RuntimeError("database is locked")

    real = cli._graph

    def graph_with_failing_state(args, run_stage=None):
        g, config = real(args, run_stage)
        real_get_state = g.get_state

        def get_state(*a, **kw):
            if broken["db"]:
                raise OSError("disk I/O error")
            return real_get_state(*a, **kw)

        g.get_state = get_state
        return g, config

    monkeypatch.setattr(cli, "_graph", graph_with_failing_state)
    with pytest.raises(RuntimeError, match="database is locked"):
        cli.main(["run", "t", "--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")], run_stage=LocksTheDb())
    out = capsys.readouterr().out
    assert "status=interrupted thread=t" in out and "disk I/O error" in out


def stage_pauses(g, *answers):
    """Answer guided stage pauses in order; returns the stages each pause came after."""
    seen = []
    for answer in answers:
        p = pause(g)
        assert p["kind"] == "stage"
        seen.append(p["stage"])
        g.invoke(Command(resume=answer), CFG)
    return seen


@pytest.mark.parametrize("status,reason,changed,paused", [
    ("MISS", "test", (), ["A", "O", "I"]),
    ("HIT", "test", (), ["I"]),  # skipped stages don't pause
    ("DELTA", "1-files-changed", ("src/a.js",), ["DELTA", "I"]),
])
def test_guided_pauses_after_each_analysis_stage_then_at_the_gate(status, reason, changed, paused):
    stub = Stub()
    g = graph(stub, status=status, reason=reason, changed=changed)
    g.invoke(start(guided=True), CFG)
    assert stage_pauses(g, *["continue"] * len(paused)) == paused
    assert pause(g)["kind"] == "plan"  # guided never replaces the [P] gate
    g.invoke(Command(resume="A"), CFG)
    assert g.get_state(CFG).values["status"] == "done"


def test_guided_note_reaches_the_later_stages():
    stub = Stub()
    g = graph(stub, status="HIT")
    g.invoke(start(guided=True), CFG)
    stage_pauses(g, {"decision": "continue", "note": "the bug is in refresh"})
    g.invoke(Command(resume="A"), CFG)
    assert stub.states["P"]["notes"] == [{"stage": "I", "note": "the bug is in refresh"}]
    assert stub.states["E"]["notes"] == [{"stage": "I", "note": "the bug is in refresh"}]


def test_guided_notes_on_cached_stages_go_into_the_cache():
    cached = []
    g = graph(Stub(), status="DELTA", reason="1-files-changed", changed=("src/a.js",), cached_notes=cached)
    g.invoke(start(guided=True), CFG)
    stage_pauses(g, {"decision": "continue", "note": "auth is in services/"},
                 {"decision": "continue", "note": "the bug is in refresh"})
    assert cached == [("O", "auth is in services/")]  # [I] isn't cached, so its note stays in the session only


def test_guided_pause_writes_the_session_with_its_notes():
    written = []
    g = build_graph(
        Stub(), checkpointer=InMemorySaver(),
        cache_check=lambda repo: {"status": "MISS", "changed": []}, cache_stamp=lambda repo: "abc",
        write_session=lambda state, status: written.append((status, list(state.get("notes", [])))),
        cache_note=lambda repo, stage, note: None,
    )
    g.invoke(start(guided=True), CFG)
    assert written == [("in_progress", [])]  # paused after [A]: the skill can continue from here
    stage_pauses(g, {"decision": "continue", "note": "focus on auth"})
    note = [{"stage": "A", "note": "focus on auth"}]
    assert written[1:] == [("in_progress", []), ("in_progress", note), ("in_progress", note)]  # re-run, note, [O]


def test_guided_stop_hands_off_before_planning():
    stub = Stub()
    g = graph(stub)
    g.invoke(start(guided=True), CFG)
    stage_pauses(g, "stop")
    values = g.get_state(CFG).values
    assert stub.calls == ["A"] and values["status"] == "handed_off" and "after stage A" in values["note"]


def test_guided_analyse_only_ends_after_inspect():
    stub = Stub()
    g = graph(stub, status="HIT")
    g.invoke(start(guided=True, analyse_only=True), CFG)
    stage_pauses(g, "continue")
    assert stub.calls == ["I"] and g.get_state(CFG).values["status"] == "analysed"


def test_cli_guided_resume_with_a_note(tmp_path, monkeypatch, capsys):
    hit_cache(monkeypatch)
    stub = Stub()
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", "--guided", *base], run_stage=stub) == 0
    out = capsys.readouterr().out
    assert "status=awaiting_stage_decision thread=t" in out and "--note" in out
    assert cli.main(["resume", "--thread", "t", "--choice", "A", *base], run_stage=stub) == 2
    assert "--choice only applies at the [P] gate" in capsys.readouterr().err
    assert cli.main(["resume", "--thread", "t", "--decision", "stop", "--note", "x", *base], run_stage=stub) == 2
    assert "--note is for continuing" in capsys.readouterr().err
    assert cli.main(["resume", "--thread", "t", "--note", "look at refresh", *base], run_stage=stub) == 0
    assert "status=awaiting_approval" in capsys.readouterr().out
    assert cli.main(["resume", "--thread", "t", "--choice", "A", "--note", "x", *base], run_stage=stub) == 2
    assert "--note only applies at a guided stage pause" in capsys.readouterr().err
    assert cli.main(["resume", "--thread", "t", "--choice", "A", *base], run_stage=stub) == 0
    assert stub.states["E"]["notes"] == [{"stage": "I", "note": "look at refresh"}]


def test_cli_interactive_guided_runs_to_the_end(tmp_path, monkeypatch, capsys):
    real = cli.build_graph
    monkeypatch.setattr(cli, "build_graph", lambda rs, checkpointer: real(
        rs, checkpointer, cache_check=lambda r: {"status": "MISS", "changed": []}, cache_stamp=lambda r: "abc"))
    stub = Stub()
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    argv = ["run", "t", "--guided", "-i", *base]
    assert cli.main(argv, run_stage=stub, ask=answers("n", "focus on auth", "c", "c", "A")) == 0
    assert "status=done" in capsys.readouterr().out
    assert stub.calls == ["A", "O", "I", "P", "E", "V"]
    assert stub.states["O"]["notes"] == [{"stage": "A", "note": "focus on auth"}]


def test_guided_notes_round_trip_through_the_session(tmp_path, monkeypatch, capsys):
    import json

    import jsonschema

    from aa_agent.tools import skill_dir

    # aa-agent → session: the note lands on the stage_log entry of the stage it followed (DELTA logs as O)
    real = cli.build_graph
    monkeypatch.setattr(cli, "build_graph", lambda rs, checkpointer: real(
        rs, checkpointer, cache_check=lambda r: {"status": "DELTA", "reason": "1-files-changed", "changed": ["x.js"]},
        cache_stamp=lambda r: "abc"))
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", "--guided", *base], run_stage=Stub()) == 0
    assert cli.main(["resume", "--thread", "t", "--note", "auth only", *base], run_stage=Stub()) == 0
    assert cli.main(["resume", "--thread", "t", "--note", "refresh path", *base], run_stage=Stub()) == 0
    data = session.read(str(tmp_path))
    assert data["status"] == "awaiting_approval"
    assert {e["stage"]: e.get("note") for e in data["stage_log"]} == {"O": "auth only", "I": "refresh path", "P": None}
    schema = json.loads((skill_dir() / "schemas" / "session.schema.json").read_text())
    jsonschema.validate(data, schema)

    # session (as the skill or that run left it) → aa-agent --from-session: the notes reach [E] and stay logged
    stub = Stub()
    base2 = ["--repo", str(tmp_path), "--db", str(tmp_path / "b.sqlite")]
    assert cli.main(["run", "--from-session", *base2], run_stage=stub) == 0
    assert cli.main(["resume", "--thread", "t", "--choice", "A", *base2], run_stage=stub) == 0
    assert stub.states["E"]["notes"] == [{"stage": "O", "note": "auth only"}, {"stage": "I", "note": "refresh path"}]
    log = session.read(str(tmp_path))["stage_log"]
    assert [(e["stage"], e.get("note")) for e in log] == [
        ("O", "auth only"), ("I", "refresh path"), ("P", None), ("E", None), ("V", None)]


def test_cli_status_and_interactive_resume_show_the_stage_being_reviewed(tmp_path, monkeypatch, capsys):
    hit_cache(monkeypatch)
    stub = Stub()
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", "--guided", *base], run_stage=stub) == 0
    capsys.readouterr()
    assert cli.main(["status", "--thread", "t", *base]) == 0
    assert "━━ [I] INSPECT ━━\n━━ [I] ━━\nI report" in capsys.readouterr().out
    assert cli.main(["resume", "--thread", "t", "-i", *base], run_stage=stub, ask=answers("q")) == 0
    assert "━━ [I] INSPECT ━━\n━━ [I] ━━\nI report" in capsys.readouterr().out


def test_cli_guided_pause_writes_the_session_and_caches_the_note(tmp_path, monkeypatch, capsys):
    real = cli.build_graph
    monkeypatch.setattr(cli, "build_graph", lambda rs, checkpointer: real(
        rs, checkpointer, cache_check=lambda r: {"status": "MISS", "changed": []}, cache_stamp=lambda r: "abc"))
    base = ["--repo", str(tmp_path), "--db", str(tmp_path / "a.sqlite")]
    assert cli.main(["run", "t", "--guided", *base], run_stage=Stub()) == 0
    assert session.read(str(tmp_path))["status"] == "in_progress"
    assert cli.main(["resume", "--thread", "t", "--note", "auth lives in services/", *base], run_stage=Stub()) == 0
    data = session.read(str(tmp_path))
    assert [(e["stage"], e.get("note")) for e in data["stage_log"]] == [("A", "auth lives in services/"), ("O", None)]
    context = (tmp_path / ".aa" / "context.md").read_text()
    assert context.endswith("## Human corrections\n- after [A]: auth lives in services/\n")
