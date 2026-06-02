"""Tests for the documents plugin (register_document_source)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# register() — tool wiring
# ---------------------------------------------------------------------------

def test_register_wires_register_document_source_tool():
    import plugins.documents as plugin

    calls = {"tools": []}

    class _Ctx:
        def register_tool(self, **kw):
            calls["tools"].append(kw["name"])

    plugin.register(_Ctx())

    assert calls["tools"] == ["register_document_source"]


def test_schema_required_fields():
    from plugins.documents.schemas import REGISTER_DOCUMENT_SOURCE_SCHEMA

    schema = REGISTER_DOCUMENT_SOURCE_SCHEMA
    assert schema["name"] == "register_document_source"
    params = schema["parameters"]
    assert params["required"] == ["file_path"]
    assert "source_type" in params["properties"]
    assert "metadata" in params["properties"]
    assert params["properties"]["file_path"]["type"] == "string"


def test_hebrew_label_present():
    import plugins.documents as plugin

    assert plugin.LABEL_HE == "רישום מסמך כמקור"
    assert plugin.CATEGORY == "documents"


# ---------------------------------------------------------------------------
# handler — validation
# ---------------------------------------------------------------------------

def test_handler_rejects_missing_file_path():
    from plugins.documents.documents_plugin import handle_register_document_source

    out = handle_register_document_source()
    assert out["ok"] is False
    assert "file_path is required" in out["error"]


def test_handler_rejects_nonexistent_file(tmp_path):
    from plugins.documents.documents_plugin import (
        _reset_approval_for_tests,
        handle_register_document_source,
    )

    _reset_approval_for_tests()
    missing = tmp_path / "does-not-exist.pdf"
    out = handle_register_document_source(file_path=str(missing))
    assert out["ok"] is False
    assert "does not exist" in out["error"]


def test_handler_rejects_directory(tmp_path):
    from plugins.documents.documents_plugin import (
        _reset_approval_for_tests,
        handle_register_document_source,
    )

    _reset_approval_for_tests()
    out = handle_register_document_source(file_path=str(tmp_path))
    assert out["ok"] is False
    assert "not a regular file" in out["error"]


# ---------------------------------------------------------------------------
# handler — happy path (IPC mocked)
# ---------------------------------------------------------------------------

def test_handler_calls_desktop_ipc_and_returns_uuid(tmp_path):
    from plugins.documents import documents_plugin as dp

    dp._reset_approval_for_tests()
    f = tmp_path / "agreement.pdf"
    f.write_bytes(b"%PDF-1.4 fake")

    fake_uuid = "11111111-2222-3333-4444-555555555555"

    captured: dict = {}

    def _fake_post(payload, timeout=15.0):
        captured["payload"] = payload
        return {
            "id": fake_uuid,
            "relative_path": "agreement.pdf",
            "status": "indexed",
            "already_existed": False,
        }

    with patch.object(dp, "_post_register", _fake_post):
        out = dp.handle_register_document_source(
            file_path=str(f),
            source_type="contract",
            metadata={"client_hint": "Aveeor"},
        )

    assert out["ok"] is True
    assert out["id"] == fake_uuid
    assert out["status"] == "indexed"
    assert out["already_existed"] is False
    assert captured["payload"]["source_type"] == "contract"
    assert captured["payload"]["metadata"] == {"client_hint": "Aveeor"}
    # file_path must be resolved to an absolute path
    assert captured["payload"]["file_path"] == str(f.resolve())


def test_handler_propagates_ipc_error(tmp_path):
    from plugins.documents import documents_plugin as dp

    dp._reset_approval_for_tests()
    f = tmp_path / "x.txt"
    f.write_text("hi")

    def _boom(payload, timeout=15.0):
        raise RuntimeError("desktop IPC unreachable at http://127.0.0.1:43117/...: refused")

    with patch.object(dp, "_post_register", _boom):
        out = dp.handle_register_document_source(file_path=str(f))

    assert out["ok"] is False
    assert "unreachable" in out["error"]


def test_handler_rejects_response_missing_id(tmp_path):
    from plugins.documents import documents_plugin as dp

    dp._reset_approval_for_tests()
    f = tmp_path / "x.txt"
    f.write_text("hi")

    with patch.object(dp, "_post_register", lambda payload, timeout=15.0: {"status": "indexed"}):
        out = dp.handle_register_document_source(file_path=str(f))

    assert out["ok"] is False
    assert "missing 'id'" in out["error"]


# ---------------------------------------------------------------------------
# approval gate
# ---------------------------------------------------------------------------

def test_first_call_requests_approval_subsequent_calls_skip(tmp_path):
    from plugins.documents import documents_plugin as dp

    dp._reset_approval_for_tests()
    f = tmp_path / "x.txt"
    f.write_text("hi")

    approval_calls = {"n": 0}

    def _approve(**kw):
        approval_calls["n"] += 1
        return {"approved": True}

    fake_resp = {"id": "uuid-1", "status": "indexed"}

    with patch.object(dp, "_post_register", lambda *a, **k: fake_resp), \
         patch("tools.approval.pre_approval_request", _approve, create=True):
        out1 = dp.handle_register_document_source(file_path=str(f))
        out2 = dp.handle_register_document_source(file_path=str(f))

    assert out1["ok"] is True
    assert out2["ok"] is True
    # First call requests approval; second call uses the cached flag.
    assert approval_calls["n"] == 1
    assert out2["approval"]["cached"] is True


def test_denied_approval_blocks_call(tmp_path):
    from plugins.documents import documents_plugin as dp

    dp._reset_approval_for_tests()
    f = tmp_path / "x.txt"
    f.write_text("hi")

    def _deny(**kw):
        return {"approved": False}

    with patch.object(dp, "_post_register", lambda *a, **k: {"id": "should-not-call"}), \
         patch("tools.approval.pre_approval_request", _deny, create=True):
        out = dp.handle_register_document_source(file_path=str(f))

    assert out["ok"] is False
    assert "denied approval" in out["error"]


def test_no_approval_framework_is_open_gate(tmp_path):
    """If tools.approval cannot be imported, the gate is open (batch/CLI runs)."""
    from plugins.documents import documents_plugin as dp

    dp._reset_approval_for_tests()
    f = tmp_path / "x.txt"
    f.write_text("hi")

    import sys
    # Simulate ImportError by removing the module from sys.modules and blocking re-import.
    real = sys.modules.pop("tools.approval", None)

    class _Blocker:
        def find_module(self, name, path=None):
            if name == "tools.approval":
                return self
            return None

        def load_module(self, name):
            raise ImportError(name)

    blocker = _Blocker()
    sys.meta_path.insert(0, blocker)
    try:
        with patch.object(dp, "_post_register", lambda *a, **k: {"id": "uuid-1"}):
            out = dp.handle_register_document_source(file_path=str(f))
    finally:
        sys.meta_path.remove(blocker)
        if real is not None:
            sys.modules["tools.approval"] = real

    assert out["ok"] is True
    assert out["id"] == "uuid-1"


# ---------------------------------------------------------------------------
# IPC URL override
# ---------------------------------------------------------------------------

def test_ipc_url_respects_env(monkeypatch):
    from plugins.documents import documents_plugin as dp

    monkeypatch.setenv("AGENTE_DESKTOP_IPC_URL", "http://example.test/ipc")
    assert dp._ipc_url() == "http://example.test/ipc"

    monkeypatch.delenv("AGENTE_DESKTOP_IPC_URL", raising=False)
    assert dp._ipc_url().startswith("http://127.0.0.1:")
