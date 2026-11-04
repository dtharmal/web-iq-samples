# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from app import main as main_module
from app.capacity import Admission
from app.config import Settings
from app.main import app, public_error
from app.performance import PerformanceTrace
from app.protocol import MESSAGE


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main_module, "admission", Admission())
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: Settings(
        agent_name="test-agent", agent_version="1", webiq_key="TEST_SECRET_NEVER_EXPOSE",
    )))
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_local_health_does_not_disclose_credentials(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["webiq_configured"] is True
    assert "TEST_SECRET" not in response.text
    assert response.json()["recording"] is False
    assert response.headers["cache-control"] == "no-store"


def test_invalid_host_and_cross_origin_rejected(client):
    assert client.get("/api/health", headers={"host": "evil.example:8765"}).status_code == 400
    assert client.get("/api/health", headers={"origin": "https://evil.example"}).status_code == 403


@pytest.mark.parametrize("headers", [{}, {"origin": "https://evil.example"}])
def test_websocket_requires_exact_origin(client, headers):
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
        "/api/voice", headers={"host": "127.0.0.1:8765", **headers},
    ):
        pytest.fail("Untrusted socket connected")


@pytest.mark.parametrize("path", ["/.env", "/app/config.py", "/.foundry/agent.json", "/.logs/performance.jsonl", "/api/docs"])
def test_secret_and_source_files_not_served(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("message", [
    '{"type":"text","text":""}', '{"type":"text","text":"a","instructions":"override"}',
    '{"type":"mute","muted":"false"}', '{"type":"response.create"}',
    '{"type":"playback","turn":0,"items":[{"item_id":"a","content_index":0,"audio_end_ms":-1}]}',
])
def test_protocol_rejects_injection_and_invalid_values(message):
    with pytest.raises(ValidationError):
        MESSAGE.validate_json(message)


def test_public_error_never_returns_raw_exception():
    assert "SECRET" not in public_error(RuntimeError("SECRET")).message
    assert "HTTP 400" in public_error(ConnectionError("HTTP 400 SECRET")).message


ENDPOINT = "https://example-resource.services.ai.azure.com/api/projects/example-project"


def test_configuration_requires_valid_foundry_and_webiq_endpoints():
    Settings(project_endpoint=ENDPOINT).validate()
    Settings(project_endpoint=ENDPOINT, tenant_id="00000000-0000-0000-0000-000000000000").validate()
    for endpoint in ["", "http://example-resource.services.ai.azure.com/api/projects/example-project",
                     "https://evil.example/api/projects/example-project",
                     "https://example-resource.services.ai.azure.com/api/projects/example-project/extra"]:
        with pytest.raises(ValueError):
            Settings(project_endpoint=endpoint).validate()
    with pytest.raises(ValueError):
        Settings(project_endpoint=ENDPOINT, tenant_id="not-a-tenant").validate()
    with pytest.raises(ValueError):
        Settings(project_endpoint=ENDPOINT, webiq_endpoint="https://evil.example").validate()


def test_active_session_limit_is_an_explicit_error(client, monkeypatch):
    main_module.admission.acquire("local", 1)
    with client.websocket_connect("/api/voice", headers={
        "origin": "http://127.0.0.1:8765", "host": "127.0.0.1:8765",
    }) as ws:
        event = ws.receive_json()
        assert event["code"] == "session_busy"
        assert event["fatal"] is True


def test_health_exposes_active_count_and_logging_failures_without_payloads(client, monkeypatch):
    main_module.admission.acquire("local", 1)
    monkeypatch.setattr(main_module.performance, "status", lambda: {
        "enabled": True, "state": "warning", "dropped": 2, "error_code": "write_failed",
    })
    result = client.get("/api/health").json()
    assert result["active_sessions"] == 1
    assert result["performance_logging"]["dropped"] == 2
    assert result["performance_logging"]["error_code"] == "write_failed"


@pytest.mark.parametrize("metric", [-1, float("inf"), "SECRET"])
def test_client_timings_reject_invalid_duration_or_content(metric):
    with pytest.raises(ValidationError):
        MESSAGE.validate_python({
            "type": "client_timing", "turn": 1, "phase": "playback_started",
            "response_id": "r", "browser_submit_to_playback_ms": metric,
        })
    with pytest.raises(ValidationError):
        MESSAGE.validate_python({
            "type": "client_timing", "turn": 1, "phase": "audio_received",
            "response_id": "r", "transcript": "SECRET",
        })


@pytest.mark.parametrize("greeting_type", [None, "template", "llm_generated"])
async def test_independent_startup_work_overlaps_before_bridge_handoff(monkeypatch, greeting_type):
    mcp_started, lookup_started = asyncio.Event(), asyncio.Event()
    ready = []
    resource = object()
    greeting = SimpleNamespace(type=greeting_type) if greeting_type else None

    @asynccontextmanager
    async def connect(_settings, **_kwargs):
        mcp_started.set()
        await lookup_started.wait()
        ready.append("mcp")
        yield resource
        ready.append("mcp_closed")

    async def get(**_kwargs):
        lookup_started.set()
        await mcp_started.wait()
        ready.append("agent")
        return SimpleNamespace(versions=SimpleNamespace(latest=SimpleNamespace(
            version="3", definition=SimpleNamespace(kind="voice", store=False, greeting=greeting),
        )))

    monkeypatch.setattr(main_module.WebIQ, "connect", connect)
    connection = object()

    @asynccontextmanager
    async def native_connect(**kwargs):
        assert "agent" in ready
        yield connection

    client = SimpleNamespace(
        agents=SimpleNamespace(get=AsyncMock(side_effect=get)),
        beta=SimpleNamespace(voice_agents=SimpleNamespace(realtime=SimpleNamespace(connect=native_connect))),
    )
    async with asyncio.timeout(1), main_module.prepare_services(
        client, Settings(agent_version="3"), PerformanceTrace(),
    ) as (webiq, conn, greeting_mode):
        assert webiq is resource
        assert conn is connection
        assert greeting_mode == (greeting.type if greeting else None)
        assert set(ready) == {"mcp", "agent"}
    assert ready[-1] == "mcp_closed"
    client.agents.get.assert_awaited_once()


@pytest.mark.parametrize("kind,store,version,code", [
    ("prompt", False, "7", "agent_config"),
    ("voice", True, "7", "agent_config"),
    ("voice", None, "7", "agent_config"),
    ("voice", False, "6", "agent_version"),
])
async def test_startup_keeps_privacy_kind_and_latest_pin_guards(monkeypatch, kind, store, version, code):
    @asynccontextmanager
    async def connect(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(main_module.WebIQ, "connect", connect)
    client = SimpleNamespace(agents=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
        versions=SimpleNamespace(latest=SimpleNamespace(version=version, definition=SimpleNamespace(
            kind=kind, store=store, greeting={"type": "template"},
        ))),
    ))))
    with pytest.raises(ExceptionGroup) as error:
        async with main_module.prepare_services(client, Settings(agent_version="7"), PerformanceTrace()):
            pytest.fail("Invalid definitions must not open native voice.")
    assert public_error(error.value).code == code


async def test_startup_failure_cancels_lookup_without_opening_voice(monkeypatch):
    started, stopped = asyncio.Event(), asyncio.Event()

    async def get(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    @asynccontextmanager
    async def connect(_settings, **_kwargs):
        await started.wait()
        raise RuntimeError("TEST_ONLY_CONNECTION_FAILURE")
        yield  # pragma: no cover

    monkeypatch.setattr(main_module.WebIQ, "connect", connect)
    client = SimpleNamespace(agents=SimpleNamespace(get=AsyncMock(side_effect=get)))
    with pytest.raises(ExceptionGroup):
        async with asyncio.timeout(1), main_module.prepare_services(client, Settings(), PerformanceTrace()):
            pytest.fail("Voice cannot start with missing grounding")
    assert stopped.is_set()
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
