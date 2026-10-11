import subprocess

import pytest

from aa_agent import tools
from aa_agent.stages import last_json_block, parse_approaches


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    git("add", ".")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return tmp_path


def by_name(repo, **kw):
    kw = {"write_aa": False, "edit": False, "checks": None, **kw}
    return {t.name: t for t in tools.make_tools(repo, **kw)}


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "sub/../../x", ".git/config"])
def test_safe_path_rejects_escapes(repo, bad):
    with pytest.raises(ValueError):
        tools.safe_path(repo, bad)


def test_read_tools(repo):
    t = by_name(repo)
    assert "2\ttwo" in t["read_file"].invoke({"path": "a.txt"})
    assert t["list_dir"].invoke({}) == "a.txt"
    assert "a.txt:2:two" in t["grep"].invoke({"pattern": "tw"})
    assert "init" in t["git"].invoke({"argv": ["log", "--oneline"]})


@pytest.mark.parametrize("argv", [
    ["push"], ["commit", "-m", "x"], ["diff", "--output=/tmp/x"], ["log", "-c"],
    # these read or run things outside the repo; a blocklist missed the first two
    ["diff", "--no-index", "/etc/hosts", "/dev/null"], ["blame", "--contents", "x", "a.txt"],
    ["grep", "-f", "x", "a"], ["grep", "-O", "a"], ["show", "../../etc/passwd"], ["log", "--exec-path"],
])
def test_git_rejects_writes_and_escapes(repo, argv):
    with pytest.raises(ValueError):
        by_name(repo)["git"].invoke({"argv": argv})


@pytest.mark.parametrize("argv", [
    ["log", "-5", "--oneline", "--format=%h %s"], ["diff", "HEAD~1..HEAD", "--", "a.txt"],
    ["grep", "-n", "-e", "/api/v1", "--", "src"], ["blame", "-L", "1,2", "a.txt"], ["show", "HEAD:a.txt"],
])
def test_git_allows_common_reads(argv):
    tools.check_git_args(argv)


def test_stage_tool_sets(repo):
    assert set(by_name(repo)) == {"read_file", "list_dir", "grep", "git"}
    assert "write_aa_file" in by_name(repo, write_aa=True)
    assert {"edit_file", "write_file", "run_check"} <= set(by_name(repo, edit=True, checks=[]))


def test_write_aa_file_stays_in_aa(repo):
    t = by_name(repo, write_aa=True)["write_aa_file"]
    for name in ("context.md", "context.web-app.md", "schema_cache.json"):
        t.invoke({"name": name, "content": "ctx"})
    assert (repo / ".aa" / "context.md").read_text() == "ctx"
    with pytest.raises(ValueError):
        t.invoke({"name": "../a.txt", "content": "x"})


@pytest.mark.parametrize("name", [
    "agent.sqlite", "AGENT.sqlite", "cache_meta.json", "dirty_at_scan.txt", "current_session.json",
    "sessions/x.json", "./cache_meta.json", "sub/context.md", "context.md/../cache_meta.json",
])
def test_runner_owned_aa_files_are_not_writable(repo, name):
    with pytest.raises(ValueError):
        by_name(repo, write_aa=True)["write_aa_file"].invoke({"name": name, "content": "x"})


@pytest.mark.parametrize("path", [".aa/agent.sqlite", ".AA/cache_meta.json", ".aa/context.md", ".GIT/config"])
def test_execute_cannot_write_aa_or_git(repo, path):
    t = by_name(repo, edit=True)
    with pytest.raises(ValueError):
        t["write_file"].invoke({"path": path, "content": "x"})


def test_edit_file_needs_unique_match(repo):
    t = by_name(repo, edit=True)["edit_file"]
    t.invoke({"path": "a.txt", "old": "two", "new": "2"})
    assert (repo / "a.txt").read_text() == "one\n2\n"
    with pytest.raises(ValueError, match="0 times"):
        t.invoke({"path": "a.txt", "old": "two", "new": "2"})


def test_run_check_only_allows_operator_commands(repo):
    t = by_name(repo, checks=["git status --short", "git --version"])["run_check"]
    assert t.invoke({"command": "git status --short a.txt"}).startswith("exit 0")  # appended path allowed
    assert t.invoke({"command": "git status --short -- a.txt"}).startswith("exit 0")
    assert t.invoke({"command": "git --version"}).startswith("exit 0")
    with pytest.raises(ValueError, match="not allowed"):
        t.invoke({"command": "rm -rf ."})


@pytest.mark.parametrize("extra", ["--porcelain", "-- --porcelain", "/etc", "../x", "~/x", "-- -- x"])
def test_run_check_rejects_appended_options_and_outside_paths(repo, extra):
    # e.g. `pytest --basetemp=<dir>` deletes <dir>; appended arguments may only name tests
    with pytest.raises(ValueError, match="only test files or names"):
        by_name(repo, checks=["git status"])["run_check"].invoke({"command": f"git status {extra}"})


def test_model_cannot_grant_itself_a_command(repo):
    # the model writes .aa/schema_cache.json during [O]; a test command recorded there must not become runnable
    by_name(repo, write_aa=True)["write_aa_file"].invoke(
        {"name": "schema_cache.json", "content": '{"tests": {"command": "sh"}}'})
    with pytest.raises(ValueError, match="none"):
        by_name(repo, checks=[])["run_check"].invoke({"command": "sh -c 'echo pwned'"})


def test_cache_check_and_stamp(repo):
    assert tools.cache_check(str(repo))["status"] == "MISS"
    (repo / ".aa").mkdir()
    (repo / ".aa" / "context.md").write_text("ctx")
    (repo / ".aa" / "schema_cache.json").write_text("{}")
    assert len(tools.cache_stamp(str(repo))) == 40
    assert tools.cache_check(str(repo)) == {"status": "HIT", "reason": "up-to-date", "changed": [],
                                            "head": tools.cache_stamp(str(repo)),
                                            "cached": tools.cache_stamp(str(repo))}


def test_git_head(repo, tmp_path_factory):
    assert len(tools.git_head(str(repo))) == 40
    assert tools.git_head(str(tmp_path_factory.mktemp("plain"))) == ""


def test_create_branch_dedupes(repo):
    assert tools.create_branch(str(repo), "Fix: the Bug!") == "aa/fix-the-bug"
    assert tools.create_branch(str(repo), "fix the bug") == "aa/fix-the-bug-2"


def test_slug_limits_length():
    assert tools.slug("x" * 100) == "x" * 40
    assert tools.slug("!!!") == "task"


def test_parse_approaches_json_and_fallback():
    text = 'matrix\n```json\n{"approaches": [{"id": "A", "title": "t", "files": []}]}\n```'
    assert parse_approaches(text)[0]["id"] == "A"
    fallback = parse_approaches("[A] Inline fix  (Recommended)\n    Files: x\n[B] Boundary\n")
    assert [(a["id"], a["recommended"]) for a in fallback] == [("A", True), ("B", False)]
    assert last_json_block('```json\n{"passed": true}\n```') == {"passed": True}
    assert last_json_block("no json") == {}


def test_diff_stats_counts_tracked_and_untracked(repo):
    (repo / "a.txt").write_text("one\nTWO\nthree\nfour\n")  # 1 changed + 2 added -> max(3, 1)
    (repo / "new.txt").write_text("x\ny\n")
    (repo / ".aa").mkdir()
    (repo / ".aa" / "context.md").write_text("ignored\n")
    assert tools.diff_stats(str(repo), {}) == (["a.txt", "new.txt"], 5)


def test_diff_stats_measures_edits_to_already_dirty_files(repo):
    (repo / "a.txt").write_text("one\ntwo\nlocal\n")  # dirty before [E]
    (repo / "keep.txt").write_text("untouched\n")
    baseline = tools.snapshot_dirty(str(repo))
    assert set(baseline) == {"a.txt", "keep.txt"}
    assert tools.diff_stats(str(repo), baseline) == ([], 0)  # [E] did nothing yet
    (repo / "a.txt").write_text("one\ntwo\nlocal\n" + "x\n" * 500)
    assert tools.diff_stats(str(repo), baseline) == (["a.txt"], 500)
    (repo / "keep.txt").unlink()
    assert tools.diff_stats(str(repo), baseline) == (["a.txt", "keep.txt"], 501)


def test_diff_stats_counts_reverting_a_dirty_file(repo):
    (repo / "a.txt").write_text("one\ntwo\nlocal\n")
    baseline = tools.snapshot_dirty(str(repo))
    subprocess.run(["git", "checkout", "-q", "a.txt"], cwd=repo, check=True)
    assert tools.diff_stats(str(repo), baseline) == (["a.txt"], 1)


def test_diff_stats_is_conservative_when_baseline_blob_is_missing(repo):
    (repo / "a.txt").write_text("one\ntwo\n" + "x\n" * 300)
    assert tools.diff_stats(str(repo), {"a.txt": "0" * 40}) == (["a.txt"], 302)


def test_diff_text_shows_tracked_and_untracked_changes(repo):
    assert tools.diff_text(str(repo)) == "(no changes)\n"
    (repo / "a.txt").write_text("one\n2\n")
    (repo / "new.txt").write_text("x\n")
    (repo / ".aa").mkdir()
    (repo / ".aa" / "context.md").write_text("ignored\n")
    text = tools.diff_text(str(repo))
    assert "-two\n+2" in text and "new files (untracked):\n  new.txt\n" in text and "context.md" not in text
    assert tools.diff_by_file(str(repo), {}) == {"a.txt": 1, "new.txt": 1}
