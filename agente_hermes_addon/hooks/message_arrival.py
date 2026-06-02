"""
Message-arrival hook — evaluates workflow rules before the LLM call.

Per hermes-202606-004: when a connector event arrives, Hermes loads matching
``workflow_rules`` for that connector, evaluates each ``matcher_pattern``
against the message, and EITHER:

* invokes the matched rule's ``target_ticket_template`` (calls the desktop
  ``create_ticket`` tool path) — and DROPS the message from the LLM queue, OR
* lets the event fall through to the default triage / LLM call.

Audit-log line is written per evaluation. Non-matched messages do not consume
LLM tokens for rule-checking — the matcher is pure-Python.

The hook is intentionally side-effect-light: it returns a structured
``HookOutcome`` and the gateway / runtime that called it is responsible for
actually invoking the desktop tool. This keeps the unit under test free of
the Hermes tool-invocation plumbing.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agente_hermes_addon.eval.matcher import find_matching_rules
from agente_hermes_addon.storage.workflow_rules import load_rules_for_connector

logger = logging.getLogger(__name__)

# Audit channel — separated so operators can grep one line per event.
audit_logger = logging.getLogger("agente.workflow.audit")


# Hook outcome reasons — stable strings, safe to assert against in tests.
OUTCOME_NO_RULES = "no_rules_for_connector"
OUTCOME_NO_MATCH = "no_match_fallthrough"
OUTCOME_MATCHED = "matched_rule"
OUTCOME_DISABLED = "hook_disabled"
OUTCOME_ERROR = "evaluator_error"


@dataclass
class HookOutcome:
    """Result of a single message-arrival evaluation."""

    short_circuit: bool
    """True ⇒ the runtime MUST NOT forward this event to the LLM."""

    reason: str
    """Stable identifier for the outcome (see ``OUTCOME_*`` constants)."""

    matched_rule_id: Optional[str] = None
    target_ticket_template: Optional[Dict[str, Any]] = None
    rules_considered: int = 0
    elapsed_ms: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)


def _audit(outcome: HookOutcome, *, connector_id: str, event_id: Optional[str]) -> None:
    """Emit a single structured audit line per evaluation."""
    payload = {
        "event": "workflow_rule_eval",
        "connector_id": connector_id,
        "event_id": event_id,
        "reason": outcome.reason,
        "short_circuit": outcome.short_circuit,
        "matched_rule_id": outcome.matched_rule_id,
        "rules_considered": outcome.rules_considered,
        "elapsed_ms": round(outcome.elapsed_ms, 3),
    }
    try:
        audit_logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        audit_logger.info(
            "workflow_rule_eval connector=%s reason=%s short_circuit=%s (audit-format-error: %s)",
            connector_id, outcome.reason, outcome.short_circuit, exc,
        )


def evaluate(
    event: Dict[str, Any],
    *,
    connector_id: Optional[str] = None,
    rules_loader: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    enabled: bool = True,
) -> HookOutcome:
    """Evaluate workflow rules for a single inbound connector event.

    Parameters
    ----------
    event:
        Connector event dict. Expected to carry at least ``connector_id`` and
        a textual payload (``text`` / ``body`` / ``message.text``).
    connector_id:
        Override for the event's connector. When ``None`` the function reads
        ``event["connector_id"]``.
    rules_loader:
        Injectable loader — defaults to the on-disk
        :func:`storage.workflow_rules.load_rules_for_connector`. Tests pass
        an in-memory function.
    enabled:
        Master kill switch. When ``False`` the hook returns immediately
        without touching the rules store (still emits an audit line).
    """
    started = time.perf_counter()
    connector = connector_id or event.get("connector_id") or ""
    event_id = event.get("id") or event.get("event_id")

    if not enabled:
        outcome = HookOutcome(
            short_circuit=False,
            reason=OUTCOME_DISABLED,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        _audit(outcome, connector_id=connector, event_id=event_id)
        return outcome

    loader = rules_loader or load_rules_for_connector

    try:
        rules = loader(connector) if connector else []
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("message_arrival: rules loader failed: %s", exc)
        outcome = HookOutcome(
            short_circuit=False,
            reason=OUTCOME_ERROR,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            extra={"error": str(exc)},
        )
        _audit(outcome, connector_id=connector, event_id=event_id)
        return outcome

    if not rules:
        outcome = HookOutcome(
            short_circuit=False,
            reason=OUTCOME_NO_RULES,
            rules_considered=0,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        _audit(outcome, connector_id=connector, event_id=event_id)
        return outcome

    matched = find_matching_rules(rules, event)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    if not matched:
        outcome = HookOutcome(
            short_circuit=False,
            reason=OUTCOME_NO_MATCH,
            rules_considered=len(rules),
            elapsed_ms=elapsed_ms,
        )
        _audit(outcome, connector_id=connector, event_id=event_id)
        return outcome

    # First-match wins — rules are sorted lexicographically by filename in the
    # loader, which is stable enough for v1. Multi-match policy is a later
    # intake.
    winner = matched[0]
    outcome = HookOutcome(
        short_circuit=True,
        reason=OUTCOME_MATCHED,
        matched_rule_id=winner.get("id"),
        target_ticket_template=winner.get("target_ticket_template"),
        rules_considered=len(rules),
        elapsed_ms=elapsed_ms,
    )
    _audit(outcome, connector_id=connector, event_id=event_id)
    return outcome
