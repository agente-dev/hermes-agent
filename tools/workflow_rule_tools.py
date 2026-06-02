"""Workflow-rule save/load tools.

Exposes two Hermes tools — ``save_workflow_rule`` and ``list_workflow_rules``
— that persist per-rule JSON records to ``<HERMES_HOME>/workflow-rules/<id>.json``.
Establishes Hermes as the source of truth for workflow rules before desktop
or any other surface wires UI editors that round-trip the same records.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from tools.registry import registry, tool_error
from tools.workflow_rules_storage import list_rules, save_rule

logger = logging.getLogger(__name__)


def save_workflow_rule_handler(
    id: str,
    connector_id: str,
    rule_natural_language: str,
    matcher_pattern: Optional[str] = None,
    target_ticket_template: Optional[str] = None,
    enabled: bool = True,
    created_by_session_id: Optional[str] = None,
) -> str:
    record: Dict[str, Any] = {
        "id": id,
        "connector_id": connector_id,
        "rule_natural_language": rule_natural_language,
        "matcher_pattern": matcher_pattern or "",
        "target_ticket_template": target_ticket_template or "",
        "enabled": bool(enabled),
        "created_by_session_id": created_by_session_id,
    }
    try:
        saved = save_rule(record)
    except ValueError as exc:
        return tool_error(str(exc), success=False)
    except OSError as exc:
        logger.exception("save_workflow_rule failed for id=%s", id)
        return tool_error(f"failed to persist workflow rule: {exc}", success=False)

    return json.dumps(
        {"success": True, "rule": saved},
        ensure_ascii=False,
    )


def list_workflow_rules_handler(connector_id: Optional[str] = None) -> str:
    try:
        rules = list_rules(connector_id=connector_id)
    except OSError as exc:
        logger.exception("list_workflow_rules failed (connector_id=%s)", connector_id)
        return tool_error(f"failed to read workflow rules: {exc}", success=False)
    return json.dumps(
        {"success": True, "rules": rules, "count": len(rules)},
        ensure_ascii=False,
    )


SAVE_WORKFLOW_RULE_SCHEMA = {
    "name": "save_workflow_rule",
    "description": (
        "Persist a workflow rule (per-connector automation rule) as a JSON "
        "record under <HERMES_HOME>/workflow-rules/<id>.json. Hermes is the "
        "canonical writer for these records — desktop and other surfaces "
        "treat the on-disk store as the source of truth."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Stable id for the rule (filesystem-safe: [A-Za-z0-9_-]{1,128}).",
            },
            "connector_id": {
                "type": "string",
                "description": "Connector this rule binds to (e.g. a WhatsApp/email connector id).",
            },
            "rule_natural_language": {
                "type": "string",
                "description": "The user's own description of the rule in their own language (Hebrew round-trips cleanly).",
            },
            "matcher_pattern": {
                "type": "string",
                "description": "Opaque matcher description (regex, semantic phrase, structured selector).",
            },
            "target_ticket_template": {
                "type": "string",
                "description": "Template id or inline template body for the ticket spawned when the matcher fires.",
            },
            "enabled": {
                "type": "boolean",
                "default": True,
                "description": "Whether the rule is active.",
            },
            "created_by_session_id": {
                "type": "string",
                "description": "Provenance — the session that created or last edited the rule.",
            },
        },
        "required": ["id", "connector_id", "rule_natural_language"],
    },
}


LIST_WORKFLOW_RULES_SCHEMA = {
    "name": "list_workflow_rules",
    "description": (
        "List workflow rules persisted under <HERMES_HOME>/workflow-rules/. "
        "Optionally filter by connector_id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "connector_id": {
                "type": "string",
                "description": "If set, return only rules whose connector_id matches exactly.",
            },
        },
    },
}


registry.register(
    name="save_workflow_rule",
    toolset="workflow_rules",
    schema=SAVE_WORKFLOW_RULE_SCHEMA,
    handler=lambda args, **_kw: save_workflow_rule_handler(
        id=args.get("id", ""),
        connector_id=args.get("connector_id", ""),
        rule_natural_language=args.get("rule_natural_language", ""),
        matcher_pattern=args.get("matcher_pattern"),
        target_ticket_template=args.get("target_ticket_template"),
        enabled=args.get("enabled", True),
        created_by_session_id=args.get("created_by_session_id"),
    ),
    emoji="📋",
)


registry.register(
    name="list_workflow_rules",
    toolset="workflow_rules",
    schema=LIST_WORKFLOW_RULES_SCHEMA,
    handler=lambda args, **_kw: list_workflow_rules_handler(
        connector_id=args.get("connector_id"),
    ),
    emoji="📋",
)
