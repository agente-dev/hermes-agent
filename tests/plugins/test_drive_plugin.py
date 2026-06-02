"""Tests for the drive plugin (plugins/drive/).

Covers:

  * Tool registration via ``register(ctx)`` — names, toolset, check_fn,
    handler callability, emoji presence.
  * Schema contract — keys, required fields, label_he + category metadata.
  * ``check_drive_available`` env / PATH branches.
  * ``handle_drive_search`` + ``handle_drive_get`` subprocess argv shape
    (subprocess.run mocked — no real gws dependency).
  * Error envelopes for missing args + gws-unavailable + gws-error branches.
  * Hebrew query round-trip through the subprocess marshaller.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from plugins.drive import (
    DRIVE_GET_SCHEMA,
    DRIVE_SEARCH_SCHEMA,
    check_drive_available,
    handle_drive_get,
    handle_drive_search,
    register,
)
from plugins.drive import drive_plugin


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def _gws_env(monkeypatch):
    """Pin AGENTE_GWS_BIN to a stable fake value for the duration of the test."""
    monkeypatch.setenv("AGENTE_GWS_BIN", "/tmp/fake-gws")
    return "/tmp/fake-gws"


@pytest.fixture
def _gws_unset(monkeypatch):
    monkeypatch.delenv("AGENTE_GWS_BIN", raising=False)
    monkeypatch.setattr(drive_plugin.shutil, "which", lambda _name: None)
    return None


@pytest.fixture
def _subprocess_capture(monkeypatch):
    """Capture subprocess.run calls and return canned JSON stdout."""
    calls: dict[str, list[Any]] = {"argv": []}

    def _fake_run(cmd, **_kwargs):
        calls["argv"].append(list(cmd))
        return _FakeCompleted(stdout=calls.get("stdout", "[]"))

    monkeypatch.setattr(drive_plugin.subprocess, "run", _fake_run)
    return calls


# ---------------------------------------------------------------------------
# registration + schema contract
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_emits_both_tools(self, _gws_env):
        registered: list[dict] = []

        class FakeCtx:
            def register_tool(self, **kw: Any) -> None:
                registered.append(kw)

        register(FakeCtx())

        names = {entry["name"] for entry in registered}
        assert names == {"drive_search", "drive_get"}
        for entry in registered:
            assert entry["toolset"] == "drive"
            assert callable(entry["handler"])
            assert entry["check_fn"] is check_drive_available
            assert entry["emoji"]


class TestSchemas:
    def test_drive_search_schema_required_query(self):
        assert DRIVE_SEARCH_SCHEMA["parameters"]["required"] == ["query"]
        assert DRIVE_SEARCH_SCHEMA["name"] == "drive_search"

    def test_drive_get_schema_required_file_id(self):
        assert DRIVE_GET_SCHEMA["parameters"]["required"] == ["file_id"]
        assert DRIVE_GET_SCHEMA["name"] == "drive_get"

    def test_hebrew_labels_present(self):
        assert DRIVE_SEARCH_SCHEMA["label_he"] == "חיפוש בדרייב"
        assert DRIVE_GET_SCHEMA["label_he"] == "הורדת קובץ מדרייב"

    def test_category_is_google(self):
        assert DRIVE_SEARCH_SCHEMA["category"] == "google"
        assert DRIVE_GET_SCHEMA["category"] == "google"


# ---------------------------------------------------------------------------
# availability check
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_available_when_env_set(self, _gws_env):
        assert check_drive_available() is True

    def test_unavailable_when_unset(self, _gws_unset):
        assert check_drive_available() is False


# ---------------------------------------------------------------------------
# handle_drive_search
# ---------------------------------------------------------------------------


class TestHandleDriveSearch:
    def test_argv_shape_minimal(self, _gws_env, _subprocess_capture):
        _subprocess_capture["stdout"] = json.dumps([{"id": "f1", "name": "contract.pdf"}])
        payload = json.loads(handle_drive_search({"query": "contract"}))
        argv = _subprocess_capture["argv"][0]

        # Binary path first, then gws subcommand chain.
        assert argv[0] == "/tmp/fake-gws"
        assert argv[1:5] == ["drive", "search", "--query", "contract"]
        assert "--json" in argv
        assert "--limit" in argv
        # mime + modified_after must NOT be present when not supplied.
        assert "--mime" not in argv
        assert "--modified-after" not in argv

        assert payload["query"] == "contract"
        assert payload["files"][0]["id"] == "f1"

    def test_argv_includes_optional_filters(self, _gws_env, _subprocess_capture):
        _subprocess_capture["stdout"] = "[]"
        handle_drive_search({
            "query": "invoice",
            "mime_type": "application/pdf",
            "modified_after": "2026-05-01T00:00:00Z",
            "limit": 10,
        })
        argv = _subprocess_capture["argv"][0]
        assert "--mime" in argv and "application/pdf" in argv
        assert "--modified-after" in argv and "2026-05-01T00:00:00Z" in argv
        assert "--limit" in argv and "10" in argv

    def test_hebrew_query_round_trip(self, _gws_env, _subprocess_capture):
        _subprocess_capture["stdout"] = "[]"
        handle_drive_search({"query": "חוזה"})
        argv = _subprocess_capture["argv"][0]
        assert "חוזה" in argv

    def test_missing_query_returns_tool_error(self, _gws_env, _subprocess_capture):
        payload = json.loads(handle_drive_search({}))
        assert payload.get("error") or "query is required" in json.dumps(payload, ensure_ascii=False)

    def test_gws_unavailable_returns_tool_error(self, _gws_unset, _subprocess_capture):
        payload = json.loads(handle_drive_search({"query": "x"}))
        body = json.dumps(payload, ensure_ascii=False)
        assert "gws" in body.lower()

    def test_gws_non_zero_returns_tool_error(self, _gws_env, monkeypatch):
        def _failing_run(cmd, **_kwargs):
            return _FakeCompleted(stdout="", stderr="auth required", returncode=2)

        monkeypatch.setattr(drive_plugin.subprocess, "run", _failing_run)
        payload = json.loads(handle_drive_search({"query": "x"}))
        body = json.dumps(payload, ensure_ascii=False)
        assert "auth required" in body or "gws exited" in body


# ---------------------------------------------------------------------------
# handle_drive_get
# ---------------------------------------------------------------------------


class TestHandleDriveGet:
    def test_argv_shape(self, _gws_env, _subprocess_capture):
        _subprocess_capture["stdout"] = json.dumps({"id": "f1", "name": "contract.pdf"})
        payload = json.loads(handle_drive_get({"file_id": "f1"}))
        argv = _subprocess_capture["argv"][0]
        assert argv[1:6] == ["drive", "get", "--id", "f1", "--json"]
        assert payload["id"] == "f1"

    def test_missing_file_id_returns_tool_error(self, _gws_env, _subprocess_capture):
        payload = json.loads(handle_drive_get({}))
        body = json.dumps(payload, ensure_ascii=False)
        assert "file_id is required" in body
