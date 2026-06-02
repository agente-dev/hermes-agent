"""Email plugin tool handlers — thin subprocess wrappers around `gws gmail`.

Each handler:
  1. Validates args against the matching schema in `schemas.py`.
  2. Builds the gws argv (always appends `--json`; `--raw` where bodies matter
     so Hebrew/RTL bytes are not normalized).
  3. Calls `gws_runner.run_gws_json(argv)`.
  4. Returns a JSON-serialized result via `tools.registry.tool_result` /
     `tool_error`.

All bodies + subjects are returned to the agent (they are the tool output),
but they are NOT logged in non-debug agent.log entries (see redaction policy
on intake hermes-agent-202606-011).
"""

from __future__ import annotations

import logging
from typing import Any

from plugins.email.gws_runner import (
    GwsCallError,
    GwsNotAvailableError,
    gws_available,
    run_gws_json,
)
from tools.registry import tool_error, tool_result

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# check_fn — gates whether tools are dispatchable
# --------------------------------------------------------------------------

def check_email_available() -> bool:
    """Check-fn: tools register unconditionally so they appear in
    ``hermes tools`` / `GET /api/tools`, but dispatch fails fast when gws is
    not present.
    """
    return gws_available()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _coerce_limit(raw: Any, *, default: int = 10, minimum: int = 1, maximum: int = 100) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _gws_tool_error(exc: Exception) -> str:
    if isinstance(exc, GwsNotAvailableError):
        return tool_error(
            "gws binary not available — operator must connect Gmail via the Connectors UI first.",
            kind="gws_not_available",
        )
    if isinstance(exc, GwsCallError):
        return tool_error(
            str(exc),
            kind="gws_call_failed",
            exit_code=exc.exit_code,
        )
    return tool_error(f"email tool failed: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# Tool handlers
# --------------------------------------------------------------------------

def handle_list_emails(args: dict, **_kw) -> str:
    folder = (args.get("folder") or "INBOX").strip() or "INBOX"
    limit = _coerce_limit(args.get("limit"), default=10)
    argv = ["gmail", "list", "--folder", folder, "--limit", str(limit), "--json"]
    if args.get("query"):
        argv += ["--query", str(args["query"])]
    if args.get("after"):
        argv += ["--after", str(args["after"])]
    if args.get("before"):
        argv += ["--before", str(args["before"])]
    try:
        data = run_gws_json(argv)
    except Exception as exc:
        return _gws_tool_error(exc)
    messages = data if isinstance(data, list) else (data or {}).get("messages", [])
    return tool_result({"messages": messages, "folder": folder, "limit": limit})


def handle_read_email(args: dict, **_kw) -> str:
    message_id = (args.get("message_id") or "").strip()
    if not message_id:
        return tool_error("message_id is required")
    argv = ["gmail", "read", "--id", message_id, "--raw", "--json"]
    try:
        data = run_gws_json(argv)
    except Exception as exc:
        return _gws_tool_error(exc)
    return tool_result(data if isinstance(data, dict) else {"message": data})


def handle_draft_reply(args: dict, **_kw) -> str:
    message_id = (args.get("message_id") or "").strip()
    body = args.get("body")
    if not message_id:
        return tool_error("message_id is required")
    if not isinstance(body, str) or not body:
        return tool_error("body is required (non-empty string)")
    argv = ["gmail", "draft", "--reply-to", message_id, "--body", body, "--json"]
    try:
        data = run_gws_json(argv)
    except Exception as exc:
        return _gws_tool_error(exc)
    return tool_result(data if isinstance(data, dict) else {"draft": data})


def handle_send_email(args: dict, **_kw) -> str:
    to = (args.get("to") or "").strip()
    subject = args.get("subject")
    body = args.get("body")
    if not to:
        return tool_error("to is required")
    if not isinstance(subject, str):
        return tool_error("subject is required (string)")
    if not isinstance(body, str) or not body:
        return tool_error("body is required (non-empty string)")
    argv = [
        "gmail", "send",
        "--to", to,
        "--subject", subject,
        "--body", body,
        "--json",
    ]
    try:
        data = run_gws_json(argv)
    except Exception as exc:
        return _gws_tool_error(exc)
    return tool_result(data if isinstance(data, dict) else {"sent": data})


def handle_mark_email(args: dict, **_kw) -> str:
    message_id = (args.get("message_id") or "").strip()
    flag = (args.get("flag") or "").strip().lower()
    if not message_id:
        return tool_error("message_id is required")
    if flag not in {"read", "unread", "starred", "unstarred", "archive", "trash"}:
        return tool_error(f"unknown flag '{flag}'")
    argv = ["gmail", "modify", "--id", message_id, "--flag", flag, "--json"]
    try:
        data = run_gws_json(argv)
    except Exception as exc:
        return _gws_tool_error(exc)
    return tool_result(data if isinstance(data, dict) else {"modified": data})
