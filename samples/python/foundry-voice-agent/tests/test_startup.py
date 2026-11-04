# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest

from app import main
from app.agent_validation import AgentValidationCache
from app.bridge import Bridge
from app.capacity import Admission
from app.config import Settings
from app.performance import PerformanceTrace
from app.webiq import PublicError


class Connection:
    def __init__(self):
        self.events = asyncio.Queue()
        self.input_audio_buffer = SimpleNamespace(append=AsyncMock())

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.events.get()


class Startup:
    def __init__(self, monkeypatch, *, greeting=None):
        self.allow_agent = asyncio.Event()
        self.allow_mcp = asyncio.Event()
        self.allow_voice = asyncio.Event()
        self.agent_started = asyncio.Event()
        self.mcp_started = asyncio.Event()
        self.voice_started = asyncio.Event()
        self.voice_opened = asyncio.Event()
        self.closed = set()
        self.owners = {}
        self.fail = None
        self.kind, self.store, self.version = "voice", False, "7"
        self.conn = Connection()
        self.events = self.conn.events
        self.webiq = object()

        async def get(**kwargs):
            self.agent_started.set()
            try:
                await self.allow_agent.wait()
                if self.fail == "agent":
                    raise TimeoutError
                return SimpleNamespace(versions=SimpleNamespace(latest=SimpleNamespace(
                    version=self.version, definition=SimpleNamespace(
                        kind=self.kind, store=self.store,
                        greeting=SimpleNamespace(type=greeting) if greeting else None,
                    ),
                )))
            finally:
                self.closed.add("lookup")

        @asynccontextmanager
        async def mcp(*args, **kwargs):
            self.owners["mcp"] = asyncio.current_task()
            self.mcp_started.set()
            try:
                # Exercise the same-task/LIFO cancel-scope requirement of the MCP SDK.
                with anyio.CancelScope():
                    await self.agent_started.wait()
                    await self.allow_mcp.wait()
                    if self.fail == "mcp":
                        raise PublicError("webiq_timeout", "Synthetic MCP timeout")
                    yield self.webiq
            finally:
                assert asyncio.current_task() is self.owners["mcp"]
                self.closed.add("mcp")

        @asynccontextmanager
        async def native(**kwargs):
            assert self.allow_agent.is_set()
            assert self.kind == "voice" and self.store is False and self.version == "7"
            assert kwargs == {
                "agent_name": "test-agent", "extra_query": {"store": "false"},
                "heartbeat": 20, "max_msg_size": 512_000,
            }
            self.owners["voice"] = asyncio.current_task()
            self.voice_started.set()
            try:
                await self.allow_voice.wait()
                if self.fail == "voice":
                    raise TimeoutError
                self.voice_opened.set()
                yield self.conn
            finally:
                assert asyncio.current_task() is self.owners["voice"]
                self.closed.add("voice")

        self.client = SimpleNamespace(
            agents=SimpleNamespace(get=AsyncMock(side_effect=get)),
            beta=SimpleNamespace(voice_agents=SimpleNamespace(realtime=SimpleNamespace(connect=native))),
        )
        monkeypatch.setattr(main.WebIQ, "connect", mcp)
        self.settings = Settings(agent_name="test-agent", agent_version="7")

    async def opened(self):
        await asyncio.gather(self.agent_started.wait(), self.mcp_started.wait())

    def release(self):
        self.allow_agent.set()
        self.allow_mcp.set()
        self.allow_voice.set()

    @asynccontextmanager
    async def services(self):
        async with asyncio.timeout(1), main.prepare_services(
            self.client, self.settings, PerformanceTrace(),
        ) as services:
            yield services


@pytest.mark.parametrize("greeting", [None, "template", "llm_generated"])
async def test_connect_overlaps_slow_mcp_but_never_precedes_agent_validation(monkeypatch, greeting):
    startup = Startup(monkeypatch, greeting=greeting)
    handed_off = asyncio.Event()
    release = asyncio.Event()

    async def use():
        async with startup.services() as (webiq, conn, mode):
            assert webiq is startup.webiq and conn is startup.conn and mode == greeting
            handed_off.set()
            await release.wait()

    task = asyncio.create_task(use())
    await startup.opened()
    assert not startup.voice_started.is_set()
    startup.allow_agent.set()
    startup.allow_voice.set()
    await startup.voice_opened.wait()
    assert not startup.allow_mcp.is_set() and not handed_off.is_set()
    # Early native events remain ordered and unread until the complete handoff.
    for event in ("session.updated", "response.created", "response.output_audio.delta", "response.done"):
        startup.events.put_nowait({"type": event})
    startup.allow_mcp.set()
    await handed_off.wait()
    assert [startup.events.get_nowait()["type"] for _ in range(4)] == [
        "session.updated", "response.created", "response.output_audio.delta", "response.done",
    ]
    assert startup.owners["mcp"] is task and startup.owners["voice"] is not task
    release.set()
    await task
    assert startup.closed == {"lookup", "mcp", "voice"}


@pytest.mark.parametrize("field,value,code", [
    ("kind", "prompt", "agent_config"), ("store", True, "agent_config"),
    ("store", None, "agent_config"), ("version", "6", "agent_version"),
])
async def test_agent_rejection_immediately_cancels_blocked_mcp_without_voice(monkeypatch, field, value, code):
    startup = Startup(monkeypatch)
    setattr(startup, field, value)
    startup.allow_agent.set()
    with pytest.raises(ExceptionGroup) as error:
        async with startup.services():
            pytest.fail("Invalid agent admitted")
    assert main.public_error(error.value).code == code
    assert not startup.voice_started.is_set()
    assert startup.closed == {"lookup", "mcp"}


@pytest.mark.parametrize("greeting", [None, "template", "llm_generated"])
async def test_fresh_cache_skips_remote_lookup_and_preserves_greeting_and_cleanup(monkeypatch, greeting):
    startup = Startup(monkeypatch)
    cache = AgentValidationCache()
    cache.store(startup.settings, greeting, main.time.monotonic())
    monkeypatch.setattr(main, "agent_validation", cache)
    startup.release()
    # MCP test helper waits for lookup start; cache-hit path deliberately never calls it.
    startup.agent_started.set()
    async with startup.services() as (_, conn, mode):
        assert conn is startup.conn and mode == greeting
        startup.client.agents.get.assert_not_awaited()
    assert startup.closed == {"mcp", "voice"}


@pytest.mark.parametrize("state", ["expired", "mismatch", "failed", "hosted"])
async def test_unusable_cache_falls_back_to_real_validation_before_voice(monkeypatch, state):
    startup = Startup(monkeypatch)
    cache = AgentValidationCache()
    cache.store(startup.settings, "template", main.time.monotonic() - (30 if state == "expired" else 0))
    if state == "mismatch":
        startup.settings = Settings(agent_name="test-agent", agent_version="8")
    elif state == "failed":
        cache.invalidate("azure_auth")
    elif state == "hosted":
        startup.settings = Settings(mode="hosted", agent_name="test-agent", agent_version="7")
    monkeypatch.setattr(main, "agent_validation", cache)
    startup.store = True
    startup.allow_agent.set()
    with pytest.raises(ExceptionGroup):
        async with startup.services():
            pytest.fail("Unsafe agent admitted")
    startup.client.agents.get.assert_awaited_once()
    assert not startup.voice_started.is_set()
    assert cache.entry is None


@pytest.mark.parametrize("failure", ["agent", "mcp", "voice"])
async def test_failure_or_timeout_cancels_sibling_startup(monkeypatch, failure):
    startup = Startup(monkeypatch)
    startup.fail = failure
    startup.allow_agent.set()
    if failure == "mcp":
        startup.allow_mcp.set()
    elif failure == "voice":
        startup.allow_voice.set()
    with pytest.raises(ExceptionGroup):
        async with startup.services():
            pytest.fail("Failed startup admitted")
    assert {"lookup", "mcp"} <= startup.closed
    if startup.voice_started.is_set():
        assert "voice" in startup.closed


@pytest.mark.parametrize("phase", ["lookup", "connecting", "mcp_pending", "voice_pending", "ready"])
async def test_cancellation_closes_all_owned_scopes_and_leaves_no_children(monkeypatch, phase):
    startup = Startup(monkeypatch)
    handoff = asyncio.Event()
    before = asyncio.all_tasks()

    async def use():
        async with startup.services():
            handoff.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(use())
    await startup.opened()
    if phase != "lookup":
        startup.allow_agent.set()
        await startup.voice_started.wait()
    if phase in {"mcp_pending", "ready"}:
        startup.allow_voice.set()
        await startup.voice_opened.wait()
    if phase in {"voice_pending", "ready"}:
        startup.allow_mcp.set()
    if phase == "ready":
        await handoff.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert {"lookup", "mcp"} <= startup.closed
    if startup.voice_started.is_set():
        assert "voice" in startup.closed
    assert not (asyncio.all_tasks() - before - {asyncio.current_task()})


async def test_outer_deadline_cleans_up_both_connections(monkeypatch):
    startup = Startup(monkeypatch)
    startup.allow_agent.set()
    startup.allow_voice.set()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(.02), main.prepare_services(
            startup.client, startup.settings, PerformanceTrace(),
        ):
            pytest.fail("MCP is not ready")
    assert startup.closed == {"lookup", "mcp", "voice"}


@pytest.mark.parametrize("phase", ["lookup", "voice"])
async def test_real_startup_deadlines_cancel_blocked_sibling_and_close_resources(monkeypatch, phase):
    startup = Startup(monkeypatch)
    if phase == "voice":
        startup.allow_agent.set()
    timeout = asyncio.timeout
    monkeypatch.setattr(main.asyncio, "timeout", lambda seconds: timeout(.01 if seconds == 25 else seconds))
    with pytest.raises(ExceptionGroup) as error:
        async with startup.services():
            pytest.fail("Incomplete startup admitted")
    assert main.public_error(error.value).code == "session_timeout"
    assert {"lookup", "mcp"} <= startup.closed
    if phase == "voice":
        assert "voice" in startup.closed


async def test_unsupported_greeting_rejected_before_native_connection(monkeypatch):
    startup = Startup(monkeypatch, greeting="unsupported")
    startup.allow_agent.set()
    with pytest.raises(ExceptionGroup) as error:
        async with startup.services():
            pytest.fail("Unknown greeting admitted")
    assert main.public_error(error.value).code == "agent_config"
    assert not startup.voice_started.is_set()
    assert startup.closed == {"lookup", "mcp"}


async def test_effective_ready_ack_and_greeting_are_consumed_after_full_handoff(monkeypatch):
    from app.agent import definition

    startup = Startup(monkeypatch, greeting="template")
    ws = SimpleNamespace(send_json=AsyncMock())
    consumed = asyncio.Event()

    async def use():
        async with startup.services() as (webiq, conn, mode):
            bridge = Bridge(ws, conn, webiq, greeting_mode=mode)
            assert not bridge.ready and bridge.pending == (0, "greeting")
            await bridge.client_event(b"\0\0")
            conn.input_audio_buffer.append.assert_not_awaited()
            reader = asyncio.create_task(bridge.read_service())
            try:
                for expected in ("session.updated", "response.created"):
                    kind, event = await bridge.queue.get()
                    assert kind == "server" and event["type"] == expected
                    await bridge.server_event(event)
                assert bridge.ready and bridge.responses["early-greeting"] == (0, "greeting")
                assert ws.send_json.call_args.args[0]["type"] == "ready"
                await bridge.client_event(b"\0\0")
                conn.input_audio_buffer.append.assert_awaited_once()
                consumed.set()
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    task = asyncio.create_task(use())
    await startup.opened()
    startup.allow_agent.set()
    startup.allow_voice.set()
    await startup.voice_opened.wait()
    startup.events.put_nowait({
        "type": "session.updated", "session": definition("gpt-realtime-1.5").as_dict(),
    })
    startup.events.put_nowait({"type": "response.created", "response": {"id": "early-greeting"}})
    assert not consumed.is_set()
    ws.send_json.assert_not_awaited()
    startup.conn.input_audio_buffer.append.assert_not_awaited()
    startup.allow_mcp.set()
    await task
    assert consumed.is_set()
    assert startup.closed == {"lookup", "mcp", "voice"}


@pytest.mark.parametrize("action", ["disconnect", "end", "logout", "mcp_failure", "voice_failure"])
async def test_endpoint_cleanup_releases_admission_during_overlapped_startup(monkeypatch, action):
    startup = Startup(monkeypatch)
    startup.allow_agent.set()
    startup.allow_voice.set()
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: startup.settings))
    monkeypatch.setattr(main, "admission", Admission())

    @asynccontextmanager
    async def credential(_settings):
        yield object()

    @asynccontextmanager
    async def client(*args, **kwargs):
        yield startup.client

    login = SimpleNamespace(subject="test-login", revoked=asyncio.Event(), expires=float("inf"))
    monkeypatch.setattr(main, "credential_for", credential)
    monkeypatch.setattr(main, "AIProjectClient", client)
    monkeypatch.setattr(main, "get_access", lambda _: SimpleNamespace(lookup=lambda _: login))
    inbound = asyncio.Queue()
    ws = SimpleNamespace(
        headers={"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765"},
        client=SimpleNamespace(host="127.0.0.1"), cookies={},
        accept=AsyncMock(), send_json=AsyncMock(), close=AsyncMock(),
        receive=inbound.get,
    )
    if action == "voice_failure":
        startup.fail = "voice"
    task = asyncio.create_task(main.voice(ws))
    await startup.voice_started.wait()
    if action != "voice_failure":
        await startup.voice_opened.wait()
    if action == "logout":
        login.revoked.set()
    elif action == "mcp_failure":
        startup.fail = "mcp"
        startup.allow_mcp.set()
    elif action in {"end", "disconnect"}:
        inbound.put_nowait({"type": "websocket.disconnect"} if action == "disconnect" else {
            "type": "websocket.receive", "text": '{"type":"end"}',
        })
    await asyncio.wait_for(task, 1)
    assert not main.admission.active
    assert startup.closed == {"lookup", "mcp", "voice"}
    ws.close.assert_awaited_once()
