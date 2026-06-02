"""Tests for the drive plugin (plugins/drive/).

Covers:

  * Tool registration via ``register(ctx)`` — names, toolset, check_fn,
    handler callability, emoji presence.
  * Schema contract — keys, required fields, label_he + category metadata.
  * ``check_drive_available`` env / PATH branches.
  * ``handle_drive_search`` + ``handle_drive_get`` gws resource/method argv
    plus ``--params`` JSON shape (subprocess.run mocked — no real gws dependency).
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


def _params_from_argv(argv: list[str]) -> dict[str, Any]:
    params_idx = argv.index("--params")
    return json.loads(argv[params_idx + 1])


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
    calls: dict[str, Any] = {"argv": [], "stdout": "[]"}

    def _fake_run(cmd, **_kwargs):
        calls["argv"].append(list(cmd))
        stdout = calls.get("stdout", "[]")
        return _FakeCompleted(stdout=stdout if isinstance(stdout, str) else "[]")

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
            assert "label_he" not in entry["schema"]
            assert "category" not in entry["schema"]


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
        _subprocess_capture["stdout"] = json.dumps({
            "files": [{
                "id": "f1",
                "name": "contract.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-06-01T00:00:00Z",
                "webViewLink": "https://drive.google.com/file/d/f1/view",
            }],
        })
        payload = json.loads(handle_drive_search({"query": "contract"}))
        argv = _subprocess_capture["argv"][0]
        params = _params_from_argv(argv)

        # Binary path first, then gws subcommand chain.
        assert argv[0] == "/tmp/fake-gws"
        assert argv[1:4] == ["drive", "files", "list"]
        assert "--params" in argv
        assert params["q"] == "fullText contains 'contract'"
        assert params["pageSize"] == 25
        assert params["fields"] == "files(id, name, mimeType, modifiedTime, webViewLink)"

        assert payload["query"] == "contract"
        assert payload["files"][0] == {
            "file_id": "f1",
            "name": "contract.pdf",
            "mime_type": "application/pdf",
            "modified_time": "2026-06-01T00:00:00Z",
            "web_view_link": "https://drive.google.com/file/d/f1/view",
        }

    def test_argv_includes_optional_filters(self, _gws_env, _subprocess_capture):
        _subprocess_capture["stdout"] = '{"files":[]}'
        handle_drive_search({
            "query": "invoice",
            "mime_type": "application/pdf",
            "modified_after": "2026-05-01T00:00:00Z",
            "limit": 10,
        })
        argv = _subprocess_capture["argv"][0]
        params = _params_from_argv(argv)
        assert params["pageSize"] == 10
        assert params["q"] == (
            "fullText contains 'invoice' and mimeType = 'application/pdf' "
            "and modifiedTime > '2026-05-01T00:00:00Z'"
        )

    def test_hebrew_query_round_trip(self, _gws_env, _subprocess_capture):
        _subprocess_capture["stdout"] = '{"files":[]}'
        handle_drive_search({"query": "חוזה"})
        argv = _subprocess_capture["argv"][0]
        params = _params_from_argv(argv)
        assert params["q"] == "fullText contains 'חוזה'"

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
        _subprocess_capture["stdout"] = json.dumps({
            "id": "f1",
            "name": "contract.pdf",
            "mimeType": "application/pdf",
            "webContentLink": "https://drive.google.com/uc?id=f1",
        })
        payload = json.loads(handle_drive_get({"file_id": "f1"}))
        argv = _subprocess_capture["argv"][0]
        params = _params_from_argv(argv)
        assert argv[1:4] == ["drive", "files", "get"]
        assert params["fileId"] == "f1"
        assert "webContentLink" in params["fields"]
        assert payload["name"] == "contract.pdf"
        assert payload["mime_type"] == "application/pdf"
        assert payload["download_url"] == "https://drive.google.com/uc?id=f1"
        assert payload["metadata"]["id"] == "f1"

    def test_missing_file_id_returns_tool_error(self, _gws_env, _subprocess_capture):
        payload = json.loads(handle_drive_get({}))
        body = json.dumps(payload, ensure_ascii=False)
        assert "file_id is required" in body
