# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import ipaddress
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

import httpx

from app.activity import ToolActivity
from app.config import Settings
from app.performance import PerformanceTrace


class PublicError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def safe_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        return None
    try:
        url = urlsplit(value)
        host = url.hostname
        if (
            url.scheme not in {"https", "http"} or not host or url.username or url.password
            or url.port not in {None, 80, 443} or "\\" in value
            or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
            or "." not in host
        ):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            # Reject numeric / non-canonical IPv4 encodings that browsers normalize.
            if host.replace(".", "").isdigit() or host.startswith("0x"):
                return None
        return value
    except ValueError:
        return None


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    url: str
    domain: str
    content: str
    publisher: str | None = None
    published_at: str | None = None
    updated_at: str | None = None
    crawled_at: str | None = None

    def public(self) -> dict:
        return {key: value for key, value in asdict(self).items() if key != "content"}


def optional_text(value: object, limit: int = 200) -> str | None:
    return value[:limit].strip() if isinstance(value, str) and value.strip() else None


def parse_sources(payload: object, turn: int) -> list[Source]:
    if (not isinstance(payload, dict) or "error" in payload or "errorCode" in payload
            or not isinstance(payload.get("results"), list)):
        raise PublicError("webiq_schema", "Web IQ Auto returned an unexpected response format.")
    sources: list[Source] = []
    seen: set[str] = set()
    for entry in payload["results"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("evidence"), dict):
            raise PublicError("webiq_schema", "Web IQ Auto returned an invalid result.")
        evidence = entry["evidence"]
        if evidence.get("type") != "text":
            continue
        if not isinstance(evidence.get("content"), str) or not isinstance(entry.get("citations"), list):
            raise PublicError("webiq_schema", "Web IQ Auto returned invalid text evidence.")
        content = optional_text(evidence["content"], 3000)
        if not content:
            continue
        for citation in entry["citations"]:
            if not isinstance(citation, dict):
                raise PublicError("webiq_schema", "Web IQ Auto returned an invalid citation.")
            url = safe_url(citation.get("url"))
            title = optional_text(citation.get("title"), 200)
            if not url or not title or url in seen or citation.get("isAdult") is True:
                continue
            seen.add(url)
            sources.append(Source(
                id=f"t{turn}-s{len(sources) + 1}", title=title, url=url,
                domain=urlsplit(url).hostname or "", content=content,
                publisher=optional_text(citation.get("contentSource")),
                updated_at=optional_text(citation.get("lastUpdatedAt")),
                crawled_at=optional_text(citation.get("crawledAt")),
            ))
            if len(sources) == 5:
                return sources
    return sources


class LimitedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream):
        self.stream = stream

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > 512_000:
                raise PublicError("webiq_size", "Web IQ exceeded the demo response-size limit.")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class LimitedTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request):
        response = await super().handle_async_request(request)
        response.stream = LimitedStream(response.stream)
        return response


def webiq_error(exc: Exception) -> PublicError:
    if isinstance(exc, BaseExceptionGroup):
        return webiq_error(exc.exceptions[0])
    if isinstance(exc, PublicError):
        return exc
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return PublicError("webiq_timeout", "Web IQ Auto timed out. Please try again.")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        message = {
            401: "Web IQ Auto rejected the API key. Update WEBIQ_API_KEY privately.",
            403: "Web IQ AppID is not enabled for Auto. Contact your Web IQ provider.",
            429: "Web IQ Auto hit a rate or quota limit. Check retry guidance before trying again.",
        }.get(status, f"Web IQ Auto failed (HTTP {status}). No alternative search was used.")
        return PublicError(f"webiq_http_{status}", message)
    return PublicError("webiq_auto", f"Web IQ Auto failed ({type(exc).__name__}). Please reconnect.")


class WebIQ:
    def __init__(self, http: httpx.AsyncClient, endpoint: str, trace: PerformanceTrace | None = None):
        self.http = http
        self.endpoint = endpoint
        self.trace = trace

    @classmethod
    @asynccontextmanager
    async def connect(cls, settings: Settings, *, trace: PerformanceTrace | None = None):
        if not settings.webiq_key:
            raise PublicError("webiq_not_configured", "Add WEBIQ_API_KEY to the private .env file.")
        yielded = False
        trace = trace or PerformanceTrace()
        trace.mark("stage_start", stage="webiq_setup")
        try:
            async with httpx.AsyncClient(
                headers={"x-apikey": settings.webiq_key}, timeout=20, follow_redirects=False,
                transport=LimitedTransport(retries=0),
            ) as http:
                yielded = True
                trace.mark("stage_end", stage="webiq_setup", outcome="success",
                           duration_ms=trace.since("stage_start", stage="webiq_setup"))
                yield cls(http, settings.webiq_endpoint, trace)
        except (ExceptionGroup, httpx.HTTPError, TimeoutError) as exc:
            if yielded:
                raise
            raise webiq_error(exc) from None
        finally:
            if not yielded:
                trace.mark("stage_end", stage="webiq_setup", outcome="failed",
                           duration_ms=trace.since("stage_start", stage="webiq_setup"))

    async def search(
        self, query: str, turn: int, *,
        activity: ToolActivity | None = None,
        on_dispatch: Callable[[], Awaitable[None]] | None = None,
    ) -> list[Source]:
        if not query.strip() or len(query) > 1000:
            raise PublicError("invalid_query", "The search query must contain 1 to 1000 characters.")
        try:
            async with asyncio.timeout(18):
                arguments = {
                    "query": query, "language": "en", "region": "US", "mode": "fast",
                    "responseFilter": ["web", "news"], "maxResponseLength": 3000,
                    "maxCitations": 5,
                }
                if on_dispatch:
                    await on_dispatch()
                if activity:
                    activity.start_clock()
                outcome = "failed"
                try:
                    result = await self.http.post(self.endpoint, json=arguments)
                    if activity:
                        activity.stop_clock(response_received=True)
                    result.raise_for_status()
                    outcome = "success"
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    raise
                finally:
                    if activity:
                        activity.stop_clock()
                        if self.trace:
                            self.trace.mark(
                                "tool_returned", turn=turn, stage="search", call_id=activity.call_id,
                                duration_ms=activity.elapsed_ms, outcome=outcome,
                            )
                try:
                    payload = result.json()
                except ValueError:
                    raise PublicError("webiq_schema", "Web IQ Auto returned invalid result JSON.") from None
                return parse_sources(payload, turn)
        except (ExceptionGroup, httpx.HTTPError, TimeoutError) as exc:
            raise webiq_error(exc) from None
