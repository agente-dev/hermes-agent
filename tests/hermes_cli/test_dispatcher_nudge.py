"""Tests for the generic immediate-dispatch nudge (hermes/dispatcher/nudge.py)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes.dispatcher.nudge import (
    nudge,
    last_nudge_at,
    consume_nudge_if_fresh,
    _nudge_path,
    _board_runtime_dir,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def test_nudge_creates_file_and_updates_mtime(kanban_home):
    board = "test-board"
    p = _nudge_path(board)
    assert not p.exists()

    before = time.time()
    nudge(board=board)
    after = time.time()

    assert p.exists()
    mtime = p.stat().st_mtime
    assert before - 0.1 <= mtime <= after + 0.5


def test_nudge_default_board(kanban_home):
    os.environ["HERMES_KANBAN_BOARD"] = "default-board"
    try:
        p = _nudge_path()
        assert not p.exists()
        nudge()
        assert p.exists()
    finally:
        del os.environ["HERMES_KANBAN_BOARD"]


def test_last_nudge_at_returns_none_when_no_file(kanban_home):
    assert last_nudge_at(board="nonexistent") is None


def test_last_nudge_at_returns_mtime_after_nudge(kanban_home):
    board = "mtime-board"
    nudge(board=board)
    ts = last_nudge_at(board=board)
    assert ts is not None
    assert isinstance(ts, float)
    assert ts > 0


def test_consume_nudge_if_fresh_no_watermark(kanban_home):
    board = "fresh-board"
    nudge(board=board)
    assert consume_nudge_if_fresh(board=board) is True


def test_consume_nudge_if_fresh_with_stale_watermark(kanban_home):
    board = "stale-watermark"
    nudge(board=board)
    stale = time.time() - 10
    assert consume_nudge_if_fresh(board=board, watermark=stale) is True


def test_consume_nudge_if_fresh_with_future_watermark(kanban_home):
    board = "future-watermark"
    nudge(board=board)
    future = time.time() + 3600
    assert consume_nudge_if_fresh(board=board, watermark=future) is False


def test_consume_nudge_if_fresh_returns_false_when_no_file(kanban_home):
    assert consume_nudge_if_fresh(board="no-nudge") is False


def test_board_runtime_dir_respects_env(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "env-board")
    d = _board_runtime_dir(board=None)
    assert d.name == "runtime"
    assert d.parent.name == "env-board"
    assert d.parent.parent.name == "kanban"


def test_nudge_is_idempotent(kanban_home):
    board = "idempotent-board"
    nudge(board=board)
    mtime1 = last_nudge_at(board=board)
    time.sleep(0.01)
    nudge(board=board)
    mtime2 = last_nudge_at(board=board)
    assert mtime2 > mtime1
