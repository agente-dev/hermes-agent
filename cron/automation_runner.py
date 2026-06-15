"""Hermes-native automation action runner.

Executes hermes-native workflow actions (scan → place-ticket sequences)
directly within the sidecar by calling desktop tools over the IPC bridge.

This module owns the step-context binding that was previously missing:
  - scan actions (list_whatsapp_messages / list_recent_messages) collect
    per-message fields (sender_jid, sender_name, message_text) from each
    returned message
  - create_ticket actions receive those fields as template variables so
    that {{sender_name}} interpolates and phone_match routing evaluates
    against the real sender JID / phone number

Called from cron.workflow_dispatch when AGENTE_TOOL_PORT is present and
the workflow contains a scan + create_ticket action sequence.

Both the manual-run and scheduled paths route through this runner when
``AGENTE_TOOL_PORT`` + ``AGENTE_TOOL_SECRET`` are configured so the
binding applies uniformly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Scan action kinds that produce per-message context consumed by later steps.
_SCAN_KINDS = frozenset({
    "list_recent_messages",
    "list_whatsapp_messages",
    "scan_whatsapp",
})

_IPC_TIMEOUT = 20  # seconds — tool calls should return quickly


# ---------------------------------------------------------------------------
# IPC helpers
# ---------------------------------------------------------------------------


def _ipc_available() -> bool:
    return bool(os.environ.get("AGENTE_TOOL_PORT") and os.environ.get("AGENTE_TOOL_SECRET"))


def _call_tool(tool_name: str, args: Dict[str, Any]) -> Tuple[bool, Any]:
    """Call a desktop tool via the Hermes IPC bridge.

    Returns ``(success, result_or_error_dict)``.
    Never raises — every code path returns a tuple.
    """
    port = os.environ.get("AGENTE_TOOL_PORT", "").strip()
    secret = os.environ.get("AGENTE_TOOL_SECRET", "").strip()
    if not port or not secret:
        return False, {"error": "agente_tool_ipc_not_configured"}

    url = f"http://127.0.0.1:{port}/dispatch/{tool_name}"
    body = json.dumps(args).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_IPC_TIMEOUT) as resp:  # noqa: S310 - loopback
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        return False, {"error": f"HTTP {exc.code}", "body": detail or exc.reason}
    except Exception as exc:  # noqa: BLE001 — defensive; never raise from here
        return False, {"error": str(exc)}

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return False, {"error": "invalid_json", "raw": raw[:256]}

    if not isinstance(envelope, dict):
        return True, envelope

    if envelope.get("ok") is True:
        return True, envelope.get("result", envelope)
    return False, envelope


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def _render_template(template: str, context: Dict[str, Any]) -> str:
    """Replace ``{{key}}`` placeholders with context values.

    Unknown keys are left as ``{{key}}`` so partial templates don't silently
    drop fields.
    """
    if not template:
        return template

    def _sub(m: "re.Match[str]") -> str:
        key = m.group(1).strip()
        if key in context:
            return str(context[key])
        return m.group(0)  # leave {{unknown}} intact

    return re.sub(r"\{\{(\w+)\}\}", _sub, template)


# ---------------------------------------------------------------------------
# Phone-match resolution
# ---------------------------------------------------------------------------


def _extract_phone(jid: str) -> str:
    """Extract the bare E.164 phone number from a WhatsApp JID.

    ``972501234567@s.whatsapp.net`` → ``972501234567``
    ``972501234567`` → ``972501234567``  (pass-through)
    """
    return jid.split("@")[0] if "@" in jid else jid


def _resolve_assignee(
    sender_jid: str,
    assigned_to: Any,
) -> Optional[str]:
    """Resolve a ``phone_match`` routing rule to an assignee name.

    ``assigned_to`` may be a list of routing rules or None:

    .. code-block:: yaml

        assigned_to:
          - phone_match: "972501234567"
            assign_to: "קטיה"
          - default: "יוסי"

    Returns the matched ``assign_to`` value, the ``default`` value if no
    phone matched, or ``None`` when ``assigned_to`` is absent / empty.
    """
    if not assigned_to or not isinstance(assigned_to, list):
        return None

    sender_phone = _extract_phone(str(sender_jid or ""))
    default_assignee: Optional[str] = None

    for rule in assigned_to:
        if not isinstance(rule, dict):
            continue
        if "phone_match" in rule:
            rule_phone = _extract_phone(str(rule["phone_match"]))
            if sender_phone and rule_phone and sender_phone == rule_phone:
                return str(rule.get("assign_to") or "")
        elif "default" in rule:
            default_assignee = str(rule["default"])

    return default_assignee


# ---------------------------------------------------------------------------
# Message context extraction
# ---------------------------------------------------------------------------


def _extract_messages(scan_result: Any) -> List[Dict[str, Any]]:
    """Normalise the result of a scan action into a list of message dicts.

    Each message dict will contain at least:
      - ``sender_jid``   (str)
      - ``sender_name``  (str)
      - ``message_text`` (str)
      - ``sender_phone`` (str, extracted from JID)
    """
    if not scan_result:
        return []

    raw: Any = scan_result
    # Some shapes nest messages under a "messages" or "chats" key.
    if isinstance(raw, dict):
        raw = (
            raw.get("messages")
            or raw.get("chats")
            or raw.get("items")
            or raw.get("result")
            or []
        )

    if not isinstance(raw, list):
        return []

    messages: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        jid = str(
            item.get("sender_jid")
            or item.get("jid")
            or item.get("from")
            or ""
        )
        name = str(
            item.get("sender_name")
            or item.get("display_name")
            or item.get("name")
            or item.get("contact_name")
            or ""
        )
        text = str(
            item.get("message_text")
            or item.get("body")
            or item.get("text")
            or item.get("content")
            or ""
        )
        messages.append({
            "sender_jid": jid,
            "sender_name": name,
            "message_text": text,
            "sender_phone": _extract_phone(jid),
        })
    return messages


# ---------------------------------------------------------------------------
# Action params resolution
# ---------------------------------------------------------------------------


def _get_action_params(action: Dict[str, Any]) -> Dict[str, Any]:
    """Return the flat params dict for an action step.

    Supports both top-level fields and nested ``params``/``args`` dicts.
    """
    nested = action.get("params") or action.get("args")
    if isinstance(nested, dict):
        # Merge: top-level (excluding 'kind', 'params', 'args') + nested
        base = {k: v for k, v in action.items() if k not in {"kind", "params", "args", "name", "tool", "action"}}
        return {**base, **nested}
    return {k: v for k, v in action.items() if k not in {"kind", "params", "args", "name", "tool", "action"}}


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


def run_workflow_actions(
    workflow: Dict[str, Any],
    trigger_context: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str, Optional[str]]:
    """Execute a workflow's actions sequentially, binding scan results to templates.

    Args:
        workflow: Loaded workflow record (dict with ``actions`` list).
        trigger_context: Optional seed context from the trigger event
            (e.g. ``{"sender_jid": ..., "sender_name": ..., "message_text": ...}``
            when triggered by a WhatsApp message directly).

    Returns:
        ``(success, summary_doc, error_or_None)``
    """
    actions: List[Dict[str, Any]] = workflow.get("actions") or []
    workflow_id = str(workflow.get("id") or "?")
    workflow_name = str(workflow.get("name_he") or workflow_id)

    if not actions:
        return True, f"# {workflow_name}\n\nNo actions defined.\n", None

    step_context: Dict[str, Any] = dict(trigger_context or {})
    scanned_messages: List[Dict[str, Any]] = []
    results: List[str] = []
    errors: List[str] = []

    # If we have a trigger-context message (wa_incoming_message path) wrap it
    # so we process it through the create_ticket step.
    if trigger_context and trigger_context.get("sender_jid"):
        scanned_messages = [trigger_context]

    for action in actions:
        if not isinstance(action, dict):
            continue
        kind = str(
            action.get("kind")
            or action.get("action")
            or action.get("tool")
            or action.get("name")
            or ""
        ).strip()
        if not kind:
            continue

        params = _get_action_params(action)

        # -- Scan step: run the tool and collect messages -------------------
        if kind in _SCAN_KINDS:
            ok, result = _call_tool(kind, params)
            if ok:
                messages = _extract_messages(result)
                scanned_messages.extend(messages)
                results.append(f"- {kind}: {len(messages)} message(s) collected")
                logger.info(
                    "automation_runner: workflow=%s scan=%s messages=%d",
                    workflow_id, kind, len(messages),
                )
            else:
                err = json.dumps(result, ensure_ascii=False)
                errors.append(f"- {kind}: {err}")
                logger.warning(
                    "automation_runner: workflow=%s scan=%s failed: %s",
                    workflow_id, kind, err,
                )
            continue

        # -- Create-ticket step: per-message template rendering + routing ---
        if kind == "create_ticket":
            title_template = str(params.get("title_template") or params.get("title") or "")
            body_template = str(params.get("body_template") or params.get("body") or "")
            assigned_to = params.get("assigned_to") or params.get("phone_routing")

            messages_to_process = scanned_messages if scanned_messages else [step_context]

            for msg in messages_to_process:
                ctx = {**step_context, **msg}
                title = _render_template(title_template, ctx) if title_template else None
                body = _render_template(body_template, ctx) if body_template else None
                assignee = _resolve_assignee(msg.get("sender_jid", ""), assigned_to)

                ticket_params: Dict[str, Any] = {}
                if title:
                    ticket_params["title"] = title
                if body:
                    ticket_params["body"] = body
                if assignee:
                    ticket_params["assignee"] = assignee
                # Preserve any other static params from the action definition
                for k, v in params.items():
                    if k not in {
                        "title", "title_template", "body", "body_template",
                        "assigned_to", "phone_routing",
                    }:
                        ticket_params.setdefault(k, v)

                if not ticket_params.get("title"):
                    errors.append("- create_ticket: missing title (template produced empty string)")
                    continue

                ok, result = _call_tool("create_ticket", ticket_params)
                sender = msg.get("sender_name") or msg.get("sender_jid") or "unknown"
                if ok:
                    results.append(
                        f"- create_ticket: '{ticket_params['title']}' "
                        f"from {sender} → {assignee or '(unassigned)'}"
                    )
                    logger.info(
                        "automation_runner: workflow=%s create_ticket title=%r assignee=%r sender=%s",
                        workflow_id, ticket_params.get("title"), assignee, sender,
                    )
                else:
                    err = json.dumps(result, ensure_ascii=False)
                    errors.append(f"- create_ticket sender={sender}: {err}")
                    logger.warning(
                        "automation_runner: workflow=%s create_ticket failed for sender=%s: %s",
                        workflow_id, sender, err,
                    )
            continue

        # -- Passthrough: call any other action kind via IPC -----------------
        ok, result = _call_tool(kind, params)
        if ok:
            results.append(f"- {kind}: ok")
        else:
            err = json.dumps(result, ensure_ascii=False)
            errors.append(f"- {kind}: {err}")

    doc_lines = [
        f"# Workflow: {workflow_name}",
        "",
        f"**Workflow ID:** {workflow_id}",
        f"**Mode:** hermes-native-runner",
        "",
        "## Results",
        *(results or ["(none)"]),
    ]
    if errors:
        doc_lines.extend(["", "## Errors", *errors])
    doc = "\n".join(doc_lines) + "\n"

    if errors:
        return False, doc, "; ".join(errors)
    return True, doc, None


def run_workflow_by_id(
    workflow_id: str,
    trigger_context: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str, Optional[str]]:
    """Load a workflow by id and run its actions.

    Returns ``(success, summary_doc, error_or_None)``.
    Returns a failure tuple when the workflow YAML cannot be loaded.
    """
    try:
        from tools.workflow_storage import load_workflow
        workflow = load_workflow(workflow_id)
    except Exception as exc:  # noqa: BLE001
        err = f"failed to load workflow {workflow_id!r}: {exc}"
        logger.warning("automation_runner: %s", err)
        return False, f"# Workflow {workflow_id}\n\n**Error:** {err}\n", err

    if workflow is None:
        err = f"workflow {workflow_id!r} not found in local store"
        logger.warning("automation_runner: %s", err)
        return False, f"# Workflow {workflow_id}\n\n**Error:** {err}\n", err

    return run_workflow_actions(workflow, trigger_context=trigger_context)
