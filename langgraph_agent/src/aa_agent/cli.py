"""aa-agent: run Analyse-Act headlessly. `run` stops at the [P] gate; `resume` continues a paused thread.

  aa-agent run "users get logged out early" --check "npm test" --plan-out plan.md
  aa-agent resume --thread users-get-logged-out-early --choice A [--edit "also cover refresh tokens"]
  aa-agent resume --thread users-get-logged-out-early --decision continue    # after a budget/drift pause
  aa-agent run "users get logged out early" --check "npm test" -i    # answer each pause in the terminal
  aa-agent status --thread users-get-logged-out-early
  aa-agent resume --thread users-get-logged-out-early    # after a crash or Ctrl-C: retry the stage it stopped in
  aa-agent run --from-session      # continue at [P] from a session the interactive skill wrote
  aa-agent run "users get logged out early" --guided    # also pause after [A], [O], [I] for review or a note
  aa-agent resume --thread users-get-logged-out-early --note "the bug is in refresh, not login"    # = continue + note
"""

import argparse
import functools
import os
import sqlite3
import sys
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from . import prompt, session, tools
from .graph import CAPS, build_graph, clean_approaches
from .stages import DEFAULT_MODEL, NAMES, make_run_stage


def _graph(args, run_stage=None):
    repo = Path(args.repo).resolve()
    db = Path(args.db) if args.db else repo / ".aa" / "agent.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver(sqlite3.connect(db, check_same_thread=False))
    run_stage = run_stage or make_run_stage(model=getattr(args, "model", DEFAULT_MODEL))
    return build_graph(run_stage, checkpointer=saver), {"configurable": {"thread_id": args.thread}}


def _stream(graph, payload, config) -> None:
    for update in graph.stream(payload, config, stream_mode="updates"):
        for node, values in update.items():
            if node == "__interrupt__" or not isinstance(values, dict):
                continue
            for letter, report in (values.get("outputs") or {}).items():
                print(f"\n━━ [{letter}] {NAMES.get(letter, letter)} ━━\n{report}", flush=True)
            if node == "cache":
                cache = values["cache"]
                print(f"cache {cache.get('status')} ({cache.get('reason')})", flush=True)
            if values.get("branch_name"):
                print(f"branch {values['branch_name']}", flush=True)


def plan_markdown(state: dict) -> str:
    rows = ["| id | approach | files | est. LOC | risk |", "|---|---|---|---|---|"]
    for a in state.get("approaches", []):
        title = a.get("title", "") + (" **(Recommended)**" if a.get("recommended") else "")
        rows.append(
            f"| {a.get('id')} | {title} | {', '.join(a.get('files', []))} | {a.get('est_loc', '')} "
            f"| {a.get('risk', '')} |"
        )
    return (
        f"## Analyse-Act plan: {state.get('task')}\n\n" + "\n".join(rows)
        + f"\n\n<details><summary>Full [P] report</summary>\n\n{state['outputs'].get('P', '')}\n\n</details>\n"
    )


def _pause(snap) -> dict | None:
    """The interrupt payload a thread is stopped at (kind: plan | budget | drift), or None."""
    return snap.interrupts[0].value if snap.interrupts else None


def _print_changes(snap) -> None:
    for path, loc in sorted(tools.diff_by_file(snap.values["repo"], snap.values.get("baseline_dirty", {})).items()):
        print(f"  {path}  {loc} LOC")


def _write_plan(snap, plan_out: str | None) -> None:
    if plan_out:
        Path(plan_out).write_text(plan_markdown(snap.values))


def _report(graph, config, plan_out: str | None) -> int:
    snap = graph.get_state(config)
    thread = config["configurable"]["thread_id"]
    pause = _pause(snap)
    if pause and pause["kind"] == "plan":
        _write_plan(snap, plan_out)
        ids = " | ".join(a["id"] for a in snap.values.get("approaches", []))
        print(f"\nstatus=awaiting_approval thread={thread}")
        print(f'approve with: aa-agent resume --thread {thread} --choice <{ids}> [--edit "<changes to the plan>"]')
        return 0
    if pause:
        print(f"\n{pause['reason']}")
        if pause["kind"] == "drift":
            _print_changes(snap)
        print(f"status=awaiting_{pause['kind']}_decision thread={thread}")
        print(f"continue with: aa-agent resume --thread {thread} --decision <continue|stop>")
        if pause["kind"] == "stage":
            print(f'or add context: aa-agent resume --thread {thread} --note "<context for the next stages>"')
        return 0
    if snap.next:
        print(f"\nstopped inside {', '.join(snap.next)} (a crash or Ctrl-C)")
        print(f"status=interrupted thread={thread}")
        print(_retry_hint(snap, thread))
        return 1
    status = snap.values.get("status", "unknown")
    if snap.values.get("note"):
        print(f"\nnote: {snap.values['note']}")
    print(f"\nstatus={status} thread={thread}")
    return 0 if status in ("analysed", "done") else 1


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


# Re-running these would snapshot their own half-made edits as "already dirty" and miss them in the drift check.
EDITING_NODES = ("execute", "fix")


def _retry_hint(snap, thread: str) -> str:
    if set(snap.next) & set(EDITING_NODES):
        return ("it stopped while editing, so it can't be retried safely: check `git diff`, then finish with "
                "/analyse-act --from E (or discard the edits and start a new run)")
    return f"retry the stage with: aa-agent resume --thread {thread}"


def _head_moved(snap) -> str | None:
    """Why the thread can't continue on this checkout, or None."""
    planned, now = snap.values.get("cache", {}).get("head", ""), tools.git_head(snap.values["repo"])
    if planned and now != planned:
        return (
            f"HEAD moved since the plan was made ({planned[:12]} → {now[:12] or 'none'}); "
            "check out the planned commit or start a new run"
        )
    return None


def _answer(snap, choice: str | None, edit: str | None, decision: str | None, note: str | None = None):
    """(resume value, None) for the pause the thread is stopped at, or (None, error message)."""
    pause = _pause(snap)
    if note and pause["kind"] != "stage":
        return None, f"--note only applies at a guided stage pause; this is a {pause['kind']} pause"
    if pause["kind"] == "plan":
        ids = [a["id"] for a in snap.values.get("approaches", [])]
        choice = (choice or "").strip().upper()
        if choice not in ids:
            return None, f"at the [P] gate: pass --choice with one of {ids}"
        return {"choice": choice, "edit": edit or ""}, None
    if edit:
        return None, f"--edit only applies at the [P] gate; this is a {pause['kind']} pause"
    if choice:
        return None, f"--choice only applies at the [P] gate; this is a {pause['kind']} pause"
    if pause["kind"] == "stage" and note:  # a note means continue with it
        if decision == "stop":
            return None, "--note is for continuing; drop it to stop"
        return {"decision": "continue", "note": note}, None
    if not decision:
        return None, f"paused for {pause['kind']}: {pause['reason']}\npass --decision continue|stop"
    if pause["kind"] == "stage":
        return {"decision": decision, "note": ""}, None
    return decision, None


def _drive(graph, config, plan_out: str | None, ask: prompt.Ask) -> int:
    """Answer each pause in the terminal until the run ends or the human quits (the thread stays paused)."""
    while pause := _pause(snap := graph.get_state(config)):
        if error := _head_moved(snap):
            return _fail(error)
        if pause["kind"] == "plan":
            _write_plan(snap, plan_out)
            picked = prompt.ask_plan(snap.values.get("approaches", []), ask, sys.stdout)
            choice, edit, decision, note = (picked or {}).get("choice"), (picked or {}).get("edit"), None, None
        elif pause["kind"] == "stage":
            picked = prompt.ask_stage(pause, ask, sys.stdout)
            choice = edit = None
            decision, note = (picked or {}).get("decision"), (picked or {}).get("note")
        else:
            details = None
            if pause["kind"] == "drift":
                _print_changes(snap)
                baseline = snap.values.get("baseline_dirty", {})
                details = functools.partial(tools.diff_text, snap.values["repo"], baseline)
            picked = decision = prompt.ask_decision(pause, ask, sys.stdout, details)
            choice = edit = note = None
        if picked is None:
            break
        answer, error = _answer(snap, choice, edit, decision, note)
        if error:
            return _fail(error)
        _stream(graph, Command(resume=answer), config)
    return _report(graph, config, plan_out)


def _seed_from_session(graph, config, args) -> int | None:
    """Start at the [P] gate from a session the interactive skill wrote. Returns an exit code on error."""
    repo = str(Path(args.repo).resolve())
    data = session.read(repo) or {}
    approaches = clean_approaches(data.get("approaches"))
    if not data.get("task") or not approaches:
        return _fail(f"no .aa/current_session.json with a task and approaches in {repo}")
    if data.get("status") not in (None, "in_progress", "awaiting_approval"):
        return _fail(f"session status is {data['status']!r}; only a session waiting at [P] can be continued")
    if data.get("chosen"):
        return _fail(
            f"approach {data['chosen']!r} was already chosen in that session and may have been executed; "
            "check the working tree, then finish it with the skill (/analyse-act --from E) instead"
        )
    if args.task and args.task != data["task"]:
        return _fail(f"session is for {data['task']!r}, not {args.task!r}")
    head = data.get("head") or ""
    if not head:
        print("note: the session doesn't record the commit its plan was made on, so a moved HEAD can't be "
              "detected; assuming the current one", file=sys.stderr)
    elif head != (now := tools.git_head(repo)):
        return _fail(f"HEAD moved since the plan was made ({head[:12]} → {now[:12] or 'none'}); "
                     "check out the planned commit or make a new plan")
    args.task = data["task"]
    plan = "\n".join(f"[{a['id']}] {a.get('title', '')}  files: {', '.join(a.get('files', []))}"
                     for a in approaches)
    tier = {"tier_assigned": data["tier"]} if data.get("tier") in CAPS else {}  # [I]'s tier, not a --tier
    log = [e for e in data.get("stage_log", []) if isinstance(e, dict)]
    notes = [{"stage": str(e.get("stage", "")), "note": e["note"]} for e in log if isinstance(e.get("note"), str)]
    graph.update_state(config, {
        **_payload(args, repo), **tier, "approaches": approaches,
        "outputs": {"P": f"(plan from the interactive session)\n{plan}"}, "last_stage": "P",
        "cache": {"status": "SESSION", "head": head or tools.git_head(repo), "changed": []},
        "session_log": log, "notes": notes,
    }, as_node="plan")
    return None


def _payload(args, repo: str) -> dict:
    return {
        "repo": repo, "task": args.task, "analyse_only": args.analyse_only, "refresh": args.refresh,
        "tier": args.tier, "branch": args.branch, "checks": args.check, "guided": args.guided,
        "outputs": {}, "stage_log": [], "retry_used": False,
    }


def main(argv=None, run_stage=None, ask: prompt.Ask | None = None) -> int:
    running: dict = {}  # _main fills in graph and config once the thread is open
    try:
        return _main(argv, run_stage, ask, running)
    except BaseException as error:
        if running:
            thread = running["config"]["configurable"]["thread_id"]
            try:
                snap = running["graph"].get_state(running["config"])
                hint = _retry_hint(snap, thread) if snap.next else None
            except Exception as state_error:  # e.g. the checkpoint db is what failed; keep the original error
                hint = f"couldn't read the checkpoint to say how to continue: {state_error}"
            if hint:
                print(f"\nstatus=interrupted thread={thread}\n{hint}", flush=True)
        if isinstance(error, KeyboardInterrupt):
            return 130
        raise


def _main(argv, run_stage, ask, running: dict) -> int:
    parser = argparse.ArgumentParser(prog="aa-agent", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--repo", default=".", help="target repo (default: .)")
        p.add_argument("--db", help="checkpoint database (default: <repo>/.aa/agent.sqlite)")
        p.add_argument("--plan-out", help="write the [P] plan as markdown here (e.g. $GITHUB_STEP_SUMMARY)")

    def interactive(p):
        p.add_argument("-i", "--interactive", action="store_true",
                       help="answer each pause in the terminal and keep going (off without a TTY or in CI)")

    run = sub.add_parser("run", help="run until the [P] gate (or the end with --analyse-only)")
    run.add_argument("task", nargs="?", help="the task (optional with --from-session)")
    run.add_argument("--thread", help="thread id (default: task slug)")
    run.add_argument("--from-session", action="store_true",
                     help="start at the [P] gate from .aa/current_session.json written by the interactive skill")
    run.add_argument("--analyse-only", action="store_true", help="stop after [I]")
    run.add_argument("--refresh", action="store_true", help="ignore the cache: full [A] and [O]")
    run.add_argument("--tier", choices=["S", "M", "L"])
    run.add_argument("--branch", action="store_true", help="create aa/<task-slug> before [E]")
    run.add_argument("--guided", action="store_true",
                     help="also pause after [A], [O] and [I] to review them or add a note; [P] still asks either way")
    run.add_argument(
        "--check", action="append", default=[],
        help="a test/lint command [E]/[V] may run, e.g. 'npm test' (repeatable). Nothing else can be run.",
    )
    run.add_argument("--model", default=DEFAULT_MODEL)
    common(run)
    interactive(run)

    resume = sub.add_parser("resume", help="continue a paused thread")
    resume.add_argument("--thread", required=True)
    resume.add_argument("--choice", help="at the [P] gate: the approach to execute")
    resume.add_argument("--edit", help="at the [P] gate: changes to the chosen approach, in plain words")
    resume.add_argument("--decision", choices=["continue", "stop"], help="at a budget, drift or guided stage pause")
    resume.add_argument("--note", help="at a guided stage pause: continue, with context or corrections for the next "
                        "stages")
    resume.add_argument("--model", default=DEFAULT_MODEL)
    common(resume)
    interactive(resume)

    status = sub.add_parser("status", help="show where a thread stopped")
    status.add_argument("--thread", required=True)
    common(status)

    args = parser.parse_args(argv)
    if not getattr(args, "interactive", False):
        ask = None
    elif ask is None:
        if os.environ.get("CI"):
            print("interactive off: CI is set", file=sys.stderr)
        elif not sys.stdin.isatty():
            print("interactive off: stdin is not a terminal", file=sys.stderr)
        else:
            ask = input
    if args.cmd == "run":
        if not args.task and not args.from_session:
            return _fail("give a task, or --from-session")
        if not args.thread:
            task = args.task or (session.read(str(Path(args.repo).resolve())) or {}).get("task", "")
            args.thread = tools.slug(task)
    graph, config = _graph(args, run_stage)
    running.update(graph=graph, config=config)

    if args.cmd == "run":
        if graph.get_state(config).values:
            return _fail(f"thread {args.thread!r} already exists; use `resume`, `status`, or a new --thread")
        if args.from_session:
            if (code := _seed_from_session(graph, config, args)) is not None:
                return code
            _stream(graph, None, config)
        else:
            _stream(graph, _payload(args, str(Path(args.repo).resolve())), config)
        return _drive(graph, config, args.plan_out, ask) if ask else _report(graph, config, args.plan_out)

    snap = graph.get_state(config)
    if not snap.values:
        return _fail(f"no thread {args.thread!r} in this database")

    if args.cmd == "resume":
        if not _pause(snap):
            if not snap.next:
                return _fail(f"thread {args.thread!r} is not paused")
            if set(snap.next) & set(EDITING_NODES) or args.choice or args.decision or args.edit or args.note:
                return _fail(f"thread {args.thread!r} was interrupted, not paused; {_retry_hint(snap, args.thread)}")
            if error := _head_moved(snap):
                return _fail(error)
            print(f"retrying {', '.join(snap.next)}", flush=True)
            _stream(graph, None, config)  # re-runs the node it stopped in, from the last checkpoint
            return _drive(graph, config, args.plan_out, ask) if ask else _report(graph, config, args.plan_out)
        if error := _head_moved(snap):
            return _fail(error)
        if ask and not (args.choice or args.decision or args.edit or args.note):
            if _pause(snap)["kind"] == "plan":  # the run that stopped here printed the report; show it again
                print(f"\n━━ [P] {NAMES['P']} ━━\n{snap.values['outputs'].get('P', '')}", flush=True)
            return _drive(graph, config, args.plan_out, ask)
        answer, error = _answer(snap, args.choice, args.edit, args.decision, args.note)
        if error:
            return _fail(error)
        _stream(graph, Command(resume=answer), config)
        return _drive(graph, config, args.plan_out, ask) if ask else _report(graph, config, args.plan_out)

    print(f"stages: {' → '.join(snap.values.get('stage_log', []))}")
    print(f"next: {snap.next or 'end'}")
    return _report(graph, config, args.plan_out)


if __name__ == "__main__":
    sys.exit(main())
