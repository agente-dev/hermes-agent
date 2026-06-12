"""
test_api_server_desktop_compat.py

Compatibility tests for the agente_desktop_adapter boundary.

These must pass after every upstream sync (see contract test_suite and plan §4.2 step 4, §5 coverage table).
"""
from __future__ import annotations

import json
from typing import Any

import pytest

# We test the moved adapter modules directly (no longer via old plugin package).
from gateway.agente_desktop_adapter import (
    tool_discovery,
    jsonrpc_compat,
    workflow_routine_bridge,
    byok_session_aliases,
    whatsapp_ticket_integration,
)
from gateway.agente_desktop_adapter.tool_discovery import TOOL_SCHEMAS, _proxy_call, _check_available, _make_handler

EXPECTED_DESKTOP_IPC_TOOL_NAMES = {
    "data_read",
    "data_write",
    "list_whatsapp_accounts",
    "list_recent_messages",
    "create_ticket",
    "move_ticket",
    "assign_ticket",
    "list_tickets",
    "save_triage_instructions",
    "evaluate_triage_rules",
    "request_approval",
    "get_office_context",
    "update_office_context",
    "list_tools",
    "list_workflows",
    "inspect_workflow",
    "start_workflow_run",
    "get_run_status",
    "resume_paused_run",
    "save_executable_workflow",
    "query_client",
    "upsert_client",
    "resolve_or_upsert_client",
    "link_ticket_to_client",
    "link_document_to_client",
    "read_file",
    "read_document",
    "write_file",
    "list_directory",
    "scan_folder",
    "list_web_connectors",
    "browse_connector",
    "list_routines",
    "get_routine",
    "run_routine",
    "pause_routine",
    "resume_routine",
    "delete_routine",
    "suggest_client_tip",
    "connect_google",
    "connect_anthropic",
    "connect_openai",
    "check_connector_status",
    "download_whatsapp_media",
    "list_agent_profiles",
    "create_agent_profile",
}


def test_tool_discovery_shape():
    # Basic shape from the moved schemas.
    assert isinstance(TOOL_SCHEMAS, dict)
    assert set(TOOL_SCHEMAS) == EXPECTED_DESKTOP_IPC_TOOL_NAMES
    assert "list_whatsapp_accounts" in TOOL_SCHEMAS
    assert "download_whatsapp_media" in TOOL_SCHEMAS
    assert "read_document" in TOOL_SCHEMAS
    assert "evaluate_triage_rules" in TOOL_SCHEMAS
    assert "create_routine" not in TOOL_SCHEMAS
    assert "save_workflow" not in TOOL_SCHEMAS
    assert "save_workflow_rule" not in TOOL_SCHEMAS
    assert "save_triage_instructions" in TOOL_SCHEMAS  # deprecated but present for back-compat
    for name, sch in TOOL_SCHEMAS.items():
        assert sch.get("name") == name
        assert "parameters" in sch


def test_tool_invocation_roundtrip(monkeypatch):
    # The IPC proxy must never raise; always returns dict (error or result).
    # Simulate no env -> configured error.
    monkeypatch.delenv("AGENTE_TOOL_PORT", raising=False)
    monkeypatch.delenv("AGENTE_TOOL_SECRET", raising=False)
    res = _proxy_call("list_tools", {})
    assert isinstance(res, dict)
    assert res.get("error") == "agente_tool_ipc_not_configured"

    # With envs (fake), the _proxy_call will attempt HTTP; we don't want real net in unit test,
    # so monkey the urllib inside the module.
    monkeypatch.setenv("AGENTE_TOOL_PORT", "12345")
    monkeypatch.setenv("AGENTE_TOOL_SECRET", "deadbeef" * 4)

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return json.dumps({"ok": True, "result": {"pong": True}}).encode()
        return _R()

    import urllib.request as ureq
    monkeypatch.setattr(ureq, "urlopen", fake_urlopen)

    res2 = _proxy_call("ping", {"x": 1})
    assert res2 == {"pong": True}

    # handler wraps to json str
    h = _make_handler("ping")
    out = h({"x": 1})
    assert json.loads(out) == {"pong": True}


def test_jsonrpc_alias_handling():
    body = {"id": 1, "method": "auth.submit_oauth_code", "params": {"request_id": "r123", "code": "c"}}
    out = jsonrpc_compat._apply_jsonrpc_desktop_aliases(body)
    assert out["params"]["session_id"] == "r123"
    assert "request_id" not in out["params"] or out["params"].get("request_id") == "r123"  # original may stay


def test_save_workflow_schema():
    # normalization lives in workflow_routine_bridge
    args = {"name": "My WF", "steps": [{"tool": "foo"}]}
    norm = workflow_routine_bridge._normalize_desktop_tool_args("save_workflow", args)
    assert "id" in norm and norm["id"].startswith("wf-")
    assert norm["name_he"] == "My WF"
    assert norm["actions"][0]["kind"] == "foo"


def test_create_routine_schema():
    args = {"name": "r1", "cron": "0 * * * *"}
    norm = workflow_routine_bridge._normalize_desktop_tool_args("create_routine", args)
    assert norm["id"].startswith("rt-")
    assert norm["cron_schedule"] == "0 * * * *"


def test_byok_session_alias():
    # basic alias presence via jsonrpc (tested more in test_jsonrpc)
    assert hasattr(jsonrpc_compat, "_apply_jsonrpc_desktop_aliases")
    byok_session_aliases.register(None, None)  # smoke, should not crash


def test_create_ticket_schema():
    assert "create_ticket" in TOOL_SCHEMAS
    sch = TOOL_SCHEMAS["create_ticket"]
    assert "title" in sch["parameters"]["required"]


def test_whatsapp_triage_ticket():
    # WhatsApp + ticket + triage tools are all present via the bridge
    for name in ("list_whatsapp_accounts", "create_ticket", "evaluate_triage_rules"):
        assert name in TOOL_SCHEMAS
    # The integration module registers (delegates)
    whatsapp_ticket_integration.register(None, None)


def test_agente_desktop_handlers_accept_registry_context_kwargs(monkeypatch):
    """Regression for handler dispatch context (moved from plugins/agente_desktop/tests/)."""
    from tools.registry import ToolRegistry

    class _RegistryContext:
        def __init__(self, registry: ToolRegistry) -> None:
            self.registry = registry

        def register_tool(self, **kwargs: Any) -> None:
            self.registry.register(**kwargs)

    registry = ToolRegistry()
    monkeypatch.setattr(tool_discovery, "_check_available", lambda: True)

    def fake_proxy_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "tool_name": tool_name, "args": args}

    monkeypatch.setattr(tool_discovery, "_proxy_call", fake_proxy_call)

    # Simulate what the old plugin register did, using the adapter's registration path.
    # We call the internal loop directly.
    for tool_name, schema in list(tool_discovery.TOOL_SCHEMAS.items())[:3]:  # spot check a few
        registry.register(
            name=tool_name,
            toolset=tool_discovery.TOOLSET,
            schema=schema,
            handler=tool_discovery._make_handler(tool_name),
            check_fn=tool_discovery._check_available,
            description=schema.get("description", ""),
            emoji=tool_discovery._TOOL_EMOJIS.get(tool_name, "🔌"),
        )

    for tool_name in list(tool_discovery.TOOL_SCHEMAS.keys())[:3]:
        args = {"probe": tool_name}
        result = registry.dispatch(
            tool_name,
            args,
            task_id="task-x",
            session_id="session-y",
            tool_call_id="call-z",
            parent_agent=None,
        )
        parsed = json.loads(result) if isinstance(result, str) else result
        assert parsed == {"ok": True, "tool_name": tool_name, "args": args}


@pytest.mark.asyncio
async def test_api_tools_list_does_not_require_adapter_registry_loader(monkeypatch):
    import tools.registry as registry_mod

    class _Entry:
        toolset = "agente-desktop"
        label_he = "probe"
        category = "test"

    class _Registry:
        def get_all_tool_names(self) -> list[str]:
            return ["probe_tool"]

        def get_definitions(self, names: set[str], quiet: bool = False) -> list[dict[str, Any]]:  # noqa: ARG002
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "probe_tool",
                        "description": "Probe tool",
                        "parameters": {"type": "object"},
                    },
                }
            ]

        def get_entry(self, name: str) -> _Entry | None:
            return _Entry() if name == "probe_tool" else None

    class _AdapterWithoutLoader:
        def _check_auth(self, request: Any) -> None:  # noqa: ARG002
            return None

    monkeypatch.setattr(registry_mod, "registry", _Registry())

    response = await tool_discovery._handle_list_tools(object(), _AdapterWithoutLoader())
    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["tools"][0]["name"] == "probe_tool"
    assert payload["tools"][0]["toolset"] == "agente-desktop"


@pytest.mark.asyncio
async def test_api_tools_list_survives_tool_entry_without_category(monkeypatch):
    """Regression: real ToolEntry uses __slots__ with NO `category` (or
    `label_he`) slot, so attribute access raises AttributeError rather than
    returning None. A bare `entry.category` therefore crashed
    _handle_list_tools -> /api/tools 500 -> gateway health check failure ->
    "Started 0/5 profile gateways". The guard must use getattr(..., None)."""
    import tools.registry as registry_mod
    from tools.registry import ToolEntry

    # Construct a genuine ToolEntry to exercise the actual __slots__ shape that
    # ships in the bundle (no `category`, no `label_he`).
    real_entry = ToolEntry(
        name="probe_tool",
        toolset="agente-desktop",
        schema={"type": "object"},
        handler=lambda **_: None,
        check_fn=None,
        requires_env=None,
        is_async=False,
        description="Probe tool",
        emoji=None,
    )
    assert not hasattr(real_entry, "category")
    assert not hasattr(real_entry, "label_he")

    class _Registry:
        def get_all_tool_names(self) -> list[str]:
            return ["probe_tool"]

        def get_definitions(self, names: set[str], quiet: bool = False) -> list[dict[str, Any]]:  # noqa: ARG002
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "probe_tool",
                        "description": "Probe tool",
                        "parameters": {"type": "object"},
                    },
                }
            ]

        def get_entry(self, name: str) -> ToolEntry | None:
            return real_entry if name == "probe_tool" else None

    class _AdapterWithoutLoader:
        def _check_auth(self, request: Any) -> None:  # noqa: ARG002
            return None

    monkeypatch.setattr(registry_mod, "registry", _Registry())

    response = await tool_discovery._handle_list_tools(object(), _AdapterWithoutLoader())
    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["tools"][0]["name"] == "probe_tool"
    assert payload["tools"][0]["toolset"] == "agente-desktop"
    # category/label_he simply omitted when the slot is absent.
    assert "category" not in payload["tools"][0]
    assert "label_he" not in payload["tools"][0]


def test_evaluate_triage_rules_schema_present():
    schema = TOOL_SCHEMAS.get("evaluate_triage_rules")
    assert schema is not None, "evaluate_triage_rules missing"
    assert schema["name"] == "evaluate_triage_rules"
    params = schema["parameters"]
    assert params["type"] == "object"
    assert set(params["required"]) == {"source", "type"}
    props = params["properties"]
    for key in ("source", "type", "text", "metadata"):
        assert key in props, f"missing property {key}"
    assert props["metadata"]["type"] == "object"


def test_download_whatsapp_media_schema_requires_chat_jid():
    schema = TOOL_SCHEMAS.get("download_whatsapp_media")
    assert schema is not None, "download_whatsapp_media missing"
    assert schema["name"] == "download_whatsapp_media"
    params = schema["parameters"]
    assert params["required"] == [
        "account_id",
        "message_id",
        "chat_jid",
        "media_type",
        "dest_path",
    ]
    assert params["additionalProperties"] is False
    props = params["properties"]
    assert set(props) == {"account_id", "message_id", "chat_jid", "media_type", "dest_path"}
    assert "list_recent_messages" in props["chat_jid"]["description"]
    assert "REST download endpoint" in props["chat_jid"]["description"]


def test_read_document_schema_present_and_current():
    schema = TOOL_SCHEMAS.get("read_document")
    assert schema is not None, "read_document missing"
    assert schema["name"] == "read_document"
    params = schema["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["path"]
    props = params["properties"]
    assert {"path", "maxChars", "pageRange"} <= set(props)
    assert props["pageRange"]["properties"]["start"]["type"] in {"integer", "number"}
    assert props["pageRange"]["properties"]["end"]["type"] in {"integer", "number"}


def test_save_workflow_rule_schema_absent():
    assert "save_workflow_rule" not in TOOL_SCHEMAS
    assert "save_workflow_rule" not in tool_discovery._TOOL_EMOJIS


def test_save_triage_instructions_still_registered_as_deprecated():
    schema = TOOL_SCHEMAS.get("save_triage_instructions")
    assert schema is not None, "save_triage_instructions should remain for back-compat"
    assert "DEPRECATED" in schema["description"]
    assert "save_workflow + create_routine" in schema["description"]


def test_total_tool_count_matches():
    assert len(TOOL_SCHEMAS) == 46
