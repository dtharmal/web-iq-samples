# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import preflight
from app.config import Settings
from app.webiq import mcp_error


@pytest.fixture
def ready(monkeypatch, tmp_path):
    (tmp_path / "dist" / "assets").mkdir(parents=True)
    for path in ("dist/index.html", "dist/audio-worklet.js", "dist/assets/index.js"):
        (tmp_path / path).write_text("test")
    settings = Settings(
        project_endpoint="https://example.services.ai.azure.com/api/projects/example",
        webiq_key="TEST_SECRET", agent_name="example-agent", agent_version="1",
    )
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr(preflight.shutil, "which", lambda name: name)
    monkeypatch.setattr(preflight.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="v22.12.0\n"))
    checks = preflight.local_checks
    monkeypatch.setattr(preflight, "local_checks", lambda settings: checks(settings, tmp_path))
    return settings, tmp_path


async def test_offline_preflight_never_contacts_services(ready, monkeypatch, capsys):
    online = AsyncMock()
    monkeypatch.setattr(preflight, "online_check", online)
    assert await preflight.run() == 0
    online.assert_not_awaited()
    output = capsys.readouterr().out
    assert "Local checks only" in output
    assert "TEST_SECRET" not in output


@pytest.mark.parametrize("failure", ["node", "npm", "az", "old_node", "key", "placeholder", "build"])
async def test_missing_prerequisites_fail_before_any_online_call(ready, monkeypatch, failure, capsys):
    _settings, root = ready
    if failure in {"node", "npm", "az"}:
        monkeypatch.setattr(preflight.shutil, "which", lambda name: None if name == failure else name)
    elif failure == "old_node":
        monkeypatch.setattr(preflight.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="v22.11.0"))
    elif failure in {"key", "placeholder"}:
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: Settings(
            webiq_key="" if failure == "key" else "<your-web-iq-api-key>",
        )))
    else:
        (root / "dist" / "audio-worklet.js").unlink()
    online = AsyncMock()
    monkeypatch.setattr(preflight, "online_check", online)
    assert await preflight.run(True) == 1
    online.assert_not_awaited()
    assert "FAIL" in capsys.readouterr().out


async def test_configuration_error_never_echoes_configured_values(monkeypatch, capsys):
    def invalid(_cls):
        raise ValueError("TEST_SECRET from malformed configuration")

    monkeypatch.setattr(Settings, "load", classmethod(invalid))
    assert await preflight.run() == 1
    output = capsys.readouterr().out
    assert "FAIL configuration" in output
    assert "TEST_SECRET" not in output


async def test_online_uses_only_existing_startup_checks_and_closes_resources(ready, monkeypatch, capsys):
    entered, closed = [], []

    @asynccontextmanager
    async def credential(_settings):
        entered.append("credential")
        try:
            yield object()
        finally:
            closed.append("credential")

    @asynccontextmanager
    async def client(*args, **kwargs):
        entered.append("client")
        try:
            yield object()
        finally:
            closed.append("client")

    @asynccontextmanager
    async def services(_client, _settings, _trace):
        entered.append("services")
        try:
            yield object()
        finally:
            closed.append("services")

    monkeypatch.setattr(preflight, "credential_for", credential)
    monkeypatch.setattr(preflight, "AIProjectClient", client)
    monkeypatch.setattr(preflight, "prepare_services", services)
    assert await preflight.run(True) == 0
    assert entered == ["credential", "client", "services"]
    assert closed == ["services", "client", "credential"]
    assert "No agent created, search submitted, or voice session opened" in capsys.readouterr().out


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_online_errors_are_actionable_and_redacted(ready, monkeypatch, capsys, status):
    request = httpx.Request("POST", "https://api.microsoft.ai/v3/mcp")
    failure = httpx.HTTPStatusError(
        "TEST_SECRET", request=request, response=httpx.Response(status, request=request),
    )
    monkeypatch.setattr(preflight, "online_check", AsyncMock(side_effect=mcp_error(failure)))
    assert await preflight.run(True) == 1
    output = capsys.readouterr().out
    assert f"webiq_http_{status}" in output
    assert "TEST_SECRET" not in output


async def test_platform_without_private_file_flags_requires_logging_opt_out(ready, monkeypatch, capsys):
    settings, _root = ready
    monkeypatch.delattr(preflight.os, "O_NOFOLLOW", raising=False)
    assert await preflight.run() == 1
    assert "PERF_LOG_ENABLED=0" in capsys.readouterr().out
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: Settings(
        webiq_key=settings.webiq_key, perf_log_enabled=False,
    )))
    assert await preflight.run() == 0


async def test_agent_creation_is_explicit_and_never_automatic(ready, monkeypatch, capsys):
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: Settings(webiq_key="TEST_SECRET")))
    assert await preflight.run() == 0
    assert "NEXT Create" in capsys.readouterr().out
    assert await preflight.run(True) == 1
    assert "FAIL agent configuration" in capsys.readouterr().out
