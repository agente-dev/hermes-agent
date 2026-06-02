"""PII guard for persistent memory writes.

Background (hermes-agent-202606-22, "memory-phantom-clients" — 2026-06-02):
An agent OCR'd third-party payslips that belonged to a client's documents
(not to the operator) and persisted the extracted names as "sub-clients"
in a downstream kanban DB. Later sessions surfaced those names as if they
were operator clients, creating a privacy incident.

Root cause class: PII extracted from arbitrary documents was written into
a long-lived store without an explicit operator confirmation. This module
classifies content that LOOKS LIKE payslip / tax-form / national-ID output
and raises ``PIIWriteBlocked`` so the caller can either:
  (a) drop the write, or
  (b) re-prompt the operator for explicit confirmation (e.g. via a tool
      argument like ``confirm_pii_write=True``).

Detection is intentionally conservative — false positives are preferable
to false negatives for this class of incident. Callers that legitimately
need to store such content (e.g. the operator explicitly asked to save a
salary record for ONE of their OWN clients) must opt in via
``allow_pii=True``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Israeli national ID ("teudat zehut") — 9 digits, sometimes formatted
# with dashes or spaces. We don't validate the check digit; we only detect
# the shape that consistently appears in payslip / Form-106 OCR output.
_ID_DIGITS_PATTERN = r"\d(?:[\s-]?\d){8}"
_IL_TZ_RE = re.compile(
    rf"(?:ת\.?\s*ז\.?|תעודת\s+זהות|מספר\s+זהות)\s*[:#-]?\s*{_ID_DIGITS_PATTERN}"
)
_NINE_DIGIT_RE = re.compile(rf"(?<!\d){_ID_DIGITS_PATTERN}(?!\d)")

# Hebrew payslip / tax-form / salary vocabulary. Presence of TWO or more
# of these alongside any numeric identifier or wage figure is a strong
# signal that the content is third-party payroll data.
_PAYSLIP_TERMS = (
    "תלוש שכר",
    "תלוש משכורת",
    "טופס 106",
    "ברוטו",
    "נטו",
    "ניכויי חובה",
    "מס הכנסה",
    "ביטוח לאומי",
    "ביטוח בריאות",
    "פנסיה",
    "תיק ניכויים",
    "מעסיק",
)

# English equivalents (some payslip templates are bilingual).
_PAYSLIP_TERMS_EN = (
    "pay stub",
    "payslip",
    "gross pay",
    "net pay",
    "social security",
    "tax id",
    "employer",
    "form 106",
)


@dataclass(frozen=True)
class PIIVerdict:
    """Result of classifying a candidate memory write."""

    is_pii: bool
    reason: str = ""
    matched_terms: tuple[str, ...] = ()


class PIIWriteBlocked(RuntimeError):
    """Raised when a memory write is blocked because the payload looks
    like third-party PII (payslip / tax form / national-ID record) and the
    caller did not pass an explicit confirmation flag.
    """

    def __init__(self, verdict: PIIVerdict) -> None:
        super().__init__(
            "memory write blocked: payload looks like third-party PII "
            f"({verdict.reason}). Pass allow_pii=True only after explicit "
            "operator confirmation."
        )
        self.verdict = verdict


def classify(content: str) -> PIIVerdict:
    """Return whether ``content`` looks like third-party payroll/tax PII.

    Heuristic: at least TWO domain terms (Hebrew or English) AND at least
    one numeric identifier (Israeli ת.ז. or a bare 9-digit run), OR three
    or more domain terms regardless of digits.
    """
    if not content or not content.strip():
        return PIIVerdict(False)

    matched: list[str] = []
    for term in _PAYSLIP_TERMS:
        if term in content:
            matched.append(term)
    lowered = content.lower()
    for term in _PAYSLIP_TERMS_EN:
        if term in lowered:
            matched.append(term)

    has_tz = bool(_IL_TZ_RE.search(content)) or bool(_NINE_DIGIT_RE.search(content))

    if len(matched) >= 3:
        return PIIVerdict(
            True,
            reason="three or more payslip / tax-form terms",
            matched_terms=tuple(matched),
        )
    if len(matched) >= 2 and has_tz:
        return PIIVerdict(
            True,
            reason="payslip / tax-form terms with national-ID-shaped digits",
            matched_terms=tuple(matched),
        )
    return PIIVerdict(False, matched_terms=tuple(matched))


def guard_write(content: str, *, allow_pii: bool = False) -> None:
    """Raise ``PIIWriteBlocked`` if ``content`` looks like third-party PII.

    Callers that legitimately need to persist such content must pass
    ``allow_pii=True`` after obtaining explicit operator confirmation.
    """
    if allow_pii:
        return
    verdict = classify(content)
    if verdict.is_pii:
        raise PIIWriteBlocked(verdict)
