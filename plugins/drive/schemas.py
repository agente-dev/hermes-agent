"""Tool argument schemas for the drive plugin.

Each schema carries `label_he` + `category` per the tool-manifest contract
from hermes-agent-202606-001 so AuditScreen's tool dictionary picks up
Hebrew labels automatically.
"""

from __future__ import annotations


LIST_FILES_SCHEMA = {
    "name": "list_files",
    "description": "List files in Google Drive. Shells `gws drive list`. Operator must have connected Drive via the Connectors UI.",
    "label_he": "רשימת קבצים",
    "category": "drive",
    "parameters": {
        "type": "object",
        "properties": {
            "folder_id": {"type": "string", "description": "Optional parent folder id."},
            "query": {"type": "string", "description": "Optional Drive query (`q` parameter, e.g. \"name contains 'contract'\")."},
            "limit": {"type": "integer", "description": "Max files to return (default 25).", "minimum": 1, "maximum": 200},
        },
        "required": [],
    },
}


GET_FILE_SCHEMA = {
    "name": "get_file",
    "description": "Download a Drive file by id. Shells `gws drive get`. Hebrew filenames preserved verbatim.",
    "label_he": "הורדת קובץ",
    "category": "drive",
    "parameters": {
        "type": "object",
        "properties": {
            "file_id": {"type": "string", "description": "Drive file id."},
            "dest_path": {"type": "string", "description": "Optional local path to save the file to (gws picks a default if omitted)."},
        },
        "required": ["file_id"],
    },
}


SEARCH_FILES_SCHEMA = {
    "name": "search_files",
    "description": "Search Google Drive. Shells `gws drive search --q <query> --json`. Returns matching files with id, name, mimeType.",
    "label_he": "חיפוש בדרייב",
    "category": "drive",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (free-text or Drive query syntax)."},
            "mime_type": {"type": "string", "description": "Optional MIME-type filter (e.g. application/pdf)."},
            "limit": {"type": "integer", "description": "Max files to return (default 25).", "minimum": 1, "maximum": 200},
        },
        "required": ["query"],
    },
}
