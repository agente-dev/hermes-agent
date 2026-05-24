from __future__ import annotations

import json

from plugins import agente_desktop


def test_agente_desktop_handler_returns_json_string(monkeypatch):
    payload = {
        "tools": [
            {
                "name": "list_tools",
                "description": "Enumerate desktop tools",
            }
        ],
        "count": 1,
    }

    def fake_proxy_call(tool_name, args):
        assert tool_name == "list_tools"
        assert args == {"scope": "desktop"}
        return payload

    monkeypatch.setattr(agente_desktop, "_proxy_call", fake_proxy_call)

    result = agente_desktop._make_handler("list_tools")(
        {"scope": "desktop"},
        task_id="session-1",
    )

    assert isinstance(result, str)
    assert json.loads(result) == payload


def test_agente_desktop_handler_serializes_error_payload(monkeypatch):
    monkeypatch.setattr(
        agente_desktop,
        "_proxy_call",
        lambda tool_name, args: {"error": "agente_tool_ipc_not_configured"},
    )

    result = agente_desktop._make_handler("list_tools")(
        {},
        task_id="session-1",
    )

    assert isinstance(result, str)
    assert json.loads(result) == {"error": "agente_tool_ipc_not_configured"}
