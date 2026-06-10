"""Tests for tools/call_profile_tool.py."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from tools.call_profile_tool import (
    _get_profile_dir,
    _read_profile_api_config,
    call_profile,
    check_call_profile_available,
)


class TestGetProfileDir:
    def test_resolves_known_profile(self, tmp_path, monkeypatch):
        profiles_root = tmp_path / "profiles"
        profiles_root.mkdir()
        comms_dir = profiles_root / "communication-agent"
        comms_dir.mkdir()

        monkeypatch.setattr(
            "hermes_cli.profiles._get_default_hermes_home",
            lambda: tmp_path,
        )

        result = _get_profile_dir("communication-agent")
        assert result == comms_dir

    def test_returns_none_for_import_error(self, monkeypatch):
        import builtins
        original_import = builtins.__import__

        def block_import(name, *args, **kwargs):
            if "hermes_cli.profiles" in name:
                raise ImportError("blocked")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=block_import):
            result = _get_profile_dir("communication-agent")
            assert result is None


class TestReadProfileApiConfig:
    def test_reads_from_env_file(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("API_SERVER_KEY=sk-test-key-123\nAPI_SERVER_PORT=9123\n")

        port, key = _read_profile_api_config(tmp_path)
        assert key == "sk-test-key-123"
        assert port == 9123

    def test_falls_back_to_config_yaml(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "gateway:\n  port: 9001\n  api_server_key: sk-config-key\n"
        )

        port, key = _read_profile_api_config(tmp_path)
        assert key == "sk-config-key"
        assert port == 9001

    def test_defaults_when_no_config(self, tmp_path):
        port, key = _read_profile_api_config(tmp_path)
        assert port == 8642
        assert key == ""

    def test_env_file_overrides_config_yaml(self, tmp_path):
        (tmp_path / ".env").write_text("API_SERVER_KEY=sk-env-key\n")
        (tmp_path / "config.yaml").write_text("gateway:\n  api_server_key: sk-yml-key\n")

        port, key = _read_profile_api_config(tmp_path)
        assert key == "sk-env-key"

    def test_respects_comment_lines_in_env(self, tmp_path):
        (tmp_path / ".env").write_text(
            "# API_SERVER_KEY=sk-old-key\nAPI_SERVER_KEY=sk-current-key\n"
        )

        port, key = _read_profile_api_config(tmp_path)
        assert key == "sk-current-key"

    def test_parses_platforms_api_server_section(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "platforms:\n"
            "  api_server:\n"
            "    key: sk-platform-key\n"
            "    port: 8888\n"
        )

        port, key = _read_profile_api_config(tmp_path)
        assert key == "sk-platform-key"
        assert port == 8888

    def test_invalid_port_falls_back_to_default(self, tmp_path):
        (tmp_path / ".env").write_text("API_SERVER_PORT=notanumber\n")

        port, key = _read_profile_api_config(tmp_path)
        assert port == 8642


class TestCallProfile:
    def test_unknown_profile_returns_error(self):
        with patch(
            "tools.call_profile_tool._get_profile_dir",
            return_value=None,
        ):
            result = json.loads(call_profile("nosuch", "hello"))
            assert "does not exist" in result["error"]

    def test_missing_api_key_returns_error(self, tmp_path):
        comms_dir = tmp_path / "communication-agent"
        comms_dir.mkdir()

        with patch(
            "tools.call_profile_tool._get_profile_dir",
            return_value=comms_dir,
        ), patch(
            "tools.call_profile_tool._read_profile_api_config",
            return_value=(8642, ""),
        ):
            result = json.loads(call_profile("communication-agent", "hello"))
            assert "no api_server_key" in result["error"].lower()

    def test_successful_call(self, tmp_path):
        comms_dir = tmp_path / "communication-agent"
        comms_dir.mkdir()

        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            resp.read.return_value = json.dumps({
                "choices": [{"message": {"content": "You have 3 unread emails."}}]
            }).encode("utf-8")
            return resp

        with patch(
            "tools.call_profile_tool._get_profile_dir",
            return_value=comms_dir,
        ), patch(
            "tools.call_profile_tool._read_profile_api_config",
            return_value=(8642, "sk-test"),
        ), patch(
            "urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            result = json.loads(call_profile("communication-agent", "find my emails"))
            assert result["profile"] == "communication-agent"
            assert "3 unread emails" in result["response"]

    def test_http_error(self, tmp_path):
        from urllib.error import HTTPError

        comms_dir = tmp_path / "communication-agent"
        comms_dir.mkdir()

        http_err = HTTPError(
            "http://127.0.0.1:8642/v1/chat/completions",
            401,
            "Unauthorized",
            {},
            MagicMock(read=MagicMock(return_value=b'{"error":"bad key"}')),
        )

        with patch(
            "tools.call_profile_tool._get_profile_dir",
            return_value=comms_dir,
        ), patch(
            "tools.call_profile_tool._read_profile_api_config",
            return_value=(8642, "sk-bad"),
        ), patch(
            "urllib.request.urlopen",
            side_effect=http_err,
        ):
            result = json.loads(call_profile("communication-agent", "hello"))
            assert "401" in result["error"]

    def test_connection_refused(self, tmp_path):
        from urllib.error import URLError

        comms_dir = tmp_path / "communication-agent"
        comms_dir.mkdir()

        url_err = URLError("Connection refused")

        with patch(
            "tools.call_profile_tool._get_profile_dir",
            return_value=comms_dir,
        ), patch(
            "tools.call_profile_tool._read_profile_api_config",
            return_value=(8642, "sk-test"),
        ), patch(
            "urllib.request.urlopen",
            side_effect=url_err,
        ):
            result = json.loads(call_profile("communication-agent", "hello"))
            assert "Could not reach" in result["error"]
            assert "Connection refused" in result["error"]


class TestCheckCallProfileAvailable:
    def test_available_with_extra_profiles(self):
        from hermes_cli.profiles import ProfileInfo

        fake_profiles = [
            ProfileInfo(
                name="default",
                path=Path("/tmp"),
                is_default=True,
                gateway_running=False,
            ),
            ProfileInfo(
                name="communication-agent",
                path=Path("/tmp/profiles/communication-agent"),
                is_default=False,
                gateway_running=True,
            ),
        ]

        with patch(
            "hermes_cli.profiles.list_profiles",
            return_value=fake_profiles,
        ):
            assert check_call_profile_available() is True

    def test_unavailable_with_only_default(self):
        from hermes_cli.profiles import ProfileInfo

        fake_profiles = [
            ProfileInfo(
                name="default",
                path=Path("/tmp"),
                is_default=True,
                gateway_running=False,
            ),
        ]

        with patch(
            "hermes_cli.profiles.list_profiles",
            return_value=fake_profiles,
        ):
            assert check_call_profile_available() is False

    def test_returns_true_on_import_error(self):
        with patch(
            "hermes_cli.profiles.list_profiles",
            side_effect=ImportError,
        ):
            assert check_call_profile_available() is True
