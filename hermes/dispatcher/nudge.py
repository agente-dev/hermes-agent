"""Generic immediate-dispatch nudge for the kanban dispatcher.

When a task is created or a step outcome may unblock dependents, the caller
(in particular the desktop-orchestrator profile) fires a nudge so the
gateway-embedded (or standalone) dispatcher runs its tick immediately
instead of waiting the next 60 s (or configured) interval.

The mechanism is intentionally simple and cross-process:
- A per-board "nudge file" (mtime bump) lives under the board's runtime dir.
- The dispatcher loop (in gateway/run.py and the kanban daemon path)
  checks for a fresh nudge on every 1 s sleep slice and runs dispatch_once
  early when it sees one, then updates a watermark so it doesn't re-fire
  on the same nudge.

This makes the happy path for orchestrator-driven workflows <2 s instead of
up to 60 s.

The nudge is generic so any code path that creates ready work (kanban_create
from any profile, manual board edits, etc.) can call it for instant pickup.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home


def _board_runtime_dir(board: Optional[str]) -> Path:
    """Resolve the runtime dir for a board (where the nudge file lives).

    Uses the same resolution as kanban_db for the active board.
    Falls back to the default board under the current hermes home.
    """
    board = board or os.environ.get("HERMES_KANBAN_BOARD") or "default"
    # Mirror the layout used by kanban home / board dirs
    home = get_hermes_home()
    # kanban boards live under <hermes_home>/kanban/<board>/
    return home / "kanban" / board / "runtime"


def _nudge_path(board: Optional[str] = None) -> Path:
    d = _board_runtime_dir(board)
    d.mkdir(parents=True, exist_ok=True)
    return d / ".dispatch_nudge"


def nudge(board: Optional[str] = None) -> None:
    """Fire an immediate-dispatch nudge for the given (or default) board.

    Safe to call from any process that just mutated kanban state into a
    ready-to-run condition (task created, parent completed, etc.).
    The dispatcher will notice within its 1 s slice and run a tick.
    """
    p = _nudge_path(board)
    # Touch with current time; mtime is the signal.
    p.touch()
    # Also write a tiny stamp for diagnostics (who nudged, when).
    try:
        stamp = f"{int(time.time())}:{os.getpid()}\n"
        p.with_suffix(".last").write_text(stamp)
    except Exception:
        pass


def last_nudge_at(board: Optional[str] = None) -> Optional[float]:
    """Return the mtime of the last nudge (or None)."""
    p = _nudge_path(board)
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return None


def consume_nudge_if_fresh(board: Optional[str] = None, watermark: Optional[float] = None) -> bool:
    """Return True if a nudge newer than watermark exists (and update watermark logic is caller's).

    The dispatcher loop calls this on each 1 s slice; if True it should
    run dispatch_once early and then take last_nudge_at() as the new watermark.
    """
    m = last_nudge_at(board)
    if m is None:
        return False
    if watermark is None:
        return True
    return m > watermark
