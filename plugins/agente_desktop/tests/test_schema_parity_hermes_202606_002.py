"""hermes-202606-002 schema-parity regression.

Ensures the agente_desktop Python plugin declares schemas for the
desktop-side `evaluate_triage_rules` and `save_workflow_rule` tools
introduced in desktop-202606-437 and desktop-202605-307 respectively,
and that the legacy `save_triage_instructions` entry survives in
deprecated form for backwards-compat.
"""

from __future__ import annotations

from plugins.agente_desktop.schemas import TOOL_SCHEMAS


def test_evaluate_triage_rules_schema_present():
    schema = TOOL_SCHEMAS.get("evaluate_triage_rules")
    assert schema is not None, "evaluate_triage_rules missing"
    assert schema["name"] == "evaluate_triage_rules"
    params = schema["parameters"]
    assert params["type"] == "object"
    # Required input fields per desktop-side TS contract.
    assert set(params["required"]) == {"source", "type"}
    props = params["properties"]
    for key in ("source", "type", "text", "metadata"):
        assert key in props, f"missing property {key}"
    assert props["metadata"]["type"] == "object"


def test_save_workflow_rule_schema_present():
    schema = TOOL_SCHEMAS.get("save_workflow_rule")
    assert schema is not None, "save_workflow_rule missing"
    assert schema["name"] == "save_workflow_rule"
    params = schema["parameters"]
    assert params["type"] == "object"
    assert set(params["required"]) == {"match_pattern", "action", "description"}
    mp = params["properties"]["match_pattern"]
    assert mp["type"] == "object"
    assert set(mp["required"]) == {"source"}
    # Optional filters block should accept text_contains + metadata_match.
    filters_props = mp["properties"]["filters"]["properties"]
    for key in ("event_type", "text_contains", "metadata_match"):
        assert key in filters_props


def test_save_triage_instructions_still_registered_as_deprecated():
    schema = TOOL_SCHEMAS.get("save_triage_instructions")
    assert schema is not None, "save_triage_instructions should remain for back-compat"
    assert "DEPRECATED" in schema["description"]


def test_total_tool_count_matches_provides_tools():
    # Sanity: schemas.py and plugin.yaml should agree on the tool surface.
    # We don't parse YAML here to avoid an extra dep; just assert the
    # known 21-tool count after this intake lands (19 upstream + 2 new).
    assert len(TOOL_SCHEMAS) == 21
