# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import hmac
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from azure.ai.projects.aio import AIProjectClient
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.access import COOKIE, LOGIN_COOKIE, SESSION_SECONDS, Access
from app.bridge import Bridge
from app.capacity import Admission
from app.config import ROOT, Settings
from app.performance import PerformanceLogger, PerformanceTrace
from app.webiq import PublicError, WebIQ

log = logging.getLogger("webiq_demo")
# SDK wire logs can contain search text, headers or transcripts; never enable them in this demo.
for name in ("azure", "httpx", "httpcore", "mcp"):
    logging.getLogger(name).setLevel(logging.CRITICAL)

admission = Admission()
access: Access | None = None
access_verifier = ""
performance = PerformanceLogger(ROOT / ".logs" / "performance.jsonl", enabled=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global performance
    settings = Settings.load()
    get_access(settings)
    log_directory = Path("/tmp/webiq-demo-performance") if settings.hosted else ROOT / ".logs"
    performance = PerformanceLogger(log_directory / "performance.jsonl", enabled=settings.perf_log_enabled)
    await asyncio.to_thread(performance.start)
    try:
        yield
    finally:
        if access:
            access.revoke_all()
        await asyncio.to_thread(performance.close)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def allowed_host(host: str, settings: Settings) -> bool:
    return host in settings.hosts


def get_access(settings: Settings) -> Access | None:
    global access, access_verifier
    if not settings.hosted:
        return None
    if access is None or not hmac.compare_digest(access_verifier, settings.auth_verifier):
        if access:
            access.revoke_all()
        access = Access(settings.auth_verifier)
        access_verifier = settings.auth_verifier
    return access


def credential_for(settings: Settings):
    if settings.hosted:
        return ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
    return AzureCliCredential(tenant_id=settings.tenant_id or None, process_timeout=15)


@app.middleware("http")
async def boundary(request: Request, call_next):
    settings = Settings.load()
    if not allowed_host(request.headers.get("host", ""), settings):
        response = JSONResponse({"error": "Invalid host"}, status_code=400)
    elif (request.headers.get("origin") and request.headers["origin"] not in settings.origins
          or settings.hosted and request.method not in {"GET", "HEAD"}
          and request.headers.get("origin") not in settings.origins):
        response = JSONResponse({"error": "Invalid origin"}, status_code=403)
    else:
        gate = get_access(settings)
        request.state.login = gate.lookup(request.cookies.get(COOKIE)) if gate else None
        if gate and not request.state.login and request.url.path not in {"/login", "/api/login"}:
            response = (RedirectResponse("/login", status_code=303) if request.url.path == "/"
                        else JSONResponse({"error": "Sign in to use this demo."}, status_code=401))
        else:
            response = await call_next(request)
    response.headers.update({
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        ),
    })
    if settings.hosted:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@app.get("/login")
async def login_page():
    if not Settings.load().hosted:
        return RedirectResponse("/", status_code=303)
    token = secrets.token_urlsafe(32)
    template = (ROOT / "app" / "login.html").read_text()
    response = HTMLResponse(template.replace("<!--CSRF-->", token))
    response.set_cookie(LOGIN_COOKIE, token, max_age=300, secure=True, httponly=True, samesite="strict")
    return response


@app.post("/api/login")
async def login(request: Request):
    gate = get_access(Settings.load())
    if gate is None:
        return JSONResponse({"error": "Login is unavailable in local mode."}, status_code=404)
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        return JSONResponse({"error": "Invalid login form."}, status_code=400)
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(body) + len(chunk) > 2048:
                    return JSONResponse({"error": "Login form too large."}, status_code=413)
                body.extend(chunk)
        values = parse_qs(body.decode("utf-8"), max_num_fields=2, strict_parsing=True)
    except (ValueError, UnicodeError, TimeoutError):
        return JSONResponse({"error": "Invalid login form."}, status_code=400)
    csrf = values.get("csrf", [])
    password = values.get("passphrase", [])
    cookie = request.cookies.get(LOGIN_COOKIE, "")
    if (len(csrf) != 1 or len(password) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", csrf[0])
            or not hmac.compare_digest(csrf[0], cookie) or not 1 <= len(password[0]) <= 256):
        return JSONResponse({"error": "Login form expired or invalid. Reload the login page."}, status_code=403)
    result, token = await gate.authenticate(password[0], request.client.host if request.client else "unknown")
    if result != "ok":
        return JSONResponse(
            {"error": "Too many login attempts or sessions. Wait a minute and try again."
             if result == "limited" else "Passphrase not accepted."},
            status_code=429 if result == "limited" else 401,
            headers={"Retry-After": "60"} if result == "limited" else {},
        )
    gate.revoke(request.cookies.get(COOKIE))
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite="strict")
    response.delete_cookie(LOGIN_COOKIE, secure=True, httponly=True, samesite="strict")
    return response


@app.post("/api/logout")
async def logout(request: Request):
    gate = get_access(Settings.load())
    login = request.state.login
    token = request.headers.get("x-csrf-token", "")
    if (gate is None or not login or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
            or not hmac.compare_digest(token, login.csrf)):
        return JSONResponse({"error": "Invalid logout request."}, status_code=403)
    gate.revoke(request.cookies.get(COOKIE))
    response = JSONResponse({"status": "signed_out"})
    response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict")
    return response


@app.get("/api/health")
async def health(request: Request):
    settings = Settings.load()
    return {
        "status": "ok", "agent_configured": bool(settings.agent_name),
        "webiq_configured": bool(settings.webiq_key),
        "agent": settings.agent_name or None, "version": settings.agent_version or None,
        "session_seconds": settings.session_seconds, "recording": False,
        "grounding": "WebIQ MCP / news", "native_voice": True,
        "active_sessions": len(admission.active), "max_sessions": settings.max_sessions,
        "performance_logging": performance.status(),
        "access": {"hosted": settings.hosted, "csrf": request.state.login.csrf if request.state.login else None},
    }


@app.websocket("/api/voice")
async def voice(ws: WebSocket):
    settings = Settings.load()
    if (
        not allowed_host(ws.headers.get("host", ""), settings)
        or ws.headers.get("origin") not in settings.origins
        or ws.client is None or not settings.hosted and ws.client.host not in {"127.0.0.1", "::1", "testclient"}
    ):
        await ws.close(code=1008)
        return
    gate = get_access(settings)
    login = gate.lookup(ws.cookies.get(COOKIE)) if gate else None
    if gate and not login:
        await ws.close(code=1008)
        return
    subject = login.subject if login else "local"
    if not admission.acquire(subject, settings.max_sessions):
        await ws.accept()
        await ws.send_json({
            "type": "error", "code": "session_busy", "fatal": True,
            "message": (
                f"The demo is at capacity ({settings.max_sessions} conversations), this login already has "
                "a conversation, or starts are temporarily rate-limited. End your other session or retry shortly."
            ),
        })
        await ws.close(code=1013)
        return
    trace = PerformanceTrace(performance)
    trace.mark("session_open")
    outcome = "ended"
    bridge = None
    watchers: list[asyncio.Task] = []
    try:
        await ws.accept()
        owner = asyncio.current_task()
        assert owner is not None
        startup_watcher = asyncio.create_task(cancel_on_startup_disconnect(ws, owner))
        watchers.append(startup_watcher)
        if login:
            async def revoked():
                await login.revoked.wait()
                owner.cancel()
            watchers.append(asyncio.create_task(revoked()))
        if not settings.agent_name:
            raise PublicError("agent_not_configured", "Create the native voice agent using the README setup command.")
        await ws.send_json({"type": "state", "state": "connecting"})
        async with (
            asyncio.timeout(min(settings.session_seconds, login.expires - time.monotonic())
                            if login else settings.session_seconds),
            credential_for(settings) as credential,
            AIProjectClient(
                settings.project_endpoint, credential=credential, allow_preview=True,
                retry_total=0, connection_timeout=15, read_timeout=20,
            ) as client,
            prepare_services(client, settings, trace) as (webiq, greeting_mode),
        ):
            manager = client.beta.voice_agents.realtime.connect(
                agent_name=settings.agent_name, extra_query={"store": "false"},
                heartbeat=20, max_msg_size=512_000,
            )
            with trace.span("voice_connect"):
                async with asyncio.timeout(25):
                    conn = await manager.__aenter__()
            try:
                startup_watcher.cancel()
                await asyncio.gather(startup_watcher, return_exceptions=True)
                bridge = Bridge(ws, conn, webiq, trace=trace, greeting_mode=greeting_mode)
                await bridge.run()
            finally:
                await manager.__aexit__(None, None, None)
    except (WebSocketDisconnect, asyncio.CancelledError):
        outcome = "disconnected"
    except Exception as exc:  # noqa: BLE001 -- final boundary reports a sanitized fatal error
        # Boundary handler intentionally never logs exception text or request content.
        error = public_error(exc, hosted=settings.hosted)
        outcome = "failed"
        trace.mark("error", turn=bridge.turn if bridge else 0, code=error.code, outcome=outcome)
        log.warning("Session stopped: %s (%s)", error.code, type(exc).__name__)
        try:
            await ws.send_json({"type": "error", "code": error.code, "message": error.message, "fatal": True})
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
    finally:
        admission.release(subject)
        for watcher in watchers:
            watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
        trace.mark("session_end", outcome=outcome, duration_ms=(time.perf_counter() - trace.started) * 1000)
        try:
            await ws.close()
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass


async def cancel_on_startup_disconnect(ws: WebSocket, owner: asyncio.Task) -> None:
    # Before native readiness, no input is valid. A cancel/disconnect releases scarce slots immediately.
    try:
        await ws.receive()
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    owner.cancel()


@asynccontextmanager
async def prepare_services(client: AIProjectClient, settings: Settings, trace: PerformanceTrace):
    async def verify_agent():
        with trace.span("agent_lookup"):
            async with asyncio.timeout(25):
                agent = await client.agents.get(agent_name=settings.agent_name)
                version = agent.versions.latest
                if version.definition.kind != "voice" or version.definition.store is not False:
                    raise PublicError("agent_config", "The agent must be native kind=voice with store=False.")
                if settings.agent_version and version.version != settings.agent_version:
                    raise PublicError("agent_version", "The active agent version changed. Review it before reconnecting.")
                greeting = getattr(version.definition, "greeting", None)
                if greeting is not None and greeting.type not in {"template", "llm_generated"}:
                    raise PublicError("agent_config", "The agent's startup greeting mode is not supported.")
                return greeting.type if greeting is not None else None

    lookup = asyncio.create_task(verify_agent())
    try:
        # These independent startup requests overlap. MCP scopes stay in their owning task.
        async with WebIQ.connect(settings, trace=trace) as webiq:
            greeting_mode = await lookup
            yield webiq, greeting_mode
    finally:
        lookup.cancel()
        await asyncio.gather(lookup, return_exceptions=True)


def public_error(exc: Exception, *, hosted=False) -> PublicError:
    if isinstance(exc, BaseExceptionGroup):
        return public_error(exc.exceptions[0], hosted=hosted)
    if isinstance(exc, PublicError):
        return exc
    if isinstance(exc, ClientAuthenticationError):
        return PublicError("azure_auth", "Hosted Foundry identity is unavailable. Contact the demo owner."
                           if hosted else "Azure login expired or is unavailable. Run the README az login command.")
    if isinstance(exc, HttpResponseError):
        return PublicError("azure_http", f"Foundry rejected the request (HTTP {exc.status_code}). Check access.")
    if isinstance(exc, TimeoutError):
        return PublicError("session_timeout", "Session or connection time limit reached. Start a new session.")
    if isinstance(exc, ConnectionError):
        match = re.search(r"(?:HTTP |status[=: ]+|: )(400|401|403|404|429|5\d\d)\b", str(exc))
        status = f" (HTTP {match[1]})" if match else ""
        return PublicError("azure_connection", f"Native Foundry voice connection failed{status}. Check auth and preview access.")
    return PublicError("session_error", "The voice session failed. Check the server error code and reconnect.")


DIST = ROOT / "dist"
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/")
async def index():
    if not (DIST / "index.html").exists():
        return JSONResponse({"error": "Build the browser app first: npm install && npm run build"}, status_code=503)
    return FileResponse(DIST / "index.html")


@app.get("/audio-worklet.js")
async def worklet():
    return FileResponse(DIST / "audio-worklet.js", media_type="text/javascript")


def main():
    import uvicorn
    settings = Settings.load()
    uvicorn.run(app, host="0.0.0.0" if settings.hosted else "127.0.0.1", port=settings.port,
                ws_max_size=8192, ws_max_queue=16, workers=1, proxy_headers=False,
                access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
