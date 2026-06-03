"""DEPRECATED — workflow rules superseded by workflow + routine primitives.

This module previously exposed ``save_workflow_rule`` and
``list_workflow_rules`` as Hermes tools that persisted per-rule JSON
records. The 2026-06 pivot (hermes-agent-202606-028 / desktop-202606-514)
collapsed automation onto three primitives — **connector**, **workflow**,
**routine** — and removed the standalone "rule" entity from the operator
chat surface entirely.

The Python helpers and the on-disk storage layer
(``tools/workflow_rules_storage.py``) are retained so any background
migration code that still references them can read existing
workflow-rules JSON. The two LLM-visible tools, however, are gone:
calling ``save_workflow_rule_handler`` or ``list_workflow_rules_handler``
now raises ``RuntimeError`` to surface the deprecation loudly during
tests, and the tools are no longer registered with the tool registry.

Use ``save_workflow`` + ``create_routine`` (see
``tools/workflow_routine_tools.py``) instead.
"""

from __future__ import annotations

from typing import Any, Optional


_DEPRECATION_MESSAGE = (
    "DEPRECATED: save_workflow_rule / list_workflow_rules have been removed. "
    "Use save_workflow + create_routine — see "
    "hermes-agent-202606-028 / desktop-202606-514."
)


def save_workflow_rule_handler(
    id: str,
    connector_id: str,
    rule_natural_language: str,
    matcher_pattern: Optional[str] = None,
    target_ticket_template: Optional[str] = None,
    enabled: bool = True,
    created_by_session_id: Optional[str] = None,
) -> str:
    raise RuntimeError(_DEPRECATION_MESSAGE)


def list_workflow_rules_handler(connector_id: Optional[str] = None) -> str:
    raise RuntimeError(_DEPRECATION_MESSAGE)


# Schemas retained as constants so any caller that inspected them at
# import time keeps a defined value. The objects are intentionally minimal
# — the tools are no longer registered.
SAVE_WORKFLOW_RULE_SCHEMA: dict[str, Any] = {
    "name": "save_workflow_rule",
    "deprecated": True,
    "description": _DEPRECATION_MESSAGE,
    "parameters": {"type": "object", "properties": {}},
}


LIST_WORKFLOW_RULES_SCHEMA: dict[str, Any] = {
    "name": "list_workflow_rules",
    "deprecated": True,
    "description": _DEPRECATION_MESSAGE,
    "parameters": {"type": "object", "properties": {}},
}


# No registry.register() calls — the deprecation is enforced by removing
# the tool from the LLM-visible surface entirely. See toolsets.py and
# model_tools.py for the matching removals.
