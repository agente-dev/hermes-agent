"""calendar plugin — thin subprocess wrapper over `gws calendar`.

Registers three agent tools (``list_calendar_events``,
``create_calendar_event``, ``get_calendar_event``) that each shell the
bundled gws (Google Workspace CLI) binary. OAuth + token storage are owned
entirely by gws. Memory: project_gws_adoption_2026-05-28.

Hebrew/RTL event titles are preserved verbatim.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from plugins.calendar import calendar_plugin as cp
from plugins.calendar.schemas import (
    AGENDA_SCHEMA,
    CHECK_AVAILABILITY_SCHEMA,
    CREATE_CALENDAR_EVENT_SCHEMA,
    DELETE_CALENDAR_EVENT_SCHEMA,
    GET_CALENDAR_EVENT_SCHEMA,
    LIST_CALENDARS_SCHEMA,
    LIST_CALENDAR_EVENTS_SCHEMA,
    QUICK_ADD_EVENT_SCHEMA,
    SEARCH_CALENDAR_EVENTS_SCHEMA,
    UPDATE_CALENDAR_EVENT_SCHEMA,
)

logger = logging.getLogger(__name__)


def _json(obj: Any) -> str:
    # ensure_ascii=False so Hebrew titles survive verbatim.
    return json.dumps(obj, ensure_ascii=False)


def _err(msg: str, **extra: Any) -> str:
    return _json({"success": False, "error": msg, **extra})


# ---------------------------------------------------------------------------
# Handlers — translate the tool-call args dict into typed kwargs, run the
# subprocess wrapper, return a JSON string per the Hermes tool contract.
# ---------------------------------------------------------------------------

def handle_list_calendar_events(args: Dict[str, Any], **_kw: Any) -> str:
    after = args.get("after")
    before = args.get("before")
    if not after or not before:
        return _err("after and before are required (ISO-8601 or gws-relative)")
    try:
        events = cp.list_calendar_events(
            after=str(after),
            before=str(before),
            calendar_id=args.get("calendar_id"),
            limit=args.get("limit"),
        )
    except RuntimeError as e:
        return _err(str(e))
    return _json({"success": True, "events": events})


def handle_create_calendar_event(args: Dict[str, Any], **_kw: Any) -> str:
    title = args.get("title")
    start = args.get("start")
    end = args.get("end")
    if not title or not start or not end:
        return _err("title, start, and end are required")
    try:
        event = cp.create_calendar_event(
            title=str(title),
            start=str(start),
            end=str(end),
            attendees=args.get("attendees"),
            description=args.get("description"),
        )
    except RuntimeError as e:
        return _err(str(e))
    return _json({"success": True, "event": event})


def handle_get_calendar_event(args: Dict[str, Any], **_kw: Any) -> str:
    event_id = args.get("event_id")
    if not event_id:
        return _err("event_id is required")
    try:
        event = cp.get_calendar_event(event_id=str(event_id))
    except RuntimeError as e:
        return _err(str(e))
    return _json({"success": True, "event": event})


def handle_search_calendar_events(args: Dict[str, Any], **_kw: Any) -> str:
    try:
        events = cp.search_calendar_events(
            query=args.get("query"),
            time_min=args.get("time_min"),
            time_max=args.get("time_max"),
            max_results=args.get("max_results", 100),
            single_events=args.get("single_events", True),
            order_by=args.get("order_by", "startTime"),
        )
    except RuntimeError as e:
        return _err(str(e))
    return _json({"success": True, "events": events})


def handle_agenda(args: Dict[str, Any], **_kw: Any) -> str:
    try:
        events = cp.agenda(days=args.get("days", "today"))
    except RuntimeError as e:
        return _err(str(e))
    return _json({"success": True, "events": events})


def handle_quick_add_event(args: Dict[str, Any], **_kw: Any) -> str:
    text = args.get("text")
    if not text:
        return _err("text is required (natural-language event description)")
    try:
        event = cp.quick_add_event(text=str(text))
    except RuntimeError as e:
        return _err(str(e))
    return _json({"success": True, "event": event})


def handle_update_calendar_event(args: Dict[str, Any], **_kw: Any) -> str:
    event_id = args.get("event_id")
    if not event_id:
        return _err("event_id is required")
    try:
        event = cp.update_calendar_event(
            event_id=str(event_id),
            title=args.get("title"),
            start=args.get("start"),
            end=args.get("end"),
            attendees=args.get("attendees"),
            description=args.get("description"),
            send_updates=args.get("send_updates", "all"),
        )
    except RuntimeError as e:
        return _err(str(e))
    return _json({"success": True, "event": event})


def handle_delete_calendar_event(args: Dict[str, Any], **_kw: Any) -> str:
    event_id = args.get("event_id")
    if not event_id:
        return _err("event_id is required")
    try:
        cp.delete_calendar_event(
            event_id=str(event_id),
            send_updates=args.get("send_updates", "all"),
        )
    except RuntimeError as e:
        return _err(str(e))
    return _json({"success": True})


def handle_check_availability(args: Dict[str, Any], **_kw: Any) -> str:
    emails = args.get("emails")
    time_min = args.get("time_min")
    time_max = args.get("time_max")
    if not emails or not time_min or not time_max:
        return _err("emails, time_min, and time_max are required")
    try:
        result = cp.check_availability(
            emails=list(emails),
            time_min=str(time_min),
            time_max=str(time_max),
        )
    except RuntimeError as e:
        return _err(str(e))
    return _json({"success": True, "availability": result})


def handle_list_calendars(args: Dict[str, Any], **_kw: Any) -> str:
    try:
        calendars = cp.list_calendars()
    except RuntimeError as e:
        return _err(str(e))
    return _json({"success": True, "calendars": calendars})


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

_TOOLS = (
    ("list_calendar_events",   LIST_CALENDAR_EVENTS_SCHEMA,   handle_list_calendar_events,   "📅"),
    ("create_calendar_event",  CREATE_CALENDAR_EVENT_SCHEMA,  handle_create_calendar_event,  "➕"),
    ("get_calendar_event",     GET_CALENDAR_EVENT_SCHEMA,     handle_get_calendar_event,     "🔍"),
    ("search_calendar_events", SEARCH_CALENDAR_EVENTS_SCHEMA, handle_search_calendar_events, "🔎"),
    ("agenda",                 AGENDA_SCHEMA,                 handle_agenda,                 "📋"),
    ("quick_add_event",        QUICK_ADD_EVENT_SCHEMA,        handle_quick_add_event,        "⚡"),
    ("update_calendar_event",  UPDATE_CALENDAR_EVENT_SCHEMA,  handle_update_calendar_event,  "✏️"),
    ("delete_calendar_event",  DELETE_CALENDAR_EVENT_SCHEMA,  handle_delete_calendar_event,  "🗑️"),
    ("check_availability",     CHECK_AVAILABILITY_SCHEMA,     handle_check_availability,     "🕐"),
    ("list_calendars",         LIST_CALENDARS_SCHEMA,         handle_list_calendars,         "📑"),
)


def check_calendar_requirements() -> bool:
    """Plugin can register on any platform — gws ships as part of the bundle.

    Resolution failures surface at tool-call time as a typed error rather than
    refusing to register the surface (so companion UI can still show the
    connector and explain why it is unauthenticated).
    """
    return True


def register(ctx: Any) -> None:
    """Register tools through the standard plugin loader contract."""
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="calendar",
            schema=schema,
            handler=handler,
            check_fn=check_calendar_requirements,
            emoji=emoji,
        )
    logger.info("calendar plugin: registered %d tools", len(_TOOLS))
