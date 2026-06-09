"""
subscription_oauth.py

Subscription OAuth session management and JSON-RPC method registration.

Houses two JSON-RPC methods — auth.start_subscription_oauth and
auth.submit_oauth_code — that drive the existing Anthropic PKCE
(agent.anthropic_adapter) and OpenAI Codex device-code (hermes_cli.auth)
flows.  Persistence reuses hermes_cli.auth_commands persist_*_oauth_credentials
helpers so the credential pool stays the single source of truth.

Per the operator delegation rule: the desktop shell calls these methods
over the gateway JSON-RPC channel; Hermes owns the OAuth client logic.
The adapter layer isolates this surface so upstream rebases of
tui_gateway/server.py cannot silently drop it again.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_OAUTH_SUBSCRIPTION_TTL_S = 15 * 60
_oauth_subscription_sessions: dict[str, dict] = {}
_oauth_subscription_lock = threading.Lock()


def _oauth_subscription_gc() -> None:
    now = time.time()
    with _oauth_subscription_lock:
        expired = [
            sid
            for sid, s in _oauth_subscription_sessions.items()
            if s.get("expires_at", 0) < now
        ]
        for sid in expired:
            _oauth_subscription_sessions.pop(sid, None)


def _oauth_label(provider: str) -> str:
    if provider == "anthropic":
        return "מנוי Claude Pro/Max"
    if provider == "openai-codex":
        return "מנוי ChatGPT Plus/Pro"
    return provider


def _resolve_provider(raw: str) -> str | None:
    provider = (raw or "").strip().lower()
    if provider == "claude":
        provider = "anthropic"
    if provider in {"codex", "openai", "chatgpt"}:
        provider = "openai-codex"
    if provider not in {"anthropic", "openai-codex"}:
        return None
    return provider


def _ok(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def handle_start_subscription_oauth(
    rid: Any,
    params: dict,
    emit_fn,
) -> dict:
    _oauth_subscription_gc()

    resolved = _resolve_provider(params.get("provider") or "")
    if resolved is None:
        return _err(rid, 4001, f"unsupported subscription oauth provider: {params.get('provider', '')}")

    provider = resolved
    session_id = uuid.uuid4().hex
    expires_at = time.time() + _OAUTH_SUBSCRIPTION_TTL_S

    if provider == "anthropic":
        try:
            from agent.anthropic_adapter import build_anthropic_authorize_url
        except Exception as exc:
            return _err(rid, 5001, f"anthropic adapter unavailable: {exc}")
        built = build_anthropic_authorize_url()
        with _oauth_subscription_lock:
            _oauth_subscription_sessions[session_id] = {
                "provider": provider,
                "expires_at": expires_at,
                "code_verifier": built["code_verifier"],
                "state": built["state"],
            }
        result = {
            "flow": "pkce",
            "session_id": session_id,
            "auth_url": built["auth_url"],
            "provider": provider,
            "label_he": _oauth_label(provider),
        }
        emit_fn(
            "clarify.request",
            session_id,
            {
                "question": "Authorization code",
                "question_he": "הדבק את קוד ההרשאה כדי לחבר את " + _oauth_label(provider),
                "kind": "oauth_code",
                "provider": provider,
                "auth_url": built["auth_url"],
            },
        )
        return _ok(rid, result)

    # provider == "openai-codex"
    try:
        from hermes_cli.auth import codex_request_device_code
    except Exception as exc:
        return _err(rid, 5001, f"codex auth module unavailable: {exc}")
    try:
        device = codex_request_device_code()
    except Exception as exc:
        return _err(
            rid,
            5002,
            f"device code request failed: {exc} (error_he: כשל בקבלת קוד מחיבור ל-{_oauth_label(provider)})",
        )

    with _oauth_subscription_lock:
        _oauth_subscription_sessions[session_id] = {
            "provider": provider,
            "expires_at": expires_at,
            "device_auth_id": device["device_auth_id"],
            "user_code": device["user_code"],
            "poll_interval": device["poll_interval"],
            "status": "pending",
        }

    def _bg_poll(sid: str, dev: dict) -> None:
        try:
            from hermes_cli.auth import codex_poll_and_exchange
            from hermes_cli.auth_commands import persist_codex_oauth_credentials

            creds = codex_poll_and_exchange(
                device_auth_id=dev["device_auth_id"],
                user_code=dev["user_code"],
                poll_interval=int(dev.get("poll_interval", 5)),
            )
            entry = persist_codex_oauth_credentials(creds)
            with _oauth_subscription_lock:
                state = _oauth_subscription_sessions.get(sid)
                if state is not None:
                    state["status"] = "completed"
                    state["label"] = entry.label
            emit_fn(
                "auth.subscription_oauth.completed",
                sid,
                {
                    "provider": "openai-codex",
                    "status": "ok",
                    "label": entry.label,
                },
            )
        except Exception as exc:
            with _oauth_subscription_lock:
                state = _oauth_subscription_sessions.get(sid)
                if state is not None:
                    state["status"] = "error"
                    state["error"] = str(exc)
            emit_fn(
                "auth.subscription_oauth.completed",
                sid,
                {
                    "provider": "openai-codex",
                    "status": "error",
                    "error": str(exc),
                    "error_he": "ההתחברות ל" + _oauth_label("openai-codex") + " נכשלה",
                },
            )

    threading.Thread(
        target=_bg_poll,
        args=(session_id, dict(device)),
        daemon=True,
        name=f"codex-oauth-poll-{session_id[:8]}",
    ).start()

    return _ok(
        rid,
        {
            "flow": "device_code",
            "session_id": session_id,
            "user_code": device["user_code"],
            "verification_uri": device["verification_uri"],
            "provider": provider,
            "label_he": _oauth_label(provider),
        },
    )


def handle_submit_oauth_code(
    rid: Any,
    params: dict,
    emit_fn,
) -> dict:
    _oauth_subscription_gc()
    session_id = (params.get("session_id") or "").strip()
    code = (params.get("code") or "").strip()
    if not session_id:
        return _err(rid, 4002, "session_id required")

    with _oauth_subscription_lock:
        state = _oauth_subscription_sessions.get(session_id)
        if state is None:
            return _err(
                rid,
                4040,
                "subscription oauth session not found or expired (error_he: ההפעלה פגה או לא נמצאה)",
            )
        provider = state.get("provider", "")

    if provider == "openai-codex":
        return _ok(
            rid,
            {
                "provider": provider,
                "status": state.get("status", "pending"),
                "label": state.get("label"),
                "error": state.get("error"),
            },
        )

    if provider != "anthropic":
        return _err(rid, 4001, f"unsupported provider for submit: {provider}")

    if not code:
        return _err(rid, 4003, "code required (error_he: קוד הרשאה חסר)")

    try:
        from agent.anthropic_adapter import exchange_anthropic_oauth_code
        from hermes_cli.auth_commands import persist_anthropic_oauth_credentials
    except Exception as exc:
        return _err(rid, 5001, f"anthropic adapter unavailable: {exc}")

    creds = exchange_anthropic_oauth_code(
        code,
        state["code_verifier"],
        state["state"],
    )
    if not creds:
        return _err(
            rid,
            5003,
            "anthropic oauth token exchange failed (error_he: החלפת קוד ההרשאה נכשלה)",
        )

    try:
        entry = persist_anthropic_oauth_credentials(creds)
    except Exception as exc:
        return _err(rid, 5004, f"persist failed: {exc} (error_he: שמירת ההרשאה נכשלה)")

    with _oauth_subscription_lock:
        _oauth_subscription_sessions.pop(session_id, None)

    emit_fn(
        "auth.subscription_oauth.completed",
        session_id,
        {
            "provider": provider,
            "status": "ok",
            "label": entry.label,
        },
    )

    return _ok(
        rid,
        {
            "provider": provider,
            "status": "ok",
            "label": entry.label,
        },
    )
