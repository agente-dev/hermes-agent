"""Integration tests for pack-level default_prompt in _find_all_skills().

Verifies the additive contract:
- a skill inside a pack WITH pack.yaml + default_prompt gets the field
  surfaced in the discovery output as ``pack_default_prompt``
- a skill inside a pack WITHOUT pack.yaml (or without the field) is
  returned WITHOUT the ``pack_default_prompt`` key — fully backward
  compatible with the existing ``{name, description, category}`` shape
"""

import pytest

from agent.skill_utils import _pack_metadata_cache_clear


SKILL_MD = """---
name: {name}
description: Synthetic skill for pack default_prompt discovery tests.
---

Body text for {name}.
"""


@pytest.fixture(autouse=True)
def _clear_pack_cache():
    _pack_metadata_cache_clear()
    yield
    _pack_metadata_cache_clear()


def _make_skill(skills_root, pack_name: str, skill_name: str) -> None:
    skill_dir = skills_root / pack_name / skill_name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        SKILL_MD.format(name=skill_name), encoding="utf-8"
    )


def test_pack_with_default_prompt_propagates_to_discovery(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _make_skill(skills_root, "example-pack", "example-skill")
    (skills_root / "example-pack" / "pack.yaml").write_text(
        "name: example-pack\ndefault_prompt: From the pack.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills_root)

    from tools.skills_tool import _find_all_skills

    skills = _find_all_skills(skip_disabled=True)
    names = {s["name"]: s for s in skills}
    assert "example-skill" in names
    assert names["example-skill"].get("pack_default_prompt") == "From the pack."
    assert names["example-skill"].get("category") == "example-pack"


def test_pack_without_pack_yaml_omits_default_prompt(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _make_skill(skills_root, "plain-pack", "plain-skill")

    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills_root)

    from tools.skills_tool import _find_all_skills

    skills = _find_all_skills(skip_disabled=True)
    names = {s["name"]: s for s in skills}
    assert "plain-skill" in names
    # Backward-compat: skills in packs without pack.yaml must NOT carry
    # a pack_default_prompt key. Existing /v1/skills consumers rely on
    # the original 3-key shape staying intact when no pack metadata exists.
    assert "pack_default_prompt" not in names["plain-skill"]


def test_pack_yaml_without_default_prompt_field_omits_key(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _make_skill(skills_root, "meta-only-pack", "meta-skill")
    (skills_root / "meta-only-pack" / "pack.yaml").write_text(
        "name: meta-only-pack\ndescription: no default_prompt\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills_root)

    from tools.skills_tool import _find_all_skills

    skills = _find_all_skills(skip_disabled=True)
    names = {s["name"]: s for s in skills}
    assert "meta-skill" in names
    assert "pack_default_prompt" not in names["meta-skill"]
