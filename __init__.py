"""deja-fix: stops your agent from putting back code that already failed.

Agents loop: try A, tests fail, try B, fail, try A again. Deja Fix remembers every version of every
file the agent writes in a session, and what happened next: the tests or build it ran, or you
saying "still broken". When the agent is about to write a file back to a version that already
failed, the edit is stopped before it runs:

  "This puts upload.py back exactly as it was at 14:02, and `pytest -q` exited 1 with it.
   Try a different approach."

If the agent tries the same thing again after being told, it is let through (it may have a reason),
and that is recorded.

  /deja      files tracked, versions that failed, edits stopped
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext

_CTX: Any = None
_LOCK = threading.Lock()
EDIT_TOOLS = {"write_file", "patch"}

CHECK_RUN = re.compile(r"""
    \b(?:pytest|py\.test|unittest|tox|nox|jest|vitest|mocha|rspec|phpunit|ctest|tsc)\b
  | \b(?:npm|yarn|pnpm|bun)\ (?:run\ )?(?:test|build|typecheck|check|lint)\b
  | \b(?:cargo|go)\ (?:test|build|check|vet)\b
  | \bmake(?:\ (?:test|check|build))?\s*$
  | \b(?:mvn|gradlew?|\./gradlew)\ (?:test|verify|build)\b
  | \bdotnet\ (?:test|build)\b
  | \bpython\d?(?:\.\d+)?\ -m\ (?:pytest|unittest)\b
""", re.I | re.X)

USER_SAYS_FAILED = re.compile(r"""
    \bstill\ (?:not|broken|fail\w*|doesn'?t|does\ not|the\ same|getting|seeing|throw\w*|crash\w*|error\w*|wrong)\b
  | \b(?:doesn'?t|does\ not|didn'?t|did\ not|isn'?t|is\ not)\ (?:\w+\ )?(?:work|fix|help|load|run|build|compile|pass)\w*\b
  | \bnot\ (?:working|fixed)\b | \bsame\ (?:error|issue|problem|bug)\b | \b(?:it'?s|still|now)\ broken\b
  | \btraceback\ \(most\ recent\ call\ last\) | \bworse\b
""", re.I | re.X)


@dataclass
class Session:
    current: dict[str, str] = field(default_factory=dict)                 # path -> hash of what is there now
    seen: dict[tuple[str, str], float] = field(default_factory=dict)      # (path, hash) -> first written at
    failed: dict[tuple[str, str], dict] = field(default_factory=dict)     # (path, hash) -> {"at", "why"}
    passed: set[tuple[str, str]] = field(default_factory=set)
    dirty: set[str] = field(default_factory=set)                          # changed since the last check run
    last_turn: set[str] = field(default_factory=set)                      # changed in the agent's last turn
    warned: set[tuple[str, str]] = field(default_factory=set)
    stopped: list[dict] = field(default_factory=list)
    let_through: int = 0


_SESSIONS: dict[str, Session] = {}


def _mode() -> str:
    try:
        value = _CTX.get_config("mode", "block") if _CTX is not None else "block"
    except Exception:
        value = "block"
    return value if value in ("block", "warn", "off") else "block"


def _session(sid: str) -> Session:
    s = _SESSIONS.get(sid)
    if s is None:
        s = _SESSIONS[sid] = Session()
        while len(_SESSIONS) > 200:
            _SESSIONS.pop(next(iter(_SESSIONS)))
    return s


def fingerprint(text: str) -> str:
    """Same code, same fingerprint: trailing spaces and blank lines at the end don't count."""
    lines = [line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return hashlib.sha256("\n".join(lines).encode("utf-8", "replace")).hexdigest()[:20]


def where(raw: str) -> str:
    raw = os.path.expanduser(raw or "")
    if os.path.isabs(raw):
        return os.path.normcase(os.path.abspath(raw))
    for root in (os.environ.get("TERMINAL_CWD"), os.getcwd()):
        if root and os.path.exists(os.path.join(root, raw)):
            return os.path.normcase(os.path.abspath(os.path.join(root, raw)))
    return os.path.normcase(os.path.abspath(os.path.join(os.environ.get("TERMINAL_CWD") or os.getcwd(), raw)))


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def predict(tool: str, args: dict, path: str) -> Optional[str]:
    """What the file will contain if this edit runs."""
    if tool == "write_file":
        content = args.get("content")
        return content if isinstance(content, str) else None
    old, new = args.get("old_string"), args.get("new_string")
    now = _read(path)
    if now is None or not isinstance(old, str) or not isinstance(new, str) or not old or old not in now:
        return None
    return now.replace(old, new) if args.get("replace_all") else now.replace(old, new, 1)


def _text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return " ".join(p.get("text", "") for p in message if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _when(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _show(path: str) -> str:
    for root in (os.environ.get("TERMINAL_CWD"), os.getcwd()):
        if root:
            try:
                rel = os.path.relpath(path, root)
                if not rel.startswith(".."):
                    return rel
            except ValueError:
                pass
    return os.path.basename(path)


# hooks ------------------------------------------------------------------

def on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any) -> Optional[dict]:
    if tool_name not in EDIT_TOOLS or not isinstance(args, dict) or _mode() == "off":
        return None
    raw = args.get("path")
    if not isinstance(raw, str) or not raw:
        return None
    path = where(raw)
    after = predict(tool_name, args, path)
    if after is None:
        return None
    key = (path, fingerprint(after))
    with _LOCK:
        s = _session(session_id or "")
        failure = s.failed.get(key)
        if failure is None or key in s.passed or s.current.get(path) == key[1]:
            return None
        written = s.seen.get(key, failure["at"])
        record = {"path": path, "at": time.time(), "version_from": written, "why": failure["why"]}
        if key in s.warned or _mode() == "warn":
            s.let_through += 1
            record["let_through"] = True
            s.stopped.append(record)
            return None
        s.warned.add(key)
        s.stopped.append(record)
    return {"action": "block", "message": (
        f"Deja Fix: this puts {_show(path)} back exactly as it was at {_when(written)}, and that version "
        f"already failed ({failure['why']}). Try a different approach. If you're sure this version is "
        "right and something else was the cause, say so and try again; it will go through.")}


def on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, status: str = "",
                      session_id: str = "", **_: Any) -> None:
    if not isinstance(args, dict):
        return None
    try:
        parsed = json.loads(result) if isinstance(result, str) else (result if isinstance(result, dict) else {})
    except Exception:
        parsed = {}
    parsed = parsed if isinstance(parsed, dict) else {}
    if tool_name in EDIT_TOOLS:
        # A failed edit changed nothing. (A failing test run also arrives with status "error",
        # which is exactly the result we want to record below, so only edits are skipped.)
        if status == "error" or parsed.get("error") or parsed.get("success") is False or not isinstance(args.get("path"), str):
            return None
        path = where(args["path"])
        now = _read(path)
        if now is None:
            return None
        h = fingerprint(now)
        with _LOCK:
            s = _session(session_id or "")
            s.current[path] = h
            s.seen.setdefault((path, h), time.time())
            s.dirty.add(path)
            s.last_turn.add(path)
        return None
    if tool_name == "terminal":
        command, code = args.get("command"), parsed.get("exit_code")
        if not isinstance(command, str) or not CHECK_RUN.search(command) or not isinstance(code, int):
            return None
        if code < 0 or code in (126, 127):
            return None
        short = " ".join(os.path.basename(w) if ("/" in w or "\\" in w) else w for w in command.split())[:60]
        with _LOCK:
            s = _session(session_id or "")
            for path in s.dirty:
                key = (path, s.current[path])
                if code == 0:
                    s.passed.add(key)
                    s.failed.pop(key, None)
                elif key not in s.passed:
                    s.failed[key] = {"at": time.time(), "why": f"`{short}` exited {code}"}
            s.dirty.clear()
    return None


def on_pre_llm_call(session_id: str = "", user_message: Any = None, **_: Any) -> None:
    text = " ".join(_text(user_message).split())
    with _LOCK:
        s = _SESSIONS.get(session_id or "")
        if s is None:
            return None
        if text and USER_SAYS_FAILED.search(text):
            for path in s.last_turn:
                key = (path, s.current[path])
                if key not in s.passed:
                    s.failed[key] = {"at": time.time(), "why": f'the user said "{text[:60]}"'}
        s.last_turn = set()
    return None


# command ----------------------------------------------------------------

def deja_command(raw_args: str = "", session_id: str = "", **_: Any) -> str:
    with _LOCK:
        s = _SESSIONS.get(session_id or "")
        if s is None or not s.current:
            return "Deja Fix hasn't seen the agent edit a file in this session yet."
        lines = [f"Deja Fix ({_mode()}): {len(s.current)} file(s), {len(s.seen)} version(s) tracked, "
                 f"{len(s.failed)} known to fail."]
        for (path, _h), f in list(s.failed.items())[-5:]:
            lines.append(f"  ✗ {_show(path)} as of {_when(f['at'])}: {f['why']}")
        blocked = [r for r in s.stopped if not r.get("let_through")]
        lines.append(f"Edits stopped: {len(blocked)}. Let through after the agent insisted: {s.let_through}.")
        for r in blocked[-3:]:
            lines.append(f"  ↺ {_when(r['at'])} {_show(r['path'])} back to the {_when(r['version_from'])} version ({r['why']})")
        return "\n".join(lines)


def register(ctx: "PluginContext") -> None:
    global _CTX
    _CTX = ctx
    with _LOCK:
        _SESSIONS.clear()
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_command("deja", deja_command, description="Code versions that already failed, and edits stopped")
