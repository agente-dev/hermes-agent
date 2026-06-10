import json
import logging
import os

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

CALL_PROFILE_SCHEMA = {
    "name": "call_profile",
    "description": (
        "Send a message to another Hermes profile's agent and return the "
        "response. The target profile must have its API server running "
        "(gateway started with API_SERVER_KEY set). Use this to delegate "
        "work to specialized profiles that own tools the calling agent "
        "doesn't have — e.g. ask the communication-agent profile to search "
        "Gmail instead of giving the chat agent Gmail tools directly. "
        "The target profile's own toolset, memory, and approval gates all "
        "fire normally."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "profile": {
                "type": "string",
                "description": (
                    "Name of the target Hermes profile to call "
                    "(e.g. 'communication-agent'). The profile must exist "
                    "and have its gateway running with API_SERVER_KEY set."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "The message or prompt to send to the target profile's "
                    "agent. This is forwarded as a single user message."
                ),
            },
        },
        "required": ["profile", "message"],
    },
}


def _get_profile_dir(name: str):
    """Resolve a profile name to its directory path using hermes_cli.profiles."""
    try:
        from hermes_cli.profiles import get_profile_dir
        return get_profile_dir(name)
    except Exception:
        return None


def _read_profile_api_config(profile_dir):
    """Read the API server port and key from a profile's configuration.

    Returns (port, api_key) tuple. Falls back to defaults when config is
    missing or unreadable.
    """
    port = 8642
    api_key = ""

    env_path = profile_dir / ".env"
    if env_path.is_file():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key == "API_SERVER_KEY" and value:
                        api_key = value
                    elif key == "API_SERVER_PORT" and value:
                        try:
                            port = int(value)
                        except ValueError:
                            pass
        except OSError:
            pass

    config_path = profile_dir / "config.yaml"
    if config_path.is_file():
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}
        if isinstance(cfg, dict):
            gateway_cfg = cfg.get("gateway", {})
            if isinstance(gateway_cfg, dict):
                if not api_key:
                    api_key = str(gateway_cfg.get("api_server_key", ""))
                if gateway_cfg.get("port"):
                    try:
                        port = int(gateway_cfg["port"])
                    except (ValueError, TypeError):
                        pass
            platforms = cfg.get("platforms", {})
            if isinstance(platforms, dict):
                api_cfg = platforms.get("api_server", {})
                if isinstance(api_cfg, dict):
                    if not api_key:
                        api_key = str(api_cfg.get("key", api_cfg.get("api_key", "")))
                    api_port = api_cfg.get("port")
                    if api_port is not None:
                        try:
                            port = int(api_port)
                        except (ValueError, TypeError):
                            pass

    if not api_key:
        api_key = os.environ.get("API_SERVER_KEY", "")

    return port, api_key


def call_profile(profile: str, message: str, task_id: str = None) -> str:
    """Call another Hermes profile's agent and return the response."""
    profile_dir = _get_profile_dir(profile)
    if profile_dir is None or not profile_dir.is_dir():
        return tool_error(f"Profile '{profile}' does not exist")

    port, api_key = _read_profile_api_config(profile_dir)
    if not api_key:
        return tool_error(
            f"Profile '{profile}' has no API_SERVER_KEY configured. "
            f"Set API_SERVER_KEY in {profile_dir}/.env or config.yaml "
            f"and restart the profile's gateway."
        )

    import urllib.request

    body = json.dumps({
        "model": "hermes",
        "messages": [{"role": "user", "content": message}],
        "stream": False,
    }).encode("utf-8")

    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:500]
        except Exception:
            detail = str(e)
        return tool_error(
            f"Profile '{profile}' gateway returned HTTP {e.code}: {detail}"
        )
    except urllib.error.URLError as e:
        return tool_error(
            f"Could not reach profile '{profile}' gateway at port {port}. "
            f"Is the profile's gateway running? ({e.reason})"
        )
    except Exception as e:
        return tool_error(
            f"Failed to call profile '{profile}': {e}"
        )

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = json.dumps(data, ensure_ascii=False)

    return tool_result({"profile": profile, "response": content})


def check_call_profile_available() -> bool:
    """Return True when at least one non-default profile exists."""
    try:
        from hermes_cli.profiles import list_profiles
        profiles = list_profiles()
        return any(not p.is_default for p in profiles)
    except Exception:
        return True


registry.register(
    name="call_profile",
    toolset="profiles",
    schema=CALL_PROFILE_SCHEMA,
    handler=lambda args, **kw: call_profile(
        profile=args.get("profile", ""),
        message=args.get("message", ""),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_call_profile_available,
    emoji="📞",
)
