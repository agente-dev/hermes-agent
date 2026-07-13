"""Tests for the optional codex app-server runtime gate.

These are unit tests for the api_mode rewriter and the wire-level transport
module. They do NOT require the `codex` CLI to be installed — that's
covered by a separate live test gated on `codex --version`.
"""

from __future__ import annotations

from unittest.mock import Mock, call

import pytest

from hermes_cli import runtime_provider as runtime_provider_mod
from hermes_cli.runtime_provider import _VALID_API_MODES


class TestApiModeRegistration:
    """The official runtime is not a generic endpoint api_mode."""

    def test_codex_app_server_is_not_a_generic_api_mode(self) -> None:
        assert "codex_app_server" not in _VALID_API_MODES

    def test_existing_api_modes_still_present(self) -> None:
        # Regression guard: don't accidentally delete other api_modes when
        # touching this set.
        for mode in (
            "chat_completions",
            "codex_responses",
            "anthropic_messages",
            "bedrock_converse",
        ):
            assert mode in _VALID_API_MODES


class TestCredentiallessRuntimeResolution:
    def test_openai_codex_app_server_skips_all_hermes_credentials(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            runtime_provider_mod,
            "_get_model_config",
            lambda: {
                "provider": "openai-codex",
                "openai_runtime": "codex_app_server",
            },
        )

        def unexpected(*args, **kwargs):
            raise AssertionError("legacy provider/token resolution must be skipped")

        monkeypatch.setattr(runtime_provider_mod, "resolve_provider", unexpected)
        monkeypatch.setattr(runtime_provider_mod, "load_pool", unexpected)
        monkeypatch.setattr(
            runtime_provider_mod,
            "resolve_codex_runtime_credentials",
            unexpected,
        )

        resolved = runtime_provider_mod.resolve_runtime_provider(
            requested="openai-codex",
            explicit_api_key="must-not-be-replayed",
            explicit_base_url="https://chatgpt.com/backend-api/codex",
        )

        assert resolved == {
            "provider": "openai-codex",
            "api_mode": "codex_app_server",
            "base_url": "",
            "api_key": "",
            "source": "codex-app-server",
            "credential_pool": None,
            "requested_provider": "openai-codex",
        }

    def test_config_selected_openai_codex_takes_same_official_branch(
        self, monkeypatch
    ) -> None:
        config = {
            "provider": "openai-codex",
            "openai_runtime": "Codex_App_Server",
        }
        monkeypatch.setattr(
            runtime_provider_mod,
            "_get_model_config",
            lambda: dict(config),
        )
        monkeypatch.setattr(
            runtime_provider_mod,
            "resolve_provider",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("canonical provider resolution must be skipped")
            ),
        )

        resolved = runtime_provider_mod.resolve_runtime_provider()

        assert resolved["provider"] == "openai-codex"
        assert resolved["api_mode"] == "codex_app_server"
        assert resolved["api_key"] == ""
        assert resolved["base_url"] == ""

    @pytest.mark.parametrize("configured_provider", ["openai", "", "openrouter"])
    def test_runtime_flag_cannot_rewrite_an_inexact_persisted_provider(
        self, monkeypatch, configured_provider
    ) -> None:
        resolved = runtime_provider_mod._credentialless_codex_app_server_runtime(
            requested_provider="openai-codex",
            model_cfg={
                "provider": configured_provider,
                "openai_runtime": "codex_app_server",
            },
        )

        assert resolved is None


class TestCodexAppServerModule:
    """Module-surface tests for the JSON-RPC speaker. Don't require codex CLI."""

    def test_module_imports(self) -> None:
        from agent.transports import codex_app_server

        assert codex_app_server.MIN_CODEX_VERSION == (0, 144, 1)
        assert callable(codex_app_server.parse_codex_version)
        assert callable(codex_app_server.check_codex_binary)

    def test_parse_codex_version_valid(self) -> None:
        from agent.transports.codex_app_server import parse_codex_version

        assert parse_codex_version("codex-cli 0.130.0") == (0, 130, 0)
        assert parse_codex_version("codex-cli 1.2.3 (extra metadata)") == (1, 2, 3)
        assert parse_codex_version("codex 99.0.1\n") == (99, 0, 1)

    def test_parse_codex_version_invalid(self) -> None:
        from agent.transports.codex_app_server import parse_codex_version

        assert parse_codex_version("nope") is None
        assert parse_codex_version("") is None
        assert parse_codex_version(None) is None  # type: ignore[arg-type]

    def test_check_binary_handles_missing_executable(self) -> None:
        from agent.transports.codex_app_server import check_codex_binary

        ok, msg = check_codex_binary(codex_bin="/nonexistent/codex/binary/path")
        assert ok is False
        assert "not found" in msg.lower() or "no such" in msg.lower()

    def test_codex_error_class_is_runtimeerror(self) -> None:
        from agent.transports.codex_app_server import CodexAppServerError

        err = CodexAppServerError(code=-32600, message="boom")
        assert isinstance(err, RuntimeError)
        assert "boom" in str(err)
        assert "-32600" in str(err)


class TestOfficialAccountAndModelHelpers:
    @staticmethod
    def _client():
        from agent.transports.codex_app_server import CodexAppServerClient

        client = object.__new__(CodexAppServerClient)
        client._initialized = True
        client.request = Mock(return_value={"ok": True})
        return client

    def test_account_read_uses_official_method(self) -> None:
        client = self._client()

        assert client.account_read(refresh_token=True, timeout=7) == {"ok": True}
        client.request.assert_called_once_with(
            "account/read", {"refreshToken": True}, timeout=7
        )

    def test_browser_login_uses_chatgpt_variant_only(self) -> None:
        client = self._client()

        client.account_login_start(
            use_hosted_login_success_page=True,
            codex_streamlined_login=False,
            app_brand="codex",
            timeout=8,
        )

        client.request.assert_called_once_with(
            "account/login/start",
            {
                "type": "chatgpt",
                "useHostedLoginSuccessPage": True,
                "codexStreamlinedLogin": False,
                "appBrand": "codex",
            },
            timeout=8,
        )

    def test_device_code_login_uses_official_variant(self) -> None:
        client = self._client()

        client.account_login_start(device_code=True)

        client.request.assert_called_once_with(
            "account/login/start",
            {"type": "chatgptDeviceCode"},
            timeout=30.0,
        )

    def test_logout_and_model_list_use_official_methods(self) -> None:
        client = self._client()

        client.account_logout(timeout=9)
        client.model_list(
            cursor="opaque-cursor",
            limit=25,
            include_hidden=False,
            timeout=10,
        )

        assert client.request.call_args_list == [
            call("account/logout", timeout=9),
            call(
                "model/list",
                {
                    "cursor": "opaque-cursor",
                    "limit": 25,
                    "includeHidden": False,
                },
                timeout=10,
            ),
        ]

    def test_parameterless_notification_omits_params(self) -> None:
        from agent.transports.codex_app_server import CodexAppServerClient

        client = object.__new__(CodexAppServerClient)
        client._send = Mock()

        client.notify("initialized")

        client._send.assert_called_once_with({"method": "initialized"})

    def test_helpers_require_initialize(self) -> None:
        client = self._client()
        client._initialized = False

        with pytest.raises(RuntimeError, match="initialized before account/read"):
            client.account_read()

        client.request.assert_not_called()

    def test_device_code_rejects_browser_only_options(self) -> None:
        client = self._client()

        with pytest.raises(ValueError, match="browser login options"):
            client.account_login_start(device_code=True, app_brand="codex")

        client.request.assert_not_called()

    @pytest.mark.parametrize("limit", [-1, True, 0x100000000])
    def test_model_list_rejects_values_outside_wire_uint32(self, limit) -> None:
        client = self._client()

        with pytest.raises(ValueError, match="uint32"):
            client.model_list(limit=limit)

        client.request.assert_not_called()


class TestSpawnEnvIsolation:
    """The codex spawn must NOT rewrite HOME — codex's shell tool spawns
    subprocesses (gh, git, npm, aws, gcloud, ...) that need to find their
    config in the real user $HOME. CODEX_HOME isolates codex's own state,
    HOME stays unchanged.

    OpenClaw hit this footgun (openclaw/openclaw#81562) — they were
    rewriting HOME to a synthetic per-agent dir alongside CODEX_HOME,
    and then `gh auth status` / git config / etc. all broke inside codex
    shell calls. We avoid the same bug by only overlaying CODEX_HOME and
    RUST_LOG on top of os.environ.copy().
    """

    def test_spawn_env_preserves_HOME(self, monkeypatch):
        """The spawn env must contain the parent process's HOME unchanged.
        Verifies via a subprocess-monkey-patch."""
        import subprocess
        from agent.transports import codex_app_server as cas

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["env"] = kwargs.get("env", {}).copy()
                # Provide minimal Popen surface so __init__ doesn't crash
                # on attribute access during construction.
                self.stdin = None
                self.stdout = None
                self.stderr = None
                self.pid = 1
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setenv("HOME", "/users/alice")

        client = cas.CodexAppServerClient(codex_bin="codex")
        client._closed = True  # so close() is a no-op

        # The spawn env must have HOME=/users/alice unchanged
        assert captured["env"].get("HOME") == "/users/alice", (
            f"HOME got rewritten in codex spawn env: "
            f"{captured['env'].get('HOME')!r}. Codex's shell tool's "
            "subprocesses (gh, git, aws, npm) need the user's real HOME."
        )

    def test_spawn_env_sets_CODEX_HOME_when_provided(self, monkeypatch):
        """CODEX_HOME isolation must still work — that's the whole point
        of the codex_home arg."""
        import subprocess
        from agent.transports import codex_app_server as cas

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["env"] = kwargs.get("env", {}).copy()
                self.stdin = None
                self.stdout = None
                self.stderr = None
                self.pid = 1
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setenv("HOME", "/users/alice")

        client = cas.CodexAppServerClient(
            codex_bin="codex", codex_home="/tmp/profile/codex"
        )
        client._closed = True

        assert captured["env"].get("CODEX_HOME") == "/tmp/profile/codex"
        # And HOME still passes through unchanged
        assert captured["env"].get("HOME") == "/users/alice"

    def test_spawn_env_scrubs_provider_and_shared_gateway_settings(
        self, monkeypatch
    ):
        import subprocess
        from agent.transports import codex_app_server as cas

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["env"] = kwargs.get("env", {}).copy()
                self.stdin = None
                self.stdout = None
                self.stderr = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://shared.invalid")
        monkeypatch.setenv("AI_GATEWAY_API_KEY", "must-not-reach-child")
        monkeypatch.setenv("VERCEL_AI_GATEWAY_API_KEY", "must-not-reach-child")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "must-not-reach-child")

        client = cas.CodexAppServerClient(
            codex_bin="codex",
            env={
                "OPENAI_API_KEY": "overlay-must-also-be-dropped",
                "HERMES_MAIN_RUNTIME_PROVIDER": "openai-codex",
            },
        )
        client._closed = True

        assert "OPENAI_API_KEY" not in captured["env"]
        assert "OPENAI_BASE_URL" not in captured["env"]
        assert "AI_GATEWAY_API_KEY" not in captured["env"]
        assert "VERCEL_AI_GATEWAY_API_KEY" not in captured["env"]
        assert "AZURE_OPENAI_API_KEY" not in captured["env"]
        assert captured["env"]["HERMES_MAIN_RUNTIME_PROVIDER"] == "openai-codex"

    def test_absolute_binary_uses_standalone_app_server_argv(
        self, monkeypatch, tmp_path
    ):
        import subprocess
        from agent.transports import codex_app_server as cas

        captured = {}
        binary = tmp_path / "codex-app-server"
        binary.touch()
        binary.chmod(0o700)

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["cmd"] = list(cmd)
                self.stdin = None
                self.stdout = None
                self.stderr = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        client = cas.CodexAppServerClient(codex_bin=str(binary))
        client._closed = True

        assert captured["cmd"] == [
            str(binary),
            "--session-source",
            "exec",
        ]

    def test_kanban_worker_adds_only_kanban_writable_root(self, monkeypatch):
        """Codex-runtime Kanban workers need to write board state outside
        their scratch/worktree workspace, but should not fall back to
        danger-full-access. Hermes passes a narrow app-server config override
        for the Kanban root only.
        """
        import subprocess
        from agent.transports import codex_app_server as cas

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["cmd"] = list(cmd)
                captured["env"] = kwargs.get("env", {}).copy()
                self.stdin = None
                self.stdout = None
                self.stderr = None
                self.pid = 1
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setenv("HOME", "/users/alice")
        monkeypatch.setenv("HERMES_HOME", "/users/alice/.hermes/profiles/backend-worker")
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_smoke")
        monkeypatch.setenv(
            "HERMES_KANBAN_DB",
            "/users/alice/.hermes/kanban/boards/smoke/kanban.db",
        )

        client = cas.CodexAppServerClient(codex_bin="codex")
        client._closed = True

        cmd = captured["cmd"]
        assert cmd[:2] == ["codex", "app-server"]
        assert 'sandbox_mode="workspace-write"' in cmd
        assert (
            'sandbox_workspace_write.writable_roots=["/users/alice/.hermes/kanban/boards/smoke"]'
            in cmd
        )
        assert "sandbox_workspace_write.network_access=false" in cmd
        assert all("danger" not in part for part in cmd)
