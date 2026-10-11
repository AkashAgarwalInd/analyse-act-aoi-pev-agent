import io

import pytest

from aa_agent.prompt import ask_decision, ask_plan

APPROACHES = [
    {"id": "A", "title": "inline", "files": ["a.js"], "est_loc": 10, "risk": "Low"},
    {"id": "B", "title": "boundary", "files": ["a.js", "b.js"], "recommended": True},
]


def scripted(*answers):
    replies = iter(answers)

    def ask(question):
        reply = next(replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    return ask


@pytest.mark.parametrize("answers,expected", [
    ([""], {"choice": "B", "edit": ""}),  # Enter = recommended
    (["a"], {"choice": "A", "edit": ""}),
    (["1"], {"choice": "A", "edit": ""}),
    (["x", "9", "B"], {"choice": "B", "edit": ""}),
    (["e", "a", "also cover refresh"], {"choice": "A", "edit": "also cover refresh"}),
    (["E", "", "keep it small"], {"choice": "B", "edit": "keep it small"}),
])
def test_ask_plan_picks(answers, expected):
    assert ask_plan(APPROACHES, scripted(*answers), io.StringIO()) == expected


@pytest.mark.parametrize("answers", [
    ["q"], [EOFError()], [KeyboardInterrupt()], ["x", "y", "z"], ["e", "q"], ["e", "A", EOFError()],
])
def test_ask_plan_quits(answers):
    assert ask_plan(APPROACHES, scripted(*answers), io.StringIO()) is None


def test_ask_plan_shows_the_approaches():
    out = io.StringIO()
    ask_plan(APPROACHES, scripted("q"), out)
    assert "[A] inline   files: a.js   ~10 LOC   risk Low" in out.getvalue()
    assert "[B] boundary (Recommended)   files: a.js, b.js" in out.getvalue()


@pytest.mark.parametrize("answers,expected", [
    (["c"], "continue"), (["Continue"], "continue"), (["s"], "stop"), (["?", "stop"], "stop"),
    (["q"], None), ([EOFError()], None), (["?", "?", "?"], None),
])
def test_ask_decision(answers, expected):
    out = io.StringIO()
    assert ask_decision({"kind": "budget", "reason": "⚠ over budget"}, scripted(*answers), out) == expected
    assert "⚠ over budget" in out.getvalue()


FIVE = [{"id": i, "title": i, "files": []} for i in "ABCDE"]


@pytest.mark.parametrize("answers,expected", [
    (["E"], {"choice": "E", "edit": ""}),  # an approach id wins over the edit key
    (["edit", "e", "smaller"], {"choice": "E", "edit": "smaller"}),
    (["quit"], None),
])
def test_ask_plan_approach_e_is_choosable(answers, expected):
    questions, ask = [], scripted(*answers)
    assert ask_plan(FIVE, lambda q: questions.append(q) or ask(q), io.StringIO()) == expected
    assert "edit to edit, q to quit" in questions[0]
