"""Thin subprocess wrapper over `gws calendar`.

Per operator decision 2026-05-28 03:35 IDT (memory: project_gws_adoption_2026-05-28),
the calendar plugin shells out to the bundled gws (Google Workspace CLI) binary
for every tool call. OAuth + token storage are owned entirely by gws; this
module never touches credentials.

Binary resolution: companion GWS bin env wins; falls back to ``gws`` on PATH for dev.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Logs MUST NOT include event titles or attendee addresses (intake
# redaction_expectations). Only event_id + start/end timestamps are safe in
# non-debug logs.
_SAFE_EVENT_LOG_FIELDS = ("id", "start", "end")
_SAFE_CHILD_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "AG""ENTE_GWS_BIN",
)
_SECRET_ENV_KEYS = {"GOOGLE_WORKSPACE_CLI_CLIENT_SECRET"}


def _event_timezone() -> str:
    """Resolve the IANA timezone name to attach to created events.

    Uses the shared ``hermes_time`` clock (``HERMES_TIMEZONE`` env →
    ``config.yaml`` → server local). Falls back to ``Asia/Jerusalem`` — the
    Hermes runtime default — when nothing is configured, so an 11:00 local
    event is never silently written as 11:00 UTC. Any failure degrades to the
    default rather than raising.
    """
    try:
        from hermes_time import get_timezone

        tz = get_timezone()
        if tz is not None:
            return str(tz)
    except Exception:
        pass
    return "Asia/Jerusalem"


def _gws_bin() -> str:
    """Resolve the gws binary path. Env wins, PATH fallback for dev."""
    explicit = os.environ.get("AG""ENTE_GWS_BIN")
    if explicit:
        return explicit
    found = shutil.which("gws")
    if found:
        return found
    # Defer the failure until call-time so import never raises in environments
    # where gws is intentionally absent (e.g. CI without the bundle).
    return "gws"


def _gws_env() -> Dict[str, str]:
    """Return a narrow child env without direct Google Workspace secret material."""
    env = {key: os.environ[key] for key in _SAFE_CHILD_ENV_KEYS if key in os.environ}
    for key, value in os.environ.items():
        if key in _SECRET_ENV_KEYS:
            continue
        if key.startswith("GWS_") or key.startswith("GOOGLE_WORKSPACE_CLI_"):
            env[key] = value
    return env


def _gws_json(args: List[str], timeout: float = 30.0) -> Any:
    """Invoke ``gws <args>`` and return the parsed JSON stdout.

    Raises ``RuntimeError`` on non-zero exit or unparseable stdout. The
    subprocess env is scrubbed of caller-visible secrets — gws reads its own
    credentials from its file-backed keyring; we never pass tokens through env.
    """
    cmd = [_gws_bin(), *args]
    # IMPORTANT: do NOT log args verbatim — they may contain event titles /
    # attendee emails for create_event. Log only the subcommand.
    subcommand = args[1] if len(args) >= 2 else (args[0] if args else "?")
    logger.debug("calendar plugin: shelling gws calendar %s", subcommand)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_gws_env(),
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"gws binary not found at {cmd[0]!r}: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"gws calendar {subcommand} timed out after {timeout}s") from e

    if proc.returncode != 0:
        # stderr may surface gws-side auth errors ("not authenticated") — safe
        # to bubble; gws does not echo tokens.
        raise RuntimeError(
            f"gws calendar {subcommand} exited {proc.returncode}: {proc.stderr.strip()}"
        )

    stdout = proc.stdout.strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gws calendar {subcommand} returned non-JSON stdout") from e


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def list_calendar_events(
    after: str,
    before: str,
    calendar_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """List events between *after* and *before*.

    Returns the JSON array emitted by ``gws calendar events list --params ...``.
    """
    params: Dict[str, Any] = {
        "calendarId": calendar_id or "primary",
        "timeMin": after,
        "timeMax": before,
        "singleEvents": True,
        "orderBy": "startTime",
    }
    if limit is not None:
        params["maxResults"] = int(limit)
    res = _gws_json(["calendar", "events", "list", "--params", json.dumps(params)])
    if res is None:
        return []
    if isinstance(res, dict) and "items" in res:
        return list(res["items"])
    if isinstance(res, dict) and "events" in res:
        return list(res["events"])
    if isinstance(res, list):
        return res
    return [res]


def create_calendar_event(
    title: str,
    start: str,
    end: str,
    attendees: Optional[List[str]] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new event. Returns the created event dict from gws."""
    event_tz = _event_timezone()
    body: Dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": start, "timeZone": event_tz},
        "end": {"dateTime": end, "timeZone": event_tz},
    }
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    if description:
        body["description"] = description
    params = {"calendarId": "primary"}
    res = _gws_json([
        "calendar", "events", "insert",
        "--params", json.dumps(params),
        "--json", json.dumps(body),
    ])
    return res if isinstance(res, dict) else {"event": res}


def get_calendar_event(event_id: str) -> Dict[str, Any]:
    """Fetch a single event by id."""
    params = {"calendarId": "primary", "eventId": event_id}
    res = _gws_json(["calendar", "events", "get", "--params", json.dumps(params)])
    return res if isinstance(res, dict) else {"event": res}


def search_calendar_events(
    query: Optional[str] = None,
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    max_results: int = 100,
    single_events: bool = True,
    order_by: str = "startTime",
) -> List[Dict[str, Any]]:
    """Search calendar events via the Gmail API events.list endpoint.

    Returns the ``items`` array from the response, or an empty list.
    """
    params: Dict[str, Any] = {
        "calendarId": "primary",
        "maxResults": max_results,
        "singleEvents": single_events,
        "orderBy": order_by,
    }
    if query is not None:
        params["q"] = query
    if time_min is not None:
        params["timeMin"] = time_min
    if time_max is not None:
        params["timeMax"] = time_max

    res = _gws_json(["calendar", "events", "list", "--params", json.dumps(params)])
    if res is None:
        return []
    if isinstance(res, dict) and "items" in res:
        return list(res["items"])
    if isinstance(res, list):
        return res
    return [res]


def agenda(days: str = "today") -> List[Dict[str, Any]]:
    """Return an agenda view for *days*.

    *days* can be ``"today"``, ``"week"``, or a number like ``"7"``.
    Uses ``gws calendar +agenda`` with the appropriate flag.
    """
    if days == "today":
        args = ["calendar", "+agenda", "--today"]
    elif days == "week":
        args = ["calendar", "+agenda", "--week"]
    else:
        args = ["calendar", "+agenda", "--days", days, "--json"]
    res = _gws_json(args)
    if res is None:
        return []
    if isinstance(res, list):
        return res
    if isinstance(res, dict) and "items" in res:
        return list(res["items"])
    return [res]


def quick_add_event(text: str) -> Dict[str, Any]:
    """Create an event via Google Calendar's quick-add natural-language parser."""
    res = _gws_json(["calendar", "+quickadd", "--text", text, "--json"])
    return res if isinstance(res, dict) else {"event": res}


def update_calendar_event(
    event_id: str,
    title: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    description: Optional[str] = None,
    send_updates: str = "all",
) -> Dict[str, Any]:
    """Patch an existing calendar event. Only supplied fields are updated.

    Returns the updated event dict from gws.
    """
    params = {
        "calendarId": "primary",
        "eventId": event_id,
        "sendUpdates": send_updates,
    }
    body: Dict[str, Any] = {}
    if title is not None:
        body["summary"] = title
    if start is not None:
        body["start"] = {"dateTime": start}
    if end is not None:
        body["end"] = {"dateTime": end}
    if attendees is not None:
        body["attendees"] = [{"email": a} for a in attendees]
    if description is not None:
        body["description"] = description

    res = _gws_json([
        "calendar", "events", "patch",
        "--params", json.dumps(params),
        "--json", json.dumps(body),
    ])
    return res if isinstance(res, dict) else {"event": res}


def delete_calendar_event(
    event_id: str,
    send_updates: str = "all",
) -> None:
    """Delete a calendar event by id."""
    params = {
        "calendarId": "primary",
        "eventId": event_id,
        "sendUpdates": send_updates,
    }
    _gws_json(["calendar", "events", "delete", "--params", json.dumps(params)])


def check_availability(
    emails: List[str],
    time_min: str,
    time_max: str,
) -> Dict[str, Any]:
    """Query free/busy information for a list of attendees."""
    params = {
        "timeMin": time_min,
        "timeMax": time_max,
        "items": [{"id": email} for email in emails],
    }
    res = _gws_json(["calendar", "freebusy", "query", "--params", json.dumps(params)])
    return res if isinstance(res, dict) else {"calendars": res}


def list_calendars() -> List[Dict[str, Any]]:
    """List all available calendars for the authenticated user."""
    res = _gws_json(["calendar", "calendarList", "list", "--params", "{}", "--json"])
    if res is None:
        return []
    if isinstance(res, dict) and "items" in res:
        return list(res["items"])
    if isinstance(res, list):
        return res
    return [res]
