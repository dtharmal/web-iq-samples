# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import ipaddress
import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

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
            or not isinstance(payload.get("newsResults", []), list)):
        raise PublicError("webiq_schema", "WebIQ returned an unexpected response format.")
    sources: list[Source] = []
    seen: set[str] = set()
    # Web IQ omits empty model collections, including newsResults.
    for entry in payload.get("newsResults", [])[:5]:
        if not isinstance(entry, dict):
            raise PublicError("webiq_schema", "WebIQ returned an invalid result.")
        url = safe_url(entry.get("url"))
        content = optional_text(entry.get("content"), 3500)
        title = optional_text(entry.get("title"), 300)
        if not url or not content or not title or url in seen:
            continue
        seen.add(url)
        sources.append(Source(
            id=f"t{turn}-s{len(sources) + 1}", title=title, url=url,
            domain=urlsplit(url).hostname or "", content=content,
            publisher=optional_text(entry.get("source")),
            # The live MCP news response exposes update/crawl dates, not publication dates.
            updated_at=optional_text(entry.get("lastUpdatedAt")),
            crawled_at=optional_text(entry.get("crawledAt")),
        ))
    return sources


class LimitedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream):
        self.stream = stream

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > 512_000:
                raise PublicError("webiq_size", "WebIQ exceeded the demo response-size limit.")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class LimitedTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request):
        response = await super().handle_async_request(request)
        response.stream = LimitedStream(response.stream)
        return response


def mcp_error(exc: Exception) -> PublicError:
    if isinstance(exc, BaseExceptionGroup):
        return mcp_error(exc.exceptions[0])
    if isinstance(exc, PublicError):
        return exc
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return PublicError("webiq_timeout", "WebIQ MCP timed out. Please try again.")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        message = {
            401: "WebIQ MCP rejected the API key. Update WEBIQ_API_KEY privately.",
            403: "WebIQ denied access. Check AppID provisioning and News/MCP permissions.",
            429: "WebIQ MCP hit a rate or daily quota limit. Check service retry guidance before trying again.",
        }.get(status, f"WebIQ MCP failed (HTTP {status}). No alternative search was used.")
        return PublicError(f"webiq_http_{status}", message)
    return PublicError("webiq_mcp", f"WebIQ MCP failed ({type(exc).__name__}). Please reconnect.")


class WebIQ:
    def __init__(self, session: ClientSession, trace: PerformanceTrace | None = None):
        self.session = session
        self.trace = trace

    @classmethod
    @asynccontextmanager
    async def connect(cls, settings: Settings, *, trace: PerformanceTrace | None = None):
        if not settings.webiq_key:
            raise PublicError("webiq_not_configured", "Add WEBIQ_API_KEY to the private .env file.")
        yielded = False
        trace = trace or PerformanceTrace()
        trace.mark("stage_start", stage="mcp_setup")
        try:
            async with (
                httpx.AsyncClient(
                    headers={"x-apikey": settings.webiq_key}, timeout=20, follow_redirects=False,
                    transport=LimitedTransport(retries=0),
                ) as http,
                streamable_http_client(settings.webiq_endpoint, http_client=http) as (read, write, _),
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=20)) as session,
            ):
                async with asyncio.timeout(20):
                    with trace.span("mcp_initialize"):
                        await session.initialize()
                    with trace.span("mcp_discovery"):
                        tools = await session.list_tools()
                    news = next((tool for tool in tools.tools if tool.name == "news"), None)
                    if news is None or not {
                        "query", "maxResults", "maxLength", "contentFormat", "language", "region",
                    }.issubset(news.inputSchema.get("properties", {})):
                        raise PublicError("webiq_schema", "WebIQ MCP did not advertise the expected news tool.")
                yielded = True
                trace.mark("stage_end", stage="mcp_setup", outcome="success",
                           duration_ms=trace.since("stage_start", stage="mcp_setup"))
                yield cls(session, trace)
        except (ExceptionGroup, httpx.HTTPError, McpError, TimeoutError) as exc:
            if yielded:
                raise
            # SDK task groups wrap failures. Expose only safe, actionable metadata.
            raise mcp_error(exc) from None
        finally:
            if not yielded:
                trace.mark("stage_end", stage="mcp_setup", outcome="failed",
                           duration_ms=trace.since("stage_start", stage="mcp_setup"))

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
                    "query": query, "maxResults": 5, "language": "en", "region": "US",
                    "contentFormat": "passage", "maxLength": 3500,
                }
                if on_dispatch:
                    await on_dispatch()
                if activity:
                    activity.start_clock()
                outcome = "failed"
                try:
                    result = await self.session.call_tool("news", arguments)
                    if activity:
                        activity.stop_clock(response_received=True)
                    outcome = "failed" if result.isError else "success"
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
                if result.isError:
                    raise PublicError("webiq_tool", "WebIQ MCP reported a news-search error. Try again.")
                payload = result.structuredContent
                if payload is None:
                    text = [part.text for part in result.content if part.type == "text"]
                    if len(text) != 1 or len(text[0]) > 256_000:
                        raise PublicError("webiq_schema", "WebIQ MCP returned an unexpected tool result.")
                    try:
                        payload = json.loads(text[0])
                    except ValueError:
                        raise PublicError("webiq_schema", "WebIQ MCP returned invalid result JSON.") from None
                return parse_sources(payload, turn)
        except (ExceptionGroup, httpx.HTTPError, McpError, TimeoutError) as exc:
            raise mcp_error(exc) from None
