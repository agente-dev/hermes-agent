"""Single-turn agent loop for the agente tool-decision endpoint.

Wraps a single LLM chat_completion call via the gateway server object.
max_turns is accepted for API compatibility but only turn 1 is used.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class _AgentResult:
    messages: List[Dict[str, Any]]
    turns_used: int = 1
    finished_naturally: bool = True
    reasoning_per_turn: List[Any] = field(default_factory=list)
    tool_errors: List[Any] = field(default_factory=list)
    managed_state: Optional[Any] = None


class HermesAgentLoop:
    """One-shot LLM call that returns tool decision or final text.

    Calls server.chat_completion once with the supplied tool_schemas,
    parses the response into _AgentResult.messages, and returns without
    executing any tools.  The caller is responsible for tool execution.
    """

    def __init__(
        self,
        server: Any,
        tool_schemas: List[Dict[str, Any]],
        valid_tool_names: Any = None,
        max_turns: int = 1,
    ) -> None:
        self._server = server
        self._tool_schemas = tool_schemas
        self._valid_tool_names = valid_tool_names
        self._max_turns = max_turns

    async def run(self, messages: List[Dict[str, Any]]) -> _AgentResult:
        call_kwargs: Dict[str, Any] = {"messages": messages}
        if self._tool_schemas:
            # Ensure each schema has "type": "function" as OpenAI requires.
            normalized = []
            for s in self._tool_schemas:
                if "type" not in s:
                    s = {"type": "function", **s}
                normalized.append(s)
            call_kwargs["tools"] = normalized

        response = await self._server.chat_completion(**call_kwargs)
        choice = response.choices[0]
        msg = choice.message

        msg_dict: Dict[str, Any] = {
            "role": "assistant",
            "content": msg.content,
        }
        raw_tool_calls = getattr(msg, "tool_calls", None)
        if raw_tool_calls:
            tcs = []
            for tc in raw_tool_calls:
                fn = getattr(tc, "function", None)
                tcs.append({
                    "id": getattr(tc, "id", ""),
                    "type": getattr(tc, "type", "function"),
                    "function": {
                        "name": getattr(fn, "name", "") if fn else "",
                        "arguments": getattr(fn, "arguments", "{}") if fn else "{}",
                    },
                })
            msg_dict["tool_calls"] = tcs

        out_messages = list(messages) + [msg_dict]
        return _AgentResult(messages=out_messages)
