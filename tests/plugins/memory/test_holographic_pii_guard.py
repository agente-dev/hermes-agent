"""Regression test for hermes-agent-202606-22 ("memory-phantom-clients").

Asserts that the holographic ``MemoryStore.add_fact`` refuses to persist
content shaped like a third-party payslip / Form 106 / national-ID record
unless the caller explicitly passes ``allow_pii=True`` (i.e. has obtained
operator confirmation).
"""

from __future__ import annotations

import pytest

pytest.importorskip("numpy")  # holographic plugin needs numpy for HRR

from agent.pii_guard import PIIWriteBlocked
from plugins.memory.holographic.store import MemoryStore


_PAYSLIP_PAYLOAD = (
    "תלוש שכר עבור עובד צד שלישי\n"
    "ת.ז. 444444444\n"
    "מעסיק: חברת דוגמה בע\"מ\n"
    "ברוטו 12,500\nנטו 9,800\n"
    "ניכויי חובה: מס הכנסה, ביטוח לאומי\n"
)


def test_add_fact_blocks_third_party_payslip(tmp_path) -> None:
    store = MemoryStore(db_path=tmp_path / "memory_store.db")
    with pytest.raises(PIIWriteBlocked):
        store.add_fact(_PAYSLIP_PAYLOAD, category="general", tags="ocr")

    # And the row was NOT persisted.
    count = store._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    assert count == 0


def test_add_fact_allow_pii_bypass(tmp_path) -> None:
    store = MemoryStore(db_path=tmp_path / "memory_store.db")
    fact_id = store.add_fact(
        _PAYSLIP_PAYLOAD,
        category="general",
        tags="ocr",
        allow_pii=True,
    )
    assert fact_id > 0


def test_update_fact_blocks_third_party_payslip(tmp_path) -> None:
    store = MemoryStore(db_path=tmp_path / "memory_store.db")
    fact_id = store.add_fact("Clean seed fact", category="general")

    with pytest.raises(PIIWriteBlocked):
        store.update_fact(fact_id, content=_PAYSLIP_PAYLOAD)

    row = store._conn.execute(
        "SELECT content FROM facts WHERE fact_id = ?", (fact_id,)
    ).fetchone()
    assert row["content"] == "Clean seed fact"


def test_update_fact_allow_pii_bypass(tmp_path) -> None:
    store = MemoryStore(db_path=tmp_path / "memory_store.db")
    fact_id = store.add_fact("Clean seed fact", category="general")

    assert store.update_fact(fact_id, content=_PAYSLIP_PAYLOAD, allow_pii=True)


def test_add_fact_allows_innocuous_content(tmp_path) -> None:
    store = MemoryStore(db_path=tmp_path / "memory_store.db")
    fact_id = store.add_fact(
        "Operator prefers Hebrew error messages.",
        category="preferences",
    )
    assert fact_id > 0
