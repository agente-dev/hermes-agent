"""Tests for plugins/email — the gws subprocess wrapper.

Covers:

  * Binary resolution via AGENTE_GWS_BIN env / PATH fallback.
  * Clear ``gws not bundled`` error when neither is set.
  * Each tool shells the expected ``gws gmail <subcmd> --json`` argv.
  * Stdout JSON is parsed and returned as a dict/list.
  * Plugin __init__ exposes 5 tools through the register() context shim.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_email_plugin():
    """Import plugins/email/email_plugin.py without requiring the full hermes_cli stack."""
    # Ensure repo root is on sys.path so `import plugins.email.email_plugin` works.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    if "plugins.email.email_plugin" in sys.modules:
        return importlib.reload(sys.modules["plugins.email.email_plugin"])
    return importlib.import_module("plugins.email.email_plugin")


@pytest.fixture
def email_plugin(monkeypatch):
    monkeypatch.setenv("AGENTE_GWS_BIN", "/fake/bin/gws")
    return _load_email_plugin()


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------


class TestResolveGwsBin:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("AGENTE_GWS_BIN", "/opt/bundled/gws")
        mod = _load_email_plugin()
        assert mod._resolve_gws_bin() == "/opt/bundled/gws"

    def test_falls_back_to_which(self, monkeypatch):
        monkeypatch.delenv("AGENTE_GWS_BIN", raising=False)
        mod = _load_email_plugin()
        with patch("plugins.email.email_plugin.shutil.which", return_value="/usr/local/bin/gws"):
            assert mod._resolve_gws_bin() == "/usr/local/bin/gws"

    def test_raises_when_missing(self, monkeypatch):
        monkeypatch.delenv("AGENTE_GWS_BIN", raising=False)
        mod = _load_email_plugin()
        with patch("plugins.email.email_plugin.shutil.which", return_value=None):
            with pytest.raises(mod.GwsUnavailableError) as exc_info:
                mod._resolve_gws_bin()
            assert "gws not bundled" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Tool argv + JSON parsing
# ---------------------------------------------------------------------------


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestListEmails:
    def test_shells_expected_argv(self, email_plugin):
        payload = {"messages": [{"id": "abc", "subject": "Hi"}]}
        with patch("plugins.email.email_plugin.subprocess.run", return_value=_completed(json.dumps(payload))) as run:
            out = email_plugin.list_emails(folder="INBOX", max=10)
        argv = run.call_args[0][0]
        assert argv == ["/fake/bin/gws", "gmail", "messages", "list", "--folder", "INBOX", "--max", "10", "--json"]
        assert out == payload

    def test_default_folder_is_inbox(self, email_plugin):
        with patch("plugins.email.email_plugin.subprocess.run", return_value=_completed("{}")) as run:
            email_plugin.list_emails()
        assert "INBOX" in run.call_args[0][0]


class TestReadEmail:
    def test_passes_id_and_raw(self, email_plugin):
        payload = {"id": "abc", "body": "שלום"}  # Hebrew round-trip
        with patch("plugins.email.email_plugin.subprocess.run", return_value=_completed(json.dumps(payload, ensure_ascii=False))) as run:
            out = email_plugin.read_email(message_id="abc")
        argv = run.call_args[0][0]
        assert argv == ["/fake/bin/gws", "gmail", "messages", "get", "abc", "--json", "--raw"]
        assert out["body"] == "שלום"


class TestDraftReply:
    def test_argv(self, email_plugin):
        with patch("plugins.email.email_plugin.subprocess.run", return_value=_completed('{"draft_id": "d1"}')) as run:
            out = email_plugin.draft_reply(message_id="m1", body="thanks")
        argv = run.call_args[0][0]
        assert argv == ["/fake/bin/gws", "gmail", "drafts", "create", "--in-reply-to", "m1", "--body", "thanks", "--json"]
        assert out == {"draft_id": "d1"}


class TestSendEmail:
    def test_argv(self, email_plugin):
        with patch("plugins.email.email_plugin.subprocess.run", return_value=_completed('{"ok": true}')) as run:
            email_plugin.send_email(to="a@b.co", subject="s", body="b")
        argv = run.call_args[0][0]
        assert argv == ["/fake/bin/gws", "gmail", "messages", "send", "--to", "a@b.co", "--subject", "s", "--body", "b", "--json"]


class TestMarkEmail:
    def test_add_label(self, email_plugin):
        with patch("plugins.email.email_plugin.subprocess.run", return_value=_completed("{}")) as run:
            email_plugin.mark_email(message_id="m1", add_label="STARRED")
        argv = run.call_args[0][0]
        assert argv == ["/fake/bin/gws", "gmail", "messages", "modify", "m1", "--json", "--add-label", "STARRED"]

    def test_remove_label(self, email_plugin):
        with patch("plugins.email.email_plugin.subprocess.run", return_value=_completed("{}")) as run:
            email_plugin.mark_email(message_id="m1", remove_label="UNREAD")
        argv = run.call_args[0][0]
        assert argv == ["/fake/bin/gws", "gmail", "messages", "modify", "m1", "--json", "--remove-label", "UNREAD"]


class TestErrorPropagation:
    def test_nonzero_exit_raises(self, email_plugin):
        with patch("plugins.email.email_plugin.subprocess.run", return_value=_completed("", returncode=2, stderr="boom")):
            with pytest.raises(RuntimeError) as exc_info:
                email_plugin.list_emails()
            assert "exit 2" in str(exc_info.value)
            assert "boom" in str(exc_info.value)

    def test_missing_binary_surfaces_unavailable(self, monkeypatch):
        monkeypatch.delenv("AGENTE_GWS_BIN", raising=False)
        mod = _load_email_plugin()
        with patch("plugins.email.email_plugin.shutil.which", return_value=None):
            with pytest.raises(mod.GwsUnavailableError):
                mod.list_emails()


# ---------------------------------------------------------------------------
# Plugin registration — discoverable + 5 tools
# ---------------------------------------------------------------------------


class _FakeCtx:
    def __init__(self) -> None:
        self.tools: list[dict] = []

    def register_tool(self, **kw) -> None:
        self.tools.append(kw)


def test_register_exposes_five_tools(monkeypatch):
    """plugins.email.register() should publish exactly five tools in the `email` toolset."""
    monkeypatch.setenv("AGENTE_GWS_BIN", "/fake/bin/gws")
    # Lazy import so the test does not require the full hermes_cli during collection.
    sys.path.insert(0, str(REPO_ROOT))

    # Stub tools.registry symbols the package imports at module top-level.
    if "tools.registry" not in sys.modules:
        registry_stub = types.ModuleType("tools.registry")
        registry_stub.tool_result = lambda data=None, **kw: json.dumps({"data": data, **kw})
        registry_stub.tool_error = lambda message, **kw: json.dumps({"error": message, **kw})
        tools_pkg = types.ModuleType("tools")
        tools_pkg.registry = registry_stub
        sys.modules["tools"] = tools_pkg
        sys.modules["tools.registry"] = registry_stub

    if "plugins.email" in sys.modules:
        del sys.modules["plugins.email"]
    pkg = importlib.import_module("plugins.email")

    ctx = _FakeCtx()
    pkg.register(ctx)

    names = [t["name"] for t in ctx.tools]
    assert names == ["list_emails", "read_email", "draft_reply", "send_email", "mark_email"]
    for t in ctx.tools:
        assert t["toolset"] == "email"
        assert "parameters" in t["schema"]
