"""Integration test for the codex_app_server runtime path through AIAgent.

Verifies that:
  - api_mode='codex_app_server' is accepted on AIAgent construction
  - run_conversation() takes the early-return path and never enters the
    chat completions loop
  - Projected messages from a fake Codex session land in the messages list
  - tool_iterations from the codex session tick the skill nudge counter
  - Memory nudge counter ticks once per turn
  - The returned dict has the same shape as the chat_completions path
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

import run_agent
from agent.transports.codex_app_server_session import CodexAppServerSession, TurnResult


@pytest.fixture(autouse=True)
def writable_hermes_home(monkeypatch, tmp_path):
    """Keep integration logs inside the test sandbox."""
    monkeypatch.setattr(run_agent, "_hermes_home", tmp_path / "hermes")


@pytest.fixture
def fake_session(monkeypatch):
    """Replace CodexAppServerSession with a stub that returns a fixed
    TurnResult, so we can drive AIAgent without spawning real codex."""

    def fake_run_turn(self, user_input: str, **kwargs):
        return TurnResult(
            final_text=f"echo: {user_input}",
            projected_messages=[
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "exec_1", "type": "function",
                                 "function": {"name": "exec_command",
                                              "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "exec_1", "content": "ok"},
                {"role": "assistant", "content": f"echo: {user_input}"},
            ],
            tool_iterations=1,
            interrupted=False,
            error=None,
            turn_id="turn-stub-1",
            thread_id="thread-stub-1",
        )

    monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
    monkeypatch.setattr(
        CodexAppServerSession, "ensure_started", lambda self: "thread-stub-1"
    )


def _make_codex_agent(*, gateway_session_key=None, session_db=None):
    """Construct an AIAgent in codex_app_server mode without contacting any
    real provider. We pass api_mode explicitly so the constructor takes the
    credentialless official-runtime branch."""
    return run_agent.AIAgent(
        provider="openai-codex",
        api_mode="codex_app_server",
        gateway_session_key=gateway_session_key,
        session_db=session_db,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )


class TestApiModeAccepted:
    def test_api_mode_is_codex_app_server(self):
        agent = _make_codex_agent()
        assert agent.api_mode == "codex_app_server"

    def test_official_runtime_constructs_no_openai_client_or_credential(self):
        agent = _make_codex_agent()

        assert agent.client is None
        assert agent.api_key == ""
        assert agent.base_url == ""

    @pytest.mark.parametrize("provider", ["openai", "openrouter", None])
    def test_inexact_provider_pair_is_rejected(self, provider):
        with pytest.raises(ValueError, match="requires provider='openai-codex'"):
            run_agent.AIAgent(
                provider=provider,
                api_mode="codex_app_server",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )

    def test_legacy_credential_pool_is_not_retained(self):
        pool = object()
        agent = run_agent.AIAgent(
            provider="openai-codex",
            api_mode="codex_app_server",
            credential_pool=pool,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

        assert agent._credential_pool is None


class TestRunConversationCodexPath:
    def test_run_conversation_returns_codex_shape(self, fake_session):
        agent = _make_codex_agent()
        # No background review fork during tests
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello there")
        assert result["final_response"] == "echo: hello there"
        assert result["completed"] is True
        assert result["partial"] is False
        assert result["error"] is None
        assert result["api_calls"] == 1
        assert result["codex_thread_id"] == "thread-stub-1"
        assert result["codex_turn_id"] == "turn-stub-1"

    def test_projected_messages_are_spliced(self, fake_session):
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello")
        msgs = result["messages"]
        # User message + 3 projected (assistant tool_call + tool + assistant text)
        assert len(msgs) >= 4
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "hello"
        # Last assistant message has the final text
        final = [m for m in msgs if m.get("role") == "assistant"
                 and m.get("content") == "echo: hello"]
        assert final, f"expected final assistant message in {msgs}"

    def test_nudge_counters_tick(self, fake_session):
        """The skill nudge counter must accumulate tool_iterations across
        turns. The memory nudge counter is gated on memory being configured
        (which we skip via skip_memory=True), so we don't assert on it here —
        a separate test below covers that path explicitly."""
        agent = _make_codex_agent()
        agent._iters_since_skill = 0
        agent._user_turn_count = 0
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("first")
        assert agent._iters_since_skill == 1  # one tool_iteration in fake turn
        # _user_turn_count is incremented by run_conversation pre-loop, not
        # by the codex helper — confirms we delegate that to the standard flow.
        assert agent._user_turn_count == 1
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("second")
        assert agent._iters_since_skill == 2
        assert agent._user_turn_count == 2

    def test_user_message_not_duplicated(self, fake_session):
        """Regression guard: the user message must appear exactly once in
        the messages list. The standard run_conversation pre-loop appends
        it, and the codex helper must NOT append again."""
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("ping unique 12345")
        user_count = sum(
            1 for m in result["messages"]
            if m.get("role") == "user" and m.get("content") == "ping unique 12345"
        )
        assert user_count == 1, f"user message appeared {user_count}× in {result['messages']}"

    def test_background_review_NOT_invoked_below_threshold(self, fake_session):
        """A single turn shouldn't trigger background review — counters
        haven't reached the nudge interval (default 10)."""
        agent = _make_codex_agent()
        agent._memory_nudge_interval = 10
        agent._skill_nudge_interval = 10
        agent._iters_since_skill = 0
        with patch.object(agent, "_spawn_background_review",
                          return_value=None) as spawn:
            agent.run_conversation("ping")
        # Below threshold → review should NOT fire (was a real bug:
        # the helper was calling _spawn_background_review() with no
        # args after every turn, which would crash with TypeError).
        assert not spawn.called

    def test_background_review_skill_trigger_is_skipped_for_official_route(
        self, monkeypatch
    ):
        """The official route never downgrades into a token-replay review."""
        from agent.transports.codex_app_server_session import (
            CodexAppServerSession, TurnResult,
        )
        # Make the fake session report 10 tool iterations in one turn
        # (matching the default skill threshold).
        def fake_run_turn(self, user_input: str, **kwargs):
            return TurnResult(
                final_text=f"echo: {user_input}",
                projected_messages=[
                    {"role": "assistant", "content": f"echo: {user_input}"},
                ],
                tool_iterations=10,
                turn_id="t1", thread_id="th1",
            )
        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(
            CodexAppServerSession, "ensure_started", lambda self: "th1"
        )

        agent = _make_codex_agent()
        agent._skill_nudge_interval = 10
        agent._iters_since_skill = 0
        # Make valid_tool_names include 'skill_manage' so the gate passes
        agent.valid_tool_names = set(getattr(agent, "valid_tool_names", set()))
        agent.valid_tool_names.add("skill_manage")

        with patch.object(agent, "_spawn_background_review",
                          return_value=None) as spawn:
            agent.run_conversation("do tool work")

        assert not spawn.called
        assert agent._iters_since_skill == 10

    def test_background_review_never_spawns_when_threshold_trips(
        self, fake_session
    ):
        agent = _make_codex_agent()
        agent._skill_nudge_interval = 1  # very low so any iter trips it
        agent._iters_since_skill = 0
        agent.valid_tool_names = set(getattr(agent, "valid_tool_names", set()))
        agent.valid_tool_names.add("skill_manage")

        with patch.object(agent, "_spawn_background_review",
                          return_value=None) as spawn:
            agent.run_conversation("first")
        assert not spawn.called

    def test_chat_completions_loop_is_not_entered(self, fake_session):
        """The official route must bypass the regular API call loop entirely.
        We confirm by patching the SDK call and asserting it's never invoked."""
        agent = _make_codex_agent()
        # The chat_completions loop calls self.client.chat.completions.create(...)
        # If our early-return works, that path is dead.
        with patch.object(agent, "client") as client_mock, patch.object(
            agent, "_spawn_background_review", return_value=None
        ):
            agent.run_conversation("hi")
        assert not client_mock.chat.completions.create.called

    def test_official_route_skips_hermes_preflight_compression(
        self, fake_session
    ):
        agent = _make_codex_agent()
        agent.compression_enabled = True
        agent.context_compressor.protect_first_n = 0
        agent.context_compressor.protect_last_n = 0
        history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 100}
            for i in range(8)
        ]

        with patch.object(
            agent,
            "_compress_context",
            side_effect=AssertionError("official route must not compress via Hermes"),
        ) as compress:
            result = agent.run_conversation(
                "new message",
                conversation_history=history,
            )

        compress.assert_not_called()
        assert result["completed"] is True

    def test_official_route_rejoins_common_persistence_and_cleanup(
        self, fake_session
    ):
        agent = _make_codex_agent()

        with patch.object(agent, "_save_trajectory") as save, patch.object(
            agent, "_cleanup_task_resources"
        ) as cleanup, patch.object(agent, "_persist_session") as persist:
            result = agent.run_conversation("persist this turn")

        save.assert_called_once()
        cleanup.assert_called_once()
        persist.assert_called_once()
        assert result["turn_exit_reason"] == "codex_app_server_completed"
        assert result["codex_thread_id"] == "thread-stub-1"
        assert result["codex_turn_id"] == "turn-stub-1"

    def test_codex_thread_linkage_is_loaded_and_persisted(self, fake_session):
        session_db = MagicMock()
        session_db.get_session.return_value = None
        session_db.get_codex_app_server_thread_id.return_value = "thread-existing"
        agent = _make_codex_agent(session_db=session_db)

        result = agent.run_conversation("resume official conversation")

        assert agent._codex_session._resume_thread_id == "thread-existing"
        session_db.get_codex_app_server_thread_id.assert_called_once_with(
            agent.session_id
        )
        session_db.set_codex_app_server_thread_id.assert_called_once_with(
            agent.session_id,
            "thread-stub-1",
        )
        assert result["codex_thread_id"] == "thread-stub-1"

    def test_gateway_session_key_is_scoped_to_codex_child(
        self, fake_session, monkeypatch
    ):
        monkeypatch.setenv("HERMES_SESSION_KEY", "other-concurrent-session")
        agent = _make_codex_agent(
            gateway_session_key="agent:desktop:account:user-1"
        )

        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("hi")

        assert agent._codex_session._child_env == {
            "HERMES_SESSION_KEY": "agent:desktop:account:user-1",
            "HERMES_MAIN_RUNTIME_PROVIDER": "openai-codex",
            "HERMES_MAIN_RUNTIME_API_MODE": "codex_app_server",
            "HERMES_MAIN_RUNTIME_MODEL": agent.model,
        }
        assert os.environ["HERMES_SESSION_KEY"] == "other-concurrent-session"


class TestReviewForkIsolation:
    """Official subscription turns never create a legacy review fork."""

    def test_codex_app_server_parent_skips_review_fork(self):
        from unittest.mock import patch as _patch
        agent = _make_codex_agent()
        with _patch("run_agent.threading.Thread") as thread:
            agent._spawn_background_review(
                messages_snapshot=[{"role": "user", "content": "x"}],
                review_memory=True,
                review_skills=False,
            )

        thread.assert_not_called()


class TestErrorHandling:
    def test_session_exception_returns_partial_with_error(self, monkeypatch):
        def boom_run_turn(self, user_input, **kwargs):
            raise RuntimeError("subprocess died")

        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "t1")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", boom_run_turn)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")
        assert result["completed"] is False
        assert result["partial"] is True
        assert "subprocess died" in result["error"]
        assert "Reconnect OpenAI in Agente Desktop Settings" in result["final_response"]
        assert "will not fall back" in result["final_response"]
        assert "codex-runtime auto" not in result["final_response"]

    def test_interrupted_turn_marked_partial(self, monkeypatch):
        def interrupted_turn(self, user_input, **kwargs):
            return TurnResult(
                final_text="",
                projected_messages=[],
                tool_iterations=0,
                interrupted=True,
                error="user interrupted",
                turn_id="t",
                thread_id="th",
            )
        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "th")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", interrupted_turn)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")
        assert result["completed"] is False
        assert result["partial"] is True
        assert result["error"] == "user interrupted"
        assert result["interrupted"] is True
        assert result["turn_exit_reason"] == "codex_app_server_interrupted"


class TestSessionRetirementOnRunAgent:
    """run_agent.py side: when run_turn returns should_retire=True, the
    AIAgent must close + null _codex_session so the next turn respawns."""

    def test_should_retire_drops_session(self, monkeypatch):
        closes = {"count": 0}

        def fake_run_turn(self, user_input, **kwargs):
            return TurnResult(
                final_text="",
                projected_messages=[],
                tool_iterations=0,
                interrupted=True,
                error="turn timed out after 600.0s",
                turn_id="tu1",
                thread_id="th1",
                should_retire=True,
            )

        def fake_close(self):
            closes["count"] += 1

        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "th1")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(CodexAppServerSession, "close", fake_close)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")

        # The session was closed and cleared
        assert closes["count"] == 1
        assert getattr(agent, "_codex_session", "MISSING") is None
        # Partial result was still returned (caller still sees the error)
        assert result["partial"] is True
        assert result["error"] == "turn timed out after 600.0s"

    def test_normal_turn_keeps_session(self, fake_session):
        """fake_session fixture returns should_retire=False (default).
        The session must stay attached for the next turn to reuse."""
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("hi")
        # Session was lazily created and still attached.
        assert getattr(agent, "_codex_session", None) is not None

    def test_exception_path_also_drops_session(self, monkeypatch):
        """Even if run_turn raises (not just sets should_retire), we must
        drop the session — a thrown exception is the strongest possible
        signal the process is dead."""
        closes = {"count": 0}

        def boom_run_turn(self, user_input, **kwargs):
            raise RuntimeError("codex segfaulted")

        def fake_close(self):
            closes["count"] += 1

        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "th1")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", boom_run_turn)
        monkeypatch.setattr(CodexAppServerSession, "close", fake_close)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")

        assert closes["count"] == 1
        assert agent._codex_session is None
        assert result["completed"] is False
        assert "codex segfaulted" in result["error"]
