"""JSON-Schema definitions for the calendar plugin tools.

These mirror the ``gws calendar`` CLI surface. Each schema also carries a
``label_he`` (Hebrew label for operator-facing surfaces) and a ``category``
field consumed by the companion tool palette.
"""

from __future__ import annotations

from typing import Any, Dict


LIST_CALENDAR_EVENTS_SCHEMA: Dict[str, Any] = {
    "name": "list_calendar_events",
    "description": (
        "List Google Calendar events between two ISO-8601 timestamps. "
        "Shells `gws calendar events list --params '{\"calendarId\":...,\"timeMin\":...,...}'` and "
        "returns the parsed event list. OAuth is owned by gws; this tool "
        "never sees a token."
    ),
    "label_he": "רשימת אירועים ביומן",
    "category": "calendar",
    "parameters": {
        "type": "object",
        "properties": {
            "after": {
                "type": "string",
                "description": "ISO-8601 timestamp or `gws`-relative form (e.g. 'today', 'today+1d').",
            },
            "before": {
                "type": "string",
                "description": "ISO-8601 timestamp or `gws`-relative form (e.g. 'today+7d').",
            },
            "calendar_id": {
                "type": "string",
                "description": "Optional calendar id; defaults to primary.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "Optional max number of events to return.",
            },
        },
        "required": ["after", "before"],
        "additionalProperties": False,
    },
}


CREATE_CALENDAR_EVENT_SCHEMA: Dict[str, Any] = {
    "name": "create_calendar_event",
    "description": (
        "Create a new Google Calendar event. Shells "
        "`gws calendar events insert --params '{\"calendarId\":...}' --body '{\"summary\":...,...}'` and returns the "
        "created event id + html link. Hebrew/RTL titles are preserved verbatim."
    ),
    "label_he": "יצירת אירוע ביומן",
    "category": "calendar",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Event title (UTF-8; RTL preserved)."},
            "start": {"type": "string", "description": "ISO-8601 start timestamp."},
            "end": {"type": "string", "description": "ISO-8601 end timestamp."},
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of attendee email addresses.",
            },
            "description": {
                "type": "string",
                "description": "Optional event description / body.",
            },
        },
        "required": ["title", "start", "end"],
        "additionalProperties": False,
    },
}


GET_CALENDAR_EVENT_SCHEMA: Dict[str, Any] = {
    "name": "get_calendar_event",
    "description": (
        "Fetch a single Google Calendar event by id. Shells "
        "`gws calendar events get --params '{\"calendarId\":...,\"eventId\":...}'` and returns the parsed event."
    ),
    "label_he": "פרטי אירוע ביומן",
    "category": "calendar",
    "parameters": {
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "description": "Google Calendar event id."},
        },
        "required": ["event_id"],
        "additionalProperties": False,
    },
}

SEARCH_CALENDAR_EVENTS_SCHEMA: Dict[str, Any] = {
    "name": "search_calendar_events",
    "description": (
        "Search Google Calendar events with full-text query, time range, and ordering. "
        "Uses the Gmail Calendar API events.list endpoint with q, timeMin, timeMax, "
        "singleEvents, and orderBy parameters. Returns parsed event items."
    ),
    "label_he": "חיפוש אירועים ביומן",
    "category": "calendar",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text search query for events (searches summary, description, location, attendees).",
            },
            "time_min": {
                "type": "string",
                "description": "ISO-8601 lower bound for event start time.",
            },
            "time_max": {
                "type": "string",
                "description": "ISO-8601 upper bound for event start time.",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2500,
                "description": "Max events to return (default 100).",
            },
            "single_events": {
                "type": "boolean",
                "description": "Expand recurring events into individual instances (default true).",
            },
            "order_by": {
                "type": "string",
                "enum": ["startTime", "updated"],
                "description": "Sort order (default startTime).",
            },
        },
        "additionalProperties": False,
    },
}

AGENDA_SCHEMA: Dict[str, Any] = {
    "name": "agenda",
    "description": (
        "Get a quick agenda view of upcoming calendar events. "
        "Uses gws calendar +agenda helper. Supports today, week, or N-day views."
    ),
    "label_he": "סיכום יומן",
    "category": "calendar",
    "parameters": {
        "type": "object",
        "properties": {
            "days": {
                "type": "string",
                "description": "View range: 'today', 'week', or a number like '7' for N days (default 'today').",
            },
        },
        "additionalProperties": False,
    },
}

QUICK_ADD_EVENT_SCHEMA: Dict[str, Any] = {
    "name": "quick_add_event",
    "description": (
        "Create a calendar event using Google Calendar's natural-language quick-add parser. "
        "Accepts free-form text like 'Lunch with Alice tomorrow at noon'."
    ),
    "label_he": "יצירת אירוע מהירה",
    "category": "calendar",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Natural-language event description (e.g. 'Team standup every Monday 9am').",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}

UPDATE_CALENDAR_EVENT_SCHEMA: Dict[str, Any] = {
    "name": "update_calendar_event",
    "description": (
        "Update an existing Google Calendar event. Only supplied fields are modified. "
        "Uses events.patch with sendUpdates=all by default."
    ),
    "label_he": "עדכון אירוע ביומן",
    "category": "calendar",
    "parameters": {
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "description": "Google Calendar event ID to update."},
            "title": {"type": "string", "description": "New event title (optional)."},
            "start": {"type": "string", "description": "New ISO-8601 start timestamp (optional)."},
            "end": {"type": "string", "description": "New ISO-8601 end timestamp (optional)."},
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Updated list of attendee email addresses (optional).",
            },
            "description": {"type": "string", "description": "Updated event description (optional)."},
            "send_updates": {
                "type": "string",
                "enum": ["all", "externalOnly", "none"],
                "description": "Who to notify of the change (default 'all').",
            },
        },
        "required": ["event_id"],
        "additionalProperties": False,
    },
}

DELETE_CALENDAR_EVENT_SCHEMA: Dict[str, Any] = {
    "name": "delete_calendar_event",
    "description": (
        "Delete a Google Calendar event by ID. Uses events.delete with sendUpdates=all by default."
    ),
    "label_he": "מחיקת אירוע ביומן",
    "category": "calendar",
    "parameters": {
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "description": "Google Calendar event ID to delete."},
            "send_updates": {
                "type": "string",
                "enum": ["all", "externalOnly", "none"],
                "description": "Who to notify of the deletion (default 'all').",
            },
        },
        "required": ["event_id"],
        "additionalProperties": False,
    },
}

CHECK_AVAILABILITY_SCHEMA: Dict[str, Any] = {
    "name": "check_availability",
    "description": (
        "Check free/busy availability for a list of attendees over a time range. "
        "Uses the freebusy.query endpoint."
    ),
    "label_he": "בדיקת זמינות",
    "category": "calendar",
    "parameters": {
        "type": "object",
        "properties": {
            "emails": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of email addresses to check availability for.",
            },
            "time_min": {"type": "string", "description": "ISO-8601 start of the time window."},
            "time_max": {"type": "string", "description": "ISO-8601 end of the time window."},
        },
        "required": ["emails", "time_min", "time_max"],
        "additionalProperties": False,
    },
}

LIST_CALENDARS_SCHEMA: Dict[str, Any] = {
    "name": "list_calendars",
    "description": (
        "List all Google Calendars available to the authenticated user. "
        "Uses the calendarList.list endpoint."
    ),
    "label_he": "רשימת יומנים",
    "category": "calendar",
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}
