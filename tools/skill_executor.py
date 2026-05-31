"""Synchronous, structured skill execution surface.

This module is the implementation behind ``POST /v1/agent/execute_skill``.
It owns the deterministic parts of a Wave-A skill run:

  1. Resolve a ``skill_id`` against the local skills index.
  2. Validate the caller's ``inputs`` against the skill's declared
     input JSON Schema (when present).
  3. Invoke an agent runner with a bounded timeout.
  4. Validate the returned ``outputs`` against the skill's declared
     output JSON Schema (when present).
  5. Translate every failure mode into a structured
     :class:`~schemas.agent_execute.AgentExecuteResponse` so the
     desktop UI doesn't have to parse free-form errors.

The actual agent invocation is pluggable.  The default runner mirrors
the leaf-agent surface that ``tools.delegate_tool.delegate_task``
uses for a single-task spawn, but tests inject a fake runner so the
executor stays unit-testable without a live model.

The synchronous endpoint is deliberately limited — multi-turn,
streamed, or human-in-the-loop skill runs flow through the
``/v1/runs`` SSE surface and the cron jobs API instead.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from schemas.agent_execute import (
    AgentExecuteRequest,
    AgentExecuteResponse,
    ExecuteError,
)


# Agent runner contract.
#
#   async (skill_id, inputs, context_budget_tokens, timeout_seconds)
#       -> Dict[str, Any] outputs
#
# Implementations may raise ``asyncio.TimeoutError`` for executor
# timeouts (we re-translate to a structured error), or any other
# Exception to signal execution_failed.
AgentRunner = Callable[[str, Dict[str, Any], int, float], Awaitable[Dict[str, Any]]]


# Process-wide default runner.  ``None`` means "no runner wired" —
# the executor will return a structured ``executor_unavailable``
# error rather than crash.  The gateway boot code sets this once
# Hermes has finished assembling the server-side AIAgent.
_DEFAULT_RUNNER: Optional[AgentRunner] = None


def register_default_runner(runner: Optional[AgentRunner]) -> None:
    """Install (or clear) the process-wide default agent runner."""
    global _DEFAULT_RUNNER
    _DEFAULT_RUNNER = runner


def get_default_runner() -> Optional[AgentRunner]:
    return _DEFAULT_RUNNER


def _validate_against_schema(
    payload: Dict[str, Any],
    schema: Dict[str, Any],
    *,
    path_prefix: str,
    error_code: str,
) -> List[ExecuteError]:
    """Validate ``payload`` against ``schema``, returning structured errors.

    Returns an empty list on success.  Each validation failure is
    converted into a single :class:`ExecuteError` so the response
    surface stays uniform.  We catch any jsonschema internal failure
    (e.g. a malformed author schema) and surface it as a
    ``schema_violation`` so a bad SKILL.md doesn't crash the
    endpoint — the operator still gets a useful message.
    """
    try:
        from jsonschema import Draft202012Validator
    except Exception as exc:  # pragma: no cover - hard dep, but defensive
        return [
            ExecuteError(
                code="executor_unavailable",
                message=f"jsonschema is unavailable: {exc}",
                path=path_prefix,
            )
        ]

    try:
        validator = Draft202012Validator(schema)
    except Exception as exc:
        return [
            ExecuteError(
                code="schema_violation",
                message=f"Skill declared an invalid JSON Schema: {exc}",
                path=path_prefix,
            )
        ]

    errors: List[ExecuteError] = []
    for verr in validator.iter_errors(payload):
        # Build a JSON-pointer-ish dotted path so the UI can highlight
        # the offending field.  ``verr.absolute_path`` is a deque of
        # str|int segments.
        segments = [str(p) for p in verr.absolute_path]
        path = path_prefix
        if segments:
            path = f"{path_prefix}." + ".".join(segments)
        errors.append(
            ExecuteError(
                code=error_code,  # type: ignore[arg-type]
                message=verr.message,
                path=path,
            )
        )
    return errors


async def execute_skill(
    request: AgentExecuteRequest,
    *,
    runner: Optional[AgentRunner] = None,
) -> AgentExecuteResponse:
    """Run ``request`` through the registered agent runner synchronously.

    Returns an :class:`AgentExecuteResponse` for every code path —
    callers (HTTP handler, in-process consumers) never need to catch
    exceptions for routine failures.
    """
    started = time.monotonic()

    # ── 1. Skill must exist ────────────────────────────────────────
    from tools.skills_hub import (
        get_skill_input_schema,
        get_skill_output_schema,
        skill_exists,
    )

    if not skill_exists(request.skill_id):
        return AgentExecuteResponse(
            status="error",
            skill_id=request.skill_id,
            errors=[
                ExecuteError(
                    code="skill_not_found",
                    message=f"Skill '{request.skill_id}' is not installed.",
                    path="skill_id",
                )
            ],
            duration_seconds=time.monotonic() - started,
        )

    # ── 2. Input validation (if a schema is declared) ──────────────
    input_schema = get_skill_input_schema(request.skill_id)
    if input_schema:
        input_errors = _validate_against_schema(
            request.inputs,
            input_schema,
            path_prefix="inputs",
            error_code="schema_violation",
        )
        if input_errors:
            return AgentExecuteResponse(
                status="error",
                skill_id=request.skill_id,
                errors=input_errors,
                duration_seconds=time.monotonic() - started,
            )

    # ── 3. Resolve a runner ────────────────────────────────────────
    effective_runner = runner or get_default_runner()
    if effective_runner is None:
        return AgentExecuteResponse(
            status="error",
            skill_id=request.skill_id,
            errors=[
                ExecuteError(
                    code="executor_unavailable",
                    message=(
                        "No agent runner is registered. The API server "
                        "must call tools.skill_executor.register_default_runner() "
                        "before serving /v1/agent/execute_skill."
                    ),
                )
            ],
            duration_seconds=time.monotonic() - started,
        )

    # ── 4. Bounded execution ───────────────────────────────────────
    timeout = request.effective_timeout()
    budget = request.effective_context_budget()
    try:
        outputs = await asyncio.wait_for(
            effective_runner(request.skill_id, request.inputs, budget, timeout),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return AgentExecuteResponse(
            status="error",
            skill_id=request.skill_id,
            errors=[
                ExecuteError(
                    code="timeout",
                    message=f"Skill execution exceeded {timeout:.1f}s budget.",
                )
            ],
            duration_seconds=time.monotonic() - started,
        )
    except Exception as exc:  # noqa: BLE001 — last line of defence
        return AgentExecuteResponse(
            status="error",
            skill_id=request.skill_id,
            errors=[
                ExecuteError(
                    code="execution_failed",
                    message=str(exc) or exc.__class__.__name__,
                    details={"exception_type": exc.__class__.__name__},
                )
            ],
            duration_seconds=time.monotonic() - started,
        )

    if not isinstance(outputs, dict):
        return AgentExecuteResponse(
            status="error",
            skill_id=request.skill_id,
            errors=[
                ExecuteError(
                    code="execution_failed",
                    message=(
                        "Skill runner returned a non-dict payload "
                        f"({type(outputs).__name__}); execute_skill requires "
                        "a JSON object."
                    ),
                )
            ],
            duration_seconds=time.monotonic() - started,
        )

    # ── 5. Output validation (if a schema is declared) ─────────────
    output_schema = get_skill_output_schema(request.skill_id)
    if output_schema:
        output_errors = _validate_against_schema(
            outputs,
            output_schema,
            path_prefix="outputs",
            error_code="schema_violation",
        )
        if output_errors:
            return AgentExecuteResponse(
                status="error",
                skill_id=request.skill_id,
                outputs=outputs,
                errors=output_errors,
                duration_seconds=time.monotonic() - started,
            )

    return AgentExecuteResponse(
        status="ok",
        skill_id=request.skill_id,
        outputs=outputs,
        duration_seconds=time.monotonic() - started,
    )
