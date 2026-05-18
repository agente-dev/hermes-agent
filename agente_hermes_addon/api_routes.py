"""
Agente Desktop gateway plugin — single-turn tool-decision endpoint.

Registers POST /api/plugins/agente/tools/run via the api_server plugin hook.
Zero diff to upstream api_server.py main route list.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable, Dict, List, Optional, Set

from aiohttp import web

logger = logging.getLogger(__name__)

# Lazy-loaded gateway internals — imported on first request so unit tests can
# patch them without needing the full gateway environment installed.
try:
    from environments.agent_loop import HermesAgentLoop
except ImportError:  # pragma: no cover
    HermesAgentLoop = None  # type: ignore[assignment,misc]

try:
    from model_tools import get_tool_definitions
except ImportError:  # pragma: no cover
    get_tool_definitions = None  # type: ignore[assignment]

try:
    from tools.approval import (
        disable_session_yolo,
        enable_session_yolo,
        reset_current_session_key,
        set_current_session_key,
    )
except ImportError:  # pragma: no cover
    enable_session_yolo = None  # type: ignore[assignment]
    disable_session_yolo = None  # type: ignore[assignment]
    set_current_session_key = None  # type: ignore[assignment]
    reset_current_session_key = None  # type: ignore[assignment]


class _GatewayLLMServer:
    """AsyncOpenAI wrapper with the chat_completion() interface HermesAgentLoop expects."""

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self._model = model

    async def chat_completion(self, **kwargs: Any) -> Any:
        kwargs.setdefault("model", self._model)
        extra_body = kwargs.pop("extra_body", None)
        return await self._client.chat.completions.create(**kwargs, extra_body=extra_body)


async def _build_gateway_server() -> _GatewayLLMServer:
    """Create a gateway LLM server using the currently configured provider."""
    from openai import AsyncOpenAI

    from gateway.run import _resolve_gateway_model, _resolve_runtime_agent_kwargs

    kwargs = _resolve_runtime_agent_kwargs()
    model = _resolve_gateway_model()
    client = AsyncOpenAI(
        api_key=kwargs.get("api_key") or "no-key",
        base_url=kwargs.get("base_url") or None,
    )
    return _GatewayLLMServer(client, model)


async def handle_tools_run(
    request: Any,
    check_auth: Callable,
) -> Any:
    """POST /api/plugins/agente/tools/run — single-turn tool-decision."""
    auth_err = check_auth(request)
    if auth_err is not None:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    task: str = body.get("task", "")
    input_text: str = body.get("input", "")
    conversation: List[Dict[str, Any]] = body.get("conversation") or []
    governance_lane: str = body.get("governance_lane", "supervised")
    allowed_tools: List[str] = body.get("allowed_tools") or []

    if not task and not input_text:
        return web.json_response({"error": "Missing 'task' or 'input' field"}, status=400)

    if governance_lane == "locked":
        return web.json_response(
            {"error": "Governance lane is locked", "rationale": "governance_locked"},
            status=403,
        )

    session_key = str(uuid.uuid4())
    approval_token = None

    if governance_lane == "safe" and set_current_session_key is not None:
        approval_token = set_current_session_key(session_key)
        if enable_session_yolo is not None:
            enable_session_yolo(session_key)

    try:
        if HermesAgentLoop is None or get_tool_definitions is None:
            return web.json_response(
                {"error": "Agent loop not available in this environment"},
                status=503,
            )

        messages: List[Dict[str, Any]] = []
        if task:
            messages.append({"role": "system", "content": task})
        for msg in conversation:
            if isinstance(msg, dict) and msg.get("role") and msg.get("content"):
                messages.append({"role": msg["role"], "content": str(msg["content"])})
        messages.append({"role": "user", "content": input_text or task})

        all_schemas = get_tool_definitions(quiet_mode=True)
        if allowed_tools:
            allowed_set: Set[str] = set(allowed_tools)
            tool_schemas = [
                s for s in all_schemas
                if s.get("function", {}).get("name") in allowed_set
            ]
            valid_names: Set[str] = allowed_set
        else:
            tool_schemas = all_schemas
            valid_names = {s["function"]["name"] for s in tool_schemas}

        server = await _build_gateway_server()
        agent = HermesAgentLoop(
            server=server,
            tool_schemas=tool_schemas,
            valid_tool_names=valid_names,
            max_turns=1,
        )
        result = await agent.run(messages)

        tool_call: Optional[Any] = None
        final_text: Optional[str] = None
        for msg in result.messages:
            if msg.get("role") == "assistant":
                tcs = msg.get("tool_calls")
                if tcs:
                    tool_call = tcs[0]
                    break
                if msg.get("content") and not final_text:
                    final_text = msg["content"]

        if tool_call is not None:
            fn: Any = (
                tool_call.get("function", {})
                if isinstance(tool_call, dict)
                else getattr(tool_call, "function", {})
            )
            fn_name: str = fn.get("name", "") if isinstance(fn, dict) else getattr(fn, "name", "")
            raw_args: Any = fn.get("arguments", "{}") if isinstance(fn, dict) else getattr(fn, "arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (json.JSONDecodeError, TypeError):
                args = {}
            return web.json_response({
                "tool_name": fn_name,
                "args": args,
                "done": False,
                "rationale": None,
                "args_summary": str(args),
            })

        return web.json_response({
            "done": True,
            "final_text": final_text or "",
        })

    finally:
        if approval_token is not None and disable_session_yolo is not None:
            disable_session_yolo(session_key)
            if reset_current_session_key is not None:
                reset_current_session_key(approval_token)


def register_routes(app: Any, check_auth: Callable) -> None:
    """Register agente plugin routes on the aiohttp application."""
    async def _handler(request: web.Request) -> web.Response:
        return await handle_tools_run(request, check_auth)

    app.router.add_post("/api/plugins/agente/tools/run", _handler)
    logger.info("[agente-addon] Registered POST /api/plugins/agente/tools/run")
