"""Tests for the per-turn current-time note injected in conversation_loop.

Bug B: the agent was time-blind. The system prompt carries only a *date*
line and is built once per session (cached, byte-stable for prompt caching),
so the model could not tell morning from evening and the date never advanced
mid-session.

The fix injects an ephemeral per-turn note carrying the CURRENT local date +
time + timezone into the *current turn's user message* — NOT the cached
system prompt — so the model always has an accurate wall clock without busting
the byte-stable system-prompt cache prefix.
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.conversation_loop import _build_current_time_note

# Import the modules that bind ``from hermes_time import now`` at *module*
# load time eagerly, BEFORE any test patches ``hermes_time.now``. If they were
# first imported lazily while ``hermes_time.now`` is patched (e.g. transitively
# via build_system_prompt_parts → run_agent → cron.jobs), their ``_hermes_now``
# alias would permanently capture the mock and leak a frozen clock into
# unrelated tests (cron due-job tests). Eager import here pins the real fn.
import cron.jobs  # noqa: E402,F401
import run_agent  # noqa: E402,F401


def _patch_now(monkeypatch, dt):
    """Force hermes_time.now() (imported lazily inside the helper) to return dt."""
    import hermes_time

    monkeypatch.setattr(hermes_time, "now", lambda: dt)


def test_note_includes_hour_minute_and_timezone(monkeypatch):
    """The note must carry hour:minute precision AND the timezone."""
    monkeypatch.setenv("HERMES_INJECT_CURRENT_TIME", "1")
    dt = datetime(2026, 6, 23, 19, 42, tzinfo=ZoneInfo("Asia/Jerusalem"))
    _patch_now(monkeypatch, dt)

    note = _build_current_time_note()

    assert note is not None
    # Hour + minute present (the system prompt deliberately lacks these).
    assert "19:42" in note
    # Timezone present — IANA name preferred.
    assert "Asia/Jerusalem" in note
    # Human-readable date too.
    assert "2026" in note
    assert re.search(r"\bTuesday\b", note)  # 2026-06-23 is a Tuesday


def test_note_advances_with_the_clock(monkeypatch):
    """Two turns at different times produce different notes (not frozen)."""
    monkeypatch.setenv("HERMES_INJECT_CURRENT_TIME", "1")
    tz = ZoneInfo("Asia/Jerusalem")

    _patch_now(monkeypatch, datetime(2026, 6, 23, 8, 5, tzinfo=tz))
    morning = _build_current_time_note()

    _patch_now(monkeypatch, datetime(2026, 6, 23, 20, 5, tzinfo=tz))
    evening = _build_current_time_note()

    assert morning is not None and evening is not None
    assert morning != evening
    assert "08:05" in morning
    assert "20:05" in evening


def test_note_disabled_returns_none(monkeypatch):
    """The flag is default-on but can be disabled."""
    monkeypatch.setenv("HERMES_INJECT_CURRENT_TIME", "0")
    _patch_now(monkeypatch, datetime(2026, 6, 23, 19, 42, tzinfo=ZoneInfo("UTC")))
    assert _build_current_time_note() is None


def test_note_default_on_when_flag_unset(monkeypatch):
    """With the flag unset, the note is produced (default-on)."""
    monkeypatch.delenv("HERMES_INJECT_CURRENT_TIME", raising=False)
    _patch_now(monkeypatch, datetime(2026, 6, 23, 9, 0, tzinfo=ZoneInfo("Asia/Jerusalem")))
    note = _build_current_time_note()
    assert note is not None
    assert "09:00" in note


def test_note_degrades_gracefully_on_clock_error(monkeypatch):
    """A clock failure must not raise — the turn proceeds without the note."""
    monkeypatch.setenv("HERMES_INJECT_CURRENT_TIME", "1")
    import hermes_time

    def boom():
        raise RuntimeError("clock unavailable")

    monkeypatch.setattr(hermes_time, "now", boom)
    assert _build_current_time_note() is None


def test_system_prompt_timestamp_line_stays_minute_free(monkeypatch):
    """The cached system-prompt time element must NOT gain minute precision.

    Bug B's fix injects time per-turn into the user message; it must NOT add
    minute-precision to the byte-stable system prompt (that would bust the
    prompt cache every minute). This guards that the system-prompt timestamp
    line remains date-only.
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    from agent.system_prompt import build_system_prompt_parts

    agent = SimpleNamespace(
        load_soul_identity=False,
        skip_context_files=True,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        _memory_enabled=False,
        _user_profile_enabled=False,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
    )

    fixed = datetime(2026, 6, 23, 19, 42, tzinfo=ZoneInfo("Asia/Jerusalem"))
    with (
        patch("hermes_time.now", return_value=fixed),
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        parts = build_system_prompt_parts(agent)

    volatile = parts.get("volatile", "")
    assert "Conversation started:" in volatile
    assert "June 23, 2026" in volatile
    # Date-only — minute precision must NOT leak into the cached prompt.
    assert "19:42" not in volatile

