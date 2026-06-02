"""Tests for the email plugin.

Uses a fake `gws` stub binary on $PATH (via AGENTE_GWS_BIN env var) that
returns canned JSON. No live Gmail — that path is only exercised on the
operator's machine. Covers:

  * Tool handlers shell the right gws argv and parse stdout JSON.
  * Error branches when gws is missing / exits non-zero.
  * Hebrew bodies round-trip without mojibake.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest


# --------------------------------------------------------------------------
# Fake gws stub fixture
# --------------------------------------------------------------------------

def _write_stub(tmp_path: Path, *, response: str, exit_code: int = 0, log_path: Path | None = None) -> Path:
    """Write a stub gws binary that prints `response` and exits `exit_code`.

    The stub also appends its argv to `log_path` (if given) so tests can
    assert on the exact subcommand shape we shelled.
    """
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
    """Factory: returns ``(install_stub, log_path)``.

    Call install_stub(response, exit_code=0) to set up the fake gws and
    point AGENTE_GWS_BIN at it. After calling, read log_path.read_text() to
    see the exact argv the plugin shelled.
    """
    log_path = tmp_path / "gws.log"

    def install(response, *, exit_code: int = 0):
        if not isinstance(response, str):
            response = json.dumps(response, ensure_ascii=False)
        stub = _write_stub(tmp_path, response=response, exit_code=exit_code, log_path=log_path)
        monkeypatch.setenv("AGENTE_GWS_BIN", str(stub))
        return stub

    return install, log_path


# --------------------------------------------------------------------------
# gws_runner
# --------------------------------------------------------------------------

def test_resolve_gws_bin_uses_env(monkeypatch, tmp_path):
    from plugins.email import gws_runner

    fake = tmp_path / "gws"
    fake.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("AGENTE_GWS_BIN", str(fake))
    assert gws_runner.resolve_gws_bin() == str(fake)
    assert gws_runner.gws_available() is True


def test_gws_unavailable_when_no_env_and_no_path(monkeypatch):
    from plugins.email import gws_runner

    monkeypatch.delenv("AGENTE_GWS_BIN", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    assert gws_runner.resolve_gws_bin() is None
    assert gws_runner.gws_available() is False


def test_run_gws_json_raises_call_error_on_nonzero(gws_stub):
    from plugins.email import gws_runner

    install, _ = gws_stub
    install("boom-on-stderr", exit_code=2)
    with pytest.raises(gws_runner.GwsCallError) as excinfo:
        gws_runner.run_gws_json(["gmail", "list", "--json"])
    assert excinfo.value.exit_code == 2


def test_run_gws_json_parses_stdout(gws_stub):
    from plugins.email import gws_runner

    install, _ = gws_stub
    install({"ok": True, "x": 1})
    result = gws_runner.run_gws_json(["gmail", "list", "--json"])
    assert result == {"ok": True, "x": 1}


def test_run_gws_json_raises_when_binary_missing(monkeypatch):
    from plugins.email import gws_runner

    monkeypatch.delenv("AGENTE_GWS_BIN", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    with pytest.raises(gws_runner.GwsNotAvailableError):
        gws_runner.run_gws_json(["gmail", "list", "--json"])


# --------------------------------------------------------------------------
# Email tool handlers
# --------------------------------------------------------------------------

def test_handle_list_emails_shells_expected_argv(gws_stub):
    from plugins.email.email_plugin import handle_list_emails

    install, log_path = gws_stub
    install({"messages": [{"id": "m1", "subject": "hi"}, {"id": "m2"}]})

    raw = handle_list_emails({"folder": "INBOX", "limit": 5, "query": "is:unread"})
    out = json.loads(raw)
    assert out["folder"] == "INBOX"
    assert out["limit"] == 5
    assert len(out["messages"]) == 2

    argv = log_path.read_text().splitlines()
    assert "gmail" in argv and "list" in argv and "--json" in argv
    assert "--folder" in argv and "INBOX" in argv
    assert "--limit" in argv and "5" in argv
    assert "--query" in argv and "is:unread" in argv


def test_handle_list_emails_defaults_folder_and_limit(gws_stub):
    from plugins.email.email_plugin import handle_list_emails

    install, log_path = gws_stub
    install([{"id": "m1"}])

    raw = handle_list_emails({})
    out = json.loads(raw)
    assert out["folder"] == "INBOX"
    assert out["limit"] == 10
    assert out["messages"] == [{"id": "m1"}]


def test_handle_read_email_preserves_hebrew(gws_stub):
    from plugins.email.email_plugin import handle_read_email

    install, log_path = gws_stub
    hebrew_subject = "שלום עולם"
    hebrew_body = "זה מייל בעברית עם RTL מלא — בלי mojibake."
    install({"id": "m1", "subject": hebrew_subject, "body": hebrew_body})

    raw = handle_read_email({"message_id": "m1"})
    out = json.loads(raw)
    assert out["subject"] == hebrew_subject
    assert out["body"] == hebrew_body

    argv = log_path.read_text().splitlines()
    assert "--raw" in argv  # bytes-faithful body, no bidi normalization
    assert "--id" in argv and "m1" in argv


def test_handle_read_email_requires_id():
    from plugins.email.email_plugin import handle_read_email

    raw = handle_read_email({})
    assert "error" in json.loads(raw)


def test_handle_draft_reply(gws_stub):
    from plugins.email.email_plugin import handle_draft_reply

    install, log_path = gws_stub
    install({"draft_id": "d1"})

    raw = handle_draft_reply({"message_id": "m1", "body": "תודה רבה"})
    out = json.loads(raw)
    assert out["draft_id"] == "d1"

    argv = log_path.read_text().splitlines()
    assert "draft" in argv and "--reply-to" in argv and "m1" in argv


def test_handle_draft_reply_requires_body():
    from plugins.email.email_plugin import handle_draft_reply

    raw = handle_draft_reply({"message_id": "m1", "body": ""})
    assert "error" in json.loads(raw)


def test_handle_send_email_validates_args(gws_stub):
    from plugins.email.email_plugin import handle_send_email

    install, log_path = gws_stub
    install({"sent_id": "s1"})

    raw = handle_send_email({"to": "boss@x.com", "subject": "hi", "body": "ok"})
    assert json.loads(raw)["sent_id"] == "s1"

    argv = log_path.read_text().splitlines()
    assert "send" in argv and "--to" in argv and "boss@x.com" in argv

    # Missing fields fail closed
    assert "error" in json.loads(handle_send_email({"subject": "s", "body": "b"}))
    assert "error" in json.loads(handle_send_email({"to": "x@y", "body": "b"}))
    assert "error" in json.loads(handle_send_email({"to": "x@y", "subject": "s"}))


def test_handle_mark_email_rejects_unknown_flag(gws_stub):
    from plugins.email.email_plugin import handle_mark_email

    install, _ = gws_stub
    install({"ok": True})

    raw = handle_mark_email({"message_id": "m1", "flag": "burn"})
    assert "error" in json.loads(raw)


def test_handle_mark_email_accepts_valid_flag(gws_stub):
    from plugins.email.email_plugin import handle_mark_email

    install, log_path = gws_stub
    install({"ok": True})

    raw = handle_mark_email({"message_id": "m1", "flag": "read"})
    assert json.loads(raw)["ok"] is True
    argv = log_path.read_text().splitlines()
    assert "modify" in argv and "--flag" in argv and "read" in argv


def test_tool_returns_gws_not_available_when_missing(monkeypatch):
    from plugins.email.email_plugin import handle_list_emails

    monkeypatch.delenv("AGENTE_GWS_BIN", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    raw = handle_list_emails({"folder": "INBOX"})
    out = json.loads(raw)
    assert "error" in out
    assert out.get("kind") == "gws_not_available"


# --------------------------------------------------------------------------
# Plugin registration
# --------------------------------------------------------------------------

def test_plugin_registers_five_tools():
    import plugins.email as email_plugin

    calls = []

    class FakeCtx:
        def register_tool(self, **kwargs):
            calls.append(kwargs["name"])

    email_plugin.register(FakeCtx())
    assert set(calls) == {
        "list_emails",
        "read_email",
        "draft_reply",
        "send_email",
        "mark_email",
    }


def test_schemas_carry_label_he_and_category():
    """Per hermes-agent-202606-001: tool descriptors must self-describe
    Hebrew labels + category so AuditScreen's tool dictionary auto-updates.
    """
    from plugins.email import schemas

    for schema in (
        schemas.LIST_EMAILS_SCHEMA,
        schemas.READ_EMAIL_SCHEMA,
        schemas.DRAFT_REPLY_SCHEMA,
        schemas.SEND_EMAIL_SCHEMA,
        schemas.MARK_EMAIL_SCHEMA,
    ):
        assert "label_he" in schema and schema["label_he"], schema["name"]
        assert schema.get("category") == "email", schema["name"]
