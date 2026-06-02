"""Workflow-YAML scheduler collapse — single scheduler in Hermes.

Owns the desktop's office/workflows/*.yaml schedule-trigger logic so the
gateway service fires workflow rules whenever it's up, not only when the
desktop window is open.

Loaded YAML shape (one file per workflow rule):

    id: <workflow_id>
    name: optional human label
    schedule:
      cron: "*/15 * * * *"          # one of cron|interval_seconds|run_at
      interval_seconds: 900
      run_at: "2026-06-02T18:00:00"
    trigger: schedule                # only "schedule" handled here
    action: <opaque payload routed to fire_callback>

The Hermes side never executes the workflow body itself — that's the
desktop's job. We:
  1. Walk a known YAML directory each tick.
  2. Decide which workflows are due (next_run cache stored next to the YAML
     so it survives restarts).
  3. Append a row to ``workflow_engine_runs.jsonl`` with
     ``workflow_id_snapshot`` for idempotency / dedupe (mirrors the
     profiles-as-step-agents snapshot pattern).
  4. Invoke an optional ``fire_callback`` per workflow — production wires
     this to a small HTTP/IPC call back into the desktop.  Tests inject a
     spy.

Scoped to the 2-install reality of the desktop today (operator + one
client): no telemetry, no feature flags, no rollout machinery.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, List, Optional

logger = logging.getLogger(__name__)

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - yaml is a hard dep at runtime
    yaml = None

try:
    from croniter import croniter
    _HAS_CRONITER = True
except ImportError:  # pragma: no cover
    _HAS_CRONITER = False


# In-process lock guarding the next_run cache + jsonl append cycle so a
# parallel tick() in tests / multi-thread gateway cannot double-fire.
_tick_lock = threading.Lock()


@dataclass
class WorkflowSchedule:
    """A parsed schedule trigger for one workflow YAML file."""

    workflow_id: str
    yaml_path: Path
    cron_expr: Optional[str] = None
    interval_seconds: Optional[int] = None
    run_at: Optional[datetime] = None
    name: str = ""
    action: object = None
    raw: dict = field(default_factory=dict)

    def next_after(self, after: datetime) -> Optional[datetime]:
        """Compute the next fire time strictly after ``after``."""
        if self.cron_expr:
            if not _HAS_CRONITER:
                logger.warning(
                    "workflow %s: cron expression set but croniter not installed",
                    self.workflow_id,
                )
                return None
            try:
                return croniter(self.cron_expr, after).get_next(datetime)
            except Exception as exc:  # malformed expression, skip
                logger.warning(
                    "workflow %s: invalid cron expression %r (%s)",
                    self.workflow_id, self.cron_expr, exc,
                )
                return None
        if self.interval_seconds and self.interval_seconds > 0:
            return after + timedelta(seconds=self.interval_seconds)
        if self.run_at:
            # One-shot: only "next" if we haven't passed it yet.
            return self.run_at if self.run_at > after else None
        return None


def _parse_run_at(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Accept ISO-8601 (with optional trailing 'Z').
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        logger.warning("workflow run_at: cannot parse %r as ISO datetime", value)
        return None


def load_workflow_schedules(yaml_dir: Path) -> List[WorkflowSchedule]:
    """Walk ``yaml_dir`` and return schedule-triggered workflows.

    Non-schedule workflows (event/webhook triggers) are silently ignored —
    those keep firing via their existing pathways on the desktop side.
    """
    out: List[WorkflowSchedule] = []
    if yaml is None:
        logger.warning("PyYAML not available; workflow scheduler disabled")
        return out
    if not yaml_dir.exists() or not yaml_dir.is_dir():
        return out

    for path in sorted(yaml_dir.glob("*.yaml")) + sorted(yaml_dir.glob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("workflow yaml %s: failed to parse (%s)", path, exc)
            continue
        if not isinstance(data, dict):
            continue
        trigger = str(data.get("trigger") or "").strip().lower()
        schedule = data.get("schedule")
        # Treat presence of a schedule block as the trigger when explicit
        # trigger field is absent — keeps YAML terse for the common case.
        if trigger and trigger != "schedule":
            continue
        if not isinstance(schedule, dict):
            continue

        workflow_id = str(data.get("id") or path.stem).strip()
        if not workflow_id:
            continue

        cron_expr = schedule.get("cron")
        interval = schedule.get("interval_seconds")
        try:
            interval_seconds = int(interval) if interval is not None else None
        except (TypeError, ValueError):
            interval_seconds = None
        run_at = _parse_run_at(schedule.get("run_at"))

        if not (cron_expr or interval_seconds or run_at):
            continue

        out.append(WorkflowSchedule(
            workflow_id=workflow_id,
            yaml_path=path,
            cron_expr=str(cron_expr) if cron_expr else None,
            interval_seconds=interval_seconds,
            run_at=run_at,
            name=str(data.get("name") or workflow_id),
            action=data.get("action"),
            raw=data,
        ))
    return out


def _state_path(state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "workflow_next_runs.json"


def _runs_log_path(state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "workflow_engine_runs.jsonl"


def _load_state(state_dir: Path) -> dict:
    p = _state_path(state_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state_dir: Path, state: dict) -> None:
    p = _state_path(state_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def _append_run(state_dir: Path, row: dict) -> None:
    p = _runs_log_path(state_dir)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def tick(
    yaml_dir: Path,
    state_dir: Path,
    *,
    now: Optional[datetime] = None,
    fire_callback: Optional[Callable[[WorkflowSchedule, datetime], None]] = None,
) -> List[dict]:
    """Run one scheduler tick. Returns the list of rows appended this tick.

    Idempotency: each workflow's next_run is persisted in
    ``state_dir/workflow_next_runs.json``.  A workflow fires only when
    ``now >= next_run`` AND the stored ``last_fire_ts`` is older than
    ``next_run``.  After a successful fire, ``last_fire_ts`` advances to
    ``now`` and ``next_run`` is re-computed from ``now``.  This prevents
    double-fire when desktop's own ``schedule-dispatcher.ts`` also runs
    during the transitional period — both writers serialize on the
    state file's contents (last writer wins, but the dedupe key is
    ``(workflow_id, next_run)`` in the run log so re-fires are visible).
    """
    if now is None:
        now = datetime.now()
    with _tick_lock:
        state = _load_state(state_dir)
        fired: List[dict] = []
        schedules = load_workflow_schedules(yaml_dir)
        for sched in schedules:
            entry = state.get(sched.workflow_id) or {}
            next_run_iso = entry.get("next_run")
            last_fire_iso = entry.get("last_fire_ts")

            try:
                next_run = datetime.fromisoformat(next_run_iso) if next_run_iso else None
            except ValueError:
                next_run = None
            try:
                last_fire = datetime.fromisoformat(last_fire_iso) if last_fire_iso else None
            except ValueError:
                last_fire = None

            if next_run is None:
                # First sighting: fire interval/cron workflows immediately so
                # operators don't wait a full interval after enabling.  For
                # one-shot ``run_at`` the explicit timestamp is honoured.
                if sched.run_at is not None and sched.cron_expr is None and not sched.interval_seconds:
                    next_run = sched.run_at
                else:
                    next_run = now

            is_due = now >= next_run
            already_fired = last_fire is not None and last_fire >= next_run
            if not is_due or already_fired:
                state[sched.workflow_id] = {
                    "next_run": next_run.isoformat(),
                    "last_fire_ts": last_fire.isoformat() if last_fire else None,
                }
                continue

            row = {
                "workflow_id": sched.workflow_id,
                "workflow_id_snapshot": sched.workflow_id,
                "yaml_path": str(sched.yaml_path),
                "scheduled_for": next_run.isoformat(),
                "fired_at": now.isoformat(),
                "name": sched.name,
            }
            _append_run(state_dir, row)
            fired.append(row)

            if fire_callback is not None:
                try:
                    fire_callback(sched, now)
                except Exception as exc:
                    logger.warning(
                        "workflow %s: fire_callback raised %s",
                        sched.workflow_id, exc,
                    )

            new_next = sched.next_after(now)
            state[sched.workflow_id] = {
                "next_run": new_next.isoformat() if new_next else None,
                "last_fire_ts": now.isoformat(),
            }
        _save_state(state_dir, state)
        return fired


def iter_due_workflows(
    yaml_dir: Path,
    state_dir: Path,
    *,
    now: Optional[datetime] = None,
) -> Iterable[WorkflowSchedule]:
    """Read-only preview of which workflows would fire on the next tick.

    Used by the desktop's introspection UI; does not mutate state.
    """
    if now is None:
        now = datetime.now()
    state = _load_state(state_dir)
    for sched in load_workflow_schedules(yaml_dir):
        entry = state.get(sched.workflow_id) or {}
        try:
            next_run = datetime.fromisoformat(entry["next_run"]) if entry.get("next_run") else None
        except (KeyError, ValueError):
            next_run = None
        if next_run is None:
            if sched.run_at is not None and sched.cron_expr is None and not sched.interval_seconds:
                next_run = sched.run_at
            else:
                next_run = now
        if now >= next_run:
            yield sched
