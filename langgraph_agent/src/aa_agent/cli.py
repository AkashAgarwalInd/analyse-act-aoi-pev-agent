"""aa-agent: run Analyse-Act headlessly. `run` stops at the [P] gate; `resume` continues with the chosen approach.

  aa-agent run "users get logged out early" --plan-out plan.md
  aa-agent resume --thread users-get-logged-out-early --choice A
  aa-agent status --thread users-get-logged-out-early
"""

import argparse
import sqlite3
import sys
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from . import tools
from .graph import build_graph
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


def _report(graph, config, plan_out: str | None) -> int:
    snap = graph.get_state(config)
    thread = config["configurable"]["thread_id"]
    if snap.next == ("gate",):
        if plan_out:
            Path(plan_out).write_text(plan_markdown(snap.values))
        ids = " | ".join(a["id"] for a in snap.values.get("approaches", []))
        print(f"\nstatus=awaiting_approval thread={thread}")
        print(f"approve with: aa-agent resume --thread {thread} --choice <{ids}>")
        return 0
    status = snap.values.get("status", "unknown")
    print(f"\nstatus={status} thread={thread}")
    return 0 if status in ("analysed", "done") else 1


def main(argv=None, run_stage=None) -> int:
    parser = argparse.ArgumentParser(prog="aa-agent", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--repo", default=".", help="target repo (default: .)")
        p.add_argument("--db", help="checkpoint database (default: <repo>/.aa/agent.sqlite)")
        p.add_argument("--plan-out", help="write the [P] plan as markdown here (e.g. $GITHUB_STEP_SUMMARY)")

    run = sub.add_parser("run", help="run until the [P] gate (or the end with --analyse-only)")
    run.add_argument("task")
    run.add_argument("--thread", help="thread id (default: task slug)")
    run.add_argument("--analyse-only", action="store_true", help="stop after [I]")
    run.add_argument("--tier", choices=["S", "M", "L"])
    run.add_argument("--branch", action="store_true", help="create aa/<task-slug> before [E]")
    run.add_argument("--check", action="append", default=[], help="extra allowed check command (repeatable)")
    run.add_argument("--model", default=DEFAULT_MODEL)
    common(run)

    resume = sub.add_parser("resume", help="continue past the [P] gate with an approach")
    resume.add_argument("--thread", required=True)
    resume.add_argument("--choice", required=True)
    resume.add_argument("--model", default=DEFAULT_MODEL)
    common(resume)

    status = sub.add_parser("status", help="show where a thread stopped")
    status.add_argument("--thread", required=True)
    common(status)

    args = parser.parse_args(argv)
    if args.cmd == "run" and not args.thread:
        args.thread = tools.slug(args.task)
    graph, config = _graph(args, run_stage)

    if args.cmd == "run":
        if graph.get_state(config).values:
            print(f"thread {args.thread!r} already exists; use `resume`, `status`, or a new --thread", file=sys.stderr)
            return 2
        payload = {
            "repo": str(Path(args.repo).resolve()), "task": args.task, "analyse_only": args.analyse_only,
            "tier": args.tier, "branch": args.branch, "checks": args.check,
            "outputs": {}, "stage_log": [], "retry_used": False,
        }
        _stream(graph, payload, config)
        return _report(graph, config, args.plan_out)

    snap = graph.get_state(config)
    if not snap.values:
        print(f"no thread {args.thread!r} in this database", file=sys.stderr)
        return 2

    if args.cmd == "resume":
        if snap.next != ("gate",):
            print(f"thread {args.thread!r} is not waiting at the [P] gate", file=sys.stderr)
            return 2
        ids = [a["id"] for a in snap.values.get("approaches", [])]
        choice = args.choice.strip().upper()
        if ids and choice not in ids:
            print(f"unknown approach {choice!r}; choose one of {ids}", file=sys.stderr)
            return 2
        _stream(graph, Command(resume=choice), config)
        return _report(graph, config, args.plan_out)

    print(f"stages: {' → '.join(snap.values.get('stage_log', []))}")
    print(f"next: {snap.next or 'end'}")
    return _report(graph, config, args.plan_out)


if __name__ == "__main__":
    sys.exit(main())
