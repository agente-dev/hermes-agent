"""Tests for curator.managed_packs allowlist (hermes-202606-034).

Verifies that skills in the managed_packs config list are included as full
content-patch + archive candidates, while non-allowlisted bundled/hub skills
retain default archive-only (or exclusion) behaviour.

LLM spawning is never exercised — _run_llm_review is monkeypatched so tests
run fully offline.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def curator_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + freshly reloaded curator + skill_usage modules."""
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.skill_usage as usage
    importlib.reload(usage)
    import agent.curator as curator
    importlib.reload(curator)

    # Neutralize the real LLM pass — tests opt in per-case.
    monkeypatch.setattr(curator, "_run_llm_review", lambda prompt: {
        "final": "",
        "summary": "stub",
        "model": "",
        "provider": "",
        "tool_calls": [],
        "error": None,
    })

    # Default: no config → curator defaults.
    monkeypatch.setattr(curator, "_load_config", lambda: {})
    # Pin prune_builtins OFF so built-ins are excluded unless tests enable it.
    monkeypatch.setattr(usage, "_prune_builtins_enabled", lambda: False)

    return {"home": home, "curator": curator, "usage": usage}


def _write_skill(skills_dir: Path, name: str) -> Path:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n", encoding="utf-8",
    )
    return d


def _mark_hub_installed(home: Path, name: str) -> None:
    """Register *name* as a hub-installed skill in the lock file."""
    lock_file = home / "skills" / ".hub_installed.json"
    data: dict = {}
    if lock_file.exists():
        data = json.loads(lock_file.read_text())
    data[name] = {"installed_at": "2024-01-01T00:00:00"}
    lock_file.write_text(json.dumps(data))


def _mark_bundled(home: Path, name: str) -> None:
    """Register *name* as a bundled skill in the manifest."""
    manifest = home / "skills" / ".bundled_manifest.json"
    data: list = []
    if manifest.exists():
        data = json.loads(manifest.read_text())
    if name not in data:
        data.append(name)
    manifest.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Test 1: Allowlisted skill IS included in the content-patch candidate list
# ---------------------------------------------------------------------------

def test_managed_pack_included_as_candidate(curator_env, monkeypatch):
    """A hub-installed skill in managed_packs must appear in the candidate list."""
    home = curator_env["home"]
    usage = curator_env["usage"]
    skills_dir = home / "skills"

    _write_skill(skills_dir, "my-org-pack")
    _mark_hub_installed(home, "my-org-pack")

    # Reload so module-level caches pick up the new files.
    importlib.reload(usage)
    monkeypatch.setattr(usage, "_prune_builtins_enabled", lambda: False)

    # Without managed_packs, hub skill is excluded.
    assert "my-org-pack" not in usage.list_agent_created_skill_names()

    # With managed_packs, it must appear.
    names = usage.list_agent_created_skill_names(managed_packs=["my-org-pack"])
    assert "my-org-pack" in names


# ---------------------------------------------------------------------------
# Test 2: Non-allowlisted bundled skill stays excluded (archive-only path)
# ---------------------------------------------------------------------------

def test_non_allowlisted_bundled_skill_excluded(curator_env, monkeypatch):
    """A bundled skill NOT in managed_packs and with prune_builtins=off is excluded."""
    home = curator_env["home"]
    usage = curator_env["usage"]
    skills_dir = home / "skills"

    _write_skill(skills_dir, "builtin-skill")
    _mark_bundled(home, "builtin-skill")

    importlib.reload(usage)
    monkeypatch.setattr(usage, "_prune_builtins_enabled", lambda: False)

    # Not in managed_packs → excluded.
    assert "builtin-skill" not in usage.list_agent_created_skill_names()

    # Also excluded when managed_packs is non-empty but doesn't contain it.
    assert "builtin-skill" not in usage.list_agent_created_skill_names(
        managed_packs=["some-other-pack"]
    )


# ---------------------------------------------------------------------------
# Test 3: Empty managed_packs → default behaviour unchanged
# ---------------------------------------------------------------------------

def test_empty_managed_packs_default_behaviour(curator_env, monkeypatch):
    """Passing an empty managed_packs list must behave identically to None."""
    home = curator_env["home"]
    usage = curator_env["usage"]
    skills_dir = home / "skills"

    # One agent-created skill.
    _write_skill(skills_dir, "agent-skill")
    # One hub-installed skill.
    _write_skill(skills_dir, "hub-skill")
    _mark_hub_installed(home, "hub-skill")

    importlib.reload(usage)
    monkeypatch.setattr(usage, "_prune_builtins_enabled", lambda: False)

    # Mark agent-skill as agent-created in usage.json.
    data = usage.load_usage()
    data["agent-skill"] = {"created_by": "agent", "use_count": 0}
    (home / "skills" / ".usage.json").write_text(json.dumps(data))
    importlib.reload(usage)
    monkeypatch.setattr(usage, "_prune_builtins_enabled", lambda: False)

    names_none = set(usage.list_agent_created_skill_names(managed_packs=None))
    names_empty = set(usage.list_agent_created_skill_names(managed_packs=[]))

    assert names_none == names_empty
    assert "hub-skill" not in names_none
    assert "agent-skill" in names_none


# ---------------------------------------------------------------------------
# Test 4: managed_packs config key defaults to empty list
# ---------------------------------------------------------------------------

def test_managed_packs_config_default_empty(curator_env):
    """get_managed_packs() must return [] when the config key is absent."""
    c = curator_env["curator"]
    # _load_config is already patched to return {} (no managed_packs key).
    assert c.get_managed_packs() == []


def test_managed_packs_config_reads_value(curator_env, monkeypatch):
    """get_managed_packs() must return the configured list."""
    c = curator_env["curator"]
    monkeypatch.setattr(c, "_load_config", lambda: {"managed_packs": ["pack-a", "pack-b"]})
    assert c.get_managed_packs() == ["pack-a", "pack-b"]


def test_managed_packs_config_non_list_returns_empty(curator_env, monkeypatch):
    """get_managed_packs() must tolerate a misconfigured non-list value."""
    c = curator_env["curator"]
    monkeypatch.setattr(c, "_load_config", lambda: {"managed_packs": "pack-a"})
    assert c.get_managed_packs() == []


# ---------------------------------------------------------------------------
# Test 5: managed_packs note is injected into the LLM review prompt
# ---------------------------------------------------------------------------

def test_managed_packs_note_in_prompt(curator_env, monkeypatch):
    """When managed_packs is non-empty the override note must appear in the prompt."""
    home = curator_env["home"]
    usage = curator_env["usage"]
    curator = curator_env["curator"]
    skills_dir = home / "skills"

    _write_skill(skills_dir, "managed-pack")
    _mark_hub_installed(home, "managed-pack")

    importlib.reload(usage)
    monkeypatch.setattr(usage, "_prune_builtins_enabled", lambda: False)

    # Wire managed_packs config.
    monkeypatch.setattr(curator, "_load_config", lambda: {"managed_packs": ["managed-pack"]})
    # Reload so get_managed_packs picks up the patched _load_config.
    importlib.reload(curator)
    monkeypatch.setattr(curator, "_run_llm_review", lambda prompt: {
        "final": prompt,  # echo the prompt so we can inspect it
        "summary": "stub",
        "model": "",
        "provider": "",
        "tool_calls": [],
        "error": None,
    })
    monkeypatch.setattr(curator, "_load_config", lambda: {"managed_packs": ["managed-pack"]})

    captured: list = []

    def _capture(prompt):
        captured.append(prompt)
        return {
            "final": "",
            "summary": "stub",
            "model": "",
            "provider": "",
            "tool_calls": [],
            "error": None,
        }

    monkeypatch.setattr(curator, "_run_llm_review", _capture)

    # Also patch _render_candidate_list to return a non-empty list so the
    # LLM pass is not skipped.
    monkeypatch.setattr(
        curator,
        "_render_candidate_list",
        lambda managed_packs=None: "Agent-created skills (1):\n- managed-pack  state=active",
    )

    # Trigger the review (synchronous, not dry-run).
    curator.run_curator_review(synchronous=True, dry_run=False)

    assert captured, "Expected _run_llm_review to be called"
    assert "MANAGED-PACKS OVERRIDE" in captured[0]
    assert "managed-pack" in captured[0]


# ---------------------------------------------------------------------------
# Test 6: Rollback restores prior content for a managed-pack skill
# ---------------------------------------------------------------------------

def test_rollback_restores_managed_pack_content(tmp_path, monkeypatch):
    """snapshot → content-patch → rollback must restore the original SKILL.md.

    This test exercises the backup/rollback plumbing (curator_backup) for a
    managed-pack skill:
      (a) The pre-patch snapshot is created via snapshot_skills.
      (b) The skill content is modified (simulating a curator content-patch).
      (c) rollback() restores the original SKILL.md content.
    """
    # Set up isolated HERMES_HOME.
    home = tmp_path / ".hermes"
    skills_dir = home / "skills"
    skills_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    import agent.curator_backup as cb
    importlib.reload(cb)

    # Create a managed-pack skill with known original content.
    pack_dir = skills_dir / "my-managed-pack"
    pack_dir.mkdir()
    original_content = "---\nname: my-managed-pack\ndescription: original\n---\n\nOriginal skill body.\n"
    (pack_dir / "SKILL.md").write_text(original_content, encoding="utf-8")

    # (a) Take a pre-patch snapshot.
    snap = cb.snapshot_skills(reason="pre-curator-run")
    assert snap is not None, "snapshot_skills should succeed"
    assert (snap / "skills.tar.gz").exists(), "snapshot archive must exist"

    # (b) Simulate a curator content-patch — overwrite SKILL.md.
    patched_content = "---\nname: my-managed-pack\ndescription: patched\n---\n\nPatched skill body.\n"
    (pack_dir / "SKILL.md").write_text(patched_content, encoding="utf-8")
    assert (pack_dir / "SKILL.md").read_text(encoding="utf-8") == patched_content

    # (c) Rollback to the pre-patch snapshot.
    ok, msg, rolled_snap = cb.rollback()
    assert ok, f"rollback() should succeed; got: {msg}"

    # Original content must be restored.
    restored = (skills_dir / "my-managed-pack" / "SKILL.md").read_text(encoding="utf-8")
    assert restored == original_content, (
        f"rollback should restore original SKILL.md content.\nGot:\n{restored}"
    )
