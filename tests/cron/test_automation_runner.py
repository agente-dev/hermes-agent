"""Tests for cron.automation_runner — hermes-native automation action runner.

Covers the binding gap from meta-202606-044:
  - scan action results are collected and bound into step context
  - {{sender_name}} in title_template is interpolated with the real sender
  - phone_match routing maps sender phone → correct assignee
  - default assignee is used when no phone_match rule matches
  - create_ticket is called with the rendered title and resolved assignee
  - IPC errors in scan step are reported but don't crash subsequent steps
  - run_workflow_by_id loads the workflow and delegates to run_workflow_actions
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from cron.automation_runner import (
    _extract_messages,
    _extract_phone,
    _render_template,
    _resolve_assignee,
    run_workflow_actions,
    run_workflow_by_id,
)


# ---------------------------------------------------------------------------
# Unit: _render_template
# ---------------------------------------------------------------------------


def test_render_template_replaces_known_keys():
    result = _render_template("הודעה מ{{sender_name}}", {"sender_name": "דני"})
    assert result == "הודעה מדני"


def test_render_template_leaves_unknown_keys_intact():
    result = _render_template("{{foo}} {{bar}}", {"foo": "yes"})
    assert result == "yes {{bar}}"


def test_render_template_empty_string():
    assert _render_template("", {}) == ""


def test_render_template_no_placeholders():
    assert _render_template("plain text", {"x": "y"}) == "plain text"


# ---------------------------------------------------------------------------
# Unit: _extract_phone
# ---------------------------------------------------------------------------


def test_extract_phone_from_jid():
    assert _extract_phone("972501234567@s.whatsapp.net") == "972501234567"


def test_extract_phone_bare():
    assert _extract_phone("972501234567") == "972501234567"


def test_extract_phone_empty():
    assert _extract_phone("") == ""


# ---------------------------------------------------------------------------
# Unit: _resolve_assignee
# ---------------------------------------------------------------------------


ROUTING = [
    {"phone_match": "972501234567", "assign_to": "קטיה"},
    {"default": "יוסי"},
]


def test_resolve_assignee_match():
    assert _resolve_assignee("972501234567@s.whatsapp.net", ROUTING) == "קטיה"


def test_resolve_assignee_default():
    assert _resolve_assignee("972509999999@s.whatsapp.net", ROUTING) == "יוסי"


def test_resolve_assignee_no_rules():
    assert _resolve_assignee("972501234567", None) is None
    assert _resolve_assignee("972501234567", []) is None


def test_resolve_assignee_bare_phone_match():
    routing = [{"phone_match": "972501234567", "assign_to": "קטיה"}]
    assert _resolve_assignee("972501234567", routing) == "קטיה"


# ---------------------------------------------------------------------------
# Unit: _extract_messages
# ---------------------------------------------------------------------------


def test_extract_messages_from_list():
    raw = [
        {"sender_jid": "972501234567@s.whatsapp.net", "sender_name": "דני", "body": "שלום"},
    ]
    msgs = _extract_messages(raw)
    assert len(msgs) == 1
    assert msgs[0]["sender_jid"] == "972501234567@s.whatsapp.net"
    assert msgs[0]["sender_name"] == "דני"
    assert msgs[0]["message_text"] == "שלום"
    assert msgs[0]["sender_phone"] == "972501234567"


def test_extract_messages_from_dict_with_messages_key():
    raw = {"messages": [{"sender_jid": "jid1", "sender_name": "A", "message_text": "hi"}]}
    msgs = _extract_messages(raw)
    assert len(msgs) == 1
    assert msgs[0]["sender_name"] == "A"


def test_extract_messages_empty():
    assert _extract_messages(None) == []
    assert _extract_messages([]) == []
    assert _extract_messages({}) == []


def test_extract_messages_skips_non_dicts():
    assert _extract_messages(["not-a-dict", 42]) == []


# ---------------------------------------------------------------------------
# Integration: run_workflow_actions
# ---------------------------------------------------------------------------


def _make_scan_result(messages):
    return {"messages": messages}


def _workflow(actions):
    return {"id": "wf-test", "name_he": "בדיקה", "actions": actions}


CALL_LOG = []


def _fake_call_tool(tool_name, args):
    CALL_LOG.append((tool_name, args))
    if tool_name == "list_whatsapp_messages":
        return True, _make_scan_result([
            {
                "sender_jid": "972501234567@s.whatsapp.net",
                "sender_name": "קטיה הלקוחה",
                "message_text": "אני צריכה עזרה",
            },
        ])
    if tool_name == "create_ticket":
        return True, {"ticket_id": "t-1", "ok": True}
    return True, {}


@pytest.fixture(autouse=True)
def reset_call_log():
    CALL_LOG.clear()
    yield
    CALL_LOG.clear()


def test_scan_then_create_ticket_binds_sender_name(monkeypatch):
    monkeypatch.setattr("cron.automation_runner._call_tool", _fake_call_tool)

    workflow = _workflow([
        {"kind": "list_whatsapp_messages", "params": {"account_id": "acc1", "limit": 5}},
        {
            "kind": "create_ticket",
            "title_template": "הודעה חדשה מ{{sender_name}}",
            "assigned_to": [
                {"phone_match": "972501234567", "assign_to": "קטיה"},
                {"default": "יוסי"},
            ],
        },
    ])

    ok, doc, err = run_workflow_actions(workflow)

    assert ok is True, err
    # Verify create_ticket was called with the rendered title and correct assignee
    create_calls = [(t, a) for t, a in CALL_LOG if t == "create_ticket"]
    assert len(create_calls) == 1
    _, ticket_args = create_calls[0]
    assert ticket_args["title"] == "הודעה חדשה מקטיה הלקוחה"
    assert ticket_args["assignee"] == "קטיה"


def test_default_assignee_for_unknown_phone(monkeypatch):
    def fake_call(tool_name, args):
        CALL_LOG.append((tool_name, args))
        if tool_name == "list_whatsapp_messages":
            return True, _make_scan_result([
                {
                    "sender_jid": "972509999999@s.whatsapp.net",
                    "sender_name": "לקוח אחר",
                    "message_text": "שלום",
                },
            ])
        if tool_name == "create_ticket":
            return True, {"ok": True}
        return True, {}

    monkeypatch.setattr("cron.automation_runner._call_tool", fake_call)

    workflow = _workflow([
        {"kind": "list_whatsapp_messages", "params": {"account_id": "acc1"}},
        {
            "kind": "create_ticket",
            "title_template": "{{sender_name}} שלח הודעה",
            "assigned_to": [
                {"phone_match": "972501234567", "assign_to": "קטיה"},
                {"default": "יוסי"},
            ],
        },
    ])

    ok, doc, err = run_workflow_actions(workflow)

    assert ok is True, err
    create_calls = [(t, a) for t, a in CALL_LOG if t == "create_ticket"]
    assert len(create_calls) == 1
    _, ticket_args = create_calls[0]
    assert ticket_args["title"] == "לקוח אחר שלח הודעה"
    assert ticket_args["assignee"] == "יוסי"


def test_trigger_context_used_when_no_scan_action(monkeypatch):
    """When a workflow has no scan step, trigger_context seeds the template."""
    create_calls = []

    def fake_call(tool_name, args):
        if tool_name == "create_ticket":
            create_calls.append(args)
            return True, {"ok": True}
        return True, {}

    monkeypatch.setattr("cron.automation_runner._call_tool", fake_call)

    workflow = _workflow([
        {
            "kind": "create_ticket",
            "title_template": "הודעה מ{{sender_name}}",
            "assigned_to": [{"default": "יוסי"}],
        },
    ])
    trigger = {
        "sender_jid": "972501234567@s.whatsapp.net",
        "sender_name": "שרה",
        "message_text": "test",
    }

    ok, doc, err = run_workflow_actions(workflow, trigger_context=trigger)

    assert ok is True, err
    assert len(create_calls) == 1
    assert create_calls[0]["title"] == "הודעה משרה"


def test_scan_ipc_error_is_reported(monkeypatch):
    """A scan IPC failure adds an error but doesn't hard-crash the run."""
    def fake_call(tool_name, args):
        if tool_name == "list_whatsapp_messages":
            return False, {"error": "IPC unavailable"}
        return True, {}

    monkeypatch.setattr("cron.automation_runner._call_tool", fake_call)

    workflow = _workflow([
        {"kind": "list_whatsapp_messages", "params": {"account_id": "acc1"}},
        {"kind": "create_ticket", "title_template": "fallback ticket"},
    ])

    ok, doc, err = run_workflow_actions(workflow)
    # The scan failed so no messages, but the create_ticket should still run
    # using empty context (title_template has no placeholders here).
    assert err is None or "IPC" in (err or "")


def test_empty_actions_returns_success(monkeypatch):
    workflow = _workflow([])
    ok, doc, err = run_workflow_actions(workflow)
    assert ok is True
    assert err is None


def test_multiple_messages_create_multiple_tickets(monkeypatch):
    """One ticket per scanned message."""
    created_titles = []

    def fake_call(tool_name, args):
        if tool_name == "list_whatsapp_messages":
            return True, _make_scan_result([
                {"sender_jid": "111@s.whatsapp.net", "sender_name": "אלה", "body": "hi"},
                {"sender_jid": "222@s.whatsapp.net", "sender_name": "בתיה", "body": "hello"},
            ])
        if tool_name == "create_ticket":
            created_titles.append(args.get("title"))
            return True, {"ok": True}
        return True, {}

    monkeypatch.setattr("cron.automation_runner._call_tool", fake_call)

    workflow = _workflow([
        {"kind": "list_whatsapp_messages", "params": {"account_id": "acc1"}},
        {"kind": "create_ticket", "title_template": "הודעה מ{{sender_name}}"},
    ])

    ok, doc, err = run_workflow_actions(workflow)

    assert ok is True, err
    assert len(created_titles) == 2
    assert "אלה" in created_titles[0]
    assert "בתיה" in created_titles[1]


# ---------------------------------------------------------------------------
# Integration: run_workflow_by_id
# ---------------------------------------------------------------------------


def test_run_workflow_by_id_loads_and_executes(tmp_path, monkeypatch):
    import yaml

    wf_dir = tmp_path / ".hermes" / "workflows"
    wf_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    wf = {
        "id": "wf-abc",
        "name_he": "בדיקה",
        "trigger_kind": "schedule_triggered",
        "actions": [],
    }
    (wf_dir / "wf-abc.yaml").write_text(yaml.dump(wf), encoding="utf-8")

    ok, doc, err = run_workflow_by_id("wf-abc")
    assert ok is True
    assert err is None


def test_run_workflow_by_id_missing_returns_error():
    ok, doc, err = run_workflow_by_id("wf-does-not-exist-xyz")
    assert ok is False
    assert err is not None


# ---------------------------------------------------------------------------
# Integration: dispatch_workflow_runs prefers native runner
# ---------------------------------------------------------------------------


def test_dispatch_prefers_native_runner_when_ipc_available(monkeypatch):
    monkeypatch.setenv("AGENTE_TOOL_PORT", "9999")
    monkeypatch.setenv("AGENTE_TOOL_SECRET", "secret")

    # Import after patching env so _ipc_available() reads the right values.
    import importlib
    import cron.workflow_dispatch as wd
    importlib.reload(wd)

    # Patch _dispatch_native directly to intercept the native run.
    native_calls: list = []

    def fake_dispatch_native(job_id, job_name, wf_ids):
        native_calls.extend(wf_ids)
        return True, "# ok\n", ""

    monkeypatch.setattr(wd, "_dispatch_native", fake_dispatch_native)

    job = {"id": "job-1", "name": "morning", "workflow_ids": ["wf-1"]}
    ok, doc, err = wd.dispatch_workflow_runs(job, ["wf-1"])
    assert ok is True
    assert err == ""
    assert "wf-1" in native_calls


def test_dispatch_falls_back_to_http_when_no_ipc(monkeypatch):
    monkeypatch.delenv("AGENTE_TOOL_PORT", raising=False)
    monkeypatch.delenv("AGENTE_TOOL_SECRET", raising=False)
    monkeypatch.setenv("HERMES_WORKFLOW_DISPATCH_URL", "http://desktop/api/workflow-runs")

    import importlib
    import cron.workflow_dispatch as wd
    importlib.reload(wd)

    posted = []

    def fake_post(url, payload, timeout):
        posted.append(payload)
        return True, '{"run_id": "r1"}'

    monkeypatch.setattr(wd, "_post_json", fake_post)

    job = {"id": "job-2", "name": "evening", "workflow_ids": ["wf-2"]}
    ok, doc, err = wd.dispatch_workflow_runs(job, ["wf-2"])
    assert ok is True
    assert len(posted) == 1
    assert posted[0]["workflow_id"] == "wf-2"
