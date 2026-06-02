---
name: aveeor-tama-intro
description: Canonical example skill inside the aveeor-tama pack. Demonstrates that pack-level default_prompt metadata flows through skill discovery.
license: MIT
---

# aveeor-tama-intro

This skill exists so the canonical `aveeor-tama` example pack has at least one
discoverable `SKILL.md`. The skill itself does nothing useful — its purpose is
to verify that the pack-level `default_prompt` declared in
`skills/aveeor-tama/pack.yaml` is surfaced through `_find_all_skills()` output
under the `pack_default_prompt` field on each member skill.

See `skills/aveeor-tama/pack.yaml` for the pack default prompt.
