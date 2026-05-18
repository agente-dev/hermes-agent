"""
Tests for agente_hermes_addon/api_routes.py — POST /api/plugins/agente/tools/run.

Three required scenarios (acceptance criteria):
1. Happy path: HermesAgentLoop emits a tool_call → 200 {tool_name, args, done: false}
2. No-tool-call: loop produces only text → 200 {done: true, final_text}
3. governance_lane=safe → enable_session_yolo called before loop runs

Additional:
4. governance_lane=locked → 403 with rationale='governance_locked'
5. Unauthenticated → 401 before any processing
"""
from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── Minimal stubs matching HermesAgentLoop AgentResult shape ────────────────


@dataclass
class _Fn:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _Fn
    type: str = "function"


@dataclass
class _AgentResult:
    messages: List[Dict[str, Any]]
    turns_used: int = 1
    finished_naturally: bool = True
    reasoning_per_turn: List[Any] = field(default_factory=list)
    tool_errors: List[Any] = field(default_factory=list)
    managed_state: Optional[Any] = None


class _MockLoop:
    """Stand-in for HermesAgentLoop — returns a preset result."""

    def __init__(self, result: _AgentResult, **kwargs: Any) -> None:
        self._result = result
        self.init_kwargs = kwargs

    async def run(self, messages: List[Dict[str, Any]]) -> _AgentResult:
        return self._result


# ─── Fake aiohttp Request/Response ───────────────────────────────────────────


class _FakeRequest:
    def __init__(self, body: Dict[str, Any]) -> None:
        self._body = body
        self.headers: Dict[str, str] = {}

    async def json(self) -> Dict[str, Any]:
        return self._body


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _tool_result(tool_name: str, args: Dict[str, Any]) -> _AgentResult:
    tc = _ToolCall(id="call_1", function=_Fn(name=tool_name, arguments=json.dumps(args)))
    return _AgentResult(
        messages=[
            {"role": "user", "content": "do it"},
            {"role": "assistant", "tool_calls": [tc], "content": None},
        ]
    )


def _text_result(text: str) -> _AgentResult:
    return _AgentResult(
        messages=[
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": text, "tool_calls": None},
        ]
    )


def _no_auth(req: Any) -> None:
    return None  # auth passes




# ─── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_tool_call_emitted():
    """HermesAgentLoop emits a tool_call → 200 with tool_name, args, done=false."""
    args_in = {"path": "/tmp/x.txt", "content": "hi"}
    loop_result = _tool_result("write_file", args_in)

    import agente_hermes_addon.api_routes as mod

    with (
        patch.object(mod, "HermesAgentLoop", side_effect=lambda **kw: _MockLoop(loop_result, **kw)),
        patch.object(mod, "get_tool_definitions", return_value=[
            {"function": {"name": "write_file", "parameters": {}}}
        ]),
        patch.object(mod, "_build_gateway_server", new_callable=AsyncMock),
    ):
        resp = await mod.handle_tools_run(
            _FakeRequest({"task": "Write", "input": "Write hi", "allowed_tools": ["write_file"]}),
            check_auth=_no_auth,
        )

    body = json.loads(resp.body)
    assert resp.status == 200
    assert body["done"] is False
    assert body["tool_name"] == "write_file"
    assert body["args"] == args_in


@pytest.mark.asyncio
async def test_no_tool_call_returns_final_text():
    """No tool_call emitted → 200 with done=true and final_text."""
    text = "I cannot help."
    loop_result = _text_result(text)

    import agente_hermes_addon.api_routes as mod

    with (
        patch.object(mod, "HermesAgentLoop", side_effect=lambda **kw: _MockLoop(loop_result, **kw)),
        patch.object(mod, "get_tool_definitions", return_value=[]),
        patch.object(mod, "_build_gateway_server", new_callable=AsyncMock),
    ):
        resp = await mod.handle_tools_run(
            _FakeRequest({"task": "Answer", "input": "What is 2+2?"}),
            check_auth=_no_auth,
        )

    body = json.loads(resp.body)
    assert resp.status == 200
    assert body["done"] is True
    assert body["final_text"] == text


@pytest.mark.asyncio
async def test_governance_lane_safe_enables_yolo():
    """governance_lane=safe calls enable_session_yolo before the loop runs; no approval raised."""
    loop_result = _text_result("ok")

    import agente_hermes_addon.api_routes as mod

    yolo_keys: list = []
    session_keys: list = []

    def _fake_set_session(k: str) -> object:
        session_keys.append(k)
        return object()

    def _fake_enable_yolo(k: str) -> None:
        yolo_keys.append(k)

    with (
        patch.object(mod, "set_current_session_key", _fake_set_session),
        patch.object(mod, "enable_session_yolo", _fake_enable_yolo),
        patch.object(mod, "disable_session_yolo", MagicMock()),
        patch.object(mod, "reset_current_session_key", MagicMock()),
        patch.object(mod, "HermesAgentLoop", side_effect=lambda **kw: _MockLoop(loop_result, **kw)),
        patch.object(mod, "get_tool_definitions", return_value=[]),
        patch.object(mod, "_build_gateway_server", new_callable=AsyncMock),
    ):
        resp = await mod.handle_tools_run(
            _FakeRequest({"task": "Run", "input": "Go", "governance_lane": "safe"}),
            check_auth=_no_auth,
        )

    assert len(session_keys) == 1, "set_current_session_key must be called once"
    assert len(yolo_keys) == 1, "enable_session_yolo must be called once"
    assert resp.status == 200


@pytest.mark.asyncio
async def test_governance_lane_locked_returns_403():
    """governance_lane=locked → 403 with rationale='governance_locked', no loop call."""
    import agente_hermes_addon.api_routes as mod

    loop_called = []
    with patch.object(mod, "HermesAgentLoop", side_effect=lambda **kw: loop_called.append(1)):
        resp = await mod.handle_tools_run(
            _FakeRequest({"task": "t", "input": "i", "governance_lane": "locked"}),
            check_auth=_no_auth,
        )

    body = json.loads(resp.body)
    assert resp.status == 403
    assert body["rationale"] == "governance_locked"
    assert not loop_called, "HermesAgentLoop must not be instantiated for locked lane"


@pytest.mark.asyncio
async def test_unauthenticated_request_returns_401():
    """check_auth rejection propagates without touching the agent loop."""
    import agente_hermes_addon.api_routes as mod

    auth_401 = mod.web.json_response({"error": "Invalid API key"}, status=401)

    loop_called = []
    with (
        patch.object(mod, "HermesAgentLoop", side_effect=lambda **kw: loop_called.append(1)),
        patch.object(mod, "_build_gateway_server", new_callable=AsyncMock),
        patch.object(mod, "get_tool_definitions", return_value=[]),
    ):
        resp = await mod.handle_tools_run(
            _FakeRequest({"task": "t", "input": "i"}),
            check_auth=lambda r: auth_401,
        )

    body = json.loads(resp.body)
    assert resp.status == 401
    assert not loop_called, "Loop must not run for unauthenticated requests"
