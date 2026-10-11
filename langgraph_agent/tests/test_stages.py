"""The LLM half of the runner, driven by a scripted tool-calling model (no network)."""

from langchain_core.messages import AIMessage

from aa_agent import stages


class ScriptedModel:
    """Replays AIMessages in order; records what it was sent and which tools were bound."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.bound = []

    def bind_tools(self, tool_list):
        self.bound = [t.name for t in tool_list]
        return self

    def invoke(self, messages):
        self.sent.append(list(messages))
        return self.replies.pop(0)


def call(name, args, id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id}])


PLAN = 'matrix\n```json\n{"approaches": [{"id": "A", "title": "inline", "files": ["a.txt"]}]}\n```'


def state(repo, **extra):
    return {"repo": str(repo), "task": "t", "cache": {"status": "MISS"}, "outputs": {}, **extra}


def test_plan_stage_runs_tools_and_parses_approaches(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    model = ScriptedModel([call("read_file", {"path": "a.txt"}), AIMessage(content=PLAN)])
    result = stages.make_run_stage(llm=model)("P", state(tmp_path))
    assert result["approaches"][0]["id"] == "A"
    tool_reply = model.sent[1][-1]
    assert tool_reply.tool_call_id == "c1" and "1\thello" in tool_reply.content
    # [P] can write .aa/ but not source, and can't run commands
    assert "write_aa_file" in model.bound and "edit_file" not in model.bound and "run_check" not in model.bound


def test_tool_errors_go_back_to_the_model(tmp_path):
    model = ScriptedModel([
        call("read_file", {"path": "../outside"}, id="c1"),
        call("no_such_tool", {}, id="c2"),
        AIMessage(content='done\n```json\n{"passed": true}\n```'),
    ])
    result = stages.make_run_stage(llm=model)("V", state(tmp_path, checks=["true"]))
    assert result["passed"] is True
    assert "outside the repo" in model.sent[1][-1].content
    assert model.sent[2][-1].content.startswith("error:")
    assert "run_check" in model.bound and "edit_file" not in model.bound


def test_validate_without_json_is_not_a_pass(tmp_path):
    model = ScriptedModel([AIMessage(content="looks fine to me")])
    assert stages.make_run_stage(llm=model)("V", state(tmp_path))["passed"] is False


def test_step_limit_stops_the_loop(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    model = ScriptedModel([call("list_dir", {}, id=str(i)) for i in range(3)])
    result = stages.make_run_stage(llm=model, max_steps=3)("I", state(tmp_path))
    assert "stopped after 3 tool steps" in result["output"]


def test_prompts_come_from_the_skill():
    system = stages.system_prompt("DELTA")
    assert "Non-negotiable rules" in system and "Caching protocol" in system and "headless" in system
    message = stages.task_message("DELTA", {"task": "t", "cache": {"status": "DELTA", "changed": ["src/a.js"]}})
    assert "src/a.js" in message and "rescan ONLY the changed files" in message


def test_default_model_builds_without_network(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert callable(stages.make_run_stage())
