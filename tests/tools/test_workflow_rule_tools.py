"""Deprecation guard for the legacy workflow_rule tools.

The Hermes tools ``save_workflow_rule`` and ``list_workflow_rules`` were
removed in hermes-agent-202606-028 / desktop-202606-514. The Python
module ``tools/workflow_rule_tools.py`` now exposes only deprecation
shims — calling them raises ``RuntimeError``, and the tools no longer
appear in the registry / toolsets / legacy toolset map.
"""

from __future__ import annotations

import pytest


def test_handlers_raise_runtime_error_after_deprecation():
    import importlib

    import tools.workflow_rule_tools as tool_mod
    importlib.reload(tool_mod)

    with pytest.raises(RuntimeError, match="DEPRECATED"):
        tool_mod.save_workflow_rule_handler(
            id="x",
            connector_id="wa",
            rule_natural_language="x",
        )
    with pytest.raises(RuntimeError, match="DEPRECATED"):
        tool_mod.list_workflow_rules_handler()


def test_not_registered_in_registry():
    import tools.workflow_rule_tools  # noqa: F401 — import for side effects
    from tools.registry import registry

    assert registry.get_entry("save_workflow_rule") is None
    assert registry.get_entry("list_workflow_rules") is None


def test_not_in_toolsets_or_legacy_map():
    from toolsets import _HERMES_CORE_TOOLS, TOOLSETS
    from model_tools import _LEGACY_TOOLSET_MAP

    for name in ("save_workflow_rule", "list_workflow_rules"):
        assert name not in _HERMES_CORE_TOOLS
    assert "workflow_rules" not in TOOLSETS
    assert "workflow_rule_tools" not in _LEGACY_TOOLSET_MAP
