"""Tests for the drive plugin (same fake-gws-stub pattern as the email plugin)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest


def _write_stub(tmp_path: Path, *, response: str, exit_code: int = 0, log_path: Path | None = None) -> Path:
    stub_dir = tmp_path / "stub_bin"
    stub_dir.mkdir(exist_ok=True)
    stub_path = stub_dir / "gws"
    log_arg = f'\nprintf "%s\\n" "$@" >> "{log_path}"' if log_path is not None else ""
    stub_path.write_text(
        "#!/usr/bin/env bash\n"
        f"{log_arg}\n"
        f"cat <<'__GWS_STUB_EOF__'\n{response}\n__GWS_STUB_EOF__\n"
        f"exit {exit_code}\n"
    )
    stub_path.chmod(stub_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub_path


@pytest.fixture
def gws_stub(tmp_path, monkeypatch):
    log_path = tmp_path / "gws.log"

    def install(response, *, exit_code: int = 0):
        if not isinstance(response, str):
            response = json.dumps(response, ensure_ascii=False)
        stub = _write_stub(tmp_path, response=response, exit_code=exit_code, log_path=log_path)
        monkeypatch.setenv("AGENTE_GWS_BIN", str(stub))
        return stub

    return install, log_path


def test_handle_list_files(gws_stub):
    from plugins.drive.drive_plugin import handle_list_files

    install, log_path = gws_stub
    install({"files": [{"id": "f1", "name": "חוזה.pdf"}]})

    raw = handle_list_files({"limit": 25})
    out = json.loads(raw)
    assert out["limit"] == 25
    assert out["files"][0]["name"] == "חוזה.pdf"  # Hebrew filename intact

    argv = log_path.read_text().splitlines()
    assert "drive" in argv and "list" in argv and "--json" in argv


def test_handle_list_files_with_folder_and_query(gws_stub):
    from plugins.drive.drive_plugin import handle_list_files

    install, log_path = gws_stub
    install([{"id": "f1"}])

    handle_list_files({"folder_id": "folder123", "query": "name contains 'contract'"})
    argv = log_path.read_text().splitlines()
    assert "--folder" in argv and "folder123" in argv
    assert "--query" in argv


def test_handle_search_files_requires_query():
    from plugins.drive.drive_plugin import handle_search_files

    raw = handle_search_files({})
    assert "error" in json.loads(raw)


def test_handle_search_files_shells_argv(gws_stub):
    from plugins.drive.drive_plugin import handle_search_files

    install, log_path = gws_stub
    install({"files": [{"id": "f1", "name": "contract.pdf", "mimeType": "application/pdf"}]})

    raw = handle_search_files({"query": "contract pdf", "mime_type": "application/pdf", "limit": 10})
    out = json.loads(raw)
    assert out["query"] == "contract pdf"
    assert out["files"][0]["name"] == "contract.pdf"

    argv = log_path.read_text().splitlines()
    assert "search" in argv and "--q" in argv and "contract pdf" in argv
    assert "--mime" in argv and "application/pdf" in argv


def test_handle_get_file_requires_id():
    from plugins.drive.drive_plugin import handle_get_file

    raw = handle_get_file({})
    assert "error" in json.loads(raw)


def test_handle_get_file_passes_dest_path(gws_stub):
    from plugins.drive.drive_plugin import handle_get_file

    install, log_path = gws_stub
    install({"id": "f1", "downloaded_to": "/tmp/x"})

    raw = handle_get_file({"file_id": "f1", "dest_path": "/tmp/x"})
    out = json.loads(raw)
    assert out["downloaded_to"] == "/tmp/x"

    argv = log_path.read_text().splitlines()
    assert "--id" in argv and "f1" in argv
    assert "--out" in argv and "/tmp/x" in argv


def test_plugin_registers_three_tools():
    import plugins.drive as drive_plugin

    calls = []

    class FakeCtx:
        def register_tool(self, **kwargs):
            calls.append(kwargs["name"])

    drive_plugin.register(FakeCtx())
    assert set(calls) == {"list_files", "get_file", "search_files"}


def test_drive_schemas_carry_label_he_and_category():
    from plugins.drive import schemas

    for schema in (
        schemas.LIST_FILES_SCHEMA,
        schemas.GET_FILE_SCHEMA,
        schemas.SEARCH_FILES_SCHEMA,
    ):
        assert "label_he" in schema and schema["label_he"], schema["name"]
        assert schema.get("category") == "drive", schema["name"]


def test_drive_returns_gws_not_available_when_missing(monkeypatch):
    from plugins.drive.drive_plugin import handle_list_files

    monkeypatch.delenv("AGENTE_GWS_BIN", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    raw = handle_list_files({})
    out = json.loads(raw)
    assert "error" in out
    assert out.get("kind") == "gws_not_available"
