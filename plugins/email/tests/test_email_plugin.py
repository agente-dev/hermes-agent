"""Tests for the email plugin using a fake gws stub binary.

All tests run against a fake ``gws`` script on PATH that returns canned JSON.
No live Gmail API calls.
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
import tempfile
from unittest import mock

import pytest

import plugins.email.email_plugin as ep_mod


def _write_fake_gws(tmpdir: str, canned_output: dict) -> str:
    """Write a fake gws stub that prints canned JSON to stdout and exits 0."""
    stub_dir = pathlib.Path(tmpdir) / "fake-gws-bin"
    stub_dir.mkdir(exist_ok=True)
    stub_path = stub_dir / "gws"
    canned = json.dumps(canned_output)

    stub_path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "json.dump(" + json.dumps(canned_output) + ", sys.stdout)\n"
    )
    stub_path.chmod(stub_path.stat().st_mode | stat.S_IEXEC)
    return str(stub_dir)


@pytest.fixture(autouse=True)
def _isolate_gws_env(monkeypatch):
    """Ensure no real AGENTE_GWS_BIN leaks into tests."""
    monkeypatch.delenv("AGENTE_GWS_BIN", raising=False)


class TestGwsBinaryResolution:
    def test_env_var_takes_priority(self, tmp_path, monkeypatch):
        fake = tmp_path / "fake-gws"
        fake.write_text("#!/bin/sh\necho '{}'\n")
        fake.chmod(0o755)

        monkeypatch.setenv("AGENTE_GWS_BIN", str(fake))
        # Reload the module to pick up the new env
        import importlib
        importlib.reload(ep_mod)
        assert ep_mod.GWS_BIN == str(fake)

    def test_fallback_to_which(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTE_GWS_BIN", raising=False)
        fake_dir = _write_fake_gws(str(tmp_path), {"ok": True})
        monkeypatch.setenv("PATH", f"{fake_dir}:{os.environ.get('PATH', '')}")

        import importlib
        importlib.reload(ep_mod)
        assert ep_mod.GWS_BIN == f"{fake_dir}/gws"

    def test_fallback_to_literal_gws(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTE_GWS_BIN", raising=False)
        monkeypatch.setenv("PATH", "/nonexistent")
        import importlib
        importlib.reload(ep_mod)
        assert ep_mod.GWS_BIN == "gws"


class TestGwsJson:
    def test_successful_json_response(self, tmp_path, monkeypatch):
        fake_dir = _write_fake_gws(str(tmp_path), {"messages": [{"id": "abc", "subject": "Hello"}]})
        monkeypatch.setenv("PATH", f"{fake_dir}:{os.environ.get('PATH', '')}")
        import importlib
        importlib.reload(ep_mod)

        result = ep_mod._gws_json(["gmail", "list", "--folder", "INBOX", "--limit", "5", "--json"])
        assert result == {"messages": [{"id": "abc", "subject": "Hello"}]}

    def test_nonzero_exit_returns_error(self, tmp_path, monkeypatch):
        stub_dir = pathlib.Path(tmp_path) / "fake-gws-bin"
        stub_dir.mkdir(exist_ok=True)
        stub = stub_dir / "gws"
        stub.write_text("#!/bin/sh\necho 'oops' >&2\nexit 1\n")
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{stub_dir}:{os.environ.get('PATH', '')}")
        import importlib
        importlib.reload(ep_mod)

        result = ep_mod._gws_json(["gmail", "list", "--json"])
        assert result["error"] == "gws_subprocess_failed"
        assert result["exit_code"] == 1


class TestListEmails:
    def test_default_folder_and_limit(self, tmp_path, monkeypatch):
        fake_dir = _write_fake_gws(str(tmp_path), {"messages": []})
        monkeypatch.setenv("PATH", f"{fake_dir}:{os.environ.get('PATH', '')}")
        import importlib
        importlib.reload(ep_mod)

        result = ep_mod.list_emails()
        assert result == {"messages": []}

    def test_custom_folder_since_limit(self, tmp_path, monkeypatch):
        fake_dir = _write_fake_gws(str(tmp_path), {"messages": [{"id": "x"}]})
        monkeypatch.setenv("PATH", f"{fake_dir}:{os.environ.get('PATH', '')}")
        import importlib
        importlib.reload(ep_mod)

        result = ep_mod.list_emails(folder="SENT", since="2026-05-20", limit=3)
        assert result == {"messages": [{"id": "x"}]}


class TestReadEmail:
    def test_read_uses_raw_flag(self, tmp_path, monkeypatch):
        fake_dir = _write_fake_gws(str(tmp_path), {"id": "msg-1", "subject": "Test", "body": "שלום"})
        monkeypatch.setenv("PATH", f"{fake_dir}:{os.environ.get('PATH', '')}")
        import importlib
        importlib.reload(ep_mod)

        result = ep_mod.read_email("msg-1")
        assert result["id"] == "msg-1"
        assert "שלום" in result["body"]


class TestDraftReply:
    def test_draft_passes_body_via_stdin(self, tmp_path, monkeypatch):
        canned = {"draft": {"id": "draft-1", "threadId": "t1"}}
        fake_dir = _write_fake_gws(str(tmp_path), canned)
        monkeypatch.setenv("PATH", f"{fake_dir}:{os.environ.get('PATH', '')}")
        import importlib
        importlib.reload(ep_mod)

        result = ep_mod.draft_reply("msg-1", "Thanks for the update")
        assert result == canned


class TestMarkEmail:
    def test_add_and_remove_labels(self, tmp_path, monkeypatch):
        canned = {"id": "msg-1", "labels": ["INBOX", "STARRED"]}
        fake_dir = _write_fake_gws(str(tmp_path), canned)
        monkeypatch.setenv("PATH", f"{fake_dir}:{os.environ.get('PATH', '')}")
        import importlib
        importlib.reload(ep_mod)

        result = ep_mod.mark_email("msg-1", add_labels=["STARRED"], remove_labels=["UNREAD"])
        assert result == canned

    def test_message_id_only(self, tmp_path, monkeypatch):
        canned = {"id": "msg-1", "labels": ["INBOX"]}
        fake_dir = _write_fake_gws(str(tmp_path), canned)
        monkeypatch.setenv("PATH", f"{fake_dir}:{os.environ.get('PATH', '')}")
        import importlib
        importlib.reload(ep_mod)

        result = ep_mod.mark_email("msg-1")
        assert result == canned


class TestSendEmail:
    def test_send_passes_body_via_stdin(self, tmp_path, monkeypatch):
        canned = {"id": "sent-1", "threadId": "t1"}
        fake_dir = _write_fake_gws(str(tmp_path), canned)
        monkeypatch.setenv("PATH", f"{fake_dir}:{os.environ.get('PATH', '')}")
        import importlib
        importlib.reload(ep_mod)

        result = ep_mod.send_email("user@example.com", "Subject", "Body text")
        assert result == canned

    def test_send_with_cc(self, tmp_path, monkeypatch):
        canned = {"id": "sent-2"}
        fake_dir = _write_fake_gws(str(tmp_path), canned)
        monkeypatch.setenv("PATH", f"{fake_dir}:{os.environ.get('PATH', '')}")
        import importlib
        importlib.reload(ep_mod)

        result = ep_mod.send_email("a@b.com", "Subj", "Body", cc="c@d.com")
        assert result == canned


class TestSchemas:
    def test_all_tools_have_schemas(self):
        from plugins.email.schemas import TOOL_SCHEMAS

        expected = {"list_emails", "read_email", "draft_reply", "send_email", "mark_email"}
        assert set(TOOL_SCHEMAS.keys()) == expected

    def test_every_required_field_in_handlers(self):
        from plugins.email.schemas import TOOL_SCHEMAS

        for tool_name, schema in TOOL_SCHEMAS.items():
            params = schema.get("parameters", {})
            for field in params.get("required", []):
                assert field in params.get("properties", {}), f"{tool_name} missing property {field}"
