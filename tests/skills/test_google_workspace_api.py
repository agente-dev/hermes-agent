"""Tests for Google Workspace gws bridge and CLI wrapper."""

import importlib.util
import json
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


BRIDGE_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/gws_bridge.py"
)
API_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/google_api.py"
)


@pytest.fixture
def bridge_module(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    spec = importlib.util.spec_from_file_location("gws_bridge_test", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api_module(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    spec = importlib.util.spec_from_file_location("gws_api_test", API_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    # Ensure the gws CLI code path is taken even when the binary isn't
    # installed (CI).  Without this, calendar_list() falls through to the
    # Python SDK path which imports ``googleapiclient`` — not in deps.
    module._gws_binary = lambda: "/usr/bin/gws"
    # Bypass authentication check — no real token file in CI.
    module._ensure_authenticated = lambda: None
    return module


def _write_token(path: Path, *, token="ya29.test", expiry=None, **extra):
    data = {
        "token": token,
        "refresh_token": "1//refresh",
        "client_id": "123.apps.googleusercontent.com",
        "client_secret": "secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        **extra,
    }
    if expiry is not None:
        data["expiry"] = expiry
    path.write_text(json.dumps(data))


def test_bridge_returns_valid_token(bridge_module, tmp_path):
    """Non-expired token is returned without refresh."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    token_path = bridge_module.get_token_path()
    _write_token(token_path, token="ya29.valid", expiry=future)

    result = bridge_module.get_valid_token()
    assert result == "ya29.valid"


def test_bridge_refreshes_expired_token(bridge_module, tmp_path):
    """Expired token triggers a refresh via token_uri."""
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    token_path = bridge_module.get_token_path()
    _write_token(token_path, token="ya29.old", expiry=past)

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({
        "access_token": "ya29.refreshed",
        "expires_in": 3600,
    }).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = bridge_module.get_valid_token()

    assert result == "ya29.refreshed"
    # Verify persisted
    saved = json.loads(token_path.read_text())
    assert saved["token"] == "ya29.refreshed"
    assert saved["type"] == "authorized_user"


def test_bridge_refresh_passes_timeout_to_urlopen(bridge_module):
    """Token refresh must pass an explicit timeout so a hung Google endpoint
    cannot block the agent turn indefinitely (no `timeout=` defaults to the
    global socket timeout, which is unset)."""
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    token_path = bridge_module.get_token_path()
    _write_token(token_path, token="ya29.old", expiry=past)

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({
        "access_token": "ya29.refreshed",
        "expires_in": 3600,
    }).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp) as mocked:
        bridge_module.get_valid_token()

    assert mocked.call_count == 1
    _, kwargs = mocked.call_args
    assert kwargs.get("timeout") is not None, (
        "urlopen call must pass timeout= to avoid hanging on unreachable upstream"
    )


def test_bridge_refresh_exits_cleanly_on_network_error(bridge_module):
    """URLError/timeout during refresh exits 1 with a readable message
    instead of crashing with a raw traceback."""
    import urllib.error

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    token_path = bridge_module.get_token_path()
    _write_token(token_path, token="ya29.old", expiry=past)

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("timed out"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            bridge_module.get_valid_token()

    assert exc_info.value.code == 1


def test_bridge_exits_on_missing_token(bridge_module):
    """Missing token file causes exit with code 1."""
    with pytest.raises(SystemExit):
        bridge_module.get_valid_token()


def test_bridge_main_injects_token_env(bridge_module, tmp_path):
    """main() sets GOOGLE_WORKSPACE_CLI_TOKEN in subprocess env."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    token_path = bridge_module.get_token_path()
    _write_token(token_path, token="ya29.injected", expiry=future)

    captured = {}

    def capture_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return MagicMock(returncode=0)

    with patch.object(sys, "argv", ["gws_bridge.py", "gmail", "+triage"]):
        with patch.object(subprocess, "run", side_effect=capture_run):
            with pytest.raises(SystemExit):
                bridge_module.main()

    assert captured["env"]["GOOGLE_WORKSPACE_CLI_TOKEN"] == "ya29.injected"
    assert captured["cmd"] == ["gws", "gmail", "+triage"]


def test_api_calendar_list_uses_events_list(api_module):
    """calendar_list calls _run_gws with events list + params."""
    captured = {}

    def capture_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="{}", stderr="")

    args = api_module.argparse.Namespace(
        start="", end="", max=25, calendar="primary", func=api_module.calendar_list,
    )

    with patch.object(api_module.subprocess, "run", side_effect=capture_run):
        api_module.calendar_list(args)

    cmd = captured["cmd"]
    # _gws_binary() returns "/usr/bin/gws", so cmd[0] is that binary
    assert cmd[0] == "/usr/bin/gws"
    assert "calendar" in cmd
    assert "events" in cmd
    assert "list" in cmd
    assert "--params" in cmd
    params = json.loads(cmd[cmd.index("--params") + 1])
    assert "timeMin" in params
    assert "timeMax" in params
    assert params["calendarId"] == "primary"


def test_api_calendar_list_respects_date_range(api_module):
    """calendar list with --start/--end passes correct time bounds."""
    captured = {}

    def capture_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="{}", stderr="")

    args = api_module.argparse.Namespace(
        start="2026-04-01T00:00:00Z",
        end="2026-04-07T23:59:59Z",
        max=25,
        calendar="primary",
        func=api_module.calendar_list,
    )

    with patch.object(api_module.subprocess, "run", side_effect=capture_run):
        api_module.calendar_list(args)

    cmd = captured["cmd"]
    params_idx = cmd.index("--params")
    params = json.loads(cmd[params_idx + 1])
    assert params["timeMin"] == "2026-04-01T00:00:00Z"
    assert params["timeMax"] == "2026-04-07T23:59:59Z"


# ---------------------------------------------------------------------------
# Bug A — bare calendar date (YYYY-MM-DD) must expand to a full RFC3339
# instant in the configured timezone so Google Calendar stops returning 400.
# ---------------------------------------------------------------------------


def test_api_calendar_list_expands_bare_dates_to_rfc3339(api_module, monkeypatch):
    """A bare --start/--end date is expanded to a full RFC3339 instant.

    Previously a bare ``2026-06-23`` was forwarded verbatim, which is not
    RFC3339, so Google Calendar replied ``error[api]: Bad Request`` (HTTP
    400). The expansion must produce a parseable datetime with an offset.
    """
    monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Jerusalem")
    captured = {}

    def capture_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="{}", stderr="")

    args = api_module.argparse.Namespace(
        start="2026-06-23",
        end="2026-06-23",
        max=25,
        calendar="primary",
        func=api_module.calendar_list,
    )

    with patch.object(api_module.subprocess, "run", side_effect=capture_run):
        api_module.calendar_list(args)

    cmd = captured["cmd"]
    params = json.loads(cmd[cmd.index("--params") + 1])
    time_min = params["timeMin"]
    time_max = params["timeMax"]

    # No longer a bare date — must carry a time component and an offset.
    assert "T" in time_min and "T" in time_max
    assert time_min != "2026-06-23"
    # START → start of the local day; END → end of the local day.
    assert time_min.startswith("2026-06-23T00:00:00")
    assert time_max.startswith("2026-06-23T23:59:59")
    # Asia/Jerusalem in June is UTC+03:00 (IDT).
    assert "+03:00" in time_min and "+03:00" in time_max
    # And both round-trip through the RFC3339 parser Google requires.
    assert datetime.fromisoformat(time_min).tzinfo is not None
    assert datetime.fromisoformat(time_max).tzinfo is not None


def test_api_calendar_list_keeps_explicit_datetimes_unchanged(api_module):
    """A full ISO datetime with offset/Z passes through untouched."""
    captured = {}

    def capture_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="{}", stderr="")

    args = api_module.argparse.Namespace(
        start="2026-06-23T09:30:00+03:00",
        end="2026-06-23T17:00:00Z",
        max=25,
        calendar="primary",
        func=api_module.calendar_list,
    )

    with patch.object(api_module.subprocess, "run", side_effect=capture_run):
        api_module.calendar_list(args)

    cmd = captured["cmd"]
    params = json.loads(cmd[cmd.index("--params") + 1])
    assert params["timeMin"] == "2026-06-23T09:30:00+03:00"
    assert params["timeMax"] == "2026-06-23T17:00:00Z"


def test_api_calendar_list_rejects_malformed_date(api_module):
    """A malformed --start fails with a clear error, not a raw Google 400."""
    args = api_module.argparse.Namespace(
        start="2026-13-99",  # impossible month/day
        end="",
        max=25,
        calendar="primary",
        func=api_module.calendar_list,
    )
    with pytest.raises(SystemExit) as exc_info:
        api_module.calendar_list(args)
    assert exc_info.value.code == 1


def test_expand_calendar_bound_honors_configured_tz(api_module, monkeypatch):
    """A configured HERMES_TIMEZONE drives the expansion offset."""
    monkeypatch.setenv("HERMES_TIMEZONE", "America/New_York")
    expanded = api_module._expand_calendar_bound("2026-06-23", is_end=False)
    assert expanded.startswith("2026-06-23T00:00:00")
    assert "-04:00" in expanded  # America/New_York in June → EDT (UTC-4)


def test_expand_calendar_bound_falls_back_to_server_local(api_module, monkeypatch):
    """With nothing configured, expansion uses server-local time (not a
    hard-coded operator zone). The result is still a valid RFC3339 instant
    with an offset, never a bare date."""
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    expanded = api_module._expand_calendar_bound("2026-06-23", is_end=False)
    assert expanded.startswith("2026-06-23T00:00:00")
    # Must carry an offset (or Z) — i.e. a real RFC3339 instant.
    parsed = datetime.fromisoformat(expanded)
    assert parsed.tzinfo is not None


def test_api_calendar_create_keeps_offsetless_datetime_local(api_module, monkeypatch):
    """Offsetless create times must NOT be pinned to UTC (P2 review fix).

    With ``HERMES_TIMEZONE=Asia/Jerusalem`` and ``--start 2026-06-23T11:00:00``
    (no offset), the dateTime must stay offsetless so Google interprets it in
    the companion ``timeZone`` — appending ``Z`` would make the 11:00 local
    event fire at 11:00 UTC (3h off).
    """
    monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Jerusalem")
    captured = {}

    def fake_run_gws(parts, *, params=None, body=None):
        captured["body"] = body
        return {"id": "evt-syn-2", "summary": body.get("summary", "")}

    api_module._run_gws = fake_run_gws
    args = api_module.argparse.Namespace(
        summary="Synthetic Local",
        start="2026-06-23T11:00:00",  # offsetless
        end="2026-06-23T12:00:00",
        location="",
        description="",
        attendees="",
        calendar="primary",
        func=api_module.calendar_create,
    )

    api_module.calendar_create(args)

    body = captured["body"]
    # Offsetless dateTime preserved (no trailing Z), tz carried separately.
    assert body["start"]["dateTime"] == "2026-06-23T11:00:00"
    assert not body["start"]["dateTime"].endswith("Z")
    assert body["start"]["timeZone"] == "Asia/Jerusalem"
    assert body["end"]["dateTime"] == "2026-06-23T12:00:00"


# ---------------------------------------------------------------------------
# Bug C (skill path) — created events must carry the configured local
# timezone, never a hardcoded UTC, so wall-clock time is preserved.
# ---------------------------------------------------------------------------


def test_api_calendar_create_uses_configured_timezone(api_module, monkeypatch):
    """calendar create attaches the configured tz to start/end, not UTC."""
    monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Jerusalem")
    captured = {}

    def fake_run_gws(parts, *, params=None, body=None):
        captured["parts"] = parts
        captured["params"] = params
        captured["body"] = body
        return {"id": "evt-syn-1", "summary": body.get("summary", "")}

    api_module._run_gws = fake_run_gws
    args = api_module.argparse.Namespace(
        summary="Synthetic Sync",
        start="2026-06-23T11:00:00+03:00",
        end="2026-06-23T12:00:00+03:00",
        location="",
        description="",
        attendees="",
        calendar="primary",
        func=api_module.calendar_create,
    )

    api_module.calendar_create(args)

    body = captured["body"]
    assert body["start"]["timeZone"] == "Asia/Jerusalem"
    assert body["end"]["timeZone"] == "Asia/Jerusalem"
    assert body["start"]["timeZone"] != "UTC"


@pytest.mark.parametrize(
    "header_names",
    [
        ("from", "to", "subject", "date"),
        ("From", "To", "Subject", "Date"),
    ],
)
def test_api_gmail_get_reads_headers_case_insensitively(api_module, capsys, header_names):
    from_name, to_name, subject_name, date_name = header_names

    def fake_run_gws(parts, *, params=None, body=None):
        assert parts == ["gmail", "users", "messages", "get"]
        assert params == {"userId": "me", "id": "msg-1", "format": "full"}
        return {
            "id": "msg-1",
            "threadId": "thread-1",
            "labelIds": ["INBOX"],
            "payload": {
                "headers": [
                    {"name": from_name, "value": "sender@example.com"},
                    {"name": to_name, "value": "recipient@example.com"},
                    {"name": subject_name, "value": "case bug"},
                    {"name": date_name, "value": "Fri, 29 May 2026 12:00:00 +0000"},
                ],
                "body": {},
            },
        }

    api_module._run_gws = fake_run_gws
    args = api_module.argparse.Namespace(message_id="msg-1", func=api_module.gmail_get)

    api_module.gmail_get(args)

    result = json.loads(capsys.readouterr().out)
    assert result["from"] == "sender@example.com"
    assert result["to"] == "recipient@example.com"
    assert result["subject"] == "case bug"
    assert result["date"] == "Fri, 29 May 2026 12:00:00 +0000"


@pytest.mark.parametrize(
    "header_names",
    [
        ("from", "to", "subject", "date"),
        ("From", "To", "Subject", "Date"),
    ],
)
def test_api_gmail_search_reads_headers_case_insensitively(
    api_module,
    capsys,
    header_names,
):
    from_name, to_name, subject_name, date_name = header_names
    calls = []

    def fake_run_gws(parts, *, params=None, body=None):
        calls.append({"parts": parts, "params": params, "body": body})
        if parts == ["gmail", "users", "messages", "list"]:
            assert params == {"userId": "me", "q": "from:sender", "maxResults": 5}
            return {"messages": [{"id": "msg-1"}]}

        assert parts == ["gmail", "users", "messages", "get"]
        assert params == {
            "userId": "me",
            "id": "msg-1",
            "format": "metadata",
            "metadataHeaders": ["From", "To", "Subject", "Date"],
        }
        return {
            "id": "msg-1",
            "threadId": "thread-1",
            "labelIds": ["INBOX"],
            "snippet": "preview",
            "payload": {
                "headers": [
                    {"name": from_name, "value": "sender@example.com"},
                    {"name": to_name, "value": "recipient@example.com"},
                    {"name": subject_name, "value": "case bug"},
                    {"name": date_name, "value": "Fri, 29 May 2026 12:00:00 +0000"},
                ],
            },
        }

    api_module._run_gws = fake_run_gws
    args = api_module.argparse.Namespace(
        query="from:sender",
        max=5,
        func=api_module.gmail_search,
    )

    api_module.gmail_search(args)

    assert len(calls) == 2
    result = json.loads(capsys.readouterr().out)
    assert result == [
        {
            "id": "msg-1",
            "threadId": "thread-1",
            "from": "sender@example.com",
            "to": "recipient@example.com",
            "subject": "case bug",
            "date": "Fri, 29 May 2026 12:00:00 +0000",
            "snippet": "preview",
            "labels": ["INBOX"],
        }
    ]


def test_api_gmail_send_uses_conventional_mime_header_casing(api_module):
    captured = {}

    def fake_run_gws(parts, *, params=None, body=None):
        captured["parts"] = parts
        captured["params"] = params
        captured["body"] = body
        return {"id": "sent-1", "threadId": "thread-1"}

    api_module._run_gws = fake_run_gws
    args = api_module.argparse.Namespace(
        to="recipient@example.com",
        subject="hello",
        body="body",
        html=False,
        cc="copy@example.com",
        from_header="sender@example.com",
        thread_id="thread-1",
        func=api_module.gmail_send,
    )

    api_module.gmail_send(args)

    raw = api_module.base64.urlsafe_b64decode(captured["body"]["raw"])
    raw_text = raw.decode()
    assert "To: recipient@example.com" in raw_text
    assert "Subject: hello" in raw_text
    assert "Cc: copy@example.com" in raw_text
    assert "From: sender@example.com" in raw_text
    assert "\nto: " not in raw_text
    assert "\nsubject: " not in raw_text


@pytest.mark.parametrize(
    "header_names",
    [
        ("from", "subject", "message-id"),
        ("From", "Subject", "Message-ID"),
    ],
)
def test_api_gmail_reply_reads_headers_case_insensitively_and_uses_conventional_mime_header_casing(
    api_module,
    header_names,
):
    from_name, subject_name, message_id_name = header_names
    calls = []

    def fake_run_gws(parts, *, params=None, body=None):
        calls.append({"parts": parts, "params": params, "body": body})
        if parts == ["gmail", "users", "messages", "get"]:
            assert params == {
                "userId": "me",
                "id": "msg-1",
                "format": "metadata",
                "metadataHeaders": ["From", "Subject", "Message-ID"],
            }
            return {
                "id": "msg-1",
                "threadId": "thread-1",
                "payload": {
                    "headers": [
                        {"name": from_name, "value": "sender@example.com"},
                        {"name": subject_name, "value": "case bug"},
                        {"name": message_id_name, "value": "<msg-1@example.com>"},
                    ],
                },
            }

        assert parts == ["gmail", "users", "messages", "send"]
        assert params == {"userId": "me"}
        return {"id": "sent-1", "threadId": "thread-1"}

    api_module._run_gws = fake_run_gws
    args = api_module.argparse.Namespace(
        message_id="msg-1",
        body="reply body",
        from_header="recipient@example.com",
        func=api_module.gmail_reply,
    )

    api_module.gmail_reply(args)

    assert len(calls) == 2
    body = calls[1]["body"]
    assert body["threadId"] == "thread-1"
    raw = api_module.base64.urlsafe_b64decode(body["raw"])
    raw_text = raw.decode()
    assert "To: sender@example.com" in raw_text
    assert "Subject: Re: case bug" in raw_text
    assert "From: recipient@example.com" in raw_text
    assert "In-Reply-To: <msg-1@example.com>" in raw_text
    assert "References: <msg-1@example.com>" in raw_text
    assert "\nto: " not in raw_text
    assert "\nsubject: " not in raw_text
    assert "\nin-reply-to: " not in raw_text
    assert "\nreferences: " not in raw_text


def test_api_get_credentials_refresh_persists_authorized_user_type(api_module, monkeypatch):
    token_path = api_module.TOKEN_PATH
    _write_token(token_path, token="ya29.old")

    class FakeCredentials:
        def __init__(self):
            self.expired = True
            self.refresh_token = "1//refresh"
            self.valid = True

        def refresh(self, request):
            self.expired = False

        def to_json(self):
            return json.dumps({
                "token": "ya29.refreshed",
                "refresh_token": "1//refresh",
                "client_id": "123.apps.googleusercontent.com",
                "client_secret": "secret",
                "token_uri": "https://oauth2.googleapis.com/token",
            })

    class FakeCredentialsModule:
        @staticmethod
        def from_authorized_user_file(filename, scopes):
            assert filename == str(token_path)
            assert scopes == api_module.SCOPES
            return FakeCredentials()

    google_module = types.ModuleType("google")
    oauth2_module = types.ModuleType("google.oauth2")
    credentials_module = types.ModuleType("google.oauth2.credentials")
    credentials_module.Credentials = FakeCredentialsModule
    transport_module = types.ModuleType("google.auth.transport")
    requests_module = types.ModuleType("google.auth.transport.requests")
    requests_module.Request = lambda: object()

    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2_module)
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", credentials_module)
    monkeypatch.setitem(sys.modules, "google.auth.transport", transport_module)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", requests_module)

    creds = api_module.get_credentials()

    saved = json.loads(token_path.read_text())
    assert isinstance(creds, FakeCredentials)
    assert saved["token"] == "ya29.refreshed"
    assert saved["type"] == "authorized_user"
