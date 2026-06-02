# OCR PII write guard

Persistent memory writes that originate from OCR of arbitrary user
documents must be treated as ephemeral by default. Concretely: if a tool
extracts payslip, tax-form, or national-ID-shaped content from a PDF (or
any other source), that content must not be persisted to long-lived
stores (holographic memory, kanban DB, fact stores) unless the operator
has explicitly confirmed the write.

## Incident this was added for

`hermes-agent-202606-22` — "memory-phantom-clients" (2026-06-02). An
agent OCR'd third-party payslips that lived inside a client's document
folder and persisted the extracted names into a downstream kanban DB as
"sub-clients". Later sessions surfaced those names as if they were the
operator's own clients, creating a privacy incident — the operator did
not recognise any of the four phantom names.

## Mechanism

`agent.pii_guard` exposes:

- `classify(content) -> PIIVerdict` — conservative heuristic detection
  of Israeli payslip / Form 106 / national-ID-shaped content (Hebrew or
  English).
- `guard_write(content, *, allow_pii=False)` — raises `PIIWriteBlocked`
  when the payload is classified as third-party PII.

The holographic store's `MemoryStore.add_fact` calls `guard_write` before
inserting. Callers that legitimately need to persist payroll-shaped
content (e.g. the operator explicitly asks Hermes to save a salary
record for one of their own clients) pass `allow_pii=True` after
gathering operator confirmation in the calling layer.

## Caller checklist

- [ ] Never default `allow_pii=True` in a tool wrapper.
- [ ] If you must persist OCR output, surface a confirmation prompt to
      the operator first and pass the confirmation forward.
- [ ] Treat the absence of confirmation as a hard drop — do not retry
      with `allow_pii=True` automatically.

## Out-of-scope clean-up

This guard is forward-looking. Pre-existing rows that already contain
phantom names in the agente-desktop kanban (`Application Support/
agente-desktop/hermes/kanban.db`) and session transcripts
(`state.db`) must be sanitised operationally — see the intake's
fix-shape steps 1 and 2. Those files are not part of this repository.
