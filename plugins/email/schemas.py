"""Tool argument schemas for the bundled email plugin."""

COMMON_STRING = {"type": "string"}

LIST_EMAILS_SCHEMA = {
    "name": "list_emails",
    "description": "List recent Gmail messages in a folder (default INBOX). Returns gws JSON envelope.",
    "parameters": {
        "type": "object",
        "properties": {
            "folder": {"type": "string", "description": "Gmail folder / label (default INBOX)"},
            "limit": {"type": "integer", "description": "Max messages to return (default 10)"},
        },
    },
}

READ_EMAIL_SCHEMA = {
    "name": "read_email",
    "description": "Read a single Gmail message by id. Body is returned raw (no bidi normalization) so Hebrew/RTL round-trips faithfully.",
    "parameters": {
        "type": "object",
        "properties": {"message_id": COMMON_STRING},
        "required": ["message_id"],
    },
}

DRAFT_REPLY_SCHEMA = {
    "name": "draft_reply",
    "description": "Create a Gmail draft replying to message_id with body.",
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": COMMON_STRING,
            "body": COMMON_STRING,
        },
        "required": ["message_id", "body"],
    },
}

SEND_EMAIL_SCHEMA = {
    "name": "send_email",
    "description": "Send a new email via Gmail.",
    "parameters": {
        "type": "object",
        "properties": {
            "to": COMMON_STRING,
            "subject": COMMON_STRING,
            "body": COMMON_STRING,
        },
        "required": ["to", "subject", "body"],
    },
}

MARK_EMAIL_SCHEMA = {
    "name": "mark_email",
    "description": "Add or remove a Gmail label on a message (e.g. UNREAD, STARRED, INBOX).",
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": COMMON_STRING,
            "add_label": COMMON_STRING,
            "remove_label": COMMON_STRING,
        },
        "required": ["message_id"],
    },
}

SEARCH_EMAILS_SCHEMA = {
    "name": "search_emails",
    "description": "Search Gmail messages with a free-text Gmail query (q param). Supports pagination via pageToken. Returns gws JSON envelope with messages and nextPageToken.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Gmail search query (same syntax as Gmail search bar). Supports operators like from:, to:, subject:, has:attachment, before:, after:, is:unread, etc."},
            "folder": {"type": "string", "description": "Gmail label/folder to search in (default INBOX)"},
            "max_results": {"type": "integer", "description": "Max results (default 20, max 500)"},
            "page_token": {"type": "string", "description": "Page token from a previous search for pagination"},
        },
        "required": ["query"],
    },
}

TRIAGE_INBOX_SCHEMA = {
    "name": "triage_inbox",
    "description": "Quick inbox triage via gws +triage helper. Returns a summary view of recent messages.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Optional Gmail search query to filter triage results"},
            "max_results": {"type": "integer", "description": "Max messages to triage (default 50)"},
        },
    },
}

READ_EMAIL_ATTACHMENTS_SCHEMA = {
    "name": "read_email_attachments",
    "description": "Read a Gmail message and extract attachment metadata (filename, mimeType, attachmentId). Returns headers and a list of attachment descriptors.",
    "parameters": {
        "type": "object",
        "properties": {"message_id": COMMON_STRING},
        "required": ["message_id"],
    },
}

GET_THREAD_SCHEMA = {
    "name": "get_thread",
    "description": "Get a full Gmail thread with all messages by thread ID.",
    "parameters": {
        "type": "object",
        "properties": {"thread_id": COMMON_STRING},
        "required": ["thread_id"],
    },
}

REPLY_EMAIL_SCHEMA = {
    "name": "reply_email",
    "description": "Reply to a Gmail message (sender only). Uses gws +reply helper which handles threading (In-Reply-To/References) automatically.",
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": COMMON_STRING,
            "body": COMMON_STRING,
        },
        "required": ["message_id", "body"],
    },
}

REPLY_ALL_EMAIL_SCHEMA = {
    "name": "reply_all_email",
    "description": "Reply-all to a Gmail message. Uses gws +reply-all helper.",
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": COMMON_STRING,
            "body": COMMON_STRING,
        },
        "required": ["message_id", "body"],
    },
}

FORWARD_EMAIL_SCHEMA = {
    "name": "forward_email",
    "description": "Forward a Gmail message to a recipient. Uses gws +forward helper.",
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": COMMON_STRING,
            "to": COMMON_STRING,
        },
        "required": ["message_id", "to"],
    },
}

DRAFT_EMAIL_SCHEMA = {
    "name": "draft_email",
    "description": "Create a Gmail draft without sending.",
    "parameters": {
        "type": "object",
        "properties": {
            "to": COMMON_STRING,
            "subject": COMMON_STRING,
            "body": COMMON_STRING,
        },
        "required": ["to", "subject", "body"],
    },
}

SEND_DRAFT_SCHEMA = {
    "name": "send_draft",
    "description": "Send an existing Gmail draft by its draft ID.",
    "parameters": {
        "type": "object",
        "properties": {"draft_id": COMMON_STRING},
        "required": ["draft_id"],
    },
}

LIST_LABELS_SCHEMA = {
    "name": "list_labels",
    "description": "List all Gmail labels for the authenticated user.",
    "parameters": {"type": "object", "properties": {}},
}

APPLY_LABEL_SCHEMA = {
    "name": "apply_label",
    "description": "Add and/or remove labels on a Gmail message. Use to organize email, mark as read/unread, archive, etc.",
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": COMMON_STRING,
            "add_labels": {"type": "array", "items": COMMON_STRING, "description": "Label IDs to add (e.g. ['STARRED', 'IMPORTANT'])"},
            "remove_labels": {"type": "array", "items": COMMON_STRING, "description": "Label IDs to remove (e.g. ['UNREAD', 'INBOX'])"},
        },
        "required": ["message_id"],
    },
}

TRASH_EMAIL_SCHEMA = {
    "name": "trash_email",
    "description": "Move a Gmail message to trash.",
    "parameters": {
        "type": "object",
        "properties": {"message_id": COMMON_STRING},
        "required": ["message_id"],
    },
}

BATCH_MODIFY_SCHEMA = {
    "name": "batch_modify",
    "description": "Batch-modify labels on multiple Gmail messages at once.",
    "parameters": {
        "type": "object",
        "properties": {
            "message_ids": {"type": "array", "items": COMMON_STRING, "description": "List of message IDs to modify"},
            "add_labels": {"type": "array", "items": COMMON_STRING, "description": "Label IDs to add to all messages"},
            "remove_labels": {"type": "array", "items": COMMON_STRING, "description": "Label IDs to remove from all messages"},
        },
        "required": ["message_ids"],
    },
}

MARK_READ_SCHEMA = {
    "name": "mark_read",
    "description": "Mark a Gmail message as read (removes UNREAD label).",
    "parameters": {
        "type": "object",
        "properties": {"message_id": COMMON_STRING},
        "required": ["message_id"],
    },
}

MARK_UNREAD_SCHEMA = {
    "name": "mark_unread",
    "description": "Mark a Gmail message as unread (adds UNREAD label).",
    "parameters": {
        "type": "object",
        "properties": {"message_id": COMMON_STRING},
        "required": ["message_id"],
    },
}

ARCHIVE_EMAIL_SCHEMA = {
    "name": "archive_email",
    "description": "Archive a Gmail message (removes INBOX label).",
    "parameters": {
        "type": "object",
        "properties": {"message_id": COMMON_STRING},
        "required": ["message_id"],
    },
}
