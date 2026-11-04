# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import main
from app.agent_validation import AgentValidationCache, validate_agent
from app.config import Settings
from app.webiq import PublicError


@pytest.fixture
def settings():
    return Settings(agent_name="test-agent", agent_version="7")


@pytest.mark.parametrize("greeting", [None, "template", "llm_generated"])
def test_cache_retains_valid_no_greeting_and_supported_modes(settings, greeting):
    cache = AgentValidationCache()
    cache.store(settings, greeting, time.monotonic())
    assert cache.lookup(settings).greeting_mode == greeting
    assert cache.status(settings)["state"] == "fresh"
    cache.invalidate("agent_config")
    assert cache.lookup(settings) is None
    assert cache.status(settings)["error_code"] == "agent_config"


@pytest.mark.parametrize("change", [
    {"mode": "hosted"}, {"agent_version": ""}, {"agent_version": "8"},
    {"agent_name": "other"}, {"project_endpoint": "other"}, {"tenant_id": "other"},
    {"local_agent_validation_enabled": False},
])
def test_config_identity_changes_and_hosted_never_reuse(settings, change):
    cache = AgentValidationCache()
    cache.store(settings, None, time.monotonic())
    assert cache.lookup(replace(settings, **change)) is None


def test_expiry_counts_from_remote_check_start_not_response(settings, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.agent_validation.time.monotonic", lambda: clock[0])
    cache = AgentValidationCache()
    clock[0] = 125
    cache.store(settings, None, 100)
    assert cache.lookup(settings) is not None
    clock[0] = 130
    assert cache.lookup(settings) is None
    assert cache.status(settings)["state"] == "unavailable"
    cache.store(settings, None, 90)
    assert cache.lookup(settings) is None


@pytest.mark.parametrize("kind,store,version,greeting", [
    ("prompt", False, "7", None), ("voice", True, "7", None),
    ("voice", None, "7", None), ("voice", False, "8", None),
    ("voice", False, "7", "unsupported"),
])
async def test_validation_rejects_all_unsafe_definitions(settings, kind, store, version, greeting):
    client = SimpleNamespace(agents=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
        versions=SimpleNamespace(latest=SimpleNamespace(
            version=version, definition=SimpleNamespace(
                kind=kind, store=store, greeting=SimpleNamespace(type=greeting) if greeting else None,
            ),
        )),
    ))))
    with pytest.raises(PublicError):
        await validate_agent(client, settings)


async def test_background_refresh_success_failure_and_cancellation(settings, monkeypatch, caplog):
    cache = AgentValidationCache()
    credentials_closed = []

    @asynccontextmanager
    async def resource(*args, **kwargs):
        try:
            yield object()
        finally:
            credentials_closed.append(True)

    calls = 0
    sleeps = asyncio.Queue()
    resume = asyncio.Event()

    async def validate(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PublicError("agent_version", "SECRET_DEFINITION")
        return "template"

    async def sleep(seconds):
        assert seconds == 20
        sleeps.put_nowait(True)
        await resume.wait()
        resume.clear()

    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr(main, "credential_for", resource)
    monkeypatch.setattr(main, "AIProjectClient", resource)
    monkeypatch.setattr(main, "validate_agent", validate)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    task = asyncio.create_task(main.refresh_agent_validation(cache))
    try:
        await asyncio.wait_for(sleeps.get(), 1)
        assert cache.lookup(settings).greeting_mode == "template"
        resume.set()
        await asyncio.wait_for(sleeps.get(), 1)
        assert cache.lookup(settings) is None and cache.error_code == "agent_version"
        assert "SECRET_DEFINITION" not in caplog.text
        resume.set()
        await asyncio.wait_for(sleeps.get(), 1)
        assert cache.lookup(settings) is not None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cache.entry is None
    assert len(credentials_closed) == 6


async def test_background_cancel_during_remote_check_closes_resources(settings, monkeypatch):
    cache = AgentValidationCache()
    started = asyncio.Event()
    closed = []

    @asynccontextmanager
    async def resource(*args, **kwargs):
        try:
            yield object()
        finally:
            closed.append(True)

    async def validate(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr(main, "credential_for", resource)
    monkeypatch.setattr(main, "AIProjectClient", resource)
    monkeypatch.setattr(main, "validate_agent", validate)
    task = asyncio.create_task(main.refresh_agent_validation(cache))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(closed) == 2 and cache.entry is None


@pytest.mark.parametrize("change", [
    {"mode": "hosted"}, {"agent_version": ""}, {"agent_name": ""},
    {"local_agent_validation_enabled": False},
])
async def test_background_never_contacts_azure_for_hosted_or_unpinned_config(settings, monkeypatch, change):
    settings = replace(settings, **change)
    cache = AgentValidationCache()
    credential = AsyncMock()
    sleeping = asyncio.Event()

    async def sleep(seconds):
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr(main, "credential_for", credential)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    task = asyncio.create_task(main.refresh_agent_validation(cache))
    await asyncio.wait_for(sleeping.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    credential.assert_not_called()
    assert cache.entry is None


async def test_lifespan_owns_refresh_task_and_discards_cache_on_shutdown(settings, monkeypatch, tmp_path):
    started = asyncio.Event()
    stopped = asyncio.Event()
    caches = []

    async def refresh(cache):
        caches.append(cache)
        cache.store(settings, "template", time.monotonic())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(main, "refresh_agent_validation", refresh)
    before = asyncio.all_tasks()
    async with main.lifespan(main.app):
        await asyncio.wait_for(started.wait(), 1)
        assert main.agent_validation.lookup(settings) is not None
    assert stopped.is_set() and main.agent_validation is None
    assert caches[0].entry is None
    assert not (asyncio.all_tasks() - before - {asyncio.current_task()})
