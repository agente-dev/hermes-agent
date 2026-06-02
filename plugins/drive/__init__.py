"""Drive connector plugin — bundled, auto-loaded.

Three Drive tools (list_files / get_file / search_files) registered into
the ``drive`` toolset. Shares the gws subprocess marshaller with the email
plugin (`plugins.email.gws_runner`). Same OAuth path as email — gws owns
the entire token lifecycle.

Audit event on load: ``hermes.plugin.drive.loaded``.
"""

from __future__ import annotations

import logging

from plugins.drive.drive_plugin import (
    check_drive_available,
    handle_get_file,
    handle_list_files,
    handle_search_files,
)
from plugins.drive.schemas import (
    GET_FILE_SCHEMA,
    LIST_FILES_SCHEMA,
    SEARCH_FILES_SCHEMA,
)

logger = logging.getLogger(__name__)


_TOOLS = (
    ("list_files",   LIST_FILES_SCHEMA,   handle_list_files,   "📂"),
    ("get_file",     GET_FILE_SCHEMA,     handle_get_file,     "📄"),
    ("search_files", SEARCH_FILES_SCHEMA, handle_search_files, "🔎"),
)


def register(ctx) -> None:
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="drive",
            schema=schema,
            handler=handler,
            check_fn=check_drive_available,
            emoji=emoji,
        )
    logger.info("hermes.plugin.drive.loaded tools=%d", len(_TOOLS))
