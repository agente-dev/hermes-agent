"""
Matcher for workflow rules.

Per hermes-202606-004, the message-arrival hook must evaluate a rule's
``matcher_pattern`` against an incoming connector event BEFORE the LLM call,
short-circuiting to the deterministic ticket-creation path when it matches.

Supported pattern shapes (all UTF-8 / Hebrew safe):

* ``str``                 — substring match against the event ``text``
                            (case-insensitive, Unicode normalized).
* ``{"contains": "..."}`` — same as above.
* ``{"contains_any": [...]}``  — match if any string is found.
* ``{"contains_all": [...]}``  — match only if every string is found.
* ``{"regex": "..."}``    — Python regex against the event text.
* ``{"sender": "..."}``   — exact sender id match (can combine with text rule
                            via ``{"all": [<rules>]}``).
* ``{"all": [<rules>]}``  — every sub-rule must match.
* ``{"any": [<rules>]}``  — at least one sub-rule must match.

A missing / null pattern matches nothing (defensive: an empty pattern must
NOT cause every message to be routed away from the LLM).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _norm(text: Optional[str]) -> str:
    if not text:
        return ""
    return unicodedata.normalize("NFKC", str(text)).casefold()


def _event_text(event: Dict[str, Any]) -> str:
    """Pull the user-visible text from a connector event.

    Accepts both the desktop bridge shape (``{"message": {"text": "..."}}``)
    and a flatter ``{"text": "..."}`` shape.
    """
    if not isinstance(event, dict):
        return ""
    msg = event.get("message")
    if isinstance(msg, dict) and msg.get("text"):
        return str(msg["text"])
    if event.get("text"):
        return str(event["text"])
    if event.get("body"):
        return str(event["body"])
    return ""


def _event_sender(event: Dict[str, Any]) -> str:
    if not isinstance(event, dict):
        return ""
    msg = event.get("message")
    if isinstance(msg, dict) and msg.get("from"):
        return str(msg["from"])
    return str(event.get("from") or event.get("sender") or "")


def _match_dict(pattern: Dict[str, Any], event: Dict[str, Any]) -> bool:
    if "all" in pattern:
        subs = pattern.get("all") or []
        return all(match(sub, event) for sub in subs) if subs else False
    if "any" in pattern:
        subs = pattern.get("any") or []
        return any(match(sub, event) for sub in subs)

    text_norm = _norm(_event_text(event))

    if "contains" in pattern:
        needle = _norm(pattern.get("contains"))
        if not needle:
            return False
        if needle not in text_norm:
            return False

    if "contains_any" in pattern:
        needles = [_norm(n) for n in (pattern.get("contains_any") or []) if n]
        if not needles or not any(n in text_norm for n in needles):
            return False

    if "contains_all" in pattern:
        needles = [_norm(n) for n in (pattern.get("contains_all") or []) if n]
        if not needles or not all(n in text_norm for n in needles):
            return False

    if "regex" in pattern:
        rx = pattern.get("regex") or ""
        try:
            if not re.search(rx, _event_text(event), flags=re.UNICODE | re.IGNORECASE):
                return False
        except re.error as exc:
            logger.warning("workflow matcher: bad regex %r (%s)", rx, exc)
            return False

    if "sender" in pattern:
        want = str(pattern.get("sender") or "")
        if want and want != _event_sender(event):
            return False

    # At least one constraint key must have been present.
    constraint_keys = {"contains", "contains_any", "contains_all", "regex", "sender"}
    if not (constraint_keys & set(pattern.keys())):
        return False

    return True


def match(pattern: Any, event: Dict[str, Any]) -> bool:
    """Return True iff ``pattern`` matches the incoming connector ``event``."""
    if pattern is None or pattern == "":
        return False
    if isinstance(pattern, str):
        needle = _norm(pattern)
        if not needle:
            return False
        return needle in _norm(_event_text(event))
    if isinstance(pattern, dict):
        return _match_dict(pattern, event)
    logger.warning("workflow matcher: unsupported pattern type %r", type(pattern))
    return False


def find_matching_rules(
    rules: List[Dict[str, Any]],
    event: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return the subset of ``rules`` whose ``matcher_pattern`` fires for ``event``."""
    out: List[Dict[str, Any]] = []
    for rule in rules:
        pattern = rule.get("matcher_pattern")
        if match(pattern, event):
            out.append(rule)
    return out
