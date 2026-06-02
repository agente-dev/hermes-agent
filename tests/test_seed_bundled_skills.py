"""Unit tests for ``tools/seed_bundled_skills.py``.

Verifies acceptance criteria from intake ``hermes-202606-006``:

  - Bundled skills present after fresh install (manifest written).
  - Desktop never writes to ``~/.hermes/skills/*`` (seeder runs from the
    Hermes-install side; this test calls it directly).
  - Manifest enumerates ``source: workspace-pack | hermes-skill |
    upstream-hermes-skill``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SEEDER_PATH = REPO_ROOT / "tools" / "seed_bundled_skills.py"


@pytest.fixture(scope="module")
def seeder_module():
    """Load the seeder as a module without polluting tools/ import side effects."""
    spec = importlib.util.spec_from_file_location("seed_bundled_skills", SEEDER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["seed_bundled_skills"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_skill(root: Path, rel: str, name: str) -> Path:
    p = root / rel / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\n"
        f"name: {name}\n"
        "description: synthetic test skill\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    return p


def test_discover_classifies_sources(seeder_module, tmp_path: Path):
    repo = tmp_path / "repo"
    _make_skill(repo, "skills/email/gmail", "gmail-skill")
    _make_skill(repo, "optional-skills/research/arxiv", "arxiv-skill")

    found = seeder_module.discover_bundled_skills(repo)
    by_name = {s.name: s for s in found}

    assert by_name["gmail-skill"].source == seeder_module.SOURCE_HERMES_SKILL
    assert by_name["arxiv-skill"].source == seeder_module.SOURCE_UPSTREAM_HERMES_SKILL
    # Hash is non-empty and stable shape.
    assert len(by_name["gmail-skill"].sha256) == 64
    assert by_name["gmail-skill"].rel_path == "skills/email/gmail/SKILL.md"


def test_seed_writes_json_manifest(seeder_module, tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "hermes_home"
    _make_skill(repo, "skills/email/gmail", "gmail-skill")
    _make_skill(repo, "optional-skills/research/arxiv", "arxiv-skill")

    seeder_module.seed(home, repo)

    manifest_json = home / "skills" / ".bundled_manifest.json"
    assert manifest_json.exists(), "JSON manifest must be present after fresh install"

    payload = json.loads(manifest_json.read_text(encoding="utf-8"))
    assert payload["format_version"] == 1
    assert set(payload["sources"]) == {
        seeder_module.SOURCE_WORKSPACE_PACK,
        seeder_module.SOURCE_HERMES_SKILL,
        seeder_module.SOURCE_UPSTREAM_HERMES_SKILL,
    }
    names = {entry["name"]: entry for entry in payload["skills"]}
    assert names["gmail-skill"]["source"] == seeder_module.SOURCE_HERMES_SKILL
    assert names["arxiv-skill"]["source"] == seeder_module.SOURCE_UPSTREAM_HERMES_SKILL


def test_seed_is_idempotent_and_atomic(seeder_module, tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "hermes_home"
    _make_skill(repo, "skills/email/gmail", "gmail-skill")

    seeder_module.seed(home, repo)
    manifest = home / "skills" / ".bundled_manifest.json"
    first = manifest.read_text(encoding="utf-8")

    # Second pass: same inputs, same content.
    seeder_module.seed(home, repo)
    second = manifest.read_text(encoding="utf-8")
    assert first == second

    # No stray tempfile leaked next to the manifest.
    leftover = [p for p in manifest.parent.iterdir() if p.name.startswith(".bundled_manifest.") and p.suffix != ".json"]
    assert leftover == []


def test_seed_skips_dot_directories(seeder_module, tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "hermes_home"
    _make_skill(repo, "skills/email/gmail", "gmail-skill")
    # Should NOT pick up archived/quarantined SKILL.md files.
    _make_skill(repo, "skills/.archive/old-thing", "should-not-appear")
    _make_skill(repo, "skills/.hub/quarantine/bad", "also-should-not-appear")

    found = {s.name for s in seeder_module.discover_bundled_skills(repo)}
    assert "gmail-skill" in found
    assert "should-not-appear" not in found
    assert "also-should-not-appear" not in found


def test_real_repo_discovery_runs(seeder_module):
    """Smoke: seeder runs over the real shipping skills/ tree without raising."""
    found = seeder_module.discover_bundled_skills(REPO_ROOT)
    # The shipping repo has >0 SKILL.md files; if this regresses to 0
    # the install seeding would silently produce an empty manifest.
    assert len(found) > 0
    # Every entry has the required keys.
    for s in found:
        assert s.name
        assert s.source in {
            seeder_module.SOURCE_WORKSPACE_PACK,
            seeder_module.SOURCE_HERMES_SKILL,
            seeder_module.SOURCE_UPSTREAM_HERMES_SKILL,
        }
        assert len(s.sha256) == 64


def test_cli_main_writes_manifest(seeder_module, tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "hermes_home"
    _make_skill(repo, "skills/email/gmail", "gmail-skill")

    rc = seeder_module.main(
        ["--hermes-home", str(home), "--repo-root", str(repo), "--quiet"]
    )
    assert rc == 0
    assert (home / "skills" / ".bundled_manifest.json").exists()
