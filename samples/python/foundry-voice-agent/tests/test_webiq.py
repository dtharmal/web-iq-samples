# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.types import CallToolResult, TextContent

from app.activity import ToolActivity
from app.config import Settings
from app.webiq import PublicError, WebIQ, mcp_error, parse_sources, safe_url


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,evil", "http://127.0.0.1/private", "http://localhost/",
    "https://user:password@example.org/", "http://2130706433/", "http://0x7f000001/",
    "http://10.2.3.4/", "http://[::1]/", "https://example.org:1234/", "https://example.org/\nfoo",
    "https://example.org\\@evil.org", "http://host.internal/a", "http://host.local/a",
])
def test_rejects_unsafe_citation_urls(url):
    assert safe_url(url) is None


def test_source_metadata_preserves_semantics():
    sources = parse_sources({"newsResults": [{
        "title": "Real title", "url": "https://news.example.org/story",
        "content": "Evidence, not instructions.", "source": "Example News",
        "lastUpdatedAt": "2026-10-01", "crawledAt": "2026-10-02",
    }]}, 3)
    source = sources[0]
    assert source.id == "t3-s1"
    assert source.publisher == "Example News"
    assert source.published_at is None
    assert source.updated_at == "2026-10-01"
    assert source.crawled_at == "2026-10-02"
    assert "content" not in source.public()


def test_unsafe_empty_and_duplicate_evidence_not_used():
    valid = {"title": "Story", "url": "https://news.example.org/a", "content": "Evidence"}
    assert len(parse_sources({"newsResults": [valid, valid, {**valid, "url": "javascript:a"}]}, 1)) == 1
    assert parse_sources({"newsResults": [{**valid, "content": ""}]}, 1) == []


@pytest.mark.parametrize("payload", [
    [], {"newsResults": None}, {"newsResults": "bad"}, {"newsResults": [42]},
    {"error": "DO_NOT_EXPOSE"}, {"errorCode": "DO_NOT_EXPOSE"},
])
def test_schema_errors_are_explicit(payload):
    with pytest.raises(PublicError, match="WebIQ"):
        parse_sources(payload, 1)


@pytest.mark.parametrize("payload", [{}, {"traceId": "synthetic-trace"}, {"newsResults": []}])
def test_omitted_empty_news_collection_is_no_evidence_not_a_schema_failure(payload):
    assert parse_sources(payload, 1) == []


@pytest.mark.parametrize("payload", [{"newsResults": []}, {}, {"traceId": "synthetic-trace"}])
async def test_mcp_calls_advertised_news_tool_without_exposing_credentials(payload):
    session = AsyncMock()
    session.call_tool.return_value = CallToolResult(
        content=[], structuredContent=payload,
    )
    assert await WebIQ(session).search("news today", 1) == []
    name, args = session.call_tool.call_args.args
    assert name == "news"
    assert args["query"] == "news today"
    assert args["contentFormat"] == "passage"
    assert args["maxResults"] == 5
    assert "api_key" not in args


async def test_mcp_json_content_fallback_uses_real_schema_only():
    session = AsyncMock()
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text=json.dumps({"newsResults": []}))],
    )
    assert await WebIQ(session).search("news", 1) == []
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text="invalid")])
    with pytest.raises(PublicError, match="invalid result JSON"):
        await WebIQ(session).search("news", 1)


async def test_mcp_error_is_not_a_success_or_raw_payload_leak():
    session = AsyncMock()
    session.call_tool.return_value = CallToolResult(
        isError=True, content=[TextContent(type="text", text="DO_NOT_EXPOSE")],
    )
    with pytest.raises(PublicError) as error:
        await WebIQ(session).search("news", 1)
    assert "DO_NOT_EXPOSE" not in str(error.value)
    assert error.value.code == "webiq_tool"


async def test_mcp_timeout_and_query_limits():
    session = AsyncMock()
    session.call_tool.side_effect = TimeoutError
    with pytest.raises(PublicError) as error:
        await WebIQ(session).search("news", 1)
    assert error.value.code == "webiq_timeout"
    for query in ("", " " * 10, "a" * 1001):
        with pytest.raises(PublicError) as error:
            await WebIQ(session).search(query, 1)
        assert error.value.code == "invalid_query"


def test_auth_errors_redacted_and_group_unwrapped():
    request = httpx.Request("POST", "https://api.microsoft.ai/v3/mcp", headers={"x-apikey": "SECRET"})
    exc = httpx.HTTPStatusError("SECRET", request=request, response=httpx.Response(401, request=request))
    error = mcp_error(ExceptionGroup("group", [exc]))
    assert error.code == "webiq_http_401"
    assert "SECRET" not in error.message
    assert "SECRET" not in repr(Settings(webiq_key="SECRET"))


async def test_timing_is_actual_mcp_round_trip_not_notifications_or_source_parsing(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("app.activity.perf_counter", lambda: clock[0])
    activity = ToolActivity("session", "call", 1, "webiq.news")
    order = []

    async def dispatched():
        order.append("dispatch")
        assert activity.started_at is None
        clock[0] = 100.0  # Notification/setup time must not be counted.

    async def call_tool(*_):
        order.append("mcp")
        assert activity.started_at == 100.0
        clock[0] = 100.012345
        return CallToolResult(content=[], structuredContent={"newsResults": []})

    def parse(payload, turn):
        clock[0] = 200.0  # Post-receipt normalization must not be counted.
        return parse_sources(payload, turn)

    monkeypatch.setattr("app.webiq.parse_sources", parse)
    session = AsyncMock()
    session.call_tool.side_effect = call_tool
    await WebIQ(session).search("news", 1, activity=activity, on_dispatch=dispatched)
    assert order == ["dispatch", "mcp"]
    assert activity.elapsed_ms == pytest.approx(12.345)
    assert activity.response_received
    activity.stop_clock()  # Terminal timing cannot grow with model/audio time.
    assert activity.elapsed_ms == pytest.approx(12.345)
    assert set(activity.event()) == {
        "session_id", "call_id", "turn", "tool", "state", "scope", "elapsed_ms",
        "source_count", "response_received",
    }


@pytest.mark.parametrize("failure", ["timeout", "tool_error", "invalid_schema"])
async def test_failed_calls_keep_actual_duration_and_receipt_boundary(monkeypatch, failure):
    clock = [7.0]
    monkeypatch.setattr("app.activity.perf_counter", lambda: clock[0])
    activity = ToolActivity("session", "call", 1, "webiq.news")
    dispatch = AsyncMock()

    async def call_tool(*_):
        dispatch.assert_awaited_once()
        clock[0] = 7.125
        if failure == "timeout":
            raise TimeoutError
        return CallToolResult(
            content=[], isError=failure == "tool_error", structuredContent={"newsResults": None},
        )

    session = AsyncMock()
    session.call_tool.side_effect = call_tool
    with pytest.raises(PublicError):
        await WebIQ(session).search("news", 1, activity=activity, on_dispatch=dispatch)
    assert activity.elapsed_ms == pytest.approx(125)
    assert activity.response_received == (failure != "timeout")


async def test_cancelled_mcp_call_times_until_abort_without_claiming_receipt(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("app.activity.perf_counter", lambda: clock[0])
    activity = ToolActivity("session", "call", 1, "webiq.news")
    in_flight = asyncio.Event()

    async def call_tool(*_):
        in_flight.set()
        await asyncio.Event().wait()

    session = AsyncMock()
    session.call_tool.side_effect = call_tool
    task = asyncio.create_task(WebIQ(session).search("news", 1, activity=activity))
    await asyncio.wait_for(in_flight.wait(), 1)
    clock[0] = 12.5
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert activity.elapsed_ms == pytest.approx(2500)
    assert not activity.response_received


async def test_invalid_query_never_claims_dispatch_or_elapsed_time():
    activity = ToolActivity("session", "call", 1, "webiq.news")
    session, dispatch = AsyncMock(), AsyncMock()
    with pytest.raises(PublicError):
        await WebIQ(session).search("", 1, activity=activity, on_dispatch=dispatch)
    dispatch.assert_not_awaited()
    session.call_tool.assert_not_awaited()
    assert activity.started_at is None and activity.elapsed_ms is None
