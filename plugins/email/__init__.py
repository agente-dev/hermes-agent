"""Email connector plugin — bundled, auto-loaded.

Five Gmail tools (list/read/draft/send/mark) registered into the ``email``
toolset. Each tool shells the bundled `gws` (Google Workspace CLI) binary
and parses its `--json` stdout — the plugin holds zero auth state. OAuth +
token storage are entirely owned by gws (operator decision 2026-05-28 03:35
IDT, intake hermes-agent-202606-011).

The gws binary path comes from the ``AGENTE_GWS_BIN`` env var set by the
Electron main process / hermes-sidecar.ts when spawning the Hermes sidecar
(paired intake `agente-desktop__intake-bundle-gws-binary-extraresources__1`),
with a `shutil.which("gws")` fallback for developer workstations and CI
(where a fake gws stub binary on PATH provides canned JSON).

Audit event on load: ``hermes.plugin.email.loaded``.
"""

from __future__ import annotations

import logging

from plugins.email.email_plugin import (
    check_email_available,
    handle_draft_reply,
    handle_list_emails,
    handle_mark_email,
    handle_read_email,
    handle_send_email,
)
from plugins.email.schemas import (
    DRAFT_REPLY_SCHEMA,
    LIST_EMAILS_SCHEMA,
    MARK_EMAIL_SCHEMA,
    READ_EMAIL_SCHEMA,
    SEND_EMAIL_SCHEMA,
)

logger = logging.getLogger(__name__)


_TOOLS = (
    ("list_emails",  LIST_EMAILS_SCHEMA,  handle_list_emails,  "📥"),
    ("read_email",   READ_EMAIL_SCHEMA,   handle_read_email,   "📧"),
    ("draft_reply",  DRAFT_REPLY_SCHEMA,  handle_draft_reply,  "✏️"),
    ("send_email",   SEND_EMAIL_SCHEMA,   handle_send_email,   "📤"),
    ("mark_email",   MARK_EMAIL_SCHEMA,   handle_mark_email,   "🏷️"),
)


def register(ctx) -> None:
    """Register all email tools. Called once by the plugin loader."""
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="email",
            schema=schema,
            handler=handler,
            check_fn=check_email_available,
            emoji=emoji,
        )
    logger.info("hermes.plugin.email.loaded tools=%d", len(_TOOLS))
