"""Tests for tools/workflow_rule_tools.py and tools/workflow_rules_storage.py."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # hermes_constants.get_hermes_home reads HERMES_HOME directly so no other
    # patching is needed; the workflow-rules dir is created lazily.
    yield tmp_path


def _import_modules():
    # Imported inside tests so the HERMES_HOME env var is set BEFORE the
    # storage module resolves any paths.
    import importlib
    import tools.workflow_rules_storage as storage_mod
    import tools.workflow_rule_tools as tool_mod
    importlib.reload(storage_mod)
    importlib.reload(tool_mod)
    return storage_mod, tool_mod


def test_save_and_list_round_trip_hebrew(hermes_home):
    storage, tools = _import_modules()

    saved = storage.save_rule({
        "id": "rule-001",
        "connector_id": "whatsapp-main",
        "rule_natural_language": "כל הודעה עם המילה דחוף תהפוך לכרטיס בעדיפות גבוהה",
        "matcher_pattern": "contains:דחוף",
        "target_ticket_template": "urgent_default",
        "enabled": True,
        "created_by_session_id": "session-abc",
    })

    assert saved["rule_natural_language"].startswith("כל הודעה")
    rule_path = hermes_home / "workflow-rules" / "rule-001.json"
    assert rule_path.is_file()

    # The stored JSON must keep Hebrew as UTF-8 (no \uXXXX escaping).
    raw = rule_path.read_text(encoding="utf-8")
    assert "דחוף" in raw

    rules = storage.list_rules()
    assert len(rules) == 1
    assert rules[0]["rule_natural_language"] == saved["rule_natural_language"]


def test_list_filters_by_connector_id(hermes_home):
    storage, _ = _import_modules()
    storage.save_rule({
        "id": "r-a",
        "connector_id": "wa",
        "rule_natural_language": "rule a",
    })
    storage.save_rule({
        "id": "r-b",
        "connector_id": "gmail",
        "rule_natural_language": "rule b",
    })

    rules = storage.list_rules(connector_id="gmail")
    assert [r["id"] for r in rules] == ["r-b"]


def test_save_rule_rejects_unsafe_id(hermes_home):
    storage, _ = _import_modules()
    with pytest.raises(ValueError):
        storage.save_rule({
            "id": "../escape",
            "connector_id": "wa",
            "rule_natural_language": "x",
        })


def test_save_rule_requires_non_empty_fields(hermes_home):
    storage, _ = _import_modules()
    with pytest.raises(ValueError):
        storage.save_rule({
            "id": "ok-id",
            "connector_id": "",
            "rule_natural_language": "x",
        })


def test_save_rule_atomic_no_partial_files(hermes_home):
    storage, _ = _import_modules()
    storage.save_rule({
        "id": "atomic",
        "connector_id": "wa",
        "rule_natural_language": "x",
    })
    files = list((hermes_home / "workflow-rules").iterdir())
    # Only the final .json file should remain — no stray .tmp leftover.
    assert [f.name for f in files] == ["atomic.json"]


def test_tool_handler_save_returns_success_json(hermes_home):
    _, tools = _import_modules()
    out = tools.save_workflow_rule_handler(
        id="t1",
        connector_id="wa",
        rule_natural_language="שלום",
    )
    payload = json.loads(out)
    assert payload["success"] is True
    assert payload["rule"]["id"] == "t1"
    assert payload["rule"]["rule_natural_language"] == "שלום"


def test_tool_handler_save_reports_validation_error(hermes_home):
    _, tools = _import_modules()
    out = tools.save_workflow_rule_handler(
        id="",
        connector_id="wa",
        rule_natural_language="x",
    )
    payload = json.loads(out)
    assert payload.get("success") is False
    assert "error" in payload


def test_tool_handler_list_filters_and_counts(hermes_home):
    storage, tools = _import_modules()
    for rid, cid in (("a", "wa"), ("b", "wa"), ("c", "gmail")):
        storage.save_rule({
            "id": rid,
            "connector_id": cid,
            "rule_natural_language": rid,
        })

    out = tools.list_workflow_rules_handler(connector_id="wa")
    payload = json.loads(out)
    assert payload["success"] is True
    assert payload["count"] == 2
    assert {r["id"] for r in payload["rules"]} == {"a", "b"}


def test_handler_registered_in_registry():
    """Registry-side check: importing the module registers both handlers."""
    import tools.workflow_rule_tools  # noqa: F401 - import side-effect registers
    from tools.registry import registry

    save_entry = registry.get_entry("save_workflow_rule")
    list_entry = registry.get_entry("list_workflow_rules")
    assert save_entry is not None and save_entry.toolset == "workflow_rules"
    assert list_entry is not None and list_entry.toolset == "workflow_rules"


def test_registered_in_inventory():
    """The tools must appear in HERMES_TOOL_INVENTORY (toolsets.py + model_tools)."""
    from toolsets import _HERMES_CORE_TOOLS, TOOLSETS
    from model_tools import _LEGACY_TOOLSET_MAP

    for name in ("save_workflow_rule", "list_workflow_rules"):
        assert name in _HERMES_CORE_TOOLS
        assert name in TOOLSETS["workflow_rules"]["tools"]
        assert name in _LEGACY_TOOLSET_MAP["workflow_rule_tools"]
