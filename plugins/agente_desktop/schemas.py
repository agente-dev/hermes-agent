"""OpenAI function-schema definitions for the 8 Agente desktop tools.

Mirrors `electron/main/hermes-tools/*.ts` in agente-dev/agente-desktop. Keep in
sync: a desktop-side regression test compares this dict against the TS source.
"""

from __future__ import annotations

from typing import Any

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_whatsapp_accounts": {
        "name": "list_whatsapp_accounts",
        "description": (
            "Returns the list of paired WhatsApp accounts (connectors) configured "
            "in the workspace, including account ID, label, phone number, "
            "governance lane, and current GOWA connection status."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "list_recent_messages": {
        "name": "list_recent_messages",
        "description": (
            "Returns recent WhatsApp messages for a given account. Use this to "
            "summarize incoming messages and decide if any need a ticket created."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "string",
                    "description": "The WhatsApp account/connector ID from list_whatsapp_accounts.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of chats to return (default 5, max 10).",
                    "default": 5,
                },
            },
            "required": ["account_id"],
        },
    },
    "create_ticket": {
        "name": "create_ticket",
        "description": (
            'Creates a new ticket in the office board. Status defaults to '
            '"חדש" (pending). Use source="whatsapp" and source_id=<message_id> '
            "when creating from a WhatsApp message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Ticket title (preferably in Hebrew)."},
                "body": {"type": "string", "description": "Optional detailed description."},
                "status": {
                    "type": "string",
                    "description": 'Status: "חדש" (default), "בטיפול", "ממתין לאישור", "done".',
                    "default": "חדש",
                },
                "assignee": {
                    "type": "string",
                    "description": "Optional agent slug or name to assign the ticket to.",
                },
                "source": {
                    "type": "string",
                    "description": 'Source of the ticket, e.g. "whatsapp" or "manual".',
                },
                "source_id": {
                    "type": "string",
                    "description": "Source message ID for back-linking (e.g. WhatsApp message ID).",
                },
            },
            "required": ["title"],
        },
    },
    "move_ticket": {
        "name": "move_ticket",
        "description": (
            'Updates a ticket status on the board. Valid statuses: '
            '"חדש" / "pending", "בטיפול" / "in_progress", "ממתין לאישור" / '
            '"pending_review", "done", "cancelled".'
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "UUID of the ticket to move."},
                "to_status": {
                    "type": "string",
                    "description": "Target status — use Hebrew or English key.",
                },
            },
            "required": ["ticket_id", "to_status"],
        },
    },
    "list_tickets": {
        "name": "list_tickets",
        "description": (
            "Returns tickets from the office board. Filter by status (Hebrew or "
            'English), and limit the count. Use this to answer "מה מחכה לי?" or '
            '"show me open tasks".'
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": (
                        'Optional status filter: "חדש", "בטיפול", "ממתין לאישור", '
                        '"done", or omit for all open.'
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of tickets to return (default 20).",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
    "save_triage_instructions": {
        "name": "save_triage_instructions",
        "description": (
            "Saves operator triage instructions to the workspace settings. "
            "Hermes will read these instructions at the start of every session "
            "to personalize its behavior."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The triage instructions text (Hebrew preferred).",
                },
            },
            "required": ["text"],
        },
    },
    "request_approval": {
        "name": "request_approval",
        "description": (
            "Proposes an action that requires operator approval before "
            "execution. Creates a pending_review ticket visible on the kanban "
            "board. The operator approves or rejects it from the board."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action_description": {
                    "type": "string",
                    "description": (
                        "Human-readable description of the action requiring "
                        "approval (Hebrew preferred)."
                    ),
                },
                "payload": {
                    "type": "object",
                    "description": (
                        "Structured payload describing the action (tool name, "
                        "args, etc.)."
                    ),
                },
            },
            "required": ["action_description"],
        },
    },
    "get_office_context": {
        "name": "get_office_context",
        "description": (
            "Returns the current office persona settings: triage instructions, "
            "office type, hours, and team configuration. Read this once at the "
            "start of a session."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}
