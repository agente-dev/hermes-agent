"""Drive plugin tool handlers — thin subprocess wrappers around `gws drive`.

Reuses `plugins.email.gws_runner` (same binary, same env, same JSON contract).
Three tools: list_files / get_file / search_files. Token + account state
live in gws's keyring; the plugin holds none.
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


def check_drive_available() -> bool:
    return gws_available()


def _coerce_limit(raw: Any, *, default: int = 25, minimum: int = 1, maximum: int = 200) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _gws_tool_error(exc: Exception) -> str:
    if isinstance(exc, GwsNotAvailableError):
        return tool_error(
            "gws binary not available — operator must connect Drive via the Connectors UI first.",
            kind="gws_not_available",
        )
    if isinstance(exc, GwsCallError):
        return tool_error(
            str(exc),
            kind="gws_call_failed",
            exit_code=exc.exit_code,
        )
    return tool_error(f"drive tool failed: {type(exc).__name__}: {exc}")


def handle_list_files(args: dict, **_kw) -> str:
    limit = _coerce_limit(args.get("limit"), default=25)
    argv = ["drive", "list", "--limit", str(limit), "--json"]
    if args.get("folder_id"):
        argv += ["--folder", str(args["folder_id"])]
    if args.get("query"):
        argv += ["--query", str(args["query"])]
    try:
        data = run_gws_json(argv)
    except Exception as exc:
        return _gws_tool_error(exc)
    files = data if isinstance(data, list) else (data or {}).get("files", [])
    return tool_result({"files": files, "limit": limit})


def handle_get_file(args: dict, **_kw) -> str:
    file_id = (args.get("file_id") or "").strip()
    if not file_id:
        return tool_error("file_id is required")
    argv = ["drive", "get", "--id", file_id, "--json"]
    dest = args.get("dest_path")
    if isinstance(dest, str) and dest.strip():
        argv += ["--out", dest.strip()]
    try:
        data = run_gws_json(argv)
    except Exception as exc:
        return _gws_tool_error(exc)
    return tool_result(data if isinstance(data, dict) else {"file": data})


def handle_search_files(args: dict, **_kw) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return tool_error("query is required")
    limit = _coerce_limit(args.get("limit"), default=25)
    argv = ["drive", "search", "--q", query, "--limit", str(limit), "--json"]
    if args.get("mime_type"):
        argv += ["--mime", str(args["mime_type"])]
    try:
        data = run_gws_json(argv)
    except Exception as exc:
        return _gws_tool_error(exc)
    files = data if isinstance(data, list) else (data or {}).get("files", [])
    return tool_result({"files": files, "query": query, "limit": limit})
