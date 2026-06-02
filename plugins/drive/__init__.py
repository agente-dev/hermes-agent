"""Drive connector plugin — bundled, auto-loaded.

Registers two tools (``drive_search``, ``drive_get``) into the ``drive``
toolset. Each is a thin subprocess wrapper over the bundled ``gws``
(googleworkspace/cli) binary. OAuth + token storage are owned entirely by
gws; this plugin holds zero credentials.

Mirrors the canonical shape of ``plugins/calendar/`` (typed handlers
returning tool-envelope strings; ``check_*_available`` preflight; no
``_wrap`` indirection) and the closed PR #32 layout, updated to match the
``label_he`` + ``category`` schema contract from ``hermes-agent-202606-001``.
"""

from __future__ import annotations

import logging

from plugins.drive.drive_plugin import (
    check_drive_available,
    handle_drive_get,
    handle_drive_search,
)
from plugins.drive.schemas import (
    DRIVE_GET_SCHEMA,
    DRIVE_SEARCH_SCHEMA,
)

logger = logging.getLogger(__name__)


_TOOLS = (
    ("drive_search", DRIVE_SEARCH_SCHEMA, handle_drive_search, "\U0001f50e"),
    ("drive_get",    DRIVE_GET_SCHEMA,    handle_drive_get,    "\U0001f4c4"),
)


def _model_schema(schema: dict) -> dict:
    """Return the model-facing function schema without registry-only metadata."""
    clean = dict(schema)
    clean.pop("label_he", None)
    clean.pop("category", None)
    return clean


def register(ctx) -> None:
    """Register all drive tools. Called once by the plugin loader."""
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="drive",
            schema=_model_schema(schema),
            handler=handler,
            check_fn=check_drive_available,
            emoji=emoji,
        )
    logger.info("hermes.plugin.drive.loaded tools=%d", len(_TOOLS))
