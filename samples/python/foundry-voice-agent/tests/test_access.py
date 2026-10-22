# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import hashlib
import re
import secrets
import stat
from contextlib import ExitStack, asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main
from app.access import COOKIE, LOGIN_COOKIE, Access, Login, make_verifier, parse_verifier
from app.capacity import Admission
from app.config import Settings
from app.setup_access import generate

TEST_PASSWORD = "test-only-not-a-real-passphrase"
ORIGIN = "https://demo.example"
ENDPOINT = "https://example-resource.services.ai.azure.com/api/projects/example-project"


@pytest.fixture(scope="module")
def verifier():
    return make_verifier(TEST_PASSWORD)


@pytest.fixture
def hosted(monkeypatch, verifier):
    settings = Settings(mode="hosted", public_origin=ORIGIN, auth_verifier=verifier, project_endpoint=ENDPOINT,
                        agent_name="test-agent", agent_version="3", webiq_key="TEST_ONLY",
                        max_sessions=10, perf_log_enabled=False)
    settings.validate()
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr(main, "access", None)
    monkeypatch.setattr(main, "access_verifier", "")
    monkeypatch.setattr(main, "admission", Admission())
    with TestClient(main.app, base_url=ORIGIN) as client:
        yield client


def login(client, passphrase=TEST_PASSWORD, **kwargs):
    page = client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    return client.post("/api/login", data={"csrf": csrf, "passphrase": passphrase},
                       headers={"origin": ORIGIN}, follow_redirects=False, **kwargs)


@pytest.mark.parametrize("path", ["/api/health", "/audio-worklet.js", "/assets/anything.js", "/.env",
                                 "/.private-demo/access.json", "/.logs/performance.jsonl"])
def test_anonymous_cannot_access_any_app_or_diagnostics(hosted, path):
    response = hosted.get(path)
    assert response.status_code == 401
    assert "test-agent" not in response.text
    assert "TEST_ONLY" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_private_login_cookie_and_csrf_logout(hosted):
    assert hosted.get("/", follow_redirects=False).headers["location"] == "/login"
    response = login(hosted)
    assert response.status_code == 303
    cookie = response.headers.get_list("set-cookie")[0]
    assert all(flag in cookie for flag in ["Secure", "HttpOnly", "SameSite=strict", "Max-Age=3600", "Path=/"])
    assert TEST_PASSWORD not in cookie and "scrypt" not in cookie
    assert "Domain=" not in cookie
    assert hosted.get("/api/health").status_code == 200
    csrf = hosted.get("/api/health").json()["access"]["csrf"]
    assert hosted.post("/api/logout", headers={"origin": ORIGIN}).status_code == 403
    assert hosted.post("/api/logout", headers={"origin": "https://evil.example", "x-csrf-token": csrf}).status_code == 403
    assert hosted.post("/api/logout", headers={"origin": ORIGIN, "x-csrf-token": csrf}).status_code == 200
    assert hosted.get("/api/health").status_code == 401


def test_wrong_passphrase_throttling_and_no_forwarded_header_bypass(hosted):
    for _ in range(10):
        assert login(hosted, "wrong").status_code == 401
    response = login(hosted)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert len(main.access.logins) == 0
    assert len(main.admission.active) == 0


def test_invalid_login_input_origin_body_and_csrf_rejected(hosted):
    hosted.get("/login")
    csrf = hosted.cookies.get(LOGIN_COOKIE)
    data = {"csrf": csrf, "passphrase": TEST_PASSWORD}
    assert hosted.post("/api/login", data=data).status_code == 403
    assert hosted.post("/api/login", data=data, headers={"origin": "https://evil.example"}).status_code == 403
    assert hosted.post("/api/login", json=data, headers={"origin": ORIGIN}).status_code == 400
    assert hosted.post("/api/login", data={**data, "csrf": "bad"},
                       headers={"origin": ORIGIN}).status_code == 403
    assert hosted.post("/api/login", data={**data, "passphrase": "x" * 4096},
                       headers={"origin": ORIGIN}).status_code == 413
    assert not main.access.attempts


def test_websocket_rejects_anonymous_and_cross_origin_before_upstream(hosted, monkeypatch):
    def forbidden(_settings):
        pytest.fail("Anonymous WebSocket reached Azure")
    monkeypatch.setattr(main, "credential_for", forbidden)
    with pytest.raises(WebSocketDisconnect), hosted.websocket_connect(
        "wss://demo.example/api/voice", headers={"origin": ORIGIN},
    ):
        pytest.fail("Anonymous websocket admitted")
    login(hosted)
    with pytest.raises(WebSocketDisconnect), hosted.websocket_connect(
        "wss://demo.example/api/voice", headers={"origin": "https://evil.example"},
    ):
        pytest.fail("Cross-origin websocket admitted")
    assert not main.admission.active


def test_ten_isolated_startups_capacity_and_cancel_release(hosted, monkeypatch):
    @asynccontextmanager
    async def fake_resource(*_args, **_kwargs):
        yield object()

    @asynccontextmanager
    async def pending_startup(*_args, **_kwargs):
        await asyncio.Event().wait()
        yield  # pragma: no cover

    monkeypatch.setattr(main, "credential_for", fake_resource)
    monkeypatch.setattr(main, "AIProjectClient", fake_resource)
    monkeypatch.setattr(main, "prepare_services", pending_startup)
    tokens = []
    for _ in range(11):
        token = secrets.token_urlsafe(32)
        subject = hashlib.sha256(token.encode()).hexdigest()
        main.access.logins[subject] = Login(subject, main.access.clock() + 3600)
        tokens.append(token)
    with ExitStack() as stack:
        sockets = []
        for token in tokens[:10]:
            ws = stack.enter_context(hosted.websocket_connect(
                "wss://demo.example/api/voice", headers={"origin": ORIGIN, "cookie": f"{COOKIE}={token}"},
            ))
            assert ws.receive_json() == {"type": "state", "state": "connecting"}
            sockets.append(ws)
        assert len(main.admission.active) == 10
        with hosted.websocket_connect(
            "wss://demo.example/api/voice", headers={"origin": ORIGIN, "cookie": f"{COOKIE}={tokens[10]}"},
        ) as rejected:
            assert rejected.receive_json()["code"] == "session_busy"
        assert len(main.admission.active) == 10
        sockets[0].send_json({"type": "end"})
        with pytest.raises(WebSocketDisconnect):
            sockets[0].receive_json()
        assert len(main.admission.active) == 9
        with hosted.websocket_connect(
            "wss://demo.example/api/voice", headers={"origin": ORIGIN, "cookie": f"{COOKIE}={tokens[10]}"},
        ) as replacement:
            assert replacement.receive_json()["state"] == "connecting"
            assert len(main.admission.active) == 10
            replacement.send_json({"type": "end"})
            with pytest.raises(WebSocketDisconnect):
                replacement.receive_json()
        for ws in sockets[1:]:
            ws.send_json({"type": "end"})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    assert not main.admission.active


def test_setup_failure_releases_slot_and_sanitizes_error(hosted, monkeypatch):
    def failing(_settings):
        raise RuntimeError("TEST_SECRET_ERROR")
    monkeypatch.setattr(main, "credential_for", failing)
    login(hosted)
    with hosted.websocket_connect("wss://demo.example/api/voice", headers={"origin": ORIGIN}) as ws:
        assert ws.receive_json()["state"] == "connecting"
        error = ws.receive_json()
        assert error["code"] == "session_error"
        assert "TEST_SECRET" not in str(error)
    assert not main.admission.active


async def test_expiry_revocation_and_bounded_auth_state(verifier):
    clock = [1.0]
    gate = Access(verifier, clock=lambda: clock[0])
    result, token = await gate.authenticate(TEST_PASSWORD, "peer")
    assert result == "ok"
    entry = gate.lookup(token)
    assert entry is not None
    clock[0] = 3601
    assert gate.lookup(token) is None
    assert entry.revoked.is_set()
    assert not gate.logins and not gate.attempts and not gate.peers
    for i in range(30):
        assert (await gate.authenticate("wrong", str(i)))[0] == "invalid"
    assert (await gate.authenticate(TEST_PASSWORD, "unseen"))[0] == "limited"
    assert len(gate.peers) == 30


def test_admission_hard_cap_duplicate_rate_and_idempotent_release():
    clock = [1.0]
    pool = Admission(clock=lambda: clock[0])
    for i in range(10):
        assert pool.acquire(str(i), 10)
    assert not pool.acquire("10", 10)
    assert not pool.acquire("0", 10)
    pool.release("0")
    pool.release("0")
    assert pool.acquire("10", 10)
    for i in range(11, 20):
        pool.release("10")
        assert pool.acquire("10", 10)
    pool.release("10")
    assert not pool.acquire("10", 10)
    clock[0] += 10
    assert pool.acquire("10", 10)


def test_hosted_config_fails_closed_and_never_reads_dotenv(monkeypatch, verifier):
    base = {"mode": "hosted", "agent_name": "test", "agent_version": "3", "project_endpoint": ENDPOINT,
            "webiq_key": "test", "auth_verifier": verifier}
    for origin in ["", "http://demo.example", "https://user@demo.example", "https://demo.example/",
                   "https://demo.example?key=x", "https://localhost"]:
        with pytest.raises(ValueError):
            Settings(**base, public_origin=origin).validate()
    with pytest.raises(ValueError):
        Settings(project_endpoint=ENDPOINT, max_sessions=11).validate()
    monkeypatch.setenv("APP_MODE", "hosted")
    monkeypatch.setenv("PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("DEMO_AUTH_VERIFIER", verifier)
    monkeypatch.setenv("WEBIQ_API_KEY", "TEST_ONLY")
    monkeypatch.setenv("FOUNDRY_VOICE_AGENT_NAME", "test")
    monkeypatch.setenv("FOUNDRY_VOICE_AGENT_VERSION", "3")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENDPOINT)
    monkeypatch.setattr("app.config.dotenv_values", lambda *_: pytest.fail("Hosted mode read .env"))
    settings = Settings.load()
    assert settings.max_sessions == 10
    assert "scrypt" not in repr(settings)


def test_private_setup_exclusive_permissions_no_stdout(tmp_path, capsys):
    directory = tmp_path / "private"
    generate(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for file in directory.iterdir():
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    parse_verifier((directory / "verifier").read_text().strip())
    with pytest.raises(FileExistsError):
        generate(directory)
    assert capsys.readouterr().out == ""


def test_credential_selection_never_uses_cli_in_hosted(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "ManagedIdentityCredential", lambda **kw: calls.append(("mi", kw)))
    monkeypatch.setattr(main, "AzureCliCredential", lambda **kw: calls.append(("cli", kw)))
    main.credential_for(Settings(mode="hosted"))
    main.credential_for(Settings())
    assert calls[0] == ("mi", {"client_id": None})
    assert calls[1] == ("cli", {"tenant_id": None, "process_timeout": 15})
