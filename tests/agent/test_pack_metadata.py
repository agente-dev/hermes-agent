"""Tests for pack-level metadata loading in agent.skill_utils.

Covers the optional ``pack.yaml`` contract:
- packs WITH ``default_prompt`` expose it via ``get_pack_default_prompt``
- packs WITHOUT ``pack.yaml`` (or without the field) return ``None`` cleanly
- malformed YAML never raises — it must degrade to ``None``
"""

from agent.skill_utils import (
    _pack_metadata_cache_clear,
    get_pack_default_prompt,
    read_pack_metadata,
)


def test_pack_with_default_prompt(tmp_path):
    _pack_metadata_cache_clear()
    pack_dir = tmp_path / "example-pack"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(
        "name: example-pack\ndefault_prompt: Hello from pack.\n",
        encoding="utf-8",
    )

    meta = read_pack_metadata(pack_dir)
    assert meta.get("name") == "example-pack"
    assert meta.get("default_prompt") == "Hello from pack."

    assert get_pack_default_prompt(pack_dir) == "Hello from pack."


def test_pack_without_pack_yaml_returns_empty(tmp_path):
    _pack_metadata_cache_clear()
    pack_dir = tmp_path / "no-pack-yaml"
    pack_dir.mkdir()

    assert read_pack_metadata(pack_dir) == {}
    assert get_pack_default_prompt(pack_dir) is None


def test_pack_yaml_without_default_prompt_returns_none(tmp_path):
    _pack_metadata_cache_clear()
    pack_dir = tmp_path / "metadata-only"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(
        "name: metadata-only\ndescription: no prompt here\n",
        encoding="utf-8",
    )

    meta = read_pack_metadata(pack_dir)
    assert meta.get("name") == "metadata-only"
    assert "default_prompt" not in meta
    assert get_pack_default_prompt(pack_dir) is None


def test_pack_yaml_with_blank_default_prompt_returns_none(tmp_path):
    _pack_metadata_cache_clear()
    pack_dir = tmp_path / "blank-prompt"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(
        'name: blank-prompt\ndefault_prompt: "   "\n',
        encoding="utf-8",
    )

    assert get_pack_default_prompt(pack_dir) is None


def test_pack_yaml_with_non_string_default_prompt_returns_none(tmp_path):
    _pack_metadata_cache_clear()
    pack_dir = tmp_path / "list-prompt"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(
        "name: list-prompt\ndefault_prompt:\n  - a\n  - b\n",
        encoding="utf-8",
    )

    assert get_pack_default_prompt(pack_dir) is None


def test_malformed_pack_yaml_does_not_raise(tmp_path):
    _pack_metadata_cache_clear()
    pack_dir = tmp_path / "broken-pack"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(
        ":: this is not valid yaml ::\n  - missing\n: colon\n",
        encoding="utf-8",
    )

    # Must not raise — pack metadata is strictly optional.
    assert read_pack_metadata(pack_dir) == {}
    assert get_pack_default_prompt(pack_dir) is None


def test_non_mapping_pack_yaml_returns_empty(tmp_path):
    _pack_metadata_cache_clear()
    pack_dir = tmp_path / "list-root"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text("- one\n- two\n", encoding="utf-8")

    assert read_pack_metadata(pack_dir) == {}
    assert get_pack_default_prompt(pack_dir) is None


def test_missing_pack_dir_returns_empty(tmp_path):
    _pack_metadata_cache_clear()
    missing = tmp_path / "does-not-exist"
    assert read_pack_metadata(missing) == {}
    assert get_pack_default_prompt(missing) is None
