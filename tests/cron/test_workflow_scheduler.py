"""Tests for the YAML-workflow scheduler collapse (hermes-agent-202606-012)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from cron import workflow_scheduler as ws


def _write_yaml(dir_: Path, name: str, body: str) -> Path:
    p = dir_ / name
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def dirs(tmp_path: Path):
    yaml_dir = tmp_path / "workflows"
    yaml_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return yaml_dir, state_dir


def test_load_workflow_schedules_filters_non_schedule(dirs):
    yaml_dir, _ = dirs
    _write_yaml(yaml_dir, "a.yaml", "id: wf-a\ntrigger: schedule\nschedule:\n  interval_seconds: 60\n")
    _write_yaml(yaml_dir, "b.yaml", "id: wf-b\ntrigger: webhook\nschedule:\n  interval_seconds: 60\n")
    _write_yaml(yaml_dir, "c.yaml", "id: wf-c\nschedule:\n  cron: '*/5 * * * *'\n")  # implicit schedule trigger
    _write_yaml(yaml_dir, "d.yaml", "id: wf-d\nschedule: {}\n")  # no concrete schedule fields
    _write_yaml(yaml_dir, "e.yml", "id: wf-e\nschedule:\n  run_at: '2099-01-01T00:00:00'\n")

    out = ws.load_workflow_schedules(yaml_dir)
    ids = sorted(s.workflow_id for s in out)
    assert ids == ["wf-a", "wf-c", "wf-e"]


def test_tick_with_three_schedules_fires_only_the_due_one(dirs):
    yaml_dir, state_dir = dirs
    base = datetime(2026, 6, 2, 12, 0, 0)
    _write_yaml(yaml_dir, "every-min.yaml",
                "id: wf-min\ntrigger: schedule\nschedule:\n  interval_seconds: 60\n")
    _write_yaml(yaml_dir, "every-hour.yaml",
                "id: wf-hour\ntrigger: schedule\nschedule:\n  interval_seconds: 3600\n")
    _write_yaml(yaml_dir, "future-oneshot.yaml",
                "id: wf-future\ntrigger: schedule\nschedule:\n  run_at: '2099-01-01T00:00:00'\n")

    # First tick — every-min and every-hour are due (initial next_run is in the past),
    # future-oneshot is not.
    fired = ws.tick(yaml_dir, state_dir, now=base)
    ids = sorted(r["workflow_id"] for r in fired)
    assert ids == ["wf-hour", "wf-min"]

    # 30s later — neither is due.
    fired_30 = ws.tick(yaml_dir, state_dir, now=base + timedelta(seconds=30))
    assert fired_30 == []

    # 70s after base — only wf-min is due again.
    fired_70 = ws.tick(yaml_dir, state_dir, now=base + timedelta(seconds=70))
    ids2 = [r["workflow_id"] for r in fired_70]
    assert ids2 == ["wf-min"]


def test_engine_runs_jsonl_has_workflow_id_snapshot(dirs):
    yaml_dir, state_dir = dirs
    _write_yaml(yaml_dir, "x.yaml",
                "id: wf-x\nname: Daily Cleanup\ntrigger: schedule\nschedule:\n  interval_seconds: 1\n")
    fired = ws.tick(yaml_dir, state_dir, now=datetime(2026, 6, 2, 9, 0, 0))
    assert len(fired) == 1

    log_path = state_dir / "workflow_engine_runs.jsonl"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["workflow_id"] == "wf-x"
    assert row["workflow_id_snapshot"] == "wf-x"
    assert row["name"] == "Daily Cleanup"
    assert "fired_at" in row
    assert "scheduled_for" in row


def test_no_double_fire_across_consecutive_ticks(dirs):
    """Simulates the transitional period where desktop schedule-dispatcher
    may also call tick(): the persisted next_run guards against re-fires."""
    yaml_dir, state_dir = dirs
    _write_yaml(yaml_dir, "x.yaml",
                "id: wf-x\ntrigger: schedule\nschedule:\n  interval_seconds: 600\n")
    now = datetime(2026, 6, 2, 10, 0, 0)
    a = ws.tick(yaml_dir, state_dir, now=now)
    b = ws.tick(yaml_dir, state_dir, now=now)
    c = ws.tick(yaml_dir, state_dir, now=now + timedelta(seconds=5))
    assert len(a) == 1
    assert b == []
    assert c == []


def test_fire_callback_invoked_per_fire(dirs):
    yaml_dir, state_dir = dirs
    _write_yaml(yaml_dir, "x.yaml",
                "id: wf-x\ntrigger: schedule\nschedule:\n  interval_seconds: 60\naction: do_something\n")
    seen = []

    def cb(sched, now):
        seen.append((sched.workflow_id, sched.action, now))

    ws.tick(yaml_dir, state_dir,
            now=datetime(2026, 6, 2, 8, 0, 0),
            fire_callback=cb)
    assert len(seen) == 1
    assert seen[0][0] == "wf-x"
    assert seen[0][1] == "do_something"


def test_malformed_yaml_is_skipped_not_crash(dirs):
    yaml_dir, state_dir = dirs
    (yaml_dir / "bad.yaml").write_text(": :\n- not valid", encoding="utf-8")
    _write_yaml(yaml_dir, "good.yaml",
                "id: wf-good\ntrigger: schedule\nschedule:\n  interval_seconds: 60\n")
    fired = ws.tick(yaml_dir, state_dir, now=datetime(2026, 6, 2, 12, 0, 0))
    assert [r["workflow_id"] for r in fired] == ["wf-good"]


def test_cron_expression(dirs):
    pytest.importorskip("croniter")
    yaml_dir, state_dir = dirs
    _write_yaml(yaml_dir, "x.yaml",
                "id: wf-x\ntrigger: schedule\nschedule:\n  cron: '0 * * * *'\n")
    fired = ws.tick(yaml_dir, state_dir, now=datetime(2026, 6, 2, 12, 0, 30))
    assert [r["workflow_id"] for r in fired] == ["wf-x"]
    fired2 = ws.tick(yaml_dir, state_dir, now=datetime(2026, 6, 2, 12, 30, 0))
    assert fired2 == []
    fired3 = ws.tick(yaml_dir, state_dir, now=datetime(2026, 6, 2, 13, 0, 5))
    assert [r["workflow_id"] for r in fired3] == ["wf-x"]


def test_missing_workflows_dir_silent(tmp_path: Path):
    out = ws.load_workflow_schedules(tmp_path / "missing")
    assert out == []
