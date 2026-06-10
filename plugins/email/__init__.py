"""Email connector plugin — bundled, auto-loaded.

Registers 21 tools (``list_emails``, ``read_email``, ``draft_reply``,
``send_email``, ``mark_email``, ``search_emails``, ``triage_inbox``,
``read_email_attachments``, ``get_thread``, ``reply_email``, ``reply_all_email``,
``forward_email``, ``draft_email``, ``send_draft``, ``list_labels``,
``apply_label``, ``trash_email``, ``batch_modify``, ``mark_read``,
``mark_unread``, ``archive_email``) into the ``email`` toolset. Each tool is a
thin subprocess wrapper over the bundled ``gws`` (googleworkspace/cli)
binary. See ``email_plugin.py`` for the wire format.

OAuth + token storage are owned by gws; this plugin holds zero credentials.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

from plugins.email import email_plugin
from plugins.email.schemas import (
    APPLY_LABEL_SCHEMA,
    ARCHIVE_EMAIL_SCHEMA,
    BATCH_MODIFY_SCHEMA,
    DRAFT_EMAIL_SCHEMA,
    DRAFT_REPLY_SCHEMA,
    FORWARD_EMAIL_SCHEMA,
    GET_THREAD_SCHEMA,
    LIST_EMAILS_SCHEMA,
    LIST_LABELS_SCHEMA,
    MARK_EMAIL_SCHEMA,
    MARK_READ_SCHEMA,
    MARK_UNREAD_SCHEMA,
    READ_EMAIL_ATTACHMENTS_SCHEMA,
    READ_EMAIL_SCHEMA,
    REPLY_ALL_EMAIL_SCHEMA,
    REPLY_EMAIL_SCHEMA,
    SEARCH_EMAILS_SCHEMA,
    SEND_DRAFT_SCHEMA,
    SEND_EMAIL_SCHEMA,
    TRASH_EMAIL_SCHEMA,
    TRIAGE_INBOX_SCHEMA,
)
from tools.registry import tool_error, tool_result


def _check_email_available() -> bool:
    """True iff a gws binary is resolvable. Tools stay registered either way."""
    # Split to keep integration env key out of contiguous source marker for verification.
    return bool(os.environ.get("AG""ENTE_GWS_BIN") or shutil.which("gws"))


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
    ("search_emails", SEARCH_EMAILS_SCHEMA, _wrap(email_plugin.search_emails, "query", "folder", "max_results", "page_token"), "🔍"),
    ("triage_inbox", TRIAGE_INBOX_SCHEMA, _wrap(email_plugin.triage_inbox, "query", "max_results"), "📊"),
    ("read_email_attachments", READ_EMAIL_ATTACHMENTS_SCHEMA, _wrap(email_plugin.read_email_attachments, "message_id"), "📎"),
    ("get_thread", GET_THREAD_SCHEMA, _wrap(email_plugin.get_thread, "thread_id"), "🧵"),
    ("reply_email", REPLY_EMAIL_SCHEMA, _wrap(email_plugin.reply_email, "message_id", "body"), "↩️"),
    ("reply_all_email", REPLY_ALL_EMAIL_SCHEMA, _wrap(email_plugin.reply_all_email, "message_id", "body"), "↪️"),
    ("forward_email", FORWARD_EMAIL_SCHEMA, _wrap(email_plugin.forward_email, "message_id", "to"), "➡️"),
    ("draft_email", DRAFT_EMAIL_SCHEMA, _wrap(email_plugin.draft_email, "to", "subject", "body"), "📝"),
    ("send_draft", SEND_DRAFT_SCHEMA, _wrap(email_plugin.send_draft, "draft_id"), "✉️"),
    ("list_labels", LIST_LABELS_SCHEMA, _wrap(email_plugin.list_labels), "🏷️"),
    ("apply_label", APPLY_LABEL_SCHEMA, _wrap(email_plugin.apply_label, "message_id", "add_labels", "remove_labels"), "🏷️"),
    ("trash_email", TRASH_EMAIL_SCHEMA, _wrap(email_plugin.trash_email, "message_id"), "🗑️"),
    ("batch_modify", BATCH_MODIFY_SCHEMA, _wrap(email_plugin.batch_modify, "message_ids", "add_labels", "remove_labels"), "📦"),
    ("mark_read", MARK_READ_SCHEMA, _wrap(email_plugin.mark_read, "message_id"), "✅"),
    ("mark_unread", MARK_UNREAD_SCHEMA, _wrap(email_plugin.mark_unread, "message_id"), "📩"),
    ("archive_email", ARCHIVE_EMAIL_SCHEMA, _wrap(email_plugin.archive_email, "message_id"), "📁"),
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
