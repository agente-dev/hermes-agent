"""Tool argument schemas for the email plugin.

Each schema follows the OpenAI/Anthropic function-calling JSON Schema shape
used throughout hermes-agent (see `plugins/spotify/tools.py` for the
canonical example). Each schema carries a `label_he` + `category` field per
the tool-manifest contract from hermes-agent-202606-001 so AuditScreen's
tool dictionary picks up Hebrew labels automatically.
"""

from __future__ import annotations


LIST_EMAILS_SCHEMA = {
    "name": "list_emails",
    "description": "List recent Gmail messages. Shells `gws gmail list`. Operator must have connected Gmail via the Connectors UI.",
    "label_he": "רשימת מיילים",
    "category": "email",
    "parameters": {
        "type": "object",
        "properties": {
            "folder": {"type": "string", "description": "Gmail label / folder. Defaults to INBOX."},
            "query": {"type": "string", "description": "Optional Gmail search query (e.g. 'from:boss@x.com is:unread')."},
            "after": {"type": "string", "description": "Optional ISO-8601 or YYYY/MM/DD lower bound."},
            "before": {"type": "string", "description": "Optional ISO-8601 or YYYY/MM/DD upper bound."},
            "limit": {"type": "integer", "description": "Max messages to return (default 10).", "minimum": 1, "maximum": 100},
        },
        "required": [],
    },
}


READ_EMAIL_SCHEMA = {
    "name": "read_email",
    "description": "Read a single Gmail message by id. Shells `gws gmail read --id <id> --raw --json`. Hebrew bodies are preserved verbatim.",
    "label_he": "קריאת מייל",
    "category": "email",
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "Gmail message id."},
        },
        "required": ["message_id"],
    },
}


DRAFT_REPLY_SCHEMA = {
    "name": "draft_reply",
    "description": "Create a Gmail draft replying to a given message. Shells `gws gmail draft --reply-to <id> --body <body>`. Returns the draft id; the operator confirms send in their web Gmail.",
    "label_he": "טיוטת תשובה",
    "category": "email",
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "Gmail message id to reply to."},
            "body": {"type": "string", "description": "Reply body. UTF-8; Hebrew preserved verbatim."},
        },
        "required": ["message_id", "body"],
    },
}


SEND_EMAIL_SCHEMA = {
    "name": "send_email",
    "description": "Send a new Gmail message. Shells `gws gmail send --to <to> --subject <s> --body <body>`. Existing-class agent capability; gated by the agente-desktop tool-approval surface.",
    "label_he": "שליחת מייל",
    "category": "email",
    "parameters": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient email address (or comma-separated list)."},
            "subject": {"type": "string", "description": "Message subject."},
            "body": {"type": "string", "description": "Message body. UTF-8; Hebrew preserved verbatim."},
        },
        "required": ["to", "subject", "body"],
    },
}


MARK_EMAIL_SCHEMA = {
    "name": "mark_email",
    "description": "Apply or remove a Gmail label on a message (read/unread/starred/archive/trash). Shells `gws gmail modify`.",
    "label_he": "סימון מייל",
    "category": "email",
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "Gmail message id."},
            "flag": {
                "type": "string",
                "description": "One of: read, unread, starred, unstarred, archive, trash.",
                "enum": ["read", "unread", "starred", "unstarred", "archive", "trash"],
            },
        },
        "required": ["message_id", "flag"],
    },
}
