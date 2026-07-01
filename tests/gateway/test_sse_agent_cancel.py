"""Tests for SSE client disconnect → agent task cancellation.

When a streaming /v1/chat/completions client disconnects mid-stream
(network drop, browser tab close), the agent is interrupted via
agent.interrupt() so it stops making LLM API calls, and the asyncio
task wrapper is cancelled.
"""

import asyncio
import json
import queue
from unittest.mock import AsyncMock, MagicMock, patch



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter():
    """Build a minimal APIServerAdapter with mocked internals."""
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.config import PlatformConfig

    config = PlatformConfig(enabled=True, token="test-key")
    adapter = APIServerAdapter(config)
    return adapter


def _make_request():
    """Build a mock aiohttp request."""
    req = MagicMock()
    req.headers = {}
    return req


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSSEAgentCancelOnDisconnect:
    """gateway/platforms/api_server.py — _write_sse_chat_completion()"""

    def test_agent_task_cancelled_on_client_disconnect(self):
        """When response.write raises ConnectionResetError (client dropped),
        the agent task must be cancelled."""
        adapter = _make_adapter()

        stream_q = queue.Queue()
        stream_q.put("hello ")  # Some data already queued

        # Agent task that runs forever (simulates a long LLM call)
        agent_done = asyncio.Event()

        async def fake_agent():
            await agent_done.wait()
            return {"final_response": "done"}, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())

            # Mock response that raises ConnectionResetError on second write
            mock_response = AsyncMock(spec=web.StreamResponse)
            call_count = 0

            async def write_side_effect(data):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise ConnectionResetError("client disconnected")

            mock_response.write = AsyncMock(side_effect=write_side_effect)
            mock_response.prepare = AsyncMock()

            with patch.object(type(adapter), '_write_sse_chat_completion',
                              adapter._write_sse_chat_completion):
                # Patch StreamResponse creation
                with patch("gateway.platforms.api_server.web.StreamResponse",
                           return_value=mock_response):
                    await adapter._write_sse_chat_completion(
                        _make_request(), "cmpl-123", "gpt-4", 1234567890,
                        stream_q, agent_task,
                    )

            # The critical assertion: agent_task must be cancelled
            assert agent_task.cancelled() or agent_task.done()
            # Clean up
            agent_done.set()

        asyncio.run(run())

    def test_agent_task_not_cancelled_on_normal_completion(self):
        """On normal stream completion, agent task should NOT be cancelled."""
        adapter = _make_adapter()

        stream_q = queue.Queue()
        stream_q.put("hello")
        stream_q.put(None)  # End-of-stream sentinel

        async def fake_agent():
            return {"final_response": "done"}, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())
            await asyncio.sleep(0)  # Let agent complete

            mock_response = AsyncMock(spec=web.StreamResponse)
            mock_response.write = AsyncMock()
            mock_response.prepare = AsyncMock()

            with patch("gateway.platforms.api_server.web.StreamResponse",
                       return_value=mock_response):
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-456", "gpt-4", 1234567890,
                    stream_q, agent_task,
                )

            # Agent should have completed normally, not been cancelled
            assert agent_task.done()
            assert not agent_task.cancelled()

        asyncio.run(run())

    def test_agent_task_auth_failure_emits_error_not_successful_stop(self):
        """Agent exceptions must surface as SSE errors instead of empty success streams."""
        adapter = _make_adapter()

        stream_q = queue.Queue()
        stream_q.put(None)

        async def fake_agent():
            raise RuntimeError("401 Unauthorized")

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())
            await asyncio.sleep(0)

            writes: list[str] = []
            mock_response = AsyncMock(spec=web.StreamResponse)

            async def write_side_effect(data):
                writes.append(data.decode() if isinstance(data, bytes) else str(data))

            mock_response.write = AsyncMock(side_effect=write_side_effect)
            mock_response.prepare = AsyncMock()

            with patch(
                "gateway.platforms.api_server.web.StreamResponse",
                return_value=mock_response,
            ):
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-auth", "gpt-4", 1234567890,
                    stream_q, agent_task,
                )

            data_frames = [
                line.removeprefix("data: ")
                for chunk in writes
                for line in chunk.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            parsed_frames = [json.loads(frame) for frame in data_frames]
            assert any(
                frame.get("error", {}).get("type") == "upstream_auth_failed"
                and frame.get("error", {}).get("code") == 401
                for frame in parsed_frames
            )
            finish_reasons = [
                choice.get("finish_reason")
                for frame in parsed_frames
                for choice in frame.get("choices", [])
            ]
            assert "error" in finish_reasons
            assert "stop" not in finish_reasons

        asyncio.run(run())

    def test_agent_dict_failed_result_emits_error_frame(self):
        """Agent returning {"final_response": None, "failed": True, "error": "401..."} must
        surface as an upstream_auth_failed SSE error frame — not a silent empty stop chunk.

        This is the dict-return path: 401/400 non-retryable errors return a
        failed dict instead of raising, so the except block never fires.
        Without the fix the client would receive only a "stop" finish chunk.
        """
        adapter = _make_adapter()

        stream_q = queue.Queue()
        stream_q.put(None)  # No content chunks — empty stream

        async def fake_agent():
            return (
                {
                    "final_response": None,
                    "failed": True,
                    "error": "Error code: 401 - {'error': {'code': 'token_invalidated'}}",
                },
                {"input_tokens": 5, "output_tokens": 0, "total_tokens": 5},
            )

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())
            await asyncio.sleep(0)  # Let the coroutine complete

            writes: list[str] = []
            mock_response = AsyncMock(spec=web.StreamResponse)

            async def write_side_effect(data):
                writes.append(data.decode() if isinstance(data, bytes) else str(data))

            mock_response.write = AsyncMock(side_effect=write_side_effect)
            mock_response.prepare = AsyncMock()

            with patch(
                "gateway.platforms.api_server.web.StreamResponse",
                return_value=mock_response,
            ):
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-authfail-dict", "gpt-4", 1234567890,
                    stream_q, agent_task,
                )

            data_frames = [
                line.removeprefix("data: ")
                for chunk in writes
                for line in chunk.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            parsed_frames = [json.loads(frame) for frame in data_frames]

            # Must have an upstream_auth_failed error frame
            assert any(
                frame.get("error", {}).get("type") == "upstream_auth_failed"
                and frame.get("error", {}).get("code") == 401
                for frame in parsed_frames
            ), f"No upstream_auth_failed frame in: {parsed_frames}"

            # Finish reason must be "error", never "stop"
            finish_reasons = [
                choice.get("finish_reason")
                for frame in parsed_frames
                for choice in frame.get("choices", [])
                if choice.get("finish_reason") is not None
            ]
            assert "error" in finish_reasons, f"Expected finish_reason=error in {finish_reasons}"
            assert "stop" not in finish_reasons, f"Unexpected stop in {finish_reasons}"

        asyncio.run(run())

    def test_failed_result_with_final_response_still_emits_error_frame(self):
        """A failed result that ALSO carries a diagnostic final_response (e.g.
        billing/credits exhausted, content-policy block, rate-limit) must still
        surface as an error frame — keying off ``failed`` regardless of
        ``final_response``, matching the non-streaming path. The error chunk
        carries an empty delta so already-streamed content is not duplicated.
        """
        adapter = _make_adapter()

        stream_q = queue.Queue()
        stream_q.put(None)  # No content chunks

        async def fake_agent():
            return (
                {
                    "final_response": "You have exhausted your monthly credits.",
                    "failed": True,
                    "error": "Billing hard limit reached",
                },
                {"input_tokens": 7, "output_tokens": 9, "total_tokens": 16},
            )

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())
            await asyncio.sleep(0)

            writes: list[str] = []
            mock_response = AsyncMock(spec=web.StreamResponse)

            async def write_side_effect(data):
                writes.append(data.decode() if isinstance(data, bytes) else str(data))

            mock_response.write = AsyncMock(side_effect=write_side_effect)
            mock_response.prepare = AsyncMock()

            with patch(
                "gateway.platforms.api_server.web.StreamResponse",
                return_value=mock_response,
            ):
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-failed-with-text", "gpt-4", 1234567890,
                    stream_q, agent_task,
                )

            parsed_frames = [
                json.loads(line.removeprefix("data: "))
                for chunk in writes
                for line in chunk.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]

            # Non-auth failure → generic agent error frame is surfaced
            assert any(
                frame.get("error", {}).get("code") == "agent_task_failed"
                for frame in parsed_frames
            ), f"No agent_task_failed error frame in: {parsed_frames}"

            finish_reasons = [
                choice.get("finish_reason")
                for frame in parsed_frames
                for choice in frame.get("choices", [])
                if choice.get("finish_reason") is not None
            ]
            assert "error" in finish_reasons, f"Expected finish_reason=error in {finish_reasons}"
            assert "stop" not in finish_reasons, f"Unexpected stop in {finish_reasons}"

        asyncio.run(run())

    def test_usage_limit_failed_result_emits_typed_frame(self):
        """A failed result carrying usage-limit context (ChatGPT-account Codex
        429 ``usage_limit_reached``) must emit the typed contract frame:

            {"error": {"type": "usage_limit_reached",
                       "code": "usage_limit_reached",   # STRING — never 429
                       "message": ..., "reset_at": <epoch int>,
                       "resets_in_seconds": <int>, "plan_type": <str>}}

        The desktop matches error.type/code === 'usage_limit_reached'.  The
        code must NEVER be 429 (int or string): agente-desktop's
        isQuotaExhaustedSignal matches code '429' and would hijack the frame
        into the managed-tier "upgrade" bubble.
        """
        import time as _time

        adapter = _make_adapter()

        stream_q = queue.Queue()
        stream_q.put(None)

        reset_at = int(_time.time()) + 3600

        async def fake_agent():
            return (
                {
                    "final_response": "API call failed after 3 retries: HTTP 429",
                    "failed": True,
                    "completed": False,
                    "error": "HTTP 429: The usage limit has been reached",
                    "failure_reason": "rate_limit",
                    "error_code": "usage_limit_reached",
                    "reset_at": reset_at,
                    "resets_in_seconds": 3600,
                    "plan_type": "pro",
                },
                {"input_tokens": 5, "output_tokens": 0, "total_tokens": 5},
            )

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())
            await asyncio.sleep(0)

            writes: list[str] = []
            mock_response = AsyncMock(spec=web.StreamResponse)

            async def write_side_effect(data):
                writes.append(data.decode() if isinstance(data, bytes) else str(data))

            mock_response.write = AsyncMock(side_effect=write_side_effect)
            mock_response.prepare = AsyncMock()

            with patch(
                "gateway.platforms.api_server.web.StreamResponse",
                return_value=mock_response,
            ):
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-usage-limit", "gpt-4", 1234567890,
                    stream_q, agent_task,
                )

            parsed_frames = [
                json.loads(line.removeprefix("data: "))
                for chunk in writes
                for line in chunk.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]

            error_frames = [f["error"] for f in parsed_frames if "error" in f]
            assert error_frames, f"No error frame emitted: {parsed_frames}"
            frame = error_frames[0]

            assert frame["type"] == "usage_limit_reached"
            # STRING code — never numeric 429 / string '429' (desktop
            # isQuotaExhaustedSignal trap).
            assert frame["code"] == "usage_limit_reached"
            assert frame["code"] != 429 and frame["code"] != "429"
            assert isinstance(frame["code"], str)
            assert frame["reset_at"] == reset_at
            assert frame["resets_in_seconds"] == 3600
            assert frame["plan_type"] == "pro"
            assert "usage limit" in frame["message"].lower()
            # Must NOT be the generic collapse.
            assert frame["type"] != "sidecar_agent_error"

            finish_reasons = [
                choice.get("finish_reason")
                for f in parsed_frames
                for choice in f.get("choices", [])
                if choice.get("finish_reason") is not None
            ]
            assert "error" in finish_reasons
            assert "stop" not in finish_reasons

        asyncio.run(run())

    def test_usage_limit_frame_from_failure_reason_and_reset_at_only(self):
        """Even without error_code, failure_reason=rate_limit + reset_at is
        enough to emit the typed frame (older terminal-dict producers)."""
        import time as _time

        adapter = _make_adapter()

        stream_q = queue.Queue()
        stream_q.put(None)

        reset_at = int(_time.time()) + 7200

        async def fake_agent():
            return (
                {
                    "final_response": None,
                    "failed": True,
                    "error": "HTTP 429: rate limited",
                    "failure_reason": "rate_limit",
                    "reset_at": reset_at,
                },
                {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())
            await asyncio.sleep(0)

            writes: list[str] = []
            mock_response = AsyncMock(spec=web.StreamResponse)

            async def write_side_effect(data):
                writes.append(data.decode() if isinstance(data, bytes) else str(data))

            mock_response.write = AsyncMock(side_effect=write_side_effect)
            mock_response.prepare = AsyncMock()

            with patch(
                "gateway.platforms.api_server.web.StreamResponse",
                return_value=mock_response,
            ):
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-usage-limit-fallback", "gpt-4",
                    1234567890, stream_q, agent_task,
                )

            parsed_frames = [
                json.loads(line.removeprefix("data: "))
                for chunk in writes
                for line in chunk.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            error_frames = [f["error"] for f in parsed_frames if "error" in f]
            assert error_frames, f"No error frame emitted: {parsed_frames}"
            frame = error_frames[0]
            assert frame["type"] == "usage_limit_reached"
            assert frame["code"] == "usage_limit_reached"
            assert frame["reset_at"] == reset_at
            # Derived from reset_at when not provided explicitly.
            assert isinstance(frame["resets_in_seconds"], int)
            assert 0 < frame["resets_in_seconds"] <= 7200

        asyncio.run(run())

    def test_non_usage_limit_failed_result_keeps_generic_error_frame(self):
        """Failures WITHOUT usage-limit context must keep emitting the
        generic sidecar_agent_error payload (regression guard for #90)."""
        adapter = _make_adapter()

        stream_q = queue.Queue()
        stream_q.put(None)

        async def fake_agent():
            return (
                {
                    "final_response": None,
                    "failed": True,
                    "error": "boom: provider exploded",
                    "failure_reason": "server_error",
                },
                {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())
            await asyncio.sleep(0)

            writes: list[str] = []
            mock_response = AsyncMock(spec=web.StreamResponse)

            async def write_side_effect(data):
                writes.append(data.decode() if isinstance(data, bytes) else str(data))

            mock_response.write = AsyncMock(side_effect=write_side_effect)
            mock_response.prepare = AsyncMock()

            with patch(
                "gateway.platforms.api_server.web.StreamResponse",
                return_value=mock_response,
            ):
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-generic-fail", "gpt-4",
                    1234567890, stream_q, agent_task,
                )

            parsed_frames = [
                json.loads(line.removeprefix("data: "))
                for chunk in writes
                for line in chunk.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            error_frames = [f["error"] for f in parsed_frames if "error" in f]
            assert error_frames, f"No error frame emitted: {parsed_frames}"
            assert error_frames[0]["type"] == "sidecar_agent_error"
            assert error_frames[0]["code"] == "agent_task_failed"

        asyncio.run(run())

    def test_broken_pipe_also_cancels_agent(self):
        """BrokenPipeError (another disconnect variant) also cancels the task."""
        adapter = _make_adapter()

        stream_q = queue.Queue()

        async def fake_agent():
            await asyncio.sleep(999)  # Never completes
            return {}, {}

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())

            mock_response = AsyncMock(spec=web.StreamResponse)
            mock_response.write = AsyncMock(side_effect=BrokenPipeError("pipe broken"))
            mock_response.prepare = AsyncMock()

            with patch("gateway.platforms.api_server.web.StreamResponse",
                       return_value=mock_response):
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-789", "gpt-4", 1234567890,
                    stream_q, agent_task,
                )

            assert agent_task.cancelled() or agent_task.done()

        asyncio.run(run())

    def test_already_done_task_not_cancelled_on_disconnect(self):
        """If agent already finished before disconnect, don't try to cancel."""
        adapter = _make_adapter()

        stream_q = queue.Queue()
        stream_q.put("data")

        async def fake_agent():
            return {"final_response": "done"}, {}

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())
            await asyncio.sleep(0)  # Let agent complete

            mock_response = AsyncMock(spec=web.StreamResponse)
            call_count = 0

            async def write_side_effect(data):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise ConnectionResetError("late disconnect")

            mock_response.write = AsyncMock(side_effect=write_side_effect)
            mock_response.prepare = AsyncMock()

            with patch("gateway.platforms.api_server.web.StreamResponse",
                       return_value=mock_response):
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-done", "gpt-4", 1234567890,
                    stream_q, agent_task,
                )

            # Task was already done — should not be cancelled
            assert agent_task.done()
            assert not agent_task.cancelled()

        asyncio.run(run())

    def test_agent_interrupt_called_on_disconnect(self):
        """When the client disconnects, agent.interrupt() must be called
        so the agent thread stops making LLM API calls."""
        adapter = _make_adapter()

        stream_q = queue.Queue()
        stream_q.put("hello ")

        agent_done = asyncio.Event()

        async def fake_agent():
            await agent_done.wait()
            return {"final_response": "done"}, {}

        # Mock agent with an interrupt method
        mock_agent = MagicMock()
        mock_agent.interrupt = MagicMock()

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())
            agent_ref = [mock_agent]

            mock_response = AsyncMock(spec=web.StreamResponse)
            call_count = 0

            async def write_side_effect(data):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise ConnectionResetError("client disconnected")

            mock_response.write = AsyncMock(side_effect=write_side_effect)
            mock_response.prepare = AsyncMock()

            with patch("gateway.platforms.api_server.web.StreamResponse",
                       return_value=mock_response):
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-int", "gpt-4", 1234567890,
                    stream_q, agent_task, agent_ref,
                )

            # agent.interrupt() must have been called
            mock_agent.interrupt.assert_called_once_with("SSE client disconnected")
            # Clean up
            agent_done.set()

        asyncio.run(run())

    def test_agent_ref_none_still_cancels_task(self):
        """When agent_ref is not provided (None), the task is still cancelled
        on disconnect — just without the interrupt() call."""
        adapter = _make_adapter()

        stream_q = queue.Queue()

        async def fake_agent():
            await asyncio.sleep(999)
            return {}, {}

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())

            mock_response = AsyncMock(spec=web.StreamResponse)
            mock_response.write = AsyncMock(side_effect=BrokenPipeError("gone"))
            mock_response.prepare = AsyncMock()

            with patch("gateway.platforms.api_server.web.StreamResponse",
                       return_value=mock_response):
                # No agent_ref passed — should still handle disconnect cleanly
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-noref", "gpt-4", 1234567890,
                    stream_q, agent_task,
                )

            assert agent_task.cancelled() or agent_task.done()

        asyncio.run(run())
