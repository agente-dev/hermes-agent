"""Tests for agent.pii_guard — third-party PII detection on memory writes.

Regression coverage for hermes-agent-202606-22 ("memory-phantom-clients"):
payslip / Form-106 / national-ID-shaped OCR output must not be silently
persisted to the holographic memory store.
"""

from __future__ import annotations

import pytest

from agent.pii_guard import (
    PIIWriteBlocked,
    classify,
    guard_write,
)


# --- The bug payload --------------------------------------------------------
#
# Synthetic reproduction of the OCR shape that triggered the original
# incident. Do not copy names, employers, or IDs from real source material.
_PAYLOAD_PAYSLIP_HE = (
    "תלוש שכר עבור עובד בדיקה\n"
    "ת.ז. 000000000\n"
    "מעסיק: חברת דוגמה בע\"מ\n"
    "ברוטו 12,500\nנטו 9,800\n"
    "ניכויי חובה: מס הכנסה, ביטוח לאומי, ביטוח בריאות\n"
)

_PAYLOAD_FORM_106_HE = (
    "טופס 106 לשנת 2022\n"
    "ת.ז. 111111111\n"
    "מעסיק: מעסיק בדיקה בע\"מ\n"
    "ברוטו שנתי: 145,200\n"
)

_PAYLOAD_PAYSLIP_EN = (
    "Pay stub for John Doe\n"
    "Employer: Acme Corp\n"
    "Tax ID 555555555\n"
    "Gross pay 5000\nNet pay 3800\n"
)


def test_classify_flags_hebrew_payslip() -> None:
    verdict = classify(_PAYLOAD_PAYSLIP_HE)
    assert verdict.is_pii, verdict
    assert verdict.matched_terms  # at least one term matched


def test_classify_flags_form_106() -> None:
    verdict = classify(_PAYLOAD_FORM_106_HE)
    assert verdict.is_pii


def test_classify_flags_spaced_or_dashed_id_shapes() -> None:
    spaced = (
        "תלוש שכר\n"
        "ת ז 222 222 222\n"
        "מעסיק: חברת בדיקות\n"
        "ברוטו 100\n"
    )
    dashed = (
        "טופס 106\n"
        "מספר זהות: 333-333-333\n"
        "מעסיק: חברת בדיקות\n"
        "נטו 80\n"
    )
    assert classify(spaced).is_pii
    assert classify(dashed).is_pii


def test_classify_flags_english_payslip() -> None:
    verdict = classify(_PAYLOAD_PAYSLIP_EN)
    assert verdict.is_pii


def test_classify_ignores_innocuous_text() -> None:
    assert not classify("Operator note: remember to call the dentist.").is_pii
    assert not classify("").is_pii
    assert not classify("   ").is_pii


def test_classify_ignores_single_term_without_digits() -> None:
    # One term alone is not enough — false-positive guard.
    assert not classify("דיון על מס הכנסה ברמה כללית").is_pii


def test_guard_write_raises_on_payslip() -> None:
    with pytest.raises(PIIWriteBlocked) as exc:
        guard_write(_PAYLOAD_PAYSLIP_HE)
    assert exc.value.verdict.is_pii


def test_guard_write_allow_pii_bypass() -> None:
    # Explicit operator confirmation path: caller passes allow_pii=True.
    guard_write(_PAYLOAD_PAYSLIP_HE, allow_pii=True)  # must not raise


def test_guard_write_pass_through_for_clean_content() -> None:
    guard_write("Customer asked us to send the proposal on Monday.")
