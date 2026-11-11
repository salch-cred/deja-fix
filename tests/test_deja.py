"""Deja Fix as Hermes drives it: edits happen for real on disk, tests are scripted exit codes."""

import json
import os

import pytest
import deja_fix


class FakeCtx:
    def __init__(self, settings=None):
        self.settings, self.hooks, self.commands = settings or {}, {}, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, cb, **_):
        self.hooks.setdefault(name, []).append(cb)

    def register_command(self, name, handler, **_):
        self.commands[name] = handler

    def fire(self, name, **kw):
        return [cb(**kw) for cb in self.hooks.get(name, [])]


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    fake = FakeCtx()
    deja_fix.register(fake)
    return fake


A = "def total(xs):\n    return sum(xs) + 1\n"
B = "def total(xs):\n    return sum(xs[1:])\n"
C = "def total(xs):\n    return sum(xs)\n"


def write(ctx, content, path="calc.py", session="S1"):
    """pre_tool_call, then the write happens, then post_tool_call: the order Hermes uses."""
    verdict = ctx.fire("pre_tool_call", tool_name="write_file", args={"path": path, "content": content},
                       session_id=session)[0]
    if verdict:
        return verdict
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    ctx.fire("post_tool_call", tool_name="write_file", args={"path": path, "content": content},
             result=json.dumps({"bytes_written": 1}), status="ok", session_id=session)
    return None


def patch(ctx, old, new, path="calc.py", session="S1"):
    args = {"path": path, "old_string": old, "new_string": new}
    verdict = ctx.fire("pre_tool_call", tool_name="patch", args=args, session_id=session)[0]
    if verdict:
        return verdict
    with open(path, encoding="utf-8") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new, 1))
    ctx.fire("post_tool_call", tool_name="patch", args=args, result=json.dumps({"success": True}),
             status="ok", session_id=session)
    return None


def run(ctx, code, command="pytest -q", session="S1"):
    # Hermes reports a non-zero exit with status "error", as it does for real.
    ctx.fire("post_tool_call", tool_name="terminal", args={"command": command},
             result=json.dumps({"output": "", "exit_code": code}), status="ok" if code == 0 else "error",
             session_id=session)


def test_registers(ctx):
    assert set(ctx.hooks) == {"pre_tool_call", "post_tool_call", "pre_llm_call"} and "deja" in ctx.commands


def test_going_back_to_a_failed_version_is_stopped_once(ctx):
    assert write(ctx, A) is None
    run(ctx, 1)
    assert write(ctx, B) is None
    run(ctx, 1)
    block = write(ctx, A)
    assert block["action"] == "block"
    assert "puts calc.py back exactly as it was at" in block["message"] and "`pytest -q` exited 1" in block["message"]
    assert write(ctx, A) is None                       # the agent insisted: let through
    assert "Let through after the agent insisted: 1" in ctx.commands["deja"]("", session_id="S1")


def test_patch_that_recreates_a_failed_version(ctx):
    write(ctx, A)
    run(ctx, 1)
    patch(ctx, "sum(xs) + 1", "sum(xs[1:])")
    run(ctx, 1)
    block = patch(ctx, "sum(xs[1:])", "sum(xs) + 1")
    assert block and block["action"] == "block"


def test_whitespace_does_not_hide_a_repeat(ctx):
    write(ctx, A)
    run(ctx, 2)
    write(ctx, B)
    assert write(ctx, A.replace("\n", "   \n") + "\n\n")["action"] == "block"


def test_passing_version_is_never_stopped(ctx):
    write(ctx, C)
    run(ctx, 0)
    write(ctx, B)
    run(ctx, 1)
    assert write(ctx, C) is None


def test_a_version_that_failed_then_passed_is_fine(ctx):
    write(ctx, A)
    run(ctx, 1)
    write(ctx, B)
    write(ctx, A)                                      # blocked once...
    write(ctx, A)                                      # ...then let through
    run(ctx, 0)                                        # and it passed this time (another file was the cause)
    write(ctx, B)
    assert write(ctx, A) is None


def test_untested_versions_are_not_stopped(ctx):
    write(ctx, A)
    write(ctx, B)
    assert write(ctx, A) is None


def test_user_saying_still_broken_marks_the_last_turn(ctx):
    write(ctx, A)
    ctx.fire("pre_llm_call", session_id="S1", user_message="still the same error")
    write(ctx, B)
    block = write(ctx, A)
    assert block and 'the user said "still the same error"' in block["message"]


def test_happy_user_marks_nothing(ctx):
    write(ctx, A)
    ctx.fire("pre_llm_call", session_id="S1", user_message="thanks, now add logging")
    write(ctx, B)
    assert write(ctx, A) is None


def test_commands_that_could_not_run_or_are_not_checks(ctx):
    write(ctx, A)
    run(ctx, 127)
    run(ctx, 1, command="ls missing-dir")
    write(ctx, B)
    assert write(ctx, A) is None


def test_only_files_changed_since_the_last_check_are_judged(ctx):
    write(ctx, A, path="a.py")
    run(ctx, 0)
    write(ctx, B, path="b.py")
    run(ctx, 1)                                        # b.py failed; a.py was not touched since it passed
    write(ctx, C, path="a.py")
    assert write(ctx, A, path="a.py") is None


def test_sessions_are_separate(ctx):
    write(ctx, A, session="X")
    run(ctx, 1, session="X")
    write(ctx, B, session="Y")
    assert write(ctx, A, session="Y") is None


def test_warn_and_off_modes():
    for mode in ("warn", "off"):
        fake = FakeCtx({"mode": mode})
        deja_fix.register(fake)
        write(fake, A)
        run(fake, 1)
        write(fake, B)
        assert write(fake, A) is None


def test_relative_paths_resolve_against_terminal_cwd(ctx, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(work))
    write(ctx, A, path=str(work / "calc.py"))
    run(ctx, 1)
    write(ctx, B, path=str(work / "calc.py"))
    block = ctx.fire("pre_tool_call", tool_name="write_file", args={"path": "calc.py", "content": A}, session_id="S1")[0]
    assert block and block["action"] == "block"


def test_status(ctx):
    assert "hasn't seen" in ctx.commands["deja"]("", session_id="S1")
    write(ctx, A)
    run(ctx, 1)
    write(ctx, B)
    write(ctx, A)
    out = ctx.commands["deja"]("", session_id="S1")
    assert "1 known to fail" in out and "Edits stopped: 1" in out and "✗ calc.py" in out


def test_hooks_never_raise_on_junk(ctx):
    assert ctx.fire("pre_tool_call", tool_name="patch", args=None) == [None]
    assert ctx.fire("post_tool_call", tool_name="terminal", args={"command": "pytest"}, result="nope") == [None]
    assert ctx.fire("pre_llm_call", user_message=None) == [None]


def test_failed_edits_are_not_recorded(ctx):
    write(ctx, A)
    ctx.fire("post_tool_call", tool_name="write_file", args={"path": "calc.py", "content": B},
             result=json.dumps({"error": "denied"}), status="error", session_id="S1")
    run(ctx, 1)
    assert "1 version(s) tracked" in ctx.commands["deja"]("", session_id="S1")
