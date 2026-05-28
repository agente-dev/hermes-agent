"""Thin subprocess wrapper over the gws (googleworkspace/cli) binary.

Each tool function shells ``gws gmail <subcommand>`` and returns the parsed
JSON response. OAuth, token refresh, and account selection are owned entirely
by the gws binary -- this plugin never touches tokens.

The binary path is resolved from AGENTE_GWS_BIN (set by the Hermes sidecar)
with a fallback to ``which gws``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)


def _resolve_gws_bin() -> str:
    env_path = os.environ.get("AGENTE_GWS_BIN")
    if env_path and os.path.isfile(env_path):
        return env_path
    resolved = shutil.which("gws")
    if resolved:
        return resolved
    return "gws"


GWS_BIN = _resolve_gws_bin()


def _gws_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("GOOGLE_WORKSPACE_CLI_CLIENT_SECRET", None)
    return env


def _gws_json(args: list[str]) -> dict:
    cmd = [GWS_BIN, *args]
    logger.debug("gws subprocess: %s", cmd)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_gws_env(),
        timeout=30,
    )
    if result.returncode != 0:
        return {"error": "gws_subprocess_failed", "exit_code": result.returncode, "stderr": result.stderr[:1024]}
    if not result.stdout.strip():
        return {"error": "gws_empty_response"}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "gws_invalid_json", "raw": result.stdout[:512]}


def _gws_json_stdin(args: list[str], stdin_text: str) -> dict:
    cmd = [GWS_BIN, *args]
    logger.debug("gws subprocess (stdin): %s", cmd)
    result = subprocess.run(
        cmd,
        input=stdin_text,
        capture_output=True,
        text=True,
        env=_gws_env(),
        timeout=30,
    )
    if result.returncode != 0:
        return {"error": "gws_subprocess_failed", "exit_code": result.returncode, "stderr": result.stderr[:1024]}
    if not result.stdout.strip():
        return {"error": "gws_empty_response"}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "gws_invalid_json", "raw": result.stdout[:512]}


def list_emails(folder: str = "INBOX", since: str | None = None, limit: int = 10, **kwargs: object) -> dict:
    args = ["gmail", "list", "--folder", folder, "--limit", str(limit), "--json"]
    if since:
        args += ["--since", since]
    return _gws_json(args)


def read_email(message_id: str, **kwargs: object) -> dict:
    return _gws_json(["gmail", "read", message_id, "--raw", "--json"])


def draft_reply(message_id: str, body: str, **kwargs: object) -> dict:
    return _gws_json_stdin(
        ["gmail", "draft", "create", "--message-id", message_id, "--body-file", "-", "--json"],
        body,
    )


def send_email(to: str, subject: str, body: str, cc: str | None = None, **kwargs: object) -> dict:
    args = ["gmail", "send", "--to", to, "--subject", subject, "--body-file", "-", "--json"]
    if cc:
        args += ["--cc", cc]
    return _gws_json_stdin(args, body)


def mark_email(message_id: str, add_labels: list[str] | None = None, remove_labels: list[str] | None = None, **kwargs: object) -> dict:
    args = ["gmail", "modify", message_id, "--json"]
    if add_labels:
        for label in add_labels:
            args += ["--add-label", label]
    if remove_labels:
        for label in remove_labels:
            args += ["--remove-label", label]
    return _gws_json(args)
