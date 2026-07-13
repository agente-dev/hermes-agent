"""Codex app-server request/notification client.

Speaks the protocol shipped by the Desktop-pinned Codex 0.144.1 app-server.
Transport is newline-delimited JSON request/notification messages over stdio;
the wire deliberately omits a ``jsonrpc`` member. Spawn `codex app-server`, do
an `initialize` handshake, then drive `thread/start`/`thread/resume` plus
`turn/start` and consume streaming `item/*` notifications until
`turn/completed`.

This module is the wire-level speaker only. Higher-level concerns (event
projection into Hermes' display, approval bridging, transcript projection into
AIAgent.messages, plugin migration) live in sibling modules.

Status: optional opt-in runtime gated behind `model.openai_runtime ==
"codex_app_server"`. Hermes' default tool dispatch is unchanged when this
runtime is not selected.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# Match the exact app-server release bundled by Agente Desktop. Hermes can also
# launch a separately installed Codex CLI, but accepting an older wire version
# would make the account/read + parameterless account/logout contract
# conditional and reintroduce an untested auth path.
MIN_CODEX_VERSION = (0, 144, 1)

_PROVIDER_ENV_PREFIXES = (
    "AGENTE_AI_GATEWAY_",
    "AI_GATEWAY_",
    "ANTHROPIC_",
    "AZURE_",
    "CEREBRAS_",
    "COHERE_",
    "DEEPSEEK_",
    "FIREWORKS_",
    "GEMINI_",
    "GOOGLE_GENERATIVE_AI_",
    "GROQ_",
    "MISTRAL_",
    "OPENAI_",
    "OPENROUTER_",
    "TOGETHER_",
    "VERCEL_",
    "XAI_",
)


def _scrub_provider_environment(environment: dict[str, str]) -> dict[str, str]:
    """Remove inherited model-provider and shared-gateway configuration."""
    blocked_exact = {
        "AGENTE_API_KEY",
        "CODEX_API_KEY",
        "HERMES_API_KEY",
        "HERMES_INFERENCE_API_KEY",
        "HERMES_INFERENCE_BASE_URL",
    }
    return {
        key: value
        for key, value in environment.items()
        if not key.upper().startswith(_PROVIDER_ENV_PREFIXES)
        and key.upper() not in blocked_exact
    }


def _app_server_command(codex_bin: str, extra_args: list[str]) -> list[str]:
    """Build argv for the packaged standalone server or the Codex CLI."""
    if not isinstance(codex_bin, str) or not codex_bin.strip():
        raise ValueError("codex_bin must be a non-empty string")
    codex_bin = os.path.expanduser(codex_bin.strip())
    if os.path.isabs(codex_bin):
        if not os.path.isfile(codex_bin):
            raise FileNotFoundError(f"codex app-server binary not found: {codex_bin}")
        if not os.access(codex_bin, os.X_OK):
            raise PermissionError(f"codex app-server binary is not executable: {codex_bin}")
        return [codex_bin, "--session-source", "exec", *extra_args]
    if os.sep in codex_bin or (os.altsep and os.altsep in codex_bin):
        raise ValueError("codex_bin must be an absolute path or a command name")
    return [codex_bin, "app-server", *extra_args]


@dataclass
class CodexAppServerError(RuntimeError):
    """Raised on JSON-RPC errors from the app-server."""

    code: int
    message: str
    data: Optional[Any] = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"codex app-server error {self.code}: {self.message}"


@dataclass
class _Pending:
    queue: queue.Queue
    method: str
    sent_at: float = field(default_factory=time.time)


class CodexAppServerClient:
    """Minimal JSON-RPC 2.0 client for `codex app-server` over stdio.

    Threading model:
      - Spawning thread (caller) drives request/response pairs synchronously.
      - One reader thread parses stdout, dispatches replies to the right
        pending future, and routes notifications + server-initiated requests
        to bounded queues that the caller drains on their own cadence.
      - One reader thread captures stderr for diagnostics; codex emits
        tracing logs there at RUST_LOG-controlled levels.

    Intentionally NOT async. AIAgent.run_conversation() is synchronous and
    runs on the main thread; layering asyncio just to drive a stdio child
    creates surprising interrupt semantics. We use blocking queues with
    timeouts and rely on `turn/interrupt` for cancellation.
    """

    def __init__(
        self,
        codex_bin: str = "codex",
        codex_home: Optional[str] = None,
        extra_args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self._codex_bin = codex_bin
        spawn_env = _scrub_provider_environment(os.environ.copy())
        if env:
            spawn_env.update(_scrub_provider_environment(dict(env)))
        if codex_home:
            spawn_env["CODEX_HOME"] = codex_home

        app_server_args = list(extra_args or [])
        # Kanban workers must be able to write their handoff/status back to
        # the board DB, which lives outside the per-task workspace. Keep the
        # Codex sandbox on, but add the Kanban root as the only extra writable
        # root. Without this, codex-runtime workers finish their actual work
        # but crash/block when kanban_complete/kanban_block writes SQLite.
        if spawn_env.get("HERMES_KANBAN_TASK"):
            kanban_db = spawn_env.get("HERMES_KANBAN_DB")
            kanban_root = (
                os.path.dirname(kanban_db)
                if kanban_db
                else spawn_env.get(
                    "HERMES_KANBAN_ROOT",
                    os.path.join(
                        spawn_env.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
                        "kanban",
                    ),
                )
            )
            app_server_args.extend(
                [
                    "-c",
                    'sandbox_mode="workspace-write"',
                    "-c",
                    f'sandbox_workspace_write.writable_roots=["{kanban_root}"]',
                    "-c",
                    "sandbox_workspace_write.network_access=false",
                ]
            )

        cmd = _app_server_command(codex_bin, app_server_args)
        # Codex emits tracing to stderr; default WARN keeps it quiet for users.
        spawn_env.setdefault("RUST_LOG", "warn")

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=spawn_env,
        )
        self._next_id = 1
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._notifications: queue.Queue = queue.Queue()
        self._server_requests: queue.Queue = queue.Queue()
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()
        self._closed = False
        self._initialized = False

        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()

    # ---------- lifecycle ----------

    def initialize(
        self,
        client_name: str = "hermes",
        client_title: str = "Hermes Agent",
        client_version: str = "0.1",
        capabilities: Optional[dict] = None,
        timeout: float = 10.0,
    ) -> dict:
        """Send `initialize` + `initialized` handshake. Returns the server's
        InitializeResponse (userAgent, codexHome, platformFamily, platformOs)."""
        if self._initialized:
            raise RuntimeError("already initialized")
        params = {
            "clientInfo": {
                "name": client_name,
                "title": client_title,
                "version": client_version,
            },
            "capabilities": capabilities or {},
        }
        result = self.request("initialize", params, timeout=timeout)
        self.notify("initialized")
        self._initialized = True
        return result

    def account_read(
        self,
        *,
        refresh_token: bool = False,
        timeout: float = 30.0,
    ) -> dict:
        """Return the official app-server account snapshot.

        The result is the unmodified ``account/read`` response with
        ``account`` and ``requiresOpenaiAuth`` fields.  No Hermes auth store or
        token parser participates in this call.
        """
        self._require_initialized("account/read")
        if not isinstance(refresh_token, bool):
            raise TypeError("refresh_token must be a bool")
        return self.request(
            "account/read",
            {"refreshToken": refresh_token},
            timeout=timeout,
        )

    def account_login_start(
        self,
        *,
        device_code: bool = False,
        use_hosted_login_success_page: Optional[bool] = None,
        codex_streamlined_login: Optional[bool] = None,
        app_brand: Optional[str] = None,
        timeout: float = 30.0,
    ) -> dict:
        """Start Codex-managed ChatGPT login through the official method.

        Browser login returns ``authUrl`` + ``loginId``.  Device-code login
        returns ``verificationUrl`` + ``userCode`` + ``loginId``.  This helper
        intentionally does not expose the protocol's internal
        ``chatgptAuthTokens`` variant; clients must let Codex own credentials.
        """
        self._require_initialized("account/login/start")
        if not isinstance(device_code, bool):
            raise TypeError("device_code must be a bool")
        if app_brand is not None and app_brand not in {"codex", "chatgpt"}:
            raise ValueError("app_brand must be 'codex', 'chatgpt', or None")
        browser_options = (
            use_hosted_login_success_page,
            codex_streamlined_login,
            app_brand,
        )
        if device_code and any(option is not None for option in browser_options):
            raise ValueError("browser login options cannot be used with device_code=True")

        if device_code:
            params: dict[str, Any] = {"type": "chatgptDeviceCode"}
        else:
            params = {"type": "chatgpt"}
            if use_hosted_login_success_page is not None:
                if not isinstance(use_hosted_login_success_page, bool):
                    raise TypeError("use_hosted_login_success_page must be a bool or None")
                params["useHostedLoginSuccessPage"] = use_hosted_login_success_page
            if codex_streamlined_login is not None:
                if not isinstance(codex_streamlined_login, bool):
                    raise TypeError("codex_streamlined_login must be a bool or None")
                params["codexStreamlinedLogin"] = codex_streamlined_login
            if app_brand is not None:
                params["appBrand"] = app_brand
        return self.request("account/login/start", params, timeout=timeout)

    def account_logout(self, *, timeout: float = 30.0) -> dict:
        """Log out the account managed by the Codex app-server."""
        self._require_initialized("account/logout")
        return self.request("account/logout", timeout=timeout)

    def model_list(
        self,
        *,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
        include_hidden: Optional[bool] = None,
        timeout: float = 30.0,
    ) -> dict:
        """Return one page from the official account-aware model catalog."""
        self._require_initialized("model/list")
        if cursor is not None and not isinstance(cursor, str):
            raise TypeError("cursor must be a string or None")
        if limit is not None and (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 0
            or limit > 0xFFFFFFFF
        ):
            raise ValueError("limit must be a uint32 integer or None")
        if include_hidden is not None and not isinstance(include_hidden, bool):
            raise TypeError("include_hidden must be a bool or None")

        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        if include_hidden is not None:
            params["includeHidden"] = include_hidden
        return self.request("model/list", params, timeout=timeout)

    def close(self, timeout: float = 3.0) -> None:
        """Close stdin and wait for the subprocess to exit, escalating to kill."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                self._proc.kill()
                self._proc.wait(timeout=1.0)
            except Exception:
                pass

    def __enter__(self) -> "CodexAppServerClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- send/receive ----------

    def request(
        self,
        method: str,
        params: Optional[dict] = None,
        timeout: float = 30.0,
    ) -> dict:
        """Send a JSON-RPC request and block on the response. Returns `result`,
        raises CodexAppServerError on `error`."""
        rid = self._take_id()
        q: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[rid] = _Pending(queue=q, method=method)
        payload: dict[str, Any] = {"id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise TimeoutError(
                f"codex app-server method {method!r} timed out after {timeout}s"
            )
        if "error" in msg:
            err = msg["error"]
            raise CodexAppServerError(
                code=err.get("code", -1),
                message=err.get("message", ""),
                data=err.get("data"),
            )
        return msg.get("result", {})

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def respond(self, request_id: Any, result: dict) -> None:
        """Reply to a server-initiated request (e.g. approval prompts)."""
        self._send({"id": request_id, "result": result})

    def respond_error(
        self, request_id: Any, code: int, message: str, data: Optional[Any] = None
    ) -> None:
        """Reply to a server-initiated request with an error."""
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._send({"id": request_id, "error": err})

    def take_notification(self, timeout: float = 0.0) -> Optional[dict]:
        """Pop the next streaming notification, or return None on timeout.

        timeout=0.0 means non-blocking. Use small positive timeouts inside the
        AIAgent turn loop to interleave reads with interrupt checks."""
        try:
            if timeout <= 0:
                return self._notifications.get_nowait()
            return self._notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def take_server_request(self, timeout: float = 0.0) -> Optional[dict]:
        """Pop the next server-initiated request (e.g. exec/applyPatch approval)."""
        try:
            if timeout <= 0:
                return self._server_requests.get_nowait()
            return self._server_requests.get(timeout=timeout)
        except queue.Empty:
            return None

    # ---------- diagnostics ----------

    def stderr_tail(self, n: int = 20) -> list[str]:
        """Return last n lines of codex's stderr (for error reports)."""
        with self._stderr_lock:
            return list(self._stderr_lines[-n:])

    def is_alive(self) -> bool:
        return self._proc.poll() is None

    # ---------- internals ----------

    def _take_id(self) -> int:
        # JSON-RPC ids only need to be unique per-connection. A simple
        # monotonically increasing int is the common choice and matches what
        # codex's own clients use.
        rid = self._next_id
        self._next_id += 1
        return rid

    def _require_initialized(self, method: str) -> None:
        if not self._initialized:
            raise RuntimeError(
                f"codex app-server must be initialized before {method}"
            )

    def _send(self, obj: dict) -> None:
        if self._closed:
            raise RuntimeError("codex app-server client is closed")
        if self._proc.stdin is None:
            raise RuntimeError("codex app-server stdin not available")
        try:
            self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise RuntimeError(
                f"codex app-server stdin closed unexpectedly: {exc}"
            ) from exc

    def _read_stdout(self) -> None:
        if self._proc.stdout is None:
            return
        try:
            for line in iter(self._proc.stdout.readline, b""):
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON output is unexpected on stdout; tracing belongs
                    # on stderr. Surface it via stderr buffer for diagnostics.
                    with self._stderr_lock:
                        self._stderr_lines.append(
                            f"<non-json on stdout> {line[:200]!r}"
                        )
                    continue
                self._dispatch(msg)
        except Exception as exc:
            with self._stderr_lock:
                self._stderr_lines.append(f"<stdout reader error> {exc}")

    def _dispatch(self, msg: dict) -> None:
        # Reply (has id + result/error, no method)
        if "id" in msg and ("result" in msg or "error" in msg):
            with self._pending_lock:
                pending = self._pending.pop(msg["id"], None)
            if pending is not None:
                try:
                    pending.queue.put_nowait(msg)
                except queue.Full:  # pragma: no cover - defensive
                    pass
            return
        # Server-initiated request (has id + method)
        if "id" in msg and "method" in msg:
            self._server_requests.put(msg)
            return
        # Notification (no id)
        if "method" in msg:
            self._notifications.put(msg)

    def _read_stderr(self) -> None:
        if self._proc.stderr is None:
            return
        try:
            for line in iter(self._proc.stderr.readline, b""):
                if not line:
                    break
                with self._stderr_lock:
                    self._stderr_lines.append(
                        line.decode("utf-8", "replace").rstrip()
                    )
                    # Bound memory: keep last 500 lines.
                    if len(self._stderr_lines) > 500:
                        self._stderr_lines = self._stderr_lines[-500:]
        except Exception:  # pragma: no cover
            pass


def parse_codex_version(output: str) -> Optional[tuple[int, int, int]]:
    """Parse `codex --version` output. Returns (major, minor, patch) or None."""
    # Output format: "codex-cli 0.130.0" possibly followed by metadata.
    import re

    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def check_codex_binary(
    codex_bin: str = "codex", min_version: tuple[int, int, int] = MIN_CODEX_VERSION
) -> tuple[bool, str]:
    """Verify codex CLI is installed and meets minimum version.

    Returns (ok, message). Used by setup wizard and runtime startup."""
    try:
        proc = subprocess.run(
            [codex_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False, (
            f"codex CLI not found at {codex_bin!r}. Install with: "
            f"npm i -g @openai/codex"
        )
    except subprocess.TimeoutExpired:
        return False, "codex --version timed out"
    if proc.returncode != 0:
        return False, f"codex --version exited {proc.returncode}: {proc.stderr.strip()}"
    version = parse_codex_version(proc.stdout)
    if version is None:
        return False, f"could not parse codex version from: {proc.stdout!r}"
    if version < min_version:
        return False, (
            f"codex {'.'.join(map(str, version))} is older than required "
            f"{'.'.join(map(str, min_version))}. Run: npm i -g @openai/codex"
        )
    return True, ".".join(map(str, version))
