"""Regression tests for agente_desktop plugin handler dispatch context."""

from __future__ import annotations

from typing import Any

from plugins import agente_desktop
from tools.registry import ToolRegistry


class _RegistryContext:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def register_tool(self, **kwargs: Any) -> None:
        self.registry.register(**kwargs)


def test_agente_desktop_handlers_accept_registry_context_kwargs(monkeypatch):
    registry = ToolRegistry()
    monkeypatch.setattr(agente_desktop, "_check_available", lambda: True)

    def fake_proxy_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "tool_name": tool_name, "args": args}

    monkeypatch.setattr(agente_desktop, "_proxy_call", fake_proxy_call)

    agente_desktop.register(_RegistryContext(registry))

    for tool_name in sorted(agente_desktop.TOOL_SCHEMAS):
        args = {"probe": tool_name}
        result = registry.dispatch(
            tool_name,
            args,
            task_id="task-x",
            session_id="session-y",
            tool_call_id="call-z",
            parent_agent=None,
        )

        assert result == {"ok": True, "tool_name": tool_name, "args": args}
