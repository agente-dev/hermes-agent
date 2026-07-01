"""Unit tests for the usage-limit reset plumbing.

Covers the three helpers that carry a ChatGPT-account plan-limit 429
(``type=usage_limit_reached`` with ``resets_at`` / ``resets_in_seconds`` /
``plan_type``) from the provider error body to the terminal failure dict:

* ``agent.agent_runtime_helpers.extract_api_error_context`` — body parsing
* ``agent.conversation_loop._usage_limit_reset_seconds`` — fail-fast gate
* ``agent.conversation_loop._rate_limit_reset_fields`` — terminal-dict fields
"""

import time

from agent.agent_runtime_helpers import extract_api_error_context
from agent.conversation_loop import (
    USAGE_LIMIT_FAIL_FAST_SECONDS,
    _rate_limit_reset_fields,
    _usage_limit_reset_seconds,
)


class _CodexUsageLimitError(Exception):
    """Verified live shape of the ChatGPT-Pro Codex plan-limit 429."""

    def __init__(self, resets_at=None, resets_in_seconds=None, plan_type="pro"):
        super().__init__("Error code: 429 - The usage limit has been reached")
        self.status_code = 429
        error: dict = {
            "type": "usage_limit_reached",
            "message": "The usage limit has been reached",
            "plan_type": plan_type,
        }
        if resets_at is not None:
            error["resets_at"] = resets_at
        if resets_in_seconds is not None:
            error["resets_in_seconds"] = resets_in_seconds
        self.body = {"error": error}


class TestExtractApiErrorContext:
    def test_codex_usage_limit_body_full(self):
        resets_at = int(time.time()) + 3600
        ctx = extract_api_error_context(
            _CodexUsageLimitError(resets_at=resets_at, resets_in_seconds=3600)
        )
        assert ctx["reason"] == "usage_limit_reached"
        assert ctx["reset_at"] == resets_at
        assert ctx["resets_in_seconds"] == 3600
        assert ctx["plan_type"] == "pro"
        assert "usage limit" in ctx["message"].lower()

    def test_resets_in_seconds_fills_missing_reset_at(self):
        before = time.time()
        ctx = extract_api_error_context(
            _CodexUsageLimitError(resets_in_seconds=1800)
        )
        assert ctx["resets_in_seconds"] == 1800
        assert before + 1700 <= ctx["reset_at"] <= time.time() + 1800

    def test_resets_at_wins_over_derived_value(self):
        resets_at = int(time.time()) + 900
        ctx = extract_api_error_context(
            _CodexUsageLimitError(resets_at=resets_at, resets_in_seconds=900)
        )
        assert ctx["reset_at"] == resets_at

    def test_garbage_resets_in_seconds_is_ignored(self):
        err = _CodexUsageLimitError()
        err.body["error"]["resets_in_seconds"] = "soon"
        ctx = extract_api_error_context(err)
        assert "resets_in_seconds" not in ctx

    def test_plain_error_without_body_unchanged(self):
        err = Exception("Error code: 429 - Rate limit exceeded.")
        ctx = extract_api_error_context(err)
        assert "reason" not in ctx
        assert "resets_in_seconds" not in ctx
        assert "plan_type" not in ctx


class TestUsageLimitResetSeconds:
    def test_far_off_reset_from_resets_in_seconds(self):
        secs = _usage_limit_reset_seconds(
            {"reason": "usage_limit_reached", "resets_in_seconds": 3600}
        )
        assert secs == 3600
        assert secs > USAGE_LIMIT_FAIL_FAST_SECONDS

    def test_reset_derived_from_reset_at(self):
        secs = _usage_limit_reset_seconds(
            {"reason": "usage_limit_reached", "reset_at": time.time() + 600}
        )
        assert secs is not None
        assert 590 <= secs <= 600

    def test_non_usage_limit_reason_returns_none(self):
        assert _usage_limit_reset_seconds(
            {"reason": "rate_limit_exceeded", "resets_in_seconds": 3600}
        ) is None

    def test_usage_limit_without_reset_returns_none(self):
        assert _usage_limit_reset_seconds({"reason": "usage_limit_reached"}) is None

    def test_none_context_returns_none(self):
        assert _usage_limit_reset_seconds(None) is None


class TestRateLimitResetFields:
    def test_full_context_maps_all_fields(self):
        reset_at = int(time.time()) + 3600
        fields = _rate_limit_reset_fields(
            {
                "reason": "usage_limit_reached",
                "reset_at": reset_at,
                "resets_in_seconds": 3600,
                "plan_type": "pro",
            }
        )
        assert fields == {
            "error_code": "usage_limit_reached",
            "reset_at": reset_at,
            "resets_in_seconds": 3600,
            "plan_type": "pro",
        }

    def test_resets_in_seconds_derived_from_reset_at(self):
        reset_at = int(time.time()) + 1200
        fields = _rate_limit_reset_fields(
            {"reason": "usage_limit_reached", "reset_at": reset_at}
        )
        assert fields["reset_at"] == reset_at
        assert 0 < fields["resets_in_seconds"] <= 1200

    def test_string_epoch_is_coerced_to_int(self):
        reset_at = int(time.time()) + 300
        fields = _rate_limit_reset_fields(
            {"reason": "usage_limit_reached", "reset_at": str(reset_at)}
        )
        assert fields["reset_at"] == reset_at
        assert isinstance(fields["reset_at"], int)

    def test_empty_context_yields_no_fields(self):
        assert _rate_limit_reset_fields({}) == {}
        assert _rate_limit_reset_fields(None) == {}

    def test_unparseable_reset_at_is_dropped(self):
        fields = _rate_limit_reset_fields(
            {"reason": "usage_limit_reached", "reset_at": "tomorrow"}
        )
        assert fields == {"error_code": "usage_limit_reached"}
