"""Unit tests for tools.skill_executor and POST /v1/agent/execute_skill.

Covers the Wave-A primitive contract:

  - validation (request shape, missing skill_id)
  - skill-not-found returns 404 with code=skill_not_found
  - input-schema violation returns 422 with code=schema_violation
  - output-schema violation returns 422 with code=schema_violation
  - runner timeout returns 504 with code=timeout
  - happy-path returns 200 with status=ok + outputs

The executor is the deterministic surface for the endpoint, so most
tests drive it directly; a smaller set exercise the HTTP wrapper to
prove the status-code mapping and JSON body shape.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from schemas.agent_execute import (
    AgentExecuteRequest,
    AgentExecuteResponse,
    ExecuteError,
)
from tools.skill_executor import execute_skill, register_default_runner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skill_record(name: str, *, inputs_schema=None, outputs_schema=None):
    fm = {"name": name}
    if inputs_schema is not None:
        fm["inputs_schema"] = inputs_schema
    if outputs_schema is not None:
        fm["outputs_schema"] = outputs_schema
    return {
        "name": name,
        "description": "test fixture",
        "path": f"/tmp/{name}/SKILL.md",
        "source": "local",
        "category": None,
        "frontmatter": fm,
    }


def _patch_skills(skill_records):
    return patch(
        "tools.skills_tool._find_all_skills",
        return_value=skill_records,
    )


def _make_adapter(api_key=None):
    extra = {}
    if api_key:
        extra["key"] = api_key
    return APIServerAdapter(PlatformConfig(enabled=True, extra=extra))


def _make_app(adapter):
    app = web.Application()
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/agent/execute_skill", adapter._handle_execute_skill)
    return app


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------


class TestRequestSchema:
    def test_minimal_request_uses_defaults(self):
        req = AgentExecuteRequest(skill_id="x")
        assert req.inputs == {}
        assert req.effective_timeout() == 30.0
        assert req.effective_context_budget() == 8_000

    def test_extra_keys_rejected(self):
        with pytest.raises(Exception):
            AgentExecuteRequest(skill_id="x", inputs={}, undeclared=True)

    def test_timeout_upper_bound(self):
        with pytest.raises(Exception):
            AgentExecuteRequest(skill_id="x", timeout_seconds=9999)


# ---------------------------------------------------------------------------
# Direct executor unit tests (in-process)
# ---------------------------------------------------------------------------


class TestExecutorDirect:
    @pytest.mark.asyncio
    async def test_skill_not_found(self):
        with _patch_skills([]):
            resp = await execute_skill(AgentExecuteRequest(skill_id="missing"))
        assert resp.status == "error"
        assert resp.errors[0].code == "skill_not_found"
        assert resp.errors[0].path == "skill_id"

    @pytest.mark.asyncio
    async def test_executor_unavailable_when_no_runner(self):
        register_default_runner(None)
        with _patch_skills([_skill_record("noop")]):
            resp = await execute_skill(AgentExecuteRequest(skill_id="noop"))
        assert resp.status == "error"
        assert resp.errors[0].code == "executor_unavailable"

    @pytest.mark.asyncio
    async def test_happy_path_no_schemas(self):
        async def runner(skill_id, inputs, budget, timeout):
            return {"echo": inputs}

        with _patch_skills([_skill_record("echo")]):
            resp = await execute_skill(
                AgentExecuteRequest(skill_id="echo", inputs={"hello": "world"}),
                runner=runner,
            )
        assert resp.status == "ok"
        assert resp.outputs == {"echo": {"hello": "world"}}
        assert resp.duration_seconds is not None and resp.duration_seconds >= 0

    @pytest.mark.asyncio
    async def test_input_schema_violation(self):
        schema = {
            "type": "object",
            "required": ["recipient"],
            "properties": {"recipient": {"type": "string"}},
            "additionalProperties": False,
        }
        async def runner(*_a, **_k):  # pragma: no cover - never reached
            return {}

        with _patch_skills([_skill_record("send", inputs_schema=schema)]):
            resp = await execute_skill(
                AgentExecuteRequest(skill_id="send", inputs={}),
                runner=runner,
            )
        assert resp.status == "error"
        assert resp.errors[0].code == "schema_violation"
        assert resp.errors[0].path.startswith("inputs")

    @pytest.mark.asyncio
    async def test_output_schema_violation(self):
        output_schema = {
            "type": "object",
            "required": ["sent_at"],
            "properties": {"sent_at": {"type": "string"}},
        }

        async def runner(*_a, **_k):
            return {"sent_at": 42}  # int, not string

        with _patch_skills(
            [_skill_record("send", outputs_schema=output_schema)]
        ):
            resp = await execute_skill(
                AgentExecuteRequest(skill_id="send", inputs={}),
                runner=runner,
            )
        assert resp.status == "error"
        assert resp.errors[0].code == "schema_violation"
        assert resp.errors[0].path.startswith("outputs")
        # outputs are surfaced even on failure so the UI can show what
        # the model actually returned.
        assert resp.outputs == {"sent_at": 42}

    @pytest.mark.asyncio
    async def test_timeout_returns_structured_error(self):
        async def slow_runner(*_a, **_k):
            await asyncio.sleep(5)
            return {}

        with _patch_skills([_skill_record("slow")]):
            resp = await execute_skill(
                AgentExecuteRequest(skill_id="slow", timeout_seconds=0.05),
                runner=slow_runner,
            )
        assert resp.status == "error"
        assert resp.errors[0].code == "timeout"

    @pytest.mark.asyncio
    async def test_runner_exception_is_translated(self):
        async def boom(*_a, **_k):
            raise RuntimeError("kaboom")

        with _patch_skills([_skill_record("boom")]):
            resp = await execute_skill(
                AgentExecuteRequest(skill_id="boom"),
                runner=boom,
            )
        assert resp.status == "error"
        assert resp.errors[0].code == "execution_failed"
        assert "kaboom" in resp.errors[0].message

    @pytest.mark.asyncio
    async def test_non_dict_output_rejected(self):
        async def runner(*_a, **_k):
            return "raw string"

        with _patch_skills([_skill_record("bad-shape")]):
            resp = await execute_skill(
                AgentExecuteRequest(skill_id="bad-shape"),
                runner=runner,
            )
        assert resp.status == "error"
        assert resp.errors[0].code == "execution_failed"


# ---------------------------------------------------------------------------
# HTTP wrapper — exercises /v1/agent/execute_skill end-to-end
# ---------------------------------------------------------------------------


class TestExecuteSkillEndpoint:
    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self):
        adapter = _make_adapter()
        app = _make_app(adapter)
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/v1/agent/execute_skill", data="not-json")
            assert r.status == 400
            body = await r.json()
            assert body["status"] == "error"
            assert body["errors"][0]["code"] == "invalid_request"

    @pytest.mark.asyncio
    async def test_missing_skill_id_returns_400(self):
        adapter = _make_adapter()
        app = _make_app(adapter)
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/v1/agent/execute_skill", json={})
            assert r.status == 400
            body = await r.json()
            assert body["errors"][0]["code"] == "invalid_request"

    @pytest.mark.asyncio
    async def test_skill_not_found_returns_404(self):
        adapter = _make_adapter()
        app = _make_app(adapter)
        with _patch_skills([]):
            async with TestClient(TestServer(app)) as client:
                r = await client.post(
                    "/v1/agent/execute_skill",
                    json={"skill_id": "ghost"},
                )
                assert r.status == 404
                body = await r.json()
                assert body["status"] == "error"
                assert body["errors"][0]["code"] == "skill_not_found"

    @pytest.mark.asyncio
    async def test_happy_path_returns_200(self):
        adapter = _make_adapter()
        app = _make_app(adapter)

        async def runner(skill_id, inputs, budget, timeout):
            return {"ok": True, "received": inputs}

        register_default_runner(runner)
        try:
            with _patch_skills([_skill_record("noop")]):
                async with TestClient(TestServer(app)) as client:
                    r = await client.post(
                        "/v1/agent/execute_skill",
                        json={"skill_id": "noop", "inputs": {"a": 1}},
                    )
                    assert r.status == 200
                    body = await r.json()
                    assert body["status"] == "ok"
                    assert body["outputs"] == {"ok": True, "received": {"a": 1}}
        finally:
            register_default_runner(None)

    @pytest.mark.asyncio
    async def test_schema_violation_maps_to_422(self):
        adapter = _make_adapter()
        app = _make_app(adapter)
        schema = {"type": "object", "required": ["x"]}

        async def runner(*_a, **_k):  # pragma: no cover
            return {}

        register_default_runner(runner)
        try:
            with _patch_skills(
                [_skill_record("strict", inputs_schema=schema)]
            ):
                async with TestClient(TestServer(app)) as client:
                    r = await client.post(
                        "/v1/agent/execute_skill",
                        json={"skill_id": "strict", "inputs": {}},
                    )
                    assert r.status == 422
                    body = await r.json()
                    assert body["errors"][0]["code"] == "schema_violation"
        finally:
            register_default_runner(None)

    @pytest.mark.asyncio
    async def test_auth_required_when_configured(self):
        adapter = _make_adapter(api_key="sk-test")
        app = _make_app(adapter)
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/v1/agent/execute_skill",
                json={"skill_id": "noop"},
            )
            assert r.status == 401
