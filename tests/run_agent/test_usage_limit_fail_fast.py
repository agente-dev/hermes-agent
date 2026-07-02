"""Usage-limit 429 (ChatGPT-account plans, e.g. Codex ``plan_type=pro``):

1. When the 429 body says ``type=usage_limit_reached`` with a reset far
   beyond the retry window (Retry-After capped at 120s, backoff at 60s),
   the loop must fail FAST — a single API call, no futile retries — and the
   terminal failure dict must carry the structured reset context
   (``error_code`` / ``reset_at`` / ``resets_in_seconds`` / ``plan_type``)
   so the gateway can surface a typed error with the actual reset time.

2. Ordinary transient 429s (no usage_limit_reached type) and usage limits
   with short resets keep the existing retry behavior.

Live incident: a Codex 429 with ``resets_at`` hours away was retried to
exhaustion, then collapsed to a generic message with no reset time, which
the desktop rendered as an unrelated generic Hebrew error.
"""

import importlib
import sys
import time
import types
from types import SimpleNamespace

# Imported via importlib so the CI lint job (which installs no test deps)
# doesn't report an unresolved `pytest` import for this file.
pytest = importlib.import_module("pytest")


def _stub_module(name: str, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for attr, value in attrs.items():
        setattr(module, attr, value)
    return module


sys.modules.setdefault("fire", _stub_module("fire", Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", _stub_module("firecrawl", Firecrawl=object))
sys.modules.setdefault("fal_client", _stub_module("fal_client"))

import run_agent


# ---------------------------------------------------------------------------
# Fast backoff (mirrors tests/run_agent/test_anthropic_error_handling.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_backoff_wait(monkeypatch):
    import asyncio as _asyncio
    import time as _time

    monkeypatch.setattr(run_agent, "jittered_backoff", lambda *a, **k: 0.0)
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)

    _real_asyncio_sleep = _asyncio.sleep

    async def _fast_sleep(delay=0, *args, **kwargs):
        await _real_asyncio_sleep(0)

    monkeypatch.setattr(_asyncio, "sleep", _fast_sleep)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_agent_bootstrap(monkeypatch):
    monkeypatch.setattr(
        run_agent,
        "get_tool_definitions",
        lambda **kwargs: [
            {
                "type": "function",
                "function": {
                    "name": "terminal",
                    "description": "Run shell commands.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})


def _anthropic_response(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        model="claude-sonnet-4-6-20250514",
    )


class _CodexUsageLimitError(Exception):
    """Simulates the ChatGPT-account Codex plan-limit 429.

    Body shape (verified live):
    ``{'error': {'type': 'usage_limit_reached', 'message': 'The usage limit
    has been reached', 'plan_type': 'pro', 'resets_at': <epoch>,
    'resets_in_seconds': <n>}}``
    """

    def __init__(self, resets_in_seconds: int):
        super().__init__("Error code: 429 - The usage limit has been reached")
        self.status_code = 429
        self.resets_at = int(time.time()) + resets_in_seconds
        self.body = {
            "error": {
                "type": "usage_limit_reached",
                "message": "The usage limit has been reached",
                "plan_type": "pro",
                "resets_at": self.resets_at,
                "resets_in_seconds": resets_in_seconds,
            }
        }


class _PlainRateLimitError(Exception):
    """Ordinary transient 429 with no usage_limit_reached type."""

    def __init__(self):
        super().__init__("Error code: 429 - Rate limit exceeded.")
        self.status_code = 429


class _FakeMessages:
    def create(self, **kwargs):
        raise NotImplementedError

    def stream(self, **kwargs):
        raise NotImplementedError


class _FakeAnthropicClient:
    def __init__(self):
        self.messages = _FakeMessages()

    def close(self):
        pass


def _fake_build_anthropic_client(key, base_url=None, **kwargs):
    return _FakeAnthropicClient()


def _make_agent_cls(make_error, recover_after=None, call_counter=None):
    """AIAgent subclass whose API calls raise ``make_error()``.

    If ``recover_after`` is set, calls succeed after that many failures.
    ``call_counter`` (a dict with key "n") observes total API attempts.
    """
    counter = call_counter if call_counter is not None else {"n": 0}

    class _Agent(run_agent.AIAgent):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("skip_context_files", True)
            kwargs.setdefault("skip_memory", True)
            kwargs.setdefault("max_iterations", 4)
            super().__init__(*args, **kwargs)

        # Persistence/cleanup no-ops (parameter names mirror AIAgent's).
        def _cleanup_task_resources(self, task_id):
            return None

        def _persist_session(self, messages, conversation_history=None):
            return None

        def _save_trajectory(self, messages, user_query, completed):
            return None

        def _save_session_log(self, messages=None):
            return None

        # Both the blocking and streaming API entry points route through the
        # same fake so the retry loop under test sees identical behavior on
        # either path.
        def _fake_api_call(self, api_kwargs, **kw):
            counter["n"] += 1
            if recover_after is not None and counter["n"] > recover_after:
                return _anthropic_response("Recovered")
            raise make_error()

        def _interruptible_api_call(self, api_kwargs, **kw):
            return self._fake_api_call(api_kwargs, **kw)

        def _interruptible_streaming_api_call(self, api_kwargs, **kw):
            return self._fake_api_call(api_kwargs, **kw)

    return _Agent


def _run_with_agent(monkeypatch, agent_cls):
    """Run one conversation turn directly through ``run_conversation``.

    Intentionally NOT routed through ``gateway/run.py``'s ``_run_agent``,
    which rebuilds the result dict and drops the structured failure keys.
    The consumer under test here is ``gateway/platforms/api_server.py``'s
    ``_run_agent``, which passes the ``run_conversation`` dict through
    unchanged — so the loop's terminal dict IS the contract surface.
    """
    _patch_agent_bootstrap(monkeypatch)
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client", _fake_build_anthropic_client
    )
    monkeypatch.setenv("HERMES_TOOL_PROGRESS", "false")

    agent = agent_cls(
        provider="anthropic",
        api_mode="anthropic_messages",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-api03-test-key",
        quiet_mode=True,
    )
    return agent.run_conversation("hello", task_id="test-usage-limit")


# ---------------------------------------------------------------------------
# Tests — fail fast on far-off resets
# ---------------------------------------------------------------------------


def test_usage_limit_long_reset_fails_fast_single_api_call(monkeypatch):
    """usage_limit_reached with a reset hours away → exactly ONE API call.

    Retrying is futile: Retry-After is capped at 120s and backoff at 60s,
    so no retry schedule can bridge a multi-hour plan-window reset.
    """
    calls = {"n": 0}
    agent_cls = _make_agent_cls(
        lambda: _CodexUsageLimitError(resets_in_seconds=3600),
        call_counter=calls,
    )
    result = _run_with_agent(monkeypatch, agent_cls)
    assert result.get("failed") is True
    assert calls["n"] == 1, (
        f"Expected fail-fast with a single API attempt, got {calls['n']} — "
        "futile retries against a plan usage limit burn quota for nothing."
    )


def test_usage_limit_terminal_dict_carries_reset_context(monkeypatch):
    """The terminal failure dict must carry error_code + reset_at +
    resets_in_seconds + plan_type so the gateway can emit the typed frame."""
    resets_in = 3600
    err = _CodexUsageLimitError(resets_in_seconds=resets_in)
    agent_cls = _make_agent_cls(lambda: err)
    result = _run_with_agent(monkeypatch, agent_cls)

    assert result.get("failed") is True
    assert result.get("failure_reason") == "rate_limit"
    assert result.get("error_code") == "usage_limit_reached"
    assert result.get("reset_at") == err.resets_at
    # resets_in_seconds is either passed through or derived from reset_at.
    assert isinstance(result.get("resets_in_seconds"), int)
    assert 0 < result["resets_in_seconds"] <= resets_in
    assert result.get("plan_type") == "pro"
    # Existing contract keys untouched.
    assert result.get("completed") is False
    assert result.get("error")
    assert result.get("final_response")


def test_usage_limit_short_reset_still_retries_and_recovers(monkeypatch):
    """A usage limit that resets within the retry window (< 180s) keeps the
    normal retry path — first call fails, second succeeds."""
    calls = {"n": 0}
    agent_cls = _make_agent_cls(
        lambda: _CodexUsageLimitError(resets_in_seconds=60),
        recover_after=1,
        call_counter=calls,
    )
    result = _run_with_agent(monkeypatch, agent_cls)
    assert result.get("final_response") == "Recovered"
    assert calls["n"] == 2


def test_plain_429_without_usage_limit_type_still_retries(monkeypatch):
    """An ordinary transient 429 (no usage_limit_reached type, no reset info)
    must keep the existing retry behavior."""
    calls = {"n": 0}
    agent_cls = _make_agent_cls(
        _PlainRateLimitError,
        recover_after=1,
        call_counter=calls,
    )
    result = _run_with_agent(monkeypatch, agent_cls)
    assert result.get("final_response") == "Recovered"
    assert calls["n"] == 2
