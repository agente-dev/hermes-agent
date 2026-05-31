"""Email connector plugin — bundled, auto-loaded.

Registers 5 tools (``list_emails``, ``read_email``, ``draft_reply``,
``send_email``, ``mark_email``) into the ``email`` toolset. Each tool is a
thin subprocess wrapper over the bundled ``gws`` (googleworkspace/cli)
binary. See ``email_plugin.py`` for the wire format.

OAuth + token storage are owned by gws; this plugin holds zero credentials.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

from plugins.email import email_plugin
from tools.registry import tool_error, tool_result


COMMON_STRING = {"type": "string"}


LIST_EMAILS_SCHEMA = {
    "name": "list_emails",
    "description": "List recent Gmail messages in a folder (default INBOX). Returns gws JSON envelope.",
    "parameters": {
        "type": "object",
        "properties": {
            "folder": {"type": "string", "description": "Gmail folder / label (default INBOX)"},
            "limit": {"type": "integer", "description": "Max messages to return (default 10)"},
        },
    },
}

READ_EMAIL_SCHEMA = {
    "name": "read_email",
    "description": "Read a single Gmail message by id. Body is returned raw (no bidi normalization) so Hebrew/RTL round-trips faithfully.",
    "parameters": {
        "type": "object",
        "properties": {"message_id": COMMON_STRING},
        "required": ["message_id"],
    },
}

DRAFT_REPLY_SCHEMA = {
    "name": "draft_reply",
    "description": "Create a Gmail draft replying to message_id with body.",
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": COMMON_STRING,
            "body": COMMON_STRING,
        },
        "required": ["message_id", "body"],
    },
}

SEND_EMAIL_SCHEMA = {
    "name": "send_email",
    "description": "Send a new email via Gmail.",
    "parameters": {
        "type": "object",
        "properties": {
            "to": COMMON_STRING,
            "subject": COMMON_STRING,
            "body": COMMON_STRING,
        },
        "required": ["to", "subject", "body"],
    },
}

MARK_EMAIL_SCHEMA = {
    "name": "mark_email",
    "description": "Add or remove a Gmail label on a message (e.g. UNREAD, STARRED, INBOX).",
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": COMMON_STRING,
            "add_label": COMMON_STRING,
            "remove_label": COMMON_STRING,
        },
        "required": ["message_id"],
    },
}


def _check_email_available() -> bool:
    """True iff a gws binary is resolvable. Tools stay registered either way."""
    return bool(os.environ.get("AGENTE_GWS_BIN") or shutil.which("gws"))


def _wrap(fn, *arg_names: str):
    """Wrap a typed email_plugin function into a Hermes tool handler."""

    def _handler(args: dict, **_kw: Any) -> str:
        kwargs = {name: args[name] for name in arg_names if name in args}
        try:
            payload = fn(**kwargs)
        except email_plugin.GwsUnavailableError as exc:
            return tool_error(str(exc))
        except Exception as exc:  # noqa: BLE001 — surface a generic error envelope
            return tool_error(f"{fn.__name__} failed: {type(exc).__name__}: {exc}")
        return tool_result(payload)

    _handler.__name__ = f"_handle_{fn.__name__}"
    return _handler


_TOOLS = (
    ("list_emails", LIST_EMAILS_SCHEMA, _wrap(email_plugin.list_emails, "folder", "limit"), "📥"),
    ("read_email",  READ_EMAIL_SCHEMA,  _wrap(email_plugin.read_email,  "message_id"),    "📨"),
    ("draft_reply", DRAFT_REPLY_SCHEMA, _wrap(email_plugin.draft_reply, "message_id", "body"), "📝"),
    ("send_email",  SEND_EMAIL_SCHEMA,  _wrap(email_plugin.send_email,  "to", "subject", "body"), "📤"),
    ("mark_email",  MARK_EMAIL_SCHEMA,  _wrap(email_plugin.mark_email,  "message_id", "add_label", "remove_label"), "🏷️"),
)


def register(ctx) -> None:
    """Register all email tools. Called once by the plugin loader."""
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="email",
            schema=schema,
            handler=handler,
            check_fn=_check_email_available,
            emoji=emoji,
        )
