"""Email connector plugin — thin subprocess wrapper over the bundled gws CLI.

Per `hermes-202605-014`, this plugin exposes five tools (list_emails, read_email,
draft_reply, send_email, mark_email) that shell out to the bundled
``googleworkspace/cli`` (``gws``) binary. OAuth, token storage, refresh, and
account management are owned entirely by gws — this module never touches
credentials.

Binary resolution order:

1. ``AGENTE_GWS_BIN`` environment variable (set by the desktop shell when
   spawning the agent runtime, pointing at the bundled binary inside the app
   resources).
2. ``shutil.which("gws")`` — a developer-installed gws on PATH.

If neither resolves the tools return a clear ``gws not bundled`` error so the
caller can surface a Connectors-UI prompt.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any


class GwsUnavailableError(RuntimeError):
    """Raised when no gws binary can be located."""


def _resolve_gws_bin() -> str:
    candidate = os.environ.get("AGENTE_GWS_BIN") or shutil.which("gws")
    if not candidate:
        raise GwsUnavailableError(
            "gws not bundled: set AGENTE_GWS_BIN or install googleworkspace/cli on PATH"
        )
    return candidate


def _run_gws(args: list[str]) -> Any:
    """Run ``gws <args>`` and return parsed JSON stdout."""
    gws_bin = _resolve_gws_bin()
    proc = subprocess.run(
        [gws_bin, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gws {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    stdout = proc.stdout.strip()
    if not stdout:
        return {}
    return json.loads(stdout)


def list_emails(folder: str = "INBOX", max: int = 10) -> Any:
    """Return the most recent messages in ``folder`` (default INBOX)."""
    return _run_gws(["gmail", "messages", "list", "--folder", folder, "--max", str(max), "--json"])


def read_email(message_id: str) -> Any:
    """Return full message payload for ``message_id`` (raw body preserved)."""
    return _run_gws(["gmail", "messages", "get", message_id, "--json", "--raw"])


def draft_reply(message_id: str, body: str) -> Any:
    """Create a Gmail draft replying to ``message_id`` with ``body``."""
    return _run_gws(["gmail", "drafts", "create", "--in-reply-to", message_id, "--body", body, "--json"])


def send_email(to: str, subject: str, body: str) -> Any:
    """Send a new email."""
    return _run_gws(["gmail", "messages", "send", "--to", to, "--subject", subject, "--body", body, "--json"])


def mark_email(message_id: str, add_label: str | None = None, remove_label: str | None = None) -> Any:
    """Add or remove a Gmail label on ``message_id``."""
    args = ["gmail", "messages", "modify", message_id, "--json"]
    if add_label:
        args += ["--add-label", add_label]
    if remove_label:
        args += ["--remove-label", remove_label]
    return _run_gws(args)
