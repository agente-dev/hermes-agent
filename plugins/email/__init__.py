"""email plugin -- Gmail connector via the gws (googleworkspace/cli) binary.

Registers 5 tools under the ``email`` toolset. Each tool shells
``gws gmail <subcommand>`` and returns parsed JSON.

Activation contract: the Hermes sidecar sets ``AGENTE_GWS_BIN`` to the
bundled gws binary path. When the env var is absent the plugin falls back
to ``which gws``. Tools are always registered but availability depends on
the gws binary being present and authenticated (``gws auth status``).
"""

from __future__ import annotations

import logging

from plugins.email.email_plugin import (
    draft_reply,
    list_emails,
    mark_email,
    read_email,
    send_email,
)
from plugins.email.schemas import TOOL_SCHEMAS

logger = logging.getLogger(__name__)

TOOLSET = "email"

_TOOL_EMOJIS: dict[str, str] = {
    "list_emails": "📋",
    "read_email": "📖",
    "draft_reply": "✍️",
    "send_email": "📤",
    "mark_email": "🏷️",
}

_HANDLERS = {
    "list_emails": list_emails,
    "read_email": read_email,
    "draft_reply": draft_reply,
    "send_email": send_email,
    "mark_email": mark_email,
}


def _check_available() -> bool:
    return True


def register(ctx) -> None:
    for tool_name, schema in TOOL_SCHEMAS.items():
        ctx.register_tool(
            name=tool_name,
            toolset=TOOLSET,
            schema=schema,
            handler=_HANDLERS[tool_name],
            check_fn=_check_available,
            description=schema.get("description", ""),
            emoji=_TOOL_EMOJIS.get(tool_name, "📧"),
        )
    logger.info(
        "email plugin registered %d tools under toolset %r",
        len(TOOL_SCHEMAS),
        TOOLSET,
    )
