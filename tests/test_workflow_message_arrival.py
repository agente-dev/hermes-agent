"""
Tests for hermes-202606-004: message-arrival hook + matcher.

Acceptance criteria:

1. Connector event triggers rule evaluation before LLM call.
2. Matched rule invokes target_ticket_template tool path.
3. Non-matched messages either drop or fall through to default triage.
4. Audit log line written per evaluation.

Plus the storage shim (loader from ~/.hermes/workflow-rules/<id>.json).
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agente_hermes_addon.eval.matcher import find_matching_rules, match  # noqa: E402
from agente_hermes_addon.hooks import message_arrival as hook_mod  # noqa: E402
from agente_hermes_addon.storage import workflow_rules as store_mod  # noqa: E402


# ─── Matcher ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "pattern,event,expected",
    [
        # plain string substring (Hebrew round-trip)
        ("חשבונית", {"text": "שלח לי חשבונית בבקשה"}, True),
        ("חשבונית", {"text": "תודה רבה"}, False),
        # case-insensitive ASCII
        ("URGENT", {"text": "this is urgent please"}, True),
        # contains dict
        ({"contains": "ticket"}, {"text": "open a TICKET"}, True),
        # contains_any
        ({"contains_any": ["foo", "bar"]}, {"text": "hello bar world"}, True),
        ({"contains_any": ["foo", "baz"]}, {"text": "hello bar world"}, False),
        # contains_all
        ({"contains_all": ["foo", "bar"]}, {"text": "foo and bar"}, True),
        ({"contains_all": ["foo", "bar"]}, {"text": "foo only"}, False),
        # regex unicode
        ({"regex": r"^שלום"}, {"text": "שלום עולם"}, True),
        ({"regex": r"\d{3}-\d{4}"}, {"text": "call 555-1234"}, True),
        # bad regex returns False, doesn't crash
        ({"regex": "[unclosed"}, {"text": "x"}, False),
        # sender match
        ({"sender": "+972501234567"}, {"text": "hi", "from": "+972501234567"}, True),
        ({"sender": "+972501234567"}, {"text": "hi", "from": "other"}, False),
        # nested all / any
        (
            {"all": [{"contains": "invoice"}, {"sender": "boss"}]},
            {"text": "send invoice", "from": "boss"},
            True,
        ),
        (
            {"all": [{"contains": "invoice"}, {"sender": "boss"}]},
            {"text": "send invoice", "from": "intern"},
            False,
        ),
        (
            {"any": [{"contains": "א"}, {"contains": "b"}]},
            {"text": "only c here"},
            False,
        ),
        # message.text nested shape (desktop bridge)
        ({"contains": "hi"}, {"message": {"text": "hi there"}}, True),
        # empty / null pattern never matches (defensive)
        (None, {"text": "anything"}, False),
        ("", {"text": "anything"}, False),
        ({}, {"text": "anything"}, False),
    ],
)
def test_matcher_shapes(pattern: Any, event: Dict[str, Any], expected: bool) -> None:
    assert match(pattern, event) is expected


def test_find_matching_rules_returns_subset() -> None:
    rules = [
        {"id": "a", "matcher_pattern": "foo"},
        {"id": "b", "matcher_pattern": "bar"},
        {"id": "c", "matcher_pattern": {"contains": "foo"}},
    ]
    matched = find_matching_rules(rules, {"text": "say foo loudly"})
    assert [r["id"] for r in matched] == ["a", "c"]


# ─── Storage loader ───────────────────────────────────────────────────────────


def _write_rule(root: Path, rule: Dict[str, Any]) -> None:
    rules_dir = root / "workflow-rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    path = rules_dir / f"{rule['id']}.json"
    path.write_text(json.dumps(rule, ensure_ascii=False), encoding="utf-8")


def test_load_rules_for_connector_filters_and_skips_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_rule(tmp_path, {
        "id": "r1",
        "connector_id": "wa-main",
        "matcher_pattern": "חשבונית",
        "enabled": True,
    })
    _write_rule(tmp_path, {
        "id": "r2",
        "connector_id": "wa-main",
        "matcher_pattern": "foo",
        "enabled": False,
    })
    _write_rule(tmp_path, {
        "id": "r3",
        "connector_id": "gmail",
        "matcher_pattern": "foo",
    })
    # malformed: missing connector_id
    (tmp_path / "workflow-rules" / "bad.json").write_text("{}", encoding="utf-8")
    # malformed: not JSON
    (tmp_path / "workflow-rules" / "broken.json").write_text("not json", encoding="utf-8")

    rules = store_mod.load_rules_for_connector("wa-main")
    assert [r["id"] for r in rules] == ["r1"]

    rules_gmail = store_mod.load_rules_for_connector("gmail")
    assert [r["id"] for r in rules_gmail] == ["r3"]

    assert store_mod.load_rules_for_connector("nonexistent") == []


def test_load_rules_missing_dir_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "void"))
    assert store_mod.load_rules_for_connector("wa-main") == []


# ─── Hook end-to-end ──────────────────────────────────────────────────────────


def test_hook_matched_rule_short_circuits_and_surfaces_template(
    caplog: pytest.LogCaptureFixture,
) -> None:
    template = {"title_he": "חשבונית חדשה", "body_he": "{{text}}"}
    rules = [
        {
            "id": "rule-invoice",
            "connector_id": "wa-main",
            "matcher_pattern": {"contains": "חשבונית"},
            "target_ticket_template": template,
        }
    ]

    caplog.set_level(logging.INFO, logger="agente.workflow.audit")
    outcome = hook_mod.evaluate(
        {"connector_id": "wa-main", "id": "evt-1", "text": "אנא שלחו חשבונית"},
        rules_loader=lambda _cid: rules,
    )
    assert outcome.short_circuit is True
    assert outcome.reason == hook_mod.OUTCOME_MATCHED
    assert outcome.matched_rule_id == "rule-invoice"
    assert outcome.target_ticket_template == template
    assert outcome.rules_considered == 1

    # Acceptance #4: audit line emitted (JSON with reason + connector).
    audit_records = [r for r in caplog.records if r.name == "agente.workflow.audit"]
    assert len(audit_records) == 1
    audit_payload = json.loads(audit_records[0].getMessage())
    assert audit_payload["reason"] == hook_mod.OUTCOME_MATCHED
    assert audit_payload["connector_id"] == "wa-main"
    assert audit_payload["matched_rule_id"] == "rule-invoice"
    assert audit_payload["short_circuit"] is True


def test_hook_no_match_falls_through(caplog: pytest.LogCaptureFixture) -> None:
    rules = [
        {
            "id": "rule-x",
            "connector_id": "wa-main",
            "matcher_pattern": {"contains": "neverhere"},
        }
    ]
    caplog.set_level(logging.INFO, logger="agente.workflow.audit")
    outcome = hook_mod.evaluate(
        {"connector_id": "wa-main", "text": "hi there"},
        rules_loader=lambda _cid: rules,
    )
    assert outcome.short_circuit is False
    assert outcome.reason == hook_mod.OUTCOME_NO_MATCH
    assert outcome.rules_considered == 1

    audit_records = [r for r in caplog.records if r.name == "agente.workflow.audit"]
    assert len(audit_records) == 1


def test_hook_no_rules_for_connector_falls_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="agente.workflow.audit")
    outcome = hook_mod.evaluate(
        {"connector_id": "wa-main", "text": "hi"},
        rules_loader=lambda _cid: [],
    )
    assert outcome.short_circuit is False
    assert outcome.reason == hook_mod.OUTCOME_NO_RULES


def test_hook_disabled_skips_loader(caplog: pytest.LogCaptureFixture) -> None:
    calls: List[str] = []

    def _loader(cid: str) -> List[Dict[str, Any]]:
        calls.append(cid)
        return [{"id": "x", "connector_id": cid, "matcher_pattern": "anything"}]

    caplog.set_level(logging.INFO, logger="agente.workflow.audit")
    outcome = hook_mod.evaluate(
        {"connector_id": "wa-main", "text": "anything"},
        rules_loader=_loader,
        enabled=False,
    )
    assert outcome.short_circuit is False
    assert outcome.reason == hook_mod.OUTCOME_DISABLED
    assert calls == []  # loader must not have been called

    audit_records = [r for r in caplog.records if r.name == "agente.workflow.audit"]
    assert len(audit_records) == 1


def test_hook_first_match_wins_with_multiple_rules() -> None:
    rules = [
        {"id": "r-first", "connector_id": "wa", "matcher_pattern": "foo",
         "target_ticket_template": {"t": 1}},
        {"id": "r-second", "connector_id": "wa", "matcher_pattern": "foo",
         "target_ticket_template": {"t": 2}},
    ]
    outcome = hook_mod.evaluate(
        {"connector_id": "wa", "text": "foo here"},
        rules_loader=lambda _cid: rules,
    )
    assert outcome.matched_rule_id == "r-first"
    assert outcome.target_ticket_template == {"t": 1}


def test_hook_evaluator_error_falls_through(caplog: pytest.LogCaptureFixture) -> None:
    def _boom(_cid: str) -> List[Dict[str, Any]]:
        raise RuntimeError("disk gone")

    caplog.set_level(logging.INFO, logger="agente.workflow.audit")
    outcome = hook_mod.evaluate(
        {"connector_id": "wa", "text": "hi"},
        rules_loader=_boom,
    )
    assert outcome.short_circuit is False
    assert outcome.reason == hook_mod.OUTCOME_ERROR
    assert "disk gone" in outcome.extra["error"]


def test_hook_event_without_connector_falls_through() -> None:
    outcome = hook_mod.evaluate(
        {"text": "hi"},
        rules_loader=lambda _cid: [{"id": "x", "matcher_pattern": "hi"}],
    )
    # no connector_id ⇒ no rules loaded ⇒ falls through (no short-circuit).
    assert outcome.short_circuit is False
    assert outcome.reason == hook_mod.OUTCOME_NO_RULES


def test_hook_end_to_end_with_real_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive evaluate() through the on-disk loader to prove the wiring."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_rule(tmp_path, {
        "id": "live-rule",
        "connector_id": "wa-main",
        "matcher_pattern": "חשבונית",
        "target_ticket_template": {"title_he": "חשבונית"},
        "enabled": True,
    })

    outcome = hook_mod.evaluate(
        {"connector_id": "wa-main", "text": "שלח חשבונית בבקשה"}
    )
    assert outcome.short_circuit is True
    assert outcome.matched_rule_id == "live-rule"
    assert outcome.target_ticket_template == {"title_he": "חשבונית"}
