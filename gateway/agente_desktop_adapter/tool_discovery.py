"""
tool_discovery.py

Stable home for all Agente Desktop tool discovery, IPC bridge proxy,
and the /api/tools + /api/tools/{name} route handlers (plus toolset
registration for the "agente-desktop" tools that proxy back to Electron).

Per hermes-upstream-compatibility-plan: this is the ONLY place with the
IPC proxy/dispatch logic (AGENTE_TOOL_PORT/SECRET, POST /dispatch/{tool},
result envelopes, error handling). The bridge is preserved exactly.

The register(app, adapter=None) wires both:
- global tool registrations (so "agente-desktop" toolset works)
- the Desktop-facing HTTP routes on the aiohttp app.

All Agente-specific knowledge is isolated here; core api_server.py
contains only the call to register_agente_desktop_routes.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --- preserved exact IPC bridge (from plugins/agente_desktop/__init__.py) ---

TOOLSET = "agente-desktop"
_REQUEST_TIMEOUT_SECONDS = 25

_TOOL_EMOJIS: dict[str, str] = {
    "list_whatsapp_accounts": "📱",
    "list_recent_messages": "💬",
    "create_ticket": "🎫",
    "move_ticket": "🔄",
    "list_tickets": "📋",
    "save_triage_instructions": "📝",
    "request_approval": "✋",
    "get_office_context": "🏢",
    # desktop-202605-161 additions
    "upsert_client": "👤",
    "query_client": "🔍",
    "link_ticket_to_client": "🔗",
    "link_document_to_client": "📎",
    "update_office_context": "🏢",
    "list_tools": "🛠️",
    "list_workflows": "📂",
    "inspect_workflow": "🔬",
    "start_workflow_run": "▶️",
    "get_run_status": "📊",
    "resume_paused_run": "⏯️",
    "evaluate_triage_rules": "🧭",
    "data_read": "📖",
    "data_write": "✏️",
    "assign_ticket": "👥",
    "resolve_or_upsert_client": "🪪",
    "read_file": "📄",
    "read_document": "📑",
    "write_file": "💾",
    "list_directory": "📂",
    "scan_folder": "🔍",
    "list_web_connectors": "🌐",
    "browse_connector": "🧭",
    "list_routines": "⏱️",
    "get_routine": "🔎",
    "run_routine": "▶️",
    "pause_routine": "⏸️",
    "resume_routine": "⏯️",
    "delete_routine": "🗑️",
    "suggest_client_tip": "💡",
    "download_whatsapp_media": "📥",
    "connect_google": "🔐",
    "connect_anthropic": "🔐",
    "connect_openai": "🔐",
    "check_connector_status": "✅",
    "save_executable_workflow": "🧩",
    "list_agent_profiles": "👥",
    "create_agent_profile": "✳️",
}

# Schemas moved here from plugins/agente_desktop/schemas.py.
# Mirrors electron/main/hermes-tools/*.ts in the desktop side. Keep this list
# in lockstep with agente-desktop/electron/main/hermes-tools/python-schema-parity.test.ts
# whenever Desktop adds or changes an IPC tool schema.
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
    # --- Agente Desktop list-recent-messages schema description patch (desktop-202606-515) ---
    "list_recent_messages": {
        "name": "list_recent_messages",
        "description": (
            "Returns recent WhatsApp messages for a given account. Use this "
            "to summarize incoming messages and decide if any need a ticket "
            "created. The result is paginated (default 25, max 100); when "
            "has_more is true, call again with offset advanced by limit. "
            "Each chat includes chat_jid; pass that value to "
            "download_whatsapp_media when downloading an attachment from a "
            "listed message."
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
                    "description": "Maximum number of chats to return in this page (default 25, max 100).",
                    "default": 25,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset — skip this many chats before returning the page (default 0).",
                    "default": 0,
                },
            },
            "required": ["account_id"],
        },
    },
    # --- end Agente Desktop list-recent-messages schema description patch (desktop-202606-515) ---

    # --- Agente Desktop create-ticket schema canonical patch (desktop-20260604-track-b) ---
    "create_ticket": {
        "name": "create_ticket",
        "description": (
            "Creates a new ticket in the office board. Status defaults to "
            "\"חדש\" (pending). Use source=\"whatsapp\" and "
            "source_id=<message_id> when creating from a WhatsApp message. "
            "When a specific client/person is named, FIRST resolve them via "
            "resolve_or_upsert_client and pass the returned slug as client_id "
            "so the ticket appears on that client's profile."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Ticket title (preferably in Hebrew).",
                },
                "body": {
                    "type": "string",
                    "description": "Optional detailed description.",
                },
                "status": {
                    "type": "string",
                    "description": "Status: \"חדש\" (default), \"בטיפול\", \"ממתין לאישור\", \"done\".",
                    "default": "חדש",
                },
                "assignee": {
                    "type": "string",
                    "description": "Optional agent slug or name to assign the ticket to.",
                },
                "source": {
                    "type": "string",
                    "description": "Source of the ticket, e.g. \"whatsapp\" or \"manual\".",
                },
                "source_id": {
                    "type": "string",
                    "description": "Source message ID for back-linking (e.g. WhatsApp message ID).",
                },
                "client_id": {
                    "type": "string",
                    "description": (
                        "Canonical client slug (YAML id) to link this ticket "
                        "to. Get from resolve_or_upsert_client / query_client."
                    ),
                },
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    },
    # --- end Agente Desktop create-ticket schema canonical patch (desktop-20260604-track-b) ---

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
            "(DEPRECATED — use save_workflow + create_routine for new rules.) "
            "Saves free-text operator triage instructions to the workspace "
            "settings. Hermes will read these instructions at the start of "
            "every session to personalize its behavior. Retained for "
            "backwards compatibility with legacy office profiles."
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
    # ── 10 additional tools added in desktop-202605-161 fix ──────────────────
    "upsert_client": {
        "name": "upsert_client",
        "description": (
            "Idempotently create or update a client by identity (phone, JID, "
            "email, or name). Does not override existing verified identity "
            "records. Calling twice with the same identity returns the same "
            "client_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "identity": {
                    "type": "object",
                    "description": "Identity to look up or create by.",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["phone", "jid", "email", "name"],
                            "description": "Identity kind.",
                        },
                        "value": {
                            "type": "string",
                            "description": "Identity value.",
                        },
                    },
                    "required": ["kind", "value"],
                },
                "patch": {
                    "type": "object",
                    "description": "Optional fields to set on create or update.",
                    "properties": {
                        "display_name": {
                            "type": "string",
                            "description": "Display name for the client.",
                        },
                        "hebrew_name": {
                            "type": "string",
                            "description": "Hebrew display name.",
                        },
                        "aliases": {
                            "type": "array",
                            "description": "Additional name aliases.",
                            "items": {"type": "string"},
                        },
                        "notes": {
                            "type": "string",
                            "description": "Optional notes about this client.",
                        },
                    },
                },
            },
            "required": ["identity"],
        },
    },
    "query_client": {
        "name": "query_client",
        "description": (
            "Query clients by JID, phone number, email, or display name "
            "(Hebrew partial match). Returns client records with open ticket "
            "counts and last contact timestamps."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "jid": {
                    "type": "string",
                    "description": 'WhatsApp JID (e.g. "972500000000@s.whatsapp.net")',
                },
                "phone": {
                    "type": "string",
                    "description": 'Phone number to look up (e.g. "972500000000")',
                },
                "email": {
                    "type": "string",
                    "description": "Email address to look up",
                },
                "name": {
                    "type": "string",
                    "description": "Display name or alias (Hebrew partial match supported)",
                },
            },
            "required": [],
        },
    },
    "link_ticket_to_client": {
        "name": "link_ticket_to_client",
        "description": (
            "Links an existing ticket to a client record. Calling again with "
            "the same ticket_id updates the link (idempotent). Fails with a "
            "structured error if the client or ticket does not exist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "string",
                    "description": "UUID of the ticket to link.",
                },
                "client_id": {
                    "type": "string",
                    "description": "UUID of the client to link the ticket to.",
                },
            },
            "required": ["ticket_id", "client_id"],
        },
    },
    "link_document_to_client": {
        "name": "link_document_to_client",
        "description": (
            "Links a document source to a client record. Fails with a "
            "structured error if the client or document does not exist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "document_source_id": {
                    "type": "string",
                    "description": "UUID of the document source to link.",
                },
                "client_id": {
                    "type": "string",
                    "description": "UUID of the client to link the document to.",
                },
            },
            "required": ["document_source_id", "client_id"],
        },
    },
    "update_office_context": {
        "name": "update_office_context",
        "description": (
            "Writes structured Office Twin profile context to canonical local "
            "workspace settings, re-renders the derived office profile mirror, "
            "and reseeds Hermes context from the canonical bundle."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "office_details": {
                    "type": "object",
                    "description": (
                        "Structured office identity details such as name, "
                        "phone, address, type, or size."
                    ),
                    "additionalProperties": True,
                },
                "team_members_op": {
                    "type": "string",
                    "enum": ["replace", "upsert", "remove_by_id"],
                    "description": (
                        "Team member mutation mode. Required when team_members "
                        "is present."
                    ),
                },
                "team_members": {
                    "type": "array",
                    "description": (
                        "Team member records. Each record must include id. "
                        "For remove_by_id, only id is required."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["id"],
                        "properties": {"id": {"type": "string"}},
                        "additionalProperties": True,
                    },
                },
                "work_days": {
                    "type": "array",
                    "description": "Working day identifiers or labels.",
                    "items": {"type": "string"},
                },
                "compliance": {
                    "type": "object",
                    "description": "Structured compliance profile settings.",
                    "additionalProperties": True,
                },
                "preferences": {
                    "type": "object",
                    "description": "Structured office preferences.",
                    "additionalProperties": True,
                },
                "connectors": {
                    "type": "array",
                    "description": "Structured connector profile metadata.",
                    "items": {"type": "object", "additionalProperties": True},
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "list_tools": {
        "name": "list_tools",
        "description": (
            "Returns the names and descriptions of all Agente desktop tools "
            "available to the assistant in this session. Call this when the "
            "operator asks what tools or capabilities the assistant has."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "list_workflows": {
        "name": "list_workflows",
        "description": "List all available workflow definitions in the workspace.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "inspect_workflow": {
        "name": "inspect_workflow",
        "description": "Return metadata about a specific workflow definition.",
        "parameters": {
            "type": "object",
            "properties": {
                "workflowId": {
                    "type": "string",
                    "minLength": 1,
                    "description": "ID of the workflow to inspect.",
                },
            },
            "required": ["workflowId"],
        },
    },
    "start_workflow_run": {
        "name": "start_workflow_run",
        "description": "Start a new workflow run for a given ticket.",
        "parameters": {
            "type": "object",
            "properties": {
                "workflowId": {
                    "type": "string",
                    "minLength": 1,
                    "description": "ID of the workflow to run.",
                },
                "ticketId": {
                    "type": "string",
                    "minLength": 1,
                    "description": "ID of the ticket to process.",
                },
                "ticketLabel": {
                    "type": "string",
                    "description": "Current label of the ticket (optional).",
                },
                "runId": {
                    "type": "string",
                    "description": "Optional deterministic run ID.",
                },
            },
            "required": ["workflowId", "ticketId"],
        },
    },
    "get_run_status": {
        "name": "get_run_status",
        "description": "Return the current status of a workflow run.",
        "parameters": {
            "type": "object",
            "properties": {
                "runId": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Run ID to query.",
                },
            },
            "required": ["runId"],
        },
    },
    "resume_paused_run": {
        "name": "resume_paused_run",
        "description": "Resume a paused workflow run from the specified step.",
        "parameters": {
            "type": "object",
            "properties": {
                "runId": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Run ID to resume.",
                },
                "stepIndex": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Step index to resume from (must match "
                        "pauseHandle.nextStepIndex)."
                    ),
                },
            },
            "required": ["runId", "stepIndex"],
        },
    },
    "save_executable_workflow": {
        "name": "save_executable_workflow",
        "description": (
            "Create or update a multi-step agent workflow in the workspace. "
            "Validates the input against the Desktop workflow schema and "
            "persists the result as a YAML file under "
            "agente-workspace/office/workflows/. Use this tool when the "
            "operator asks you to build or modify a workflow. After saving, "
            "workflows are inspectable via inspect_workflow and runnable via "
            "start_workflow_run."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "workflow_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        'Stable kebab-case workflow identifier (e.g. "wa-triage"). '
                        "Must match the pattern ^[a-z0-9]+(?:-[a-z0-9]+)*$."
                    ),
                },
                "agents": {
                    "type": "array",
                    "minItems": 1,
                    "description": (
                        "Ordered list of workflow steps. Each step defines a "
                        "single agent action."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_id": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Stable kebab-case step identifier.",
                            },
                            "role": {
                                "type": "string",
                                "minLength": 1,
                                "description": (
                                    "Workspace-relative role manifest path "
                                    '(e.g. "ops/roles/triage-agent.yaml").'
                                ),
                            },
                            "skill_ref": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Workspace-relative skill reference.",
                            },
                            "lane": {
                                "type": "string",
                                "enum": ["safe", "supervised", "locked"],
                                "description": "Desktop governance lane for this step.",
                            },
                            "instructions": {
                                "type": "string",
                                "description": (
                                    "Optional natural-language instructions "
                                    "passed to the sidecar agent for this step."
                                ),
                            },
                            "artifact_path": {
                                "type": "string",
                                "minLength": 1,
                                "description": (
                                    "Expected run artifact path relative to "
                                    "agente-workspace/."
                                ),
                            },
                            "ticket_transition": {
                                "type": "object",
                                "properties": {
                                    "from": {"type": "string", "minLength": 1},
                                    "to": {"type": "string", "minLength": 1},
                                },
                                "required": ["from", "to"],
                                "description": "Ticket label transition for this step.",
                            },
                        },
                        "required": [
                            "step_id",
                            "role",
                            "skill_ref",
                            "lane",
                            "artifact_path",
                            "ticket_transition",
                        ],
                    },
                },
            },
            "required": ["workflow_id", "agents"],
        },
    },
    "evaluate_triage_rules": {
        "name": "evaluate_triage_rules",
        "description": (
            "Evaluates an inbound event against the operator's triage rules "
            "(office/triage-rules.md) and returns a structured decision: "
            "create_ticket | drop | draft_reply | escalate. Fail-open: any "
            "internal error returns create_ticket so no real customer message "
            "is silently dropped."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": (
                        'Event source, e.g. "whatsapp", "email".'
                    ),
                },
                "type": {
                    "type": "string",
                    "description": (
                        'Event type, e.g. "whatsapp_message".'
                    ),
                },
                "text": {
                    "type": "string",
                    "description": (
                        "Free-text payload of the event (Hebrew preferred)."
                    ),
                },
                "metadata": {
                    "type": "object",
                    "description": (
                        "Arbitrary structured metadata about the event."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["source", "type"],
        },
    },
    # --- Agente Desktop folder-sandbox schemas patch (desktop-202605-folder-tools) ---
    "read_file": {
        "name": "read_file",
        "description": (
            "Reads a file from a connected folder. Returns text content for "
            'text files; returns a {"kind":"binary", "sizeBytes", "mimeType"} '
            "descriptor for binary files. Paths must be inside an allowlisted "
            "connected folder; out-of-allowlist access fails closed with a "
            "structured error."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute or relative path to the file. Relative paths "
                        "resolve against the primary connected folder."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    "read_document": {
        "name": "read_document",
        "description": (
            "Reads a document file (PDF, DOCX, XLSX, PPTX) from a connected "
            "folder and extracts its text content. Binary and image files "
            "return a descriptor. Text files return their content directly. "
            "Paths must be inside an allowlisted connected folder; "
            "out-of-allowlist access fails closed with a structured error."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute or relative path to the document. Relative "
                        "paths resolve against the primary connected folder."
                    ),
                },
                "maxChars": {
                    "type": "integer",
                    "description": (
                        "Maximum number of characters to return. Defaults to "
                        "50000. Content beyond this limit is truncated and "
                        'marked with "truncated": true.'
                    ),
                },
                "pageRange": {
                    "type": "object",
                    "description": (
                        "Optional page range for PDFs. Use {start: N, end: M} "
                        "to extract only those pages (1-indexed, inclusive)."
                    ),
                    "properties": {
                        "start": {"type": "integer"},
                        "end": {"type": "integer"},
                    },
                },
            },
            "required": ["path"],
        },
    },
    "write_file": {
        "name": "write_file",
        "description": (
            "Writes a text file inside a connected folder. First use emits an "
            "approval.request; once the operator approves the resulting "
            "pending_review ticket, the call must be retried with approval_id "
            "and the file is written then. Paths must be inside an allowlisted "
            "connected folder; out-of-allowlist or no-folder-connected requests "
            "fail closed with a structured error. Content size capped at 2MB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute or relative path to the file to write. "
                        "Relative paths resolve against the primary connected "
                        "folder."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "UTF-8 text content to write to the file. Maximum 2MB."
                    ),
                },
                "approval_id": {
                    "type": "string",
                    "description": (
                        "Optional. Set this to the approval_id returned from "
                        "the first call to retry the write once the operator "
                        "approved it on the ticket board."
                    ),
                },
            },
            "required": ["path", "content"],
        },
    },
    "list_directory": {
        "name": "list_directory",
        "description": (
            "Lists immediate entries (files and subdirectories) of a folder "
            "inside an allowlisted connected folder. Supports an optional "
            'basename glob pattern (e.g. "*.pdf"). Out-of-allowlist paths fail '
            "closed with a structured error."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute or relative directory path. Relative paths "
                        "resolve against the primary connected folder."
                    ),
                },
                "pattern": {
                    "type": "string",
                    "description": (
                        'Optional basename glob (e.g. "*.pdf", "invoice-*"). '
                        "Matches case-insensitively."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    "scan_folder": {
        "name": "scan_folder",
        "description": (
            "Scans a folder for files matching an optional basename glob "
            "pattern. Recursive by default. Returns a list of relative file "
            "paths capped by `limit` (default 500). Out-of-allowlist paths "
            "fail closed with a structured error."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute or relative directory path. Relative paths "
                        "resolve against the primary connected folder."
                    ),
                },
                "recursive": {
                    "type": "boolean",
                    "description": (
                        "If false, only scans the immediate directory. "
                        "Defaults to true."
                    ),
                },
                "pattern": {
                    "type": "string",
                    "description": (
                        'Optional basename glob (e.g. "*.pdf"). Matches '
                        "case-insensitively."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of files to return. Defaults to 500, "
                        "hard cap 5000."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    # --- end Agente Desktop folder-sandbox schemas patch (desktop-202605-folder-tools) ---

    # --- Agente Desktop data-surface schemas patch (desktop-202605-data-tools) ---
    "data_read": {
        "name": "data_read",
        "description": (
            "Reads structured data from a named surface registry. Use "
            'surface="_index" to list all available surfaces and their schemas.'
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "surface": {
                    "type": "string",
                    "description": (
                        'The surface identifier to read from. Use "_index" to '
                        "list all surfaces."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Query or slice hint passed to the surface read "
                        'handler. Defaults to "all".'
                    ),
                },
                "slice": {
                    "type": "string",
                    "description": (
                        "Alias for query. Surface-specific slice selector."
                    ),
                },
            },
            "required": ["surface"],
            "additionalProperties": False,
        },
    },
    "data_write": {
        "name": "data_write",
        "description": (
            "Writes structured mutations to a named surface registry entry. "
            "Supports dry_run to validate without applying."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "surface": {
                    "type": "string",
                    "description": "The surface identifier to write to.",
                },
                "mutation": {
                    "type": "string",
                    "description": (
                        'The mutation type to apply (e.g. "upsert").'
                    ),
                },
                "payload": {
                    "type": "object",
                    "description": "The patch payload to write.",
                    "additionalProperties": True,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "When true, validates the payload and returns the "
                        "planned mutation without writing."
                    ),
                },
            },
            "required": ["surface", "mutation", "payload"],
            "additionalProperties": False,
        },
    },
    # --- end Agente Desktop data-surface schemas patch (desktop-202605-data-tools) ---

    # --- Agente Desktop web-connector schemas patch (desktop-202605-336) ---
    "list_web_connectors": {
        "name": "list_web_connectors",
        "description": (
            "List the web connectors the operator has configured (URL, label, auth_type). "
            "Use this to find a connector_id before calling browse_connector."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "browse_connector": {
        "name": "browse_connector",
        "description": (
            "Navigate a saved web connector and execute an instruction. Requires operator "
            "approval before the first use per connector per session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "description": "The connector_id of the saved web connector to navigate.",
                },
                "instruction": {
                    "type": "string",
                    "description": "Natural-language instruction describing what to do on the site.",
                },
                "approved": {
                    "type": "boolean",
                    "description": "Set to true after the operator explicitly approves this navigation.",
                },
            },
            "required": ["connector_id", "instruction"],
        },
    },
    # --- end Agente Desktop web-connector schemas patch (desktop-202605-336) ---

    # --- Agente Desktop routines-surface schemas patch (desktop-202605-283) ---
    "list_routines": {
        "name": "list_routines",
        "description": "List all scheduled routine jobs registered with the Hermes sidecar.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "get_routine": {
        "name": "get_routine",
        "description": "Get the detail and run history for a specific routine job.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    "run_routine": {
        "name": "run_routine",
        "description": "Trigger an immediate run of a routine job.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    "pause_routine": {
        "name": "pause_routine",
        "description": "Pause a scheduled routine job so it no longer fires on its cron schedule.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    "resume_routine": {
        "name": "resume_routine",
        "description": "Resume a previously paused routine job.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    "delete_routine": {
        "name": "delete_routine",
        "description": "Delete a routine from the scheduler. Accepts a user-facing routine slug or scheduler job ID.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    # --- end Agente Desktop routines-surface schemas patch (desktop-202605-283) ---

    # --- Agente Desktop agent-tip schemas patch (desktop-202606-423) ---
    "suggest_client_tip": {
        "name": "suggest_client_tip",
        "description": (
            "Returns the agent tip card payload for a single client. Reads the "
            "markdown cache at office/clients/<id>/agent-tip.md and, on "
            "force=true, regenerates it. The user-instructed-triage persona "
            "suppresses auto-generation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "clientId": {"type": "string"},
                "persona": {"type": "string"},
                "force": {"type": "boolean"},
            },
            "required": ["clientId"],
            "additionalProperties": False,
        },
    },
    # --- end Agente Desktop agent-tip schemas patch (desktop-202606-423) ---

    # --- Agente Desktop download-whatsapp-media schema patch (hermes-202606-download-whatsapp-media-schema) ---
    "download_whatsapp_media": {
        "name": "download_whatsapp_media",
        "description": (
            "Downloads a WhatsApp media attachment (image / document / audio / "
            "video) referenced by message_id from a connected GOWA WhatsApp "
            "account, and writes it to dest_path inside an allowlisted "
            "connected folder. Media is fetched via the single GOWA REST "
            "process through desktop IPC — Python must never call GOWA "
            "directly. First-use per session emits an approval.request on the "
            "desktop side; out-of-allowlist dest_path fails closed with a "
            "structured error."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "string",
                    "description": (
                        "GOWA WhatsApp account id (matches an entry returned "
                        "by list_whatsapp_accounts)."
                    ),
                },
                "message_id": {
                    "type": "string",
                    "description": (
                        "Id of the WhatsApp message whose media attachment "
                        "should be downloaded."
                    ),
                },
                "chat_jid": {
                    "type": "string",
                    "description": (
                        "Chat JID the message belongs to — from the "
                        "list_recent_messages chat_jid field. Required by "
                        "the GOWA REST download endpoint."
                    ),
                },
                "media_type": {
                    "type": "string",
                    "description": (
                        'Expected media kind, one of "image" | "document" | '
                        '"audio" | "video". Used by the desktop side to '
                        "select the correct GOWA endpoint and mime-sniff the "
                        "result."
                    ),
                },
                "dest_path": {
                    "type": "string",
                    "description": (
                        "Absolute or connected-folder-relative destination "
                        "path for the downloaded file. Must resolve inside "
                        "an allowlisted connected folder."
                    ),
                },
            },
            "required": ["account_id", "message_id", "chat_jid", "media_type", "dest_path"],
            "additionalProperties": False,
        },
    },
    # --- end Agente Desktop download-whatsapp-media schema patch (hermes-202606-download-whatsapp-media-schema) ---

    # --- Agente Desktop OAuth connector tools schemas patch (desktop-connector-cascade-001) ---
    "connect_google": {
        "name": "connect_google",
        "description": (
            "Start the Google (Gmail + Calendar) OAuth flow on behalf of the "
            "user. Opens the system browser for consent. Returns a structured "
            "status envelope (status=connected with the email, or error with "
            "Hebrew error_he)."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "connect_anthropic": {
        "name": "connect_anthropic",
        "description": (
            "Start the Anthropic (Claude) OAuth/PKCE sign-in on behalf of the "
            "user. Returns status=pending with auth_url + session_id so the "
            "chat can surface the link; the renderer completes the flow via "
            "the existing BYOK card."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "connect_openai": {
        "name": "connect_openai",
        "description": (
            "Start the OpenAI (ChatGPT Plus / Codex) OAuth device-code flow on "
            "behalf of the user. Returns status=pending with verification_uri "
            "+ user_code so the chat can read the URL and code out loud."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "check_connector_status": {
        "name": "check_connector_status",
        "description": (
            "Read-only check of the current connection status for a given "
            "provider (google / anthropic / openai). Never starts an OAuth "
            "flow. Returns connected (with account when known), needs_reauth, "
            "or error."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["google", "anthropic", "openai"],
                },
            },
            "required": ["provider"],
        },
    },
    # --- end Agente Desktop OAuth connector tools schemas patch (desktop-connector-cascade-001) ---

    # --- Agente Desktop assign-ticket schema patch (desktop-202606-535) ---
    "assign_ticket": {
        "name": "assign_ticket",
        "description": (
            "Assigns an existing ticket to a team member declared in "
            "office/profile.yaml team_members[]. Accepts the team_member id "
            "(e.g. \"leon\") OR display name (e.g. \"לאון\"); Hebrew-nikud-"
            "insensitive fuzzy match. Returns a Hebrew confirmation message "
            "and the resolved team_member; returns a structured error listing "
            "valid team_members when no match."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "string",
                    "description": "UUID of the ticket to assign.",
                },
                "team_member": {
                    "type": "string",
                    "description": (
                        "team_members[].id (e.g. \"leon\") OR display name "
                        "(e.g. \"לאון\" / \"לאון זינגר\")."
                    ),
                },
            },
            "required": ["ticket_id", "team_member"],
        },
    },
    # --- end Agente Desktop assign-ticket schema patch (desktop-202606-535) ---

    # --- Agente Desktop resolve_or_upsert_client schema patch (desktop-202606-536) ---
    "resolve_or_upsert_client": {
        "name": "resolve_or_upsert_client",
        "description": (
            "Resolve a client by slug / alias / Hebrew partial-name. If none "
            "matches, create a new YAML client and return its slug. Always "
            "returns {slug, was_created}. Use this before create_ticket "
            "whenever a person/business is named in the user request, then "
            "pass the returned slug as client_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name_or_slug": {"type": "string"},
                "hebrew_name": {"type": "string"},
                "phone": {"type": "string"},
                "jid": {"type": "string"},
                "email": {"type": "string"},
            },
            "required": ["name_or_slug"],
        },
    },
    # --- end Agente Desktop resolve_or_upsert_client schema patch (desktop-202606-536) ---

    # --- Agente Desktop agent-management (profile) schemas patch (desktop-202606-578) ---
    "list_agent_profiles": {
        "name": "list_agent_profiles",
        "description": (
            "List all Hermes agent profiles available for workflows and team "
            "management. Shows name, model, skill count, gateway status, and "
            "description for each profile."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "create_agent_profile": {
        "name": "create_agent_profile",
        "description": (
            "Create a new Hermes agent profile for use in workflows and team "
            "management. The new profile has its own isolated config, skills, "
            "memory, and sessions. By default it clones from the current agent "
            "profile so it inherits model and credentials."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Profile name (lowercase, alphanumeric, hyphens, "
                        "underscores). Pattern: [a-z0-9][a-z0-9_-]{0,63}. "
                        'Examples: "communication-agent", "office-manager".'
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "One- to two-sentence description of what this agent "
                        "is good at, for kanban routing."
                    ),
                },
                "soul_content": {
                    "type": "string",
                    "description": (
                        "SOUL.md persona content in Hebrew. If omitted, uses "
                        "the default Agente office personality."
                    ),
                },
                "clone_from": {
                    "type": "string",
                    "description": (
                        "Source profile to clone config from. Defaults to "
                        "'default' (current desktop agent)."
                    ),
                },
                "clone_all": {
                    "type": "boolean",
                    "description": (
                        "Full state copy (all config, skills, memory, sessions) "
                        "from source. Default: false (copies config + skills only)."
                    ),
                },
                "no_skills": {
                    "type": "boolean",
                    "description": (
                        "Skip bundled skill seeding. Use for bare profiles that "
                        "get custom skills later. Default: false."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Model identifier. Only set if different from default."
                    ),
                },
                "provider": {
                    "type": "string",
                    "description": (
                        "Model provider (e.g., \"openai\", \"anthropic\", "
                        "\"custom\"). Required when model is set."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    # --- end Agente Desktop agent-management (profile) schemas patch (desktop-202606-578) ---

}


def _ipc_endpoint(tool_name: str) -> str | None:
    port = os.environ.get("AGENTE_TOOL_PORT")
    if not port:
        return None
    return f"http://127.0.0.1:{port}/dispatch/{tool_name}"


def _proxy_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Forward a tool call to the Electron main process over loopback HTTP.

    Returns the unwrapped ``result`` payload on success or an ``{"error":
    ...}`` dict on any failure. Never raises — every path returns a dict so
    the AIAgent can fold the response into the next conversation turn.
    """
    endpoint = _ipc_endpoint(tool_name)
    secret = os.environ.get("AGENTE_TOOL_SECRET")
    if endpoint is None or not secret:
        return {"error": "agente_tool_ipc_not_configured"}

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(args).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return {
            "error": "agente_tool_http_error",
            "status": exc.code,
            "body": exc.read().decode("utf-8", "replace"),
        }
    except Exception as exc:  # noqa: BLE001 — defensive; never raise from a tool handler
        return {"error": "agente_tool_exception", "message": str(exc)}

    try:
        envelope = json.loads(body)
    except json.JSONDecodeError:
        return {"error": "agente_tool_invalid_json", "raw": body[:512]}

    if not isinstance(envelope, dict):
        return {"error": "agente_tool_invalid_envelope", "raw": body[:512]}

    if envelope.get("ok") is True:
        result = envelope.get("result")
        return result if isinstance(result, dict) else {"result": result}

    return {
        "error": envelope.get("error", "agente_tool_unknown_error"),
        "details": envelope.get("details"),
    }


def _check_available() -> bool:
    return bool(os.environ.get("AGENTE_TOOL_PORT") and os.environ.get("AGENTE_TOOL_SECRET"))


def _make_handler(tool_name: str):
    """Bind the tool name into a closure so each registered handler has its
    own dispatch target. Avoids the late-binding-loop pitfall.
    """

    def handler(args: dict[str, Any], **kwargs: Any) -> str:
        return json.dumps(_proxy_call(tool_name, args), ensure_ascii=False)

    return handler


def _json_safe_metadata(value: Any) -> Any:
    """Coerce parsed YAML metadata into values accepted by JSON responses.
    (Duplicated from api_server for desktop compat isolation; pure func.)
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe_metadata(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_metadata(item) for item in value]
    return str(value)


def _get_tool_registry(adapter: Any) -> Any:
    loader = getattr(adapter, "_ensure_tool_registry_loaded", None)
    if callable(loader):
        return loader()

    from tools.registry import registry as _tool_registry

    return _tool_registry


# --- route handlers (moved from api_server.py _handle_list_tools / _handle_dispatch_tool) ---

async def _handle_list_tools(request: "web.Request", adapter: Any) -> "web.Response":
    """GET /api/tools — Desktop-facing Hermes tool discovery (compat shim)."""
    from aiohttp import web

    auth_err = adapter._check_auth(request)
    if auth_err is not None:
        return auth_err

    try:
        registry = _get_tool_registry(adapter)
        definitions = registry.get_definitions(set(registry.get_all_tool_names()), quiet=True)
        tools: List[Dict[str, Any]] = []
        for definition in definitions:
            if not isinstance(definition, dict):
                continue
            function = definition.get("function")
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "")
            if not name:
                continue
            entry = registry.get_entry(name)
            item: Dict[str, Any] = {
                "name": name,
                "description": str(function.get("description") or ""),
                "parameters": _json_safe_metadata(function.get("parameters") or {}),
                "definition": _json_safe_metadata(definition),
            }
            if entry is not None:
                item["toolset"] = str(entry.toolset or "")
                if getattr(entry, 'label_he', None):
                    item["label_he"] = str(entry.label_he)
                # ToolEntry uses __slots__ without a `category` slot, so a bare
                # `entry.category` raises AttributeError (not returns None) and
                # crashes /api/tools discovery, killing the gateway health check
                # ("Started 0/5 profile gateways"). Mirror the label_he guard.
                if getattr(entry, 'category', None):
                    item["category"] = str(entry.category)
            tools.append(item)
        return web.json_response({"tools": tools})
    except Exception as exc:
        logger.exception("[api_server] tool discovery failed: %s", exc)
        return web.json_response(
            {"error": {"message": "Failed to discover tools", "code": "tools_discovery_failed"}},
            status=500,
        )


async def _handle_dispatch_tool(request: "web.Request", adapter: Any) -> "web.Response":
    """POST /api/tools/{tool_name} — Desktop-facing direct tool dispatch (compat shim)."""
    from aiohttp import web

    auth_err = adapter._check_auth(request)
    if auth_err is not None:
        return auth_err

    tool_name = str(request.match_info.get("tool_name") or "").strip()
    if not tool_name:
        return web.json_response(
            {"error": {"message": "tool_name required", "code": "tool_name_required"}},
            status=400,
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": {"message": "Invalid JSON body", "code": "invalid_json"}},
            status=400,
        )
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return web.json_response(
            {"error": {"message": "JSON body must be an object", "code": "invalid_json"}},
            status=400,
        )

    arguments = body.get("arguments")
    if arguments is None:
        arguments = body.get("args")
    if arguments is None:
        arguments = {k: v for k, v in body.items() if k != "tool_name"}
    if not isinstance(arguments, dict):
        return web.json_response(
            {"error": {"message": "Tool arguments must be an object", "code": "invalid_arguments"}},
            status=400,
        )

    try:
        registry = _get_tool_registry(adapter)
        if registry.get_entry(tool_name) is None:
            return web.json_response(
                {"error": {"message": f"Unknown tool: {tool_name}", "code": "unknown_tool"}},
                status=404,
            )

        # normalize for desktop arg shapes (e.g. save_workflow/create_routine)
        from .workflow_routine_bridge import _normalize_desktop_tool_args

        normalized_args = _normalize_desktop_tool_args(tool_name, arguments)
        from model_tools import handle_function_call

        raw_result = handle_function_call(
            tool_name,
            normalized_args,
            task_id=f"api-tools-{uuid.uuid4().hex}",
        )
        try:
            result = json.loads(raw_result)
        except Exception:
            result = {"result": raw_result}
        return web.json_response(_json_safe_metadata(result))
    except Exception as exc:
        logger.exception("[api_server] tool dispatch failed for %s: %s", tool_name, exc)
        return web.json_response(
            {"error": {"message": f"Tool execution failed: {exc}", "code": "tool_execution_failed"}},
            status=500,
        )


# --- public register (orchestrated from __init__) ---

def register(app: Any, adapter: Any = None) -> None:
    """Register Desktop toolset + discovery/dispatch routes.

    Called from api_server.py (the single allowed integration point).
    Also registers the agente-desktop proxy tools into the global registry
    (replaces the old plugins/agente_desktop/__init__.py register path).
    """
    if adapter is None:
        adapter = getattr(app, "get", lambda k: None)("api_server_adapter") if hasattr(app, "get") else None
        if adapter is None and hasattr(app, "__getitem__"):
            try:
                adapter = app["api_server_adapter"]
            except Exception:
                adapter = None

    if adapter is None:
        logger.debug("agente_desktop_adapter.tool_discovery: no api_server_adapter; skipping route+tool reg")
        return

    # 1. Ensure the proxy tools are registered (so enabled_toolsets=["agente-desktop"] works)
    #    Use direct registry to avoid plugin manager tracking (this is the adapter path).
    try:
        from tools.registry import registry as _tool_registry
        for tool_name, schema in TOOL_SCHEMAS.items():
            _tool_registry.register(
                name=tool_name,
                toolset=TOOLSET,
                schema=schema,
                handler=_make_handler(tool_name),
                check_fn=_check_available,
                description=schema.get("description", ""),
                emoji=_TOOL_EMOJIS.get(tool_name, "🔌"),
            )
        logger.info(
            "agente_desktop_adapter registered %d proxy tools under toolset %r (via thin adapter)",
            len(TOOL_SCHEMAS),
            TOOLSET,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("agente_desktop_adapter tool registration failed: %s", exc)

    # 2. Add the /api/tools discovery + dispatch routes (Desktop compat).
    #    These were previously ad-hoc in api_server.py.
    try:
        from aiohttp import web
    except ImportError:
        logger.warning("aiohttp not available; cannot register /api/tools desktop compat routes")
        return

    # Use closures so handlers can close over the concrete adapter instance (for _check_auth etc).
    async def list_tools(req: "web.Request") -> "web.Response":
        return await _handle_list_tools(req, adapter)

    async def dispatch_tool(req: "web.Request") -> "web.Response":
        return await _handle_dispatch_tool(req, adapter)

    # Idempotent add: check if already present (in case of reloads).
    existing_paths = {r.path for r in app.router.routes()}
    if "/api/tools" not in existing_paths:
        app.router.add_get("/api/tools", list_tools)
    if "/api/tools/{tool_name}" not in existing_paths:
        app.router.add_post("/api/tools/{tool_name}", dispatch_tool)

    logger.debug("agente_desktop_adapter.tool_discovery: /api/tools routes registered")
