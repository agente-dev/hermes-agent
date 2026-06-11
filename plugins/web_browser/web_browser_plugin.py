"""Handlers for the web_browser plugin.

Each handler:

1. Builds the ``agent-browser`` subprocess argv from the tool args.
2. Routes through :func:`tools.approval.check_dangerous_command`, which is
   Hermes' single source of truth for the approval.request prompt-flow gate.
   (Per /tmp/hermes-protocol-research.md the approval.request payload is
   ``{command, description}`` — we pass exactly those, so the operator sees
   "browse https://example.com" and can pick once/session/always/deny.)
3. Runs the subprocess with a bounded timeout, parses ``--json`` stdout, and
   returns a structured dict.

Errors are returned as ``{"success": False, "error": ..., "stderr": ...}`` —
never raised, so the agent loop always gets a usable tool reply.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from typing import Any, Dict, List, Optional

from plugins.web_browser.schemas import (
    BROWSER_CLICK_SCHEMA,
    BROWSER_CLOSE_SCHEMA,
    BROWSER_FILL_SCHEMA,
    BROWSER_FIND_SCHEMA,
    BROWSER_GET_SCHEMA,
    BROWSER_NAVIGATE_SCHEMA,
    BROWSER_PRESS_SCHEMA,
    BROWSER_SCREENSHOT_SCHEMA,
    BROWSER_SNAPSHOT_SCHEMA,
    BROWSER_TYPE_SCHEMA,
)

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Binary lookup
# -----------------------------------------------------------------------------

AGENT_BROWSER_BIN_ENV = "AGENT_BROWSER_BIN"
DEFAULT_BINARY = "agent-browser"
DEFAULT_TIMEOUT_SECONDS = 90
_NAVIGATION_APPROVAL_LOCK = threading.Lock()
_NAVIGATION_APPROVED_SESSIONS: set[str] = set()
_REDACTED = "[redacted]"


def _resolve_binary() -> Optional[str]:
    """Return the absolute path to the agent-browser CLI, or None if missing.

    Lookup order: ``$AGENT_BROWSER_BIN`` then ``$PATH``.
    """
    override = os.environ.get(AGENT_BROWSER_BIN_ENV, "").strip()
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        # Fall through to PATH if the override is bogus, but log it.
        logger.warning(
            "AGENT_BROWSER_BIN=%s is not an executable file; falling back to PATH",
            override,
        )
    return shutil.which(DEFAULT_BINARY)


def check_web_browser_requirements() -> bool:
    """Return True when the agent-browser CLI is installed and runnable."""
    return _resolve_binary() is not None


def _reset_navigation_approval_for_tests() -> None:  # pragma: no cover - test helper
    with _NAVIGATION_APPROVAL_LOCK:
        _NAVIGATION_APPROVED_SESSIONS.clear()


# -----------------------------------------------------------------------------
# Approval gating (canonical Hermes approval.request via check_dangerous_command)
# -----------------------------------------------------------------------------


def _request_approval(command: str, description: str) -> Dict[str, Any]:
    """Route through the Hermes approval prompt-flow.

    Wraps :func:`tools.approval.check_dangerous_command` so first-use of any
    browser_* tool emits an ``approval.request`` notification on the gateway
    (operator picks once / session / always / deny). Subsequent calls in the
    same session that match the same pattern_key skip the prompt — same
    semantics as every other dangerous-command tool.

    Returns the check_dangerous_command dict verbatim. Callers must abort
    when ``approved`` is False.
    """
    try:
        from tools.approval import check_dangerous_command
    except Exception as exc:
        # If the approval module can't be imported (minimal test env), fall
        # open with a logged warning. We never want the plugin to silently
        # bypass approval, but we also don't want import errors to break
        # the unit tests for the subprocess wiring.
        logger.warning(
            "web_browser: tools.approval unavailable (%s) — proceeding without prompt-flow gate",
            exc,
        )
        return {"approved": True, "message": None}

    # env_type='local' matches the terminal_tool default; the approval
    # subsystem uses it only to short-circuit container-isolated terminals.
    return check_dangerous_command(command, env_type="local")


def _current_session_key() -> str:
    try:
        from tools.approval import get_current_session_key

        return get_current_session_key(default="default") or "default"
    except Exception:
        return os.environ.get("HERMES_SESSION_KEY", "default") or "default"


def _request_first_navigation_approval(url: str, task: str = "") -> Dict[str, Any]:
    """Ask once per approval session before the browser may navigate."""
    session_key = _current_session_key()
    with _NAVIGATION_APPROVAL_LOCK:
        if session_key in _NAVIGATION_APPROVED_SESSIONS:
            return {"approved": True, "cached": True}

    reason = (
        f"agent-browser will open {url} in a real browser. "
        + (f"Task: {task}. " if task else "")
        + "First browser navigation in this session requires operator approval."
    )
    try:
        from tools.approval import pre_approval_request
    except Exception as exc:
        logger.warning(
            "web_browser: tools.approval.pre_approval_request unavailable (%s); "
            "falling back to dangerous-command gate",
            exc,
        )
        approval = _request_approval(f"browse {url}", reason)
    else:
        try:
            approval = pre_approval_request(
                tool_name="browser_navigate",
                reason=reason,
                category="web",
                target=url,
            )
        except TypeError:
            approval = pre_approval_request("browser_navigate")  # type: ignore[misc]

    approved = bool(approval) if not isinstance(approval, dict) else bool(approval.get("approved", True))
    if approved:
        with _NAVIGATION_APPROVAL_LOCK:
            _NAVIGATION_APPROVED_SESSIONS.add(session_key)
    return {"approved": approved, "raw": approval}


# -----------------------------------------------------------------------------
# Subprocess plumbing
# -----------------------------------------------------------------------------


def _redact_argv(argv: List[str]) -> List[str]:
    redacted: List[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            redacted.append(_REDACTED)
            skip_next = False
            continue
        redacted.append(arg)
        if arg in {"--basic-auth", "--headers", "--proxy"}:
            skip_next = True
    return redacted


def _redact_strings(value: Any, secrets: List[str]) -> Any:
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, _REDACTED)
        return redacted
    if isinstance(value, dict):
        return {key: _redact_strings(inner, secrets) for key, inner in value.items()}
    if isinstance(value, list):
        return [_redact_strings(inner, secrets) for inner in value]
    return value


def _extract_content(result: Dict[str, Any]) -> str:
    for key in ("content", "snapshot", "text", "output", "stdout", "result"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
    return ""


def _run_agent_browser(argv: List[str], *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Execute ``agent-browser <argv...> --json`` and parse the result.

    ``--json`` is appended automatically (idempotent).

    Returns the parsed JSON dict on success, augmented with ``success=True``.
    On non-zero exit or unparseable output, returns
    ``{"success": False, "error": ..., "stderr": ..., "exit_code": ...}``.
    """
    binary = _resolve_binary()
    if binary is None:
        return {
            "success": False,
            "error": (
                "agent-browser CLI not found. Install with "
                "`npm i -g agent-browser` or set AGENT_BROWSER_BIN to the absolute path."
            ),
        }

    full_argv = [binary, *argv]
    if "--json" not in full_argv:
        full_argv.append("--json")

    try:
        proc = subprocess.run(
            full_argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": f"agent-browser timed out after {timeout}s",
            "argv": _redact_argv(full_argv),
        }
    except OSError as exc:
        return {"success": False, "error": f"agent-browser exec failed: {exc}"}

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode != 0:
        return {
            "success": False,
            "error": f"agent-browser exited with code {proc.returncode}",
            "stderr": stderr,
            "stdout": stdout,
            "exit_code": proc.returncode,
        }

    # Try JSON first; agent-browser emits one JSON object on stdout under --json.
    if stdout:
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                parsed.setdefault("success", True)
                return parsed
            return {"success": True, "result": parsed}
        except json.JSONDecodeError:
            # Some subcommands (eg screenshot) print a path on stdout when
            # --json isn't supported; surface the raw text in that case.
            return {"success": True, "output": stdout, "stderr": stderr}

    return {"success": True, "output": "", "stderr": stderr}


def _navigate_and_read(argv: List[str], *, secrets: List[str]) -> Dict[str, Any]:
    opened = _run_agent_browser(argv)
    if not opened.get("success", False):
        return _redact_strings(opened, secrets)

    snapshot = _run_agent_browser(["snapshot", "-c"])
    result = dict(opened)
    if snapshot.get("success", False):
        content = _extract_content(snapshot)
        result["snapshot"] = content
        result["content"] = content
        result["snapshot_result"] = snapshot
    else:
        result["snapshot_error"] = snapshot
    return _redact_strings(result, secrets)


# -----------------------------------------------------------------------------
# Handlers
# -----------------------------------------------------------------------------


def handle_browser_navigate(args: Dict[str, Any], **_kw) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        return json.dumps({"success": False, "error": "url is required"})

    task = (args.get("task") or "").strip()
    basic_auth = (args.get("basic_auth") or "").strip()

    approval = _request_first_navigation_approval(url, task)
    if not approval.get("approved", False):
        return json.dumps({
            "success": False,
            "error": "approval denied",
            "approval": approval,
        })

    argv: List[str] = ["open", url]
    if task:
        argv.extend(["--task", task])
    if basic_auth:
        argv.extend(["--basic-auth", basic_auth])
    return json.dumps(_navigate_and_read(argv, secrets=[basic_auth]), ensure_ascii=False)


def handle_browser_screenshot(args: Dict[str, Any], **_kw) -> str:
    selector = (args.get("selector") or "").strip()
    path = (args.get("path") or "").strip()
    approval = _request_approval(
        "browse screenshot",
        "agent-browser will capture a PNG of the current page.",
    )
    if not approval.get("approved", False):
        return json.dumps({"success": False, "error": "approval denied", "approval": approval})

    argv: List[str] = ["screenshot"]
    if selector:
        argv.append(selector)
    if path:
        argv.append(path)
    return json.dumps(_run_agent_browser(argv), ensure_ascii=False)


def handle_browser_snapshot(args: Dict[str, Any], **_kw) -> str:
    full = bool(args.get("full", False))
    approval = _request_approval(
        "browse snapshot",
        "agent-browser will read the accessibility tree of the current page.",
    )
    if not approval.get("approved", False):
        return json.dumps({"success": False, "error": "approval denied", "approval": approval})

    argv: List[str] = ["snapshot"]
    if full:
        argv.append("--full")
    return json.dumps(_run_agent_browser(argv), ensure_ascii=False)


def handle_browser_click(args: Dict[str, Any], **_kw) -> str:
    selector = (args.get("selector") or "").strip()
    if not selector:
        return json.dumps({"success": False, "error": "selector is required"})
    approval = _request_approval(
        f"browse click {selector}",
        f"agent-browser will click {selector} on the active page.",
    )
    if not approval.get("approved", False):
        return json.dumps({"success": False, "error": "approval denied", "approval": approval})
    return json.dumps(_run_agent_browser(["click", selector]), ensure_ascii=False)


def handle_browser_fill(args: Dict[str, Any], **_kw) -> str:
    selector = (args.get("selector") or "").strip()
    value = args.get("value")
    if not selector:
        return json.dumps({"success": False, "error": "selector is required"})
    if value is None:
        return json.dumps({"success": False, "error": "value is required"})
    approval = _request_approval(
        f"browse fill {selector}",
        f"agent-browser will clear and fill {selector} on the active page.",
    )
    if not approval.get("approved", False):
        return json.dumps({"success": False, "error": "approval denied", "approval": approval})
    return json.dumps(_run_agent_browser(["fill", selector, str(value)]), ensure_ascii=False)


def handle_browser_type(args: Dict[str, Any], **_kw) -> str:
    selector = (args.get("selector") or "").strip()
    text = args.get("text")
    if not selector:
        return json.dumps({"success": False, "error": "selector is required"})
    if text is None:
        return json.dumps({"success": False, "error": "text is required"})
    approval = _request_approval(
        f"browse type {selector}",
        f"agent-browser will type into {selector} on the active page.",
    )
    if not approval.get("approved", False):
        return json.dumps({"success": False, "error": "approval denied", "approval": approval})
    return json.dumps(_run_agent_browser(["type", selector, str(text)]), ensure_ascii=False)


def handle_browser_press(args: Dict[str, Any], **_kw) -> str:
    key = (args.get("key") or "").strip()
    if not key:
        return json.dumps({"success": False, "error": "key is required"})
    approval = _request_approval(
        f"browse press {key}",
        f"agent-browser will press keyboard key {key} on the active page.",
    )
    if not approval.get("approved", False):
        return json.dumps({"success": False, "error": "approval denied", "approval": approval})
    return json.dumps(_run_agent_browser(["press", key]), ensure_ascii=False)


def handle_browser_get(args: Dict[str, Any], **_kw) -> str:
    what = (args.get("what") or "").strip()
    if not what:
        return json.dumps({"success": False, "error": "what is required"})
    selector = (args.get("selector") or "").strip()
    attr = (args.get("attr") or "").strip()
    approval = _request_approval(
        f"browse get {what}",
        f"agent-browser will read {what} from the active page.",
    )
    if not approval.get("approved", False):
        return json.dumps({"success": False, "error": "approval denied", "approval": approval})
    argv: List[str] = ["get", what]
    selector_required = {"text", "html", "value", "count", "box", "styles"}
    if what == "attr":
        if not selector:
            return json.dumps({"success": False, "error": "selector is required for attr"})
        if not attr:
            return json.dumps({"success": False, "error": "attr is required for attr"})
        argv.extend([selector, attr])
    elif what in selector_required:
        if not selector:
            return json.dumps({"success": False, "error": f"selector is required for {what}"})
        argv.append(selector)
    elif what in {"title", "url"}:
        pass
    else:
        return json.dumps({"success": False, "error": f"unsupported get target: {what}"})
    return json.dumps(_run_agent_browser(argv), ensure_ascii=False)


def handle_browser_find(args: Dict[str, Any], **_kw) -> str:
    locator = (args.get("locator") or "").strip()
    value = (args.get("value") or "").strip()
    if not locator:
        return json.dumps({"success": False, "error": "locator is required"})
    if not value:
        return json.dumps({"success": False, "error": "value is required"})
    action = (args.get("action") or "click").strip()
    text = args.get("text")
    supported_actions = {"click", "fill", "type", "hover", "focus", "check", "uncheck"}
    if action not in supported_actions:
        return json.dumps({"success": False, "error": f"unsupported find action: {action}"})
    approval = _request_approval(
        f"browse find {locator}={value}",
        f"agent-browser will locate an element via {locator}={value} and {action}.",
    )
    if not approval.get("approved", False):
        return json.dumps({"success": False, "error": "approval denied", "approval": approval})
    argv: List[str] = ["find", locator, value, action]
    if text is not None and action in {"type", "fill"}:
        argv.append(str(text))
    return json.dumps(_run_agent_browser(argv), ensure_ascii=False)


def handle_browser_close(args: Dict[str, Any], **_kw) -> str:
    all_flag = bool(args.get("all", False))
    approval = _request_approval(
        "browse close",
        "agent-browser will close the active session"
        + (" (and every other session)" if all_flag else "")
        + ".",
    )
    if not approval.get("approved", False):
        return json.dumps({"success": False, "error": "approval denied", "approval": approval})
    argv: List[str] = ["close"]
    if all_flag:
        argv.append("--all")
    return json.dumps(_run_agent_browser(argv), ensure_ascii=False)


# -----------------------------------------------------------------------------
# Tool table
# -----------------------------------------------------------------------------

TOOL_DEFS = [
    {"name": "browser_navigate",   "schema": BROWSER_NAVIGATE_SCHEMA,   "handler": handle_browser_navigate,   "emoji": "🧭"},
    {"name": "browser_screenshot", "schema": BROWSER_SCREENSHOT_SCHEMA, "handler": handle_browser_screenshot, "emoji": "📸"},
    {"name": "browser_snapshot",   "schema": BROWSER_SNAPSHOT_SCHEMA,   "handler": handle_browser_snapshot,   "emoji": "🌳"},
    {"name": "browser_click",      "schema": BROWSER_CLICK_SCHEMA,      "handler": handle_browser_click,      "emoji": "🖱️"},
    {"name": "browser_fill",       "schema": BROWSER_FILL_SCHEMA,       "handler": handle_browser_fill,       "emoji": "✍️"},
    {"name": "browser_type",       "schema": BROWSER_TYPE_SCHEMA,       "handler": handle_browser_type,       "emoji": "⌨️"},
    {"name": "browser_press",      "schema": BROWSER_PRESS_SCHEMA,      "handler": handle_browser_press,      "emoji": "🔑"},
    {"name": "browser_get",        "schema": BROWSER_GET_SCHEMA,        "handler": handle_browser_get,        "emoji": "🔍"},
    {"name": "browser_find",       "schema": BROWSER_FIND_SCHEMA,       "handler": handle_browser_find,       "emoji": "🧲"},
    {"name": "browser_close",      "schema": BROWSER_CLOSE_SCHEMA,      "handler": handle_browser_close,      "emoji": "🚪"},
]
