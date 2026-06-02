"""
Workflow-rule canonical store.

Hermes-side source of truth for workflow rules. Each rule is one JSON file at
``~/.hermes/workflow-rules/<id>.json`` following the schema laid out in
hermes-202606-003::

    {
      "id": str,
      "connector_id": str,
      "rule_natural_language": str,   # Hebrew, user-typed
      "matcher_pattern": str | dict,  # see eval.matcher.match()
      "target_ticket_template": dict | None,
      "enabled": bool,
      "created_by_session_id": str | None
    }

This module provides the read-side projection used by the message-arrival hook
(hermes-202606-004). Write paths (save_workflow_rule / list_workflow_rules
tools) land with intake 003; this loader is duck-typed against the same file
layout so both intakes converge on a single store.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


def workflow_rules_dir() -> Path:
    """Resolve the canonical workflow-rules directory.

    Honors ``HERMES_HOME`` for tests, falling back to ``~/.hermes``.
    """
    base = os.environ.get("HERMES_HOME")
    root = Path(base) if base else Path.home() / ".hermes"
    return root / "workflow-rules"


def _safe_load_rule(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("workflow-rules: skipping unreadable %s (%s)", path, exc)
        return None

    if not isinstance(data, dict):
        logger.warning("workflow-rules: %s is not a JSON object, skipping", path)
        return None

    # Minimal shape contract — intake 003 is the schema owner.
    if not data.get("id") or not data.get("connector_id"):
        logger.warning("workflow-rules: %s missing id/connector_id, skipping", path)
        return None

    return data


def load_rules_for_connector(connector_id: str) -> List[Dict[str, Any]]:
    """Return all enabled rules whose ``connector_id`` matches.

    Disabled rules (``enabled is False``) are filtered out. Unknown / missing
    ``enabled`` defaults to True so newly-saved rules without that field still
    fire.
    """
    rules_dir = workflow_rules_dir()
    if not rules_dir.exists():
        return []

    out: List[Dict[str, Any]] = []
    for path in sorted(rules_dir.glob("*.json")):
        rule = _safe_load_rule(path)
        if rule is None:
            continue
        if rule.get("connector_id") != connector_id:
            continue
        if rule.get("enabled", True) is False:
            continue
        out.append(rule)
    return out


def iter_all_rules() -> Iterable[Dict[str, Any]]:
    """Yield every readable rule (enabled or not). Used by diagnostics."""
    rules_dir = workflow_rules_dir()
    if not rules_dir.exists():
        return
    for path in sorted(rules_dir.glob("*.json")):
        rule = _safe_load_rule(path)
        if rule is not None:
            yield rule
