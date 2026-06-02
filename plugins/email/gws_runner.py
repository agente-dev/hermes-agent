"""Thin subprocess marshaller for the bundled `gws` (Google Workspace CLI) binary.

Shared by `plugins/email/` and `plugins/drive/`. The plugin layer never touches
OAuth tokens — gws owns the entire token lifecycle in its file-backed keyring
at `<userData>/gws-config/`. We just shell out, parse stdout JSON, return.

Binary resolution:
  1. `AGENTE_GWS_BIN` env var (set by Electron main / hermes-sidecar.ts when
     spawning the Hermes sidecar so the bundled gws is found inside the
     packaged app's extraResources). Hard dep on agente-desktop intake
     `agente-desktop__intake-bundle-gws-binary-extraresources__1`.
  2. `shutil.which("gws")` fallback (developer workstations / CI).

Subprocess env:
  - `GOOGLE_WORKSPACE_CLI_CLIENT_SECRET` is forwarded if set on the parent
    Hermes process; it is NOT echoed in logs or error payloads.

Redaction:
  - On failure we return the gws exit code + stderr's last line; we do not
    dump the full env or any token material. (Sensitive-evidence policy on
    intake hermes-agent-202606-011.)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


class GwsNotAvailableError(RuntimeError):
    """gws binary missing — operator has not connected Google Workspace yet."""


class GwsCallError(RuntimeError):
    """gws ran but exited non-zero (auth required, network down, etc.)."""

    def __init__(self, message: str, *, exit_code: int, stderr_tail: str | None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail


def resolve_gws_bin() -> Optional[str]:
    """Locate the gws binary. Returns None when unavailable."""
    explicit = os.environ.get("AGENTE_GWS_BIN")
    if explicit and os.path.exists(explicit):
        return explicit
    return shutil.which("gws")


def gws_available() -> bool:
    """Check-fn for the plugin loader: are gws tools dispatchable?"""
    return resolve_gws_bin() is not None


def _gws_env() -> dict:
    """Build subprocess env. Forwards client-secret if set, scrubs nothing
    else (gws needs PATH, HOME, USER, etc.).
    """
    return dict(os.environ)


def _stderr_tail(stderr: str | bytes, limit: int = 240) -> str:
    if isinstance(stderr, bytes):
        try:
            stderr = stderr.decode("utf-8", errors="replace")
        except Exception:
            stderr = ""
    stderr = (stderr or "").strip().splitlines()
    if not stderr:
        return ""
    last = stderr[-1]
    return last[:limit]


def run_gws_json(args: Iterable[str], *, timeout: float = 30.0) -> Any:
    """Run `gws <args> --json`, parse stdout, return decoded JSON.

    The `--json` flag is appended by the caller (most gws subcommands
    accept it); this function just shells and parses.
    """
    gws_bin = resolve_gws_bin()
    if not gws_bin:
        raise GwsNotAvailableError(
            "gws binary not found (set AGENTE_GWS_BIN or install gws on PATH)"
        )

    argv = [gws_bin, *list(args)]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            env=_gws_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise GwsCallError(
            f"gws call timed out after {timeout}s",
            exit_code=-1,
            stderr_tail=None,
        )

    if proc.returncode != 0:
        tail = _stderr_tail(proc.stderr)
        raise GwsCallError(
            f"gws exited {proc.returncode}: {tail}",
            exit_code=proc.returncode,
            stderr_tail=tail,
        )

    stdout = proc.stdout.decode("utf-8", errors="replace") if isinstance(proc.stdout, bytes) else proc.stdout
    stdout = (stdout or "").strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise GwsCallError(
            f"gws returned non-JSON stdout: {exc}",
            exit_code=proc.returncode,
            stderr_tail=None,
        )
