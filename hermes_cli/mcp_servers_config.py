"""Shared helpers for Hermes MCP server configuration."""

from __future__ import annotations

import base64
from typing import Any, Dict


def normalize_mcp_servers_config(raw_servers: Any) -> Dict[str, dict]:
    """Return canonical name-keyed ``mcp_servers`` config.

    The config file schema is a mapping:

        mcp_servers:
          server-name:
            url: ...

    A few legacy embedders wrote a list of entries with a ``name`` field. Keep
    that shape as a compatibility input, but always return the canonical mapping
    so runtime callers do not crash on ``.items()``.
    """
    if not raw_servers:
        return {}

    if isinstance(raw_servers, dict):
        return {
            str(name): server_cfg
            for name, server_cfg in raw_servers.items()
            if isinstance(server_cfg, dict)
        }

    if not isinstance(raw_servers, list):
        return {}

    normalized: Dict[str, dict] = {}
    for entry in raw_servers:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue

        server_cfg = {key: value for key, value in entry.items() if key != "name"}

        protocol = server_cfg.pop("protocol", None)
        if protocol and "transport" not in server_cfg:
            server_cfg["transport"] = protocol

        tools_cfg = server_cfg.get("tools")
        if isinstance(tools_cfg, (list, tuple, set)):
            server_cfg["tools"] = {"include": [str(tool) for tool in tools_cfg]}

        auth_cfg = server_cfg.get("auth")
        if isinstance(auth_cfg, dict):
            auth_type = str(auth_cfg.get("type", "")).strip().lower()
            if auth_type == "basic":
                username = str(auth_cfg.get("username", ""))
                password = str(auth_cfg.get("password", ""))
                token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
                headers = server_cfg.get("headers")
                if not isinstance(headers, dict):
                    headers = {}
                if not any(str(key).lower() == "authorization" for key in headers):
                    headers["Authorization"] = f"Basic {token}"
                server_cfg["headers"] = headers
                server_cfg.pop("auth", None)
            elif auth_type == "oauth":
                server_cfg["auth"] = "oauth"
            else:
                server_cfg.pop("auth", None)

        normalized[str(name)] = server_cfg

    return normalized
