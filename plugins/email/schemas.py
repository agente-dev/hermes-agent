"""OpenAI function-schema definitions for the 5 email tools.

Mirrors the plugin.yaml tool list. Each tool maps to a gws gmail subcommand
documented at https://github.com/googleworkspace/cli.
"""

from __future__ import annotations

from typing import Any

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_emails": {
        "name": "list_emails",
        "description": (
            "List recent emails from a Gmail folder. Returns message IDs, "
            "subjects, senders, and timestamps. Use --folder to pick a label "
            "(INBOX, SENT, DRAFT, etc.) and --since to filter by date."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "Gmail folder/label to list from (default INBOX).",
                    "default": "INBOX",
                },
                "since": {
                    "type": "string",
                    "description": "ISO-format date filter (e.g. 2026-05-20).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of emails to return (default 10).",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
    "read_email": {
        "name": "read_email",
        "description": (
            "Read the full content of a single email by its Gmail message ID. "
            "Returns subject, sender, recipients, body text, and attachments list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Gmail message ID from list_emails output.",
                },
            },
            "required": ["message_id"],
        },
    },
    "draft_reply": {
        "name": "draft_reply",
        "description": (
            "Create a Gmail draft reply to an existing email thread. "
            "The draft is saved server-side and visible in the operator's "
            "web Gmail client. Does not send."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Gmail message ID of the thread to reply to.",
                },
                "body": {
                    "type": "string",
                    "description": "Reply body text (plain text).",
                },
            },
            "required": ["message_id", "body"],
        },
    },
    "send_email": {
        "name": "send_email",
        "description": (
            "Send an email through Gmail. Requires explicit operator approval "
            "before calling — this is a destructive write action."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address.",
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line.",
                },
                "body": {
                    "type": "string",
                    "description": "Email body text (plain text).",
                },
                "cc": {
                    "type": "string",
                    "description": "Optional CC recipients (comma-separated).",
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
    "mark_email": {
        "name": "mark_email",
        "description": (
            "Modify Gmail labels on an email — mark as read, unread, archived, "
            "starred, or move to a folder."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Gmail message ID to modify.",
                },
                "add_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Labels to add (e.g. STARRED, INBOX).",
                },
                "remove_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Labels to remove (e.g. UNREAD, INBOX).",
                },
            },
            "required": ["message_id"],
        },
    },
}
