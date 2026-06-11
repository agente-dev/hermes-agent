"""web_browser plugin — Hermes wrapper around the agent-browser CLI.

Exposes ten ``browser_*`` tools that shell out to ``agent-browser <subcommand>
--json`` (subprocess). Mirrors the gws / google_meet plugin pattern: each tool
declares a JSON schema, a Hebrew operator label (``label_he``), a category, and
a handler that returns a structured dict.

First-use of any tool in a session emits a canonical ``approval.request``
event per the Hermes prompt-flow protocol (see /tmp/hermes-protocol-research.md
and ui-tui/README.md "Prompt flows"). The agent must wait for an
``approval.respond`` with choice ∈ {once, session, always, deny} before the
subprocess is actually launched. session/always scopes are remembered via
``tools.approval.resolve_gateway_approval`` exactly like every other
dangerous-command tool.
"""

from __future__ import annotations

import inspect
import logging

from plugins.web_browser.web_browser_plugin import (
    AGENT_BROWSER_BIN_ENV,
    TOOL_DEFS,
    check_web_browser_requirements,
)

logger = logging.getLogger(__name__)


def _register_tool(ctx, **kwargs) -> None:
    register_tool = ctx.register_tool
    try:
        signature = inspect.signature(register_tool)
    except (TypeError, ValueError):
        supports_override = True
    else:
        supports_override = "override" in signature.parameters or any(
            param.kind is inspect.Parameter.VAR_KEYWORD
            for param in signature.parameters.values()
        )

    if supports_override:
        kwargs["override"] = True
    register_tool(**kwargs)


def register(ctx) -> None:
    """Register all ten browser_* tools.

    Called once by the plugin loader when ``plugins.enabled`` includes
    ``web_browser`` in config.yaml.

    Hermes ships a built-in
    ``browser_navigate`` / ``browser_click`` / ... family in
    ``tools/browser_tool.py`` (Playwright-direct). When the host context
    supports explicit overrides, this plugin replaces them with the
    agent-browser CLI implementation so a single global ``agent-browser``
    install services every Hermes operator. Lightweight gateway contexts that
    do not accept the ``override`` kwarg still receive the plugin tools.
    """
    for tool in TOOL_DEFS:
        _register_tool(
            ctx,
            name=tool["name"],
            toolset="web_browser",
            schema=tool["schema"],
            handler=tool["handler"],
            check_fn=check_web_browser_requirements,
            emoji=tool["emoji"],
        )
    logger.info(
        "web_browser plugin registered %d tools (agent-browser binary env: %s)",
        len(TOOL_DEFS), AGENT_BROWSER_BIN_ENV,
    )
