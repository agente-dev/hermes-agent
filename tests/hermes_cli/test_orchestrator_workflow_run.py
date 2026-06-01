"""Tests for desktop-orchestrator workflow tools (workflow_run.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


@pytest.fixture
def board(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "wf-test")
    return "wf-test"


VALID_WORKFLOW_YAML = """
name: intake_then_schedule
steps:
  - key: intake
    assignee: intake
    title: "Intake for client {client_id}"
    body: "Process the intake for {client_id}"
    on_complete: scheduler
  - key: scheduler
    assignee: scheduler
    title: "Schedule for client {client_id}"
    body: "Schedule work for {client_id}"
"""


def test_start_workflow_run_creates_tasks(board):
    from hermes.profiles.desktop_orchestrator.tools.workflow_run import (
        start_workflow_run,
    )

    result = json.loads(start_workflow_run(VALID_WORKFLOW_YAML, client_id="client_42"))
    assert result["ok"] is True
    assert result["client_id"] == "client_42"
    assert result["workflow"] == "intake_then_schedule"
    assert len(result["task_ids"]) == 2

    conn = kb.connect(board=board)
    try:
        for tid in result["task_ids"]:
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.assignee in ("intake", "scheduler")
    finally:
        conn.close()


def test_start_workflow_run_wires_task_links(board):
    from hermes.profiles.desktop_orchestrator.tools.workflow_run import (
        start_workflow_run,
    )

    result = json.loads(start_workflow_run(VALID_WORKFLOW_YAML, "client_42"))
    task_ids = result["task_ids"]
    assert len(task_ids) == 2

    conn = kb.connect(board=board)
    try:
        intake = kb.get_task(conn, task_ids[0])
        scheduler = kb.get_task(conn, task_ids[1])
        assert intake.assignee == "intake"
        assert scheduler.assignee == "scheduler"

        children = kb.child_ids(conn, task_ids[0])
        assert task_ids[1] in children
    finally:
        conn.close()


def test_start_workflow_run_requires_yaml(board):
    from hermes.profiles.desktop_orchestrator.tools.workflow_run import (
        start_workflow_run,
    )

    result = json.loads(start_workflow_run("", "c"))
    assert "error" in result


def test_start_workflow_run_rejects_empty_steps(board):
    from hermes.profiles.desktop_orchestrator.tools.workflow_run import (
        start_workflow_run,
    )

    result = json.loads(start_workflow_run("name: no_steps\nsteps: []", "c"))
    assert "error" in result


def test_start_workflow_run_rejects_invalid_yaml(board):
    from hermes.profiles.desktop_orchestrator.tools.workflow_run import (
        start_workflow_run,
    )

    result = json.loads(start_workflow_run(": invalid: yaml: [", "c"))
    assert "error" in result


def test_start_workflow_run_stamps_workflow_metadata(board):
    from hermes.profiles.desktop_orchestrator.tools.workflow_run import (
        start_workflow_run,
    )

    result = json.loads(start_workflow_run(VALID_WORKFLOW_YAML, "client_42"))
    assert result["run_id"].startswith("intake_then_schedule:client_42:")

    conn = kb.connect(board=board)
    try:
        for tid in result["task_ids"]:
            task = kb.get_task(conn, tid)
            assert task.workflow_template_id == "intake_then_schedule"
    finally:
        conn.close()


def test_record_step_outcome(board):
    from hermes.profiles.desktop_orchestrator.tools.workflow_run import (
        record_step_outcome,
    )

    result = json.loads(record_step_outcome("run-1", "intake", "success", summary="done"))
    assert result["ok"] is True
    assert result["run_id"] == "run-1"
    assert result["step_key"] == "intake"
    assert result["outcome"] == "success"


def test_resume_step(board):
    from hermes.profiles.desktop_orchestrator.tools.workflow_run import (
        start_workflow_run,
        resume_step,
    )

    start = json.loads(start_workflow_run(VALID_WORKFLOW_YAML, "client_42"))
    task_id = start["task_ids"][0]

    conn = kb.connect(board=board)
    try:
        # Mark the task as blocked via direct SQL to test resume_step unblock path
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,))

        result = json.loads(resume_step(task_id, "approved", reason="go ahead"))
        assert result["ok"] is True
        assert result["task_id"] == task_id

        task = kb.get_task(conn, task_id)
        assert task.status != "blocked"
    finally:
        conn.close()
