"""Terminal prompts for `aa-agent -i`: answer each pause in place instead of running `resume`.

`ask(question) -> str` is `input` in a terminal and a scripted function in tests. Every prompt returns None
when the human quits (q, EOF, Ctrl-C, or too many bad answers); the thread then stays paused.
"""

from collections.abc import Callable
from typing import TextIO

Ask = Callable[[str], str]
TRIES = 3


def _read(ask: Ask, question: str) -> str | None:
    try:
        return ask(question).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def ask_plan(approaches: list[dict], ask: Ask, out: TextIO) -> dict | None:
    """{"choice": id, "edit": str} for the approach the human picks, or None to quit."""
    ids = [a["id"] for a in approaches]
    default = next((a["id"] for a in approaches if a.get("recommended")), ids[0])
    print(file=out)
    for a in approaches:
        title = a.get("title", "") + (" (Recommended)" if a.get("recommended") else "")
        details = [f"files: {', '.join(a.get('files') or []) or '?'}"]
        if a.get("est_loc"):
            details.append(f"~{a['est_loc']} LOC")
        if a.get("risk"):
            details.append(f"risk {a['risk']}")
        print(f"[{a['id']}] {title}   {'   '.join(details)}", file=out)

    def pick(answer: str) -> str | None:
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(ids):
            return ids[int(answer) - 1]
        return answer.upper() if answer.upper() in ids else None

    question = f"Choose {'/'.join(ids)} (or 1-{len(ids)}), e to edit, q to quit [{default}]: "
    for _ in range(TRIES):
        answer = _read(ask, question)
        if answer is None or answer.lower() == "q":
            return None
        if answer.lower() == "e":
            for _ in range(TRIES):
                which = _read(ask, f"Which approach? [{default}]: ")
                if which is None or which.lower() == "q":
                    return None
                if choice := pick(which):
                    edit = _read(ask, "Your changes to the plan: ")
                    return None if edit is None else {"choice": choice, "edit": edit}
                print(f"pick one of {', '.join(ids)}", file=out)
            return None
        if choice := pick(answer):
            return {"choice": choice, "edit": ""}
        print(f"pick one of {', '.join(ids)}, e or q", file=out)
    return None


def ask_decision(pause: dict, ask: Ask, out: TextIO, details: Callable[[], str] | None = None) -> str | None:
    """"continue" or "stop" at a budget or drift pause, or None to quit. `details` (e.g. the full diff) is
    shown on `d` and doesn't count as a bad answer."""
    print(f"\n{pause['reason']}", file=out)
    question = f"[c]ontinue / [s]top{' / [d]iff' if details else ''} / [q]uit: "
    tries = 0
    while tries < TRIES:
        answer = _read(ask, question)
        if answer is None or answer.lower() in ("q", "quit"):
            return None
        if details and answer.lower() in ("d", "diff"):
            print(details(), file=out)
            continue
        if answer.lower() in ("c", "continue"):
            return "continue"
        if answer.lower() in ("s", "stop"):
            return "stop"
        print(f"answer c, s{', d' if details else ''} or q", file=out)
        tries += 1
    return None
