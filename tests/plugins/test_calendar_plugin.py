"""Unit tests for the calendar plugin — uses a fake gws stub binary."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

import plugins.calendar as calendar_pkg
from plugins.calendar import calendar_plugin as cp
from plugins.calendar.schemas import (
    CREATE_CALENDAR_EVENT_SCHEMA,
    GET_CALENDAR_EVENT_SCHEMA,
    LIST_CALENDAR_EVENTS_SCHEMA,
)


# ---------------------------------------------------------------------------
# Fake gws stub — a tiny shell script that echoes a JSON envelope describing
# the args it was called with. Lets us assert the plugin shells the right
# command without depending on the real gws binary or live Google APIs.
# ---------------------------------------------------------------------------

_STUB_SCRIPT = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # Fake gws — emits canned JSON per subcommand for tests.
    action="$3"
    case "$action" in
      list)
        echo '[{"id":"evt1","start":"2026-06-02T09:00:00Z","end":"2026-06-02T10:00:00Z","title":"פגישה עם לקוח"}]'
        ;;
      insert)
        # Parse --json body for the summary (Hebrew/RTL passthrough test).
        body_json=""
        while [ "$#" -gt 0 ]; do
          if [ "$1" = "--json" ]; then body_json="$2"; shift 2; else shift; fi
        done
        title="$(printf '%s' "$body_json" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("summary",""))' 2>/dev/null)"
        printf '{"id":"new-evt","htmlLink":"https://calendar.google.com/event?eid=x","summary":%s}\\n' "$(printf '%s' "$title" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')"
        ;;
      get)
        echo '{"id":"evt1","start":"2026-06-02T09:00:00Z","end":"2026-06-02T10:00:00Z"}'
        ;;
      *)
        echo "unknown calendar action: $action" >&2
        exit 2
        ;;
    esac
    """
)


@pytest.fixture
def fake_gws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    stub = tmp_path / "gws"
    stub.write_text(_STUB_SCRIPT)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("AGENTE_GWS_BIN", str(stub))
    return stub


def test_resolve_gws_bin_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTE_GWS_BIN", "/opt/bundle/gws")
    assert cp._gws_bin() == "/opt/bundle/gws"


def test_list_calendar_events_shells_gws(fake_gws: Path) -> None:
    events = cp.list_calendar_events(after="today", before="today+1d")
    assert isinstance(events, list)
    assert len(events) == 1
    assert events[0]["id"] == "evt1"
    # Hebrew survives end-to-end (no mojibake from the subprocess pipe).
    assert events[0]["title"] == "פגישה עם לקוח"


def test_list_calendar_events_with_calendar_and_limit(fake_gws: Path) -> None:
    # Smoke: optional args don't break the wrapper.
    events = cp.list_calendar_events(
        after="today", before="today+7d", calendar_id="cal-xyz", limit=10,
    )
    assert events and events[0]["id"] == "evt1"


def test_create_calendar_event_preserves_hebrew_title(fake_gws: Path) -> None:
    event = cp.create_calendar_event(
        title="ישיבה עם רואה החשבון",
        start="2026-06-02T14:00:00Z",
        end="2026-06-02T15:00:00Z",
        attendees=["a@x.com", "b@x.com"],
        description="מעקב חשבונית",
    )
    assert event["id"] == "new-evt"
    # The stub echoes the summary verbatim — proves the Hebrew title round-trips
    # through --json payload → stdout JSON → json.loads.
    assert event["summary"] == "ישיבה עם רואה החשבון"


def test_raw_calendar_body_calls_use_json_flag_not_body(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001, ANN003
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='{"ok": true}', stderr="")

    monkeypatch.setenv("AGENTE_GWS_BIN", "/fake/bin/gws")
    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    cp.create_calendar_event(title="x", start="2026-06-02T14:00:00Z", end="2026-06-02T15:00:00Z")
    cp.update_calendar_event(event_id="evt1", title="updated")

    assert calls
    for argv in calls:
        assert "--json" in argv
        assert "--body" not in argv
        assert json.loads(argv[argv.index("--json") + 1])


def test_create_calendar_event_uses_configured_timezone_not_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Created events must carry the configured local timezone, not UTC.

    Bug C: ``calendar_plugin`` hardcoded ``"timeZone": "UTC"`` for both
    start and end, so an 11:00 Asia/Jerusalem event was written as 11:00 UTC
    (3 hours off). The body must now reflect the resolved local zone.
    """
    monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Jerusalem")
    # hermes_time caches the resolved zone; force a fresh read for this test.
    import hermes_time

    hermes_time._cached_tz = None
    hermes_time._cached_tz_name = None
    hermes_time._cache_resolved = False

    captured: dict = {}

    def fake_run(cmd, **_kwargs):  # noqa: ANN001, ANN003
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"id":"new-evt"}', stderr="",
        )

    monkeypatch.setenv("AGENTE_GWS_BIN", "/fake/bin/gws")
    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    cp.create_calendar_event(
        title="Synthetic Standup",
        start="2026-06-23T11:00:00+03:00",
        end="2026-06-23T11:30:00+03:00",
    )

    argv = captured["cmd"]
    body = json.loads(argv[argv.index("--json") + 1])
    assert body["start"]["timeZone"] == "Asia/Jerusalem"
    assert body["end"]["timeZone"] == "Asia/Jerusalem"
    assert body["start"]["timeZone"] != "UTC"
    assert body["end"]["timeZone"] != "UTC"


def test_create_calendar_event_timezone_falls_back_to_server_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no configured tz, events use the server-local zone — NOT a
    hard-coded operator timezone. The key contract is that a real zone name
    is attached (never empty), mirroring hermes_time's server-local fallback.
    """
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    import hermes_time

    hermes_time._cached_tz = None
    hermes_time._cached_tz_name = None
    hermes_time._cache_resolved = False

    captured: dict = {}

    def fake_run(cmd, **_kwargs):  # noqa: ANN001, ANN003
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"id":"new-evt"}', stderr="",
        )

    monkeypatch.setenv("AGENTE_GWS_BIN", "/fake/bin/gws")
    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    cp.create_calendar_event(
        title="Synthetic Review",
        start="2026-06-23T14:00:00+03:00",
        end="2026-06-23T15:00:00+03:00",
    )

    argv = captured["cmd"]
    body = json.loads(argv[argv.index("--json") + 1])
    # A concrete zone is attached (server-local), never empty.
    assert body["start"]["timeZone"]
    assert body["end"]["timeZone"]


def test_create_calendar_event_honors_configured_non_jerusalem_tz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured non-Jerusalem zone is attached verbatim (no hard-code)."""
    monkeypatch.setenv("HERMES_TIMEZONE", "America/New_York")
    import hermes_time

    hermes_time._cached_tz = None
    hermes_time._cached_tz_name = None
    hermes_time._cache_resolved = False

    captured: dict = {}

    def fake_run(cmd, **_kwargs):  # noqa: ANN001, ANN003
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"id":"new-evt"}', stderr="",
        )

    monkeypatch.setenv("AGENTE_GWS_BIN", "/fake/bin/gws")
    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    cp.create_calendar_event(
        title="Synthetic NYC",
        start="2026-06-23T09:00:00-04:00",
        end="2026-06-23T10:00:00-04:00",
    )

    argv = captured["cmd"]
    body = json.loads(argv[argv.index("--json") + 1])
    assert body["start"]["timeZone"] == "America/New_York"
    assert body["start"]["timeZone"] != "UTC"


def test_get_calendar_event(fake_gws: Path) -> None:
    event = cp.get_calendar_event(event_id="evt1")
    assert event["id"] == "evt1"


def test_handler_returns_json_string_with_success_flag(fake_gws: Path) -> None:
    out = calendar_pkg.handle_list_calendar_events(
        {"after": "today", "before": "today+1d"}
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["events"][0]["id"] == "evt1"


def test_handler_rejects_missing_required_args() -> None:
    out = calendar_pkg.handle_create_calendar_event({"title": "x"})
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "required" in parsed["error"]


def test_handler_get_requires_event_id() -> None:
    out = calendar_pkg.handle_get_calendar_event({})
    parsed = json.loads(out)
    assert parsed["success"] is False


def test_gws_nonzero_exit_surfaces_runtime_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failing = tmp_path / "gws"
    failing.write_text("#!/usr/bin/env bash\necho 'not authenticated' >&2\nexit 7\n")
    failing.chmod(failing.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("AGENTE_GWS_BIN", str(failing))
    with pytest.raises(RuntimeError, match="not authenticated"):
        cp.list_calendar_events(after="today", before="today+1d")


def test_gws_child_env_scrubs_google_workspace_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = tmp_path / "gws"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "if [ -n \"$GOOGLE_WORKSPACE_CLI_CLIENT_SECRET\" ]; then\n"
        "  echo '{\"has_secret\":true}'\n"
        "else\n"
        "  echo '{\"has_secret\":false}'\n"
        "fi\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("AGENTE_GWS_BIN", str(stub))
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CLIENT_SECRET", "do-not-forward")

    event = cp.get_calendar_event(event_id="evt1")

    assert event["has_secret"] is False


def test_handler_translates_runtime_error_to_error_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failing = tmp_path / "gws"
    failing.write_text("#!/usr/bin/env bash\necho 'boom' >&2\nexit 3\n")
    failing.chmod(failing.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("AGENTE_GWS_BIN", str(failing))
    out = calendar_pkg.handle_get_calendar_event({"event_id": "evt1"})
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "boom" in parsed["error"]


# ---------------------------------------------------------------------------
# Schema contract: every tool must carry label_he + category for the desktop
# tool palette.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "schema",
    [LIST_CALENDAR_EVENTS_SCHEMA, CREATE_CALENDAR_EVENT_SCHEMA, GET_CALENDAR_EVENT_SCHEMA],
)
def test_schema_has_label_he_and_category(schema: dict) -> None:
    assert schema.get("label_he"), f"missing label_he on {schema['name']}"
    assert schema.get("category") == "calendar"


def test_register_wires_all_tools() -> None:
    """Plugin must register exactly the tools declared in plugin.yaml."""
    calls: list[dict] = []

    class _Ctx:
        def register_tool(self, **kw):  # noqa: ANN003
            calls.append(kw)

    calendar_pkg.register(_Ctx())
    names = sorted(c["name"] for c in calls)
    assert names == [
        "agenda", "check_availability", "create_calendar_event",
        "delete_calendar_event", "get_calendar_event", "list_calendar_events",
        "list_calendars", "quick_add_event", "search_calendar_events",
        "update_calendar_event",
    ]
    for c in calls:
        assert c["toolset"] == "calendar"
