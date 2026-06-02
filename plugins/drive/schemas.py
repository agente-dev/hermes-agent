"""Tool argument schemas for the drive plugin.

Each schema carries ``label_he`` + ``category`` per the tool-manifest
contract from ``hermes-agent-202606-001`` so AuditScreen's tool dictionary
picks up Hebrew labels automatically. The schema-level metadata is
forward-compatible: the registry ignores unknown top-level keys today and
will surface them once the ``label_he`` registry kwarg lands.
"""

from __future__ import annotations

from typing import Any


DRIVE_SEARCH_SCHEMA: dict[str, Any] = {
    "name": "drive_search",
    "description": (
        "Search Google Drive. Shells `gws drive files list --params ...`. "
        "Optional filters: mime_type (e.g. application/pdf), modified_after "
        "(RFC3339 timestamp), limit (default 25)."
    ),
    "label_he": "חיפוש בדרייב",
    "category": "google",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Drive search query — matches file name / fullText. "
                    "Hebrew/RTL supported via UTF-8."
                ),
            },
            "mime_type": {
                "type": "string",
                "description": (
                    "Optional MIME-type filter (e.g. application/pdf, "
                    "application/vnd.google-apps.document)."
                ),
            },
            "modified_after": {
                "type": "string",
                "description": (
                    "Optional RFC3339 timestamp (e.g. 2026-05-01T00:00:00Z) — "
                    "only files modified after this instant are returned."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max files to return (default 25, max 200).",
                "minimum": 1,
                "maximum": 200,
            },
        },
        "required": ["query"],
    },
}


DRIVE_GET_SCHEMA: dict[str, Any] = {
    "name": "drive_get",
    "description": (
        "Fetch a single Drive file by id. Shells `gws drive files get --params ...`. "
        "Returns file metadata plus either an inline base64 payload "
        "(content_b64) or a short-lived download_url, depending on file size."
    ),
    "label_he": "הורדת קובץ מדרייב",
    "category": "google",
    "parameters": {
        "type": "object",
        "properties": {
            "file_id": {
                "type": "string",
                "description": "Google Drive file id (from drive_search results).",
            },
        },
        "required": ["file_id"],
    },
}


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "drive_search": DRIVE_SEARCH_SCHEMA,
    "drive_get": DRIVE_GET_SCHEMA,
}
