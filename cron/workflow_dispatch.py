"""Cron → workflow dispatch bridge.

Per the routine_workflow_arch (meta-202606-008): cron jobs created via
/api/jobs may carry ``metadata.workflow_ids: [...]``. When present, the
scheduler must delegate execution to the workflow engine instead of
spawning a plain LLM prompt subprocess.

This module owns the delegation step.

Resolution order for the dispatch path:

1. **Hermes-native runner** (preferred when IPC available):
   When ``AGENTE_TOOL_PORT`` + ``AGENTE_TOOL_SECRET`` are set, the
   sidecar owns the full execution — it calls the scan action via the
   IPC bridge, binds scanned message fields (sender_jid / sender_name /
   message_text) into the step context, renders ``title_template`` /
   ``body_template`` placeholders, resolves ``phone_match`` routing, and
   calls ``create_ticket`` with the fully-resolved params.  This is the
   canonical path and fixes the binding gap (meta-202606-044): without
   this binding the desktop's workflow engine received raw
   ``{{sender_name}}`` strings that were never interpolated.

2. **HTTP dispatch** (fallback when IPC unavailable):
   When IPC is not configured, falls back to the original behaviour:
   POST ``workflow_id`` + trigger context to the desktop's workflow
   engine at ``HERMES_WORKFLOW_DISPATCH_URL`` (or
   ``AGENTE_DESKTOP_BASE_URL``-derived URL via adapter patch).

   URL resolution for HTTP dispatch (resolved on each scheduled fire):

   a. ``HERMES_HOME/.env`` — re-read on every call so a companion app
      restart that changes the gateway port is picked up without
      restarting the sidecar.
   b. ``HERMES_WORKFLOW_DISPATCH_URL`` process env — fallback for
      standalone/non-desktop installs.
   c. ``AGENTE_DESKTOP_BASE_URL`` process env — base-URL construction
      fallback (patched in by the adapter layer).

3. ``None`` — caller falls back to the legacy prompt-spawn LLM path so
   pre-routine-workflow installs keep working unchanged.

Companion resolution (e.g. via companion base URL env) is patched in by
the adapter layer at startup so core stays free of companion specifics.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 30  # seconds — workflow start should return quickly; the run executes asynchronously.


def resolve_dispatch_url() -> Optional[str]:
    """Resolve the workflow-engine dispatch URL from env, or ``None``."""
    explicit = (os.getenv("HERMES_WORKFLOW_DISPATCH_URL") or "").strip()
    if explicit:
        return explicit
    # Companion support is injected via adapter patch
    # into this module's resolve_dispatch_url at register time.
    return None


def extract_workflow_ids(job: Dict[str, Any]) -> List[str]:
    """Return cron job workflow_ids as a list of clean strings, or ``[]``.

    Reads the canonical ``job["workflow_ids"]`` slot first
    (``cron.jobs._normalize_workflow_ids`` writes here for every job
    created via ``create_job`` / ``/api/jobs``). Falls back to
    ``job["metadata"]["workflow_ids"]`` for any forward-compat job
    shape that may stash routine associations under metadata.

    Tolerates non-list scalars (single string → one-element list) and
    drops empty / whitespace-only ids so an accidental ``[""]`` doesn't
    trip the non-empty check.
    """

    def _clean(raw: Any) -> List[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return []
        cleaned: List[str] = []
        for item in raw:
            text = str(item or "").strip()
            if text:
                cleaned.append(text)
        return cleaned

    top_level = _clean(job.get("workflow_ids"))
    if top_level:
        return top_level
    metadata = job.get("metadata")
    if isinstance(metadata, dict):
        return _clean(metadata.get("workflow_ids"))
    return []


def _post_json(url: str, payload: Dict[str, Any], timeout: int) -> Tuple[bool, str]:
    """POST ``payload`` as JSON, return (success, body_or_error)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback control plane
            response_body = resp.read().decode("utf-8", errors="replace")
            return True, response_body
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return False, f"HTTP {exc.code}: {detail or exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"transport error: {exc}"


def _ipc_available() -> bool:
    """Return True when the Hermes → Desktop IPC bridge is configured."""
    return bool(os.environ.get("AGENTE_TOOL_PORT") and os.environ.get("AGENTE_TOOL_SECRET"))


def dispatch_workflow_runs(
    job: Dict[str, Any],
    workflow_ids: List[str],
    *,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Tuple[bool, str, str]:
    """Start a workflow run for each id attached to a cron job.

    Returns ``(success, summary_doc, error_message_or_empty)``.

    ``success`` is True only when every workflow_id completed cleanly.
    Partial successes are reported as failures so the operator notices.

    When the IPC bridge is available (``AGENTE_TOOL_PORT`` set), the
    hermes-native runner executes each workflow directly in the sidecar,
    binding scanned message fields into template context before calling
    ``create_ticket``.  This is the preferred path.

    When IPC is unavailable, falls back to HTTP dispatch to the desktop
    workflow engine (original behaviour).
    """
    job_id = job.get("id") or "?"
    job_name = job.get("name") or job_id

    # ── Preferred: hermes-native runner (IPC available) ──────────────────
    if _ipc_available():
        return _dispatch_native(job_id, job_name, workflow_ids)

    # ── Fallback: HTTP dispatch to desktop workflow engine ────────────────
    dispatch_url = resolve_dispatch_url()
    if not dispatch_url:
        err = (
            "no workflow dispatch URL configured — set "
            "HERMES_WORKFLOW_DISPATCH_URL or configure AGENTE_TOOL_PORT "
            "for the hermes-native runner (companion support via adapter)"
        )
        logger.warning("Cron job '%s': %s", job_name, err)
        return False, "", err

    return _dispatch_http(job_id, job_name, workflow_ids, dispatch_url, timeout)


def _dispatch_native(
    job_id: str,
    job_name: str,
    workflow_ids: List[str],
) -> Tuple[bool, str, str]:
    """Run each workflow via the hermes-native action runner."""
    from cron.automation_runner import run_workflow_by_id

    results: List[str] = []
    errors: List[str] = []

    for workflow_id in workflow_ids:
        ok, wf_doc, wf_err = run_workflow_by_id(workflow_id)
        if ok:
            logger.info(
                "Cron job '%s': native run completed workflow_id=%s",
                job_name, workflow_id,
            )
            results.append(f"- {workflow_id}: completed (native runner)")
        else:
            logger.warning(
                "Cron job '%s': native run failed workflow_id=%s — %s",
                job_name, workflow_id, wf_err,
            )
            errors.append(f"- {workflow_id}: {wf_err}")

    doc_lines = [
        f"# Cron Job: {job_name}",
        "",
        f"**Job ID:** {job_id}",
        f"**Mode:** hermes-native-runner",
        f"**Workflow IDs:** {', '.join(workflow_ids)}",
        "",
        "## Results",
        *(results or ["(none)"]),
    ]
    if errors:
        doc_lines.extend(["", "## Errors", *errors])
    doc = "\n".join(doc_lines) + "\n"

    if errors:
        return False, doc, "; ".join(errors)
    return True, doc, ""


def _dispatch_http(
    job_id: str,
    job_name: str,
    workflow_ids: List[str],
    dispatch_url: str,
    timeout: int,
) -> Tuple[bool, str, str]:
    """POST each workflow_id to the desktop's workflow engine (original path)."""
    results: List[str] = []
    errors: List[str] = []
    for workflow_id in workflow_ids:
        payload = {
            "workflow_id": workflow_id,
            "trigger": {
                "source": "hermes_cron",
                "job_id": str(job_id),
                "job_name": str(job_name),
            },
        }
        ok, body = _post_json(dispatch_url, payload, timeout=timeout)
        if ok:
            logger.info(
                "Cron job '%s': dispatched workflow_id=%s via %s",
                job_name, workflow_id, dispatch_url,
            )
            results.append(f"- {workflow_id}: started ({body[:120]})")
        else:
            logger.warning(
                "Cron job '%s': workflow_id=%s dispatch failed — %s",
                job_name, workflow_id, body,
            )
            errors.append(f"- {workflow_id}: {body}")

    doc_lines = [
        f"# Cron Job: {job_name}",
        "",
        f"**Job ID:** {job_id}",
        f"**Mode:** workflow-dispatch",
        f"**Dispatch URL:** {dispatch_url}",
        f"**Workflow IDs:** {', '.join(workflow_ids)}",
        "",
        "## Results",
        *results,
    ]
    if errors:
        doc_lines.extend(["", "## Errors", *errors])
    doc = "\n".join(doc_lines) + "\n"

    if errors:
        return False, doc, "; ".join(errors)
    return True, doc, ""
