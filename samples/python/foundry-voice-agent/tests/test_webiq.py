# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import json

import httpx
import pytest

from app.activity import ToolActivity
from app.config import Settings
from app.webiq import LimitedStream, PublicError, WebIQ, parse_sources, safe_url, webiq_error


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,evil", "http://127.0.0.1/private", "http://localhost/",
    "http://2130706433/", "http://0x7f000001/", "http://10.2.3.4/", "http://[::1]/",
    "https://example.org:1234/", "https://example.org/\nfoo", "https://example.org\\@evil.org",
    "http://host.internal/a", "http://host.local/a",
])
def test_rejects_unsafe_citation_urls(url):
    assert safe_url(url) is None


def test_auto_result_scoped_evidence_and_metadata():
    payload = {"results": [
        {"evidence": {"type": "text", "content": "Evidence A"},
         "citations": [{"title": "T" * 250, "url": "https://example.org/a", "contentSource": "web",
                        "lastUpdatedAt": "2026-10-01", "crawledAt": "2026-10-02"}]},
        {"evidence": {"type": "text", "content": "Evidence B" * 1000},
         "citations": [{"title": "B", "url": "https://example.org/b"}]},
    ]}
    first, second = parse_sources(payload, 3)
    assert first.id == "t3-s1"
    assert first.title == "T" * 200
    assert first.content == "Evidence A"
    assert first.publisher == "web"
    assert first.published_at is None
    assert first.updated_at == "2026-10-01"
    assert first.crawled_at == "2026-10-02"
    assert "content" not in first.public()
    assert second.content == ("Evidence B" * 1000)[:3000]


def test_invalid_duplicate_adult_and_nontext_evidence_are_not_used():
    citation = {"title": "Story", "url": "https://example.org/a"}
    payload = {"results": [
        {"evidence": {"type": "text", "content": "Evidence"}, "citations": [
            citation, citation, {**citation, "url": "javascript:a"},
            {**citation, "url": "https://example.org/b", "isAdult": True},
        ]},
        {"evidence": {"type": "media/image", "content": {}}, "citations": [citation]},
    ]}
    assert len(parse_sources(payload, 1)) == 1
    assert parse_sources({"results": []}, 1) == []


@pytest.mark.parametrize("payload", [
    [], {}, {"results": None}, {"results": "bad"}, {"results": [42]},
    {"error": "DO_NOT_EXPOSE"}, {"results": [{"evidence": {"type": "text", "content": 1}}]},
])
def test_schema_errors_are_explicit_and_redacted(payload):
    with pytest.raises(PublicError) as error:
        parse_sources(payload, 1)
    assert "DO_NOT_EXPOSE" not in str(error.value)


async def test_auto_request_and_entitlement_errors():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), headers={"x-apikey": "SECRET"}) as client:
        assert await WebIQ(client, "https://api.microsoft.ai/v3/auto").search("news today", 1) == []
    assert requests[0].method == "POST"
    assert json.loads(requests[0].content) == {
        "query": "news today", "language": "en", "region": "US", "mode": "fast",
        "responseFilter": ["web", "news"], "maxResponseLength": 3000, "maxCitations": 5,
    }
    assert requests[0].headers["x-apikey"] == "SECRET"
    for status in (401, 403, 429):
        request = httpx.Request("POST", "https://api.microsoft.ai/v3/auto")
        exc = httpx.HTTPStatusError("SECRET", request=request, response=httpx.Response(status, request=request))
        error = webiq_error(exc)
        assert error.code == f"webiq_http_{status}"
        assert "SECRET" not in error.message
        if status == 403:
            assert "not enabled for Auto" in error.message
    assert "SECRET" not in repr(Settings(webiq_key="SECRET"))


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_http_errors_do_not_expose_response_body(status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text="PRIVATE_RESPONSE"))
    ) as client:
        with pytest.raises(PublicError) as error:
            await WebIQ(client, "https://api.microsoft.ai/v3/auto").search("news", 1)
    assert error.value.code == f"webiq_http_{status}"
    assert "PRIVATE_RESPONSE" not in str(error.value)


async def test_invalid_json_and_timeout_are_sanitized():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="PRIVATE_INVALID"))
    ) as client:
        with pytest.raises(PublicError) as error:
            await WebIQ(client, "https://api.microsoft.ai/v3/auto").search("news", 1)
    assert error.value.code == "webiq_schema"
    assert "PRIVATE_INVALID" not in str(error.value)

    def timeout(request):
        raise httpx.ReadTimeout("PRIVATE_TIMEOUT", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(PublicError) as error:
            await WebIQ(client, "https://api.microsoft.ai/v3/auto").search("news", 1)
    assert error.value.code == "webiq_timeout"
    assert "PRIVATE_TIMEOUT" not in str(error.value)


@pytest.mark.parametrize("size,accepted", [(512_000, True), (512_001, False)])
async def test_response_stream_size_limit(size, accepted):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 256_000
            yield b"x" * (size - 256_000)

    stream = LimitedStream(Stream())
    if accepted:
        assert sum([len(chunk) async for chunk in stream]) == size
    else:
        with pytest.raises(PublicError) as error:
            _ = [chunk async for chunk in stream]
        assert error.value.code == "webiq_size"


async def test_timing_and_cancel_boundaries(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("app.activity.perf_counter", lambda: clock[0])
    activity = ToolActivity("session", "call", 1, "webiq.auto")

    async def dispatch():
        assert activity.started_at is None
        clock[0] = 100.0

    async def handle(request):
        assert activity.started_at == 100.0
        clock[0] = 100.012345
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await WebIQ(client, "https://api.microsoft.ai/v3/auto").search(
            "news", 1, activity=activity, on_dispatch=dispatch,
        )
    assert activity.elapsed_ms == pytest.approx(12.345)
    assert activity.response_received

    activity = ToolActivity("session", "call", 1, "webiq.auto")
    started = asyncio.Event()

    async def blocked(request):
        started.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as client:
        task = asyncio.create_task(WebIQ(client, "https://api.microsoft.ai/v3/auto").search(
            "news", 1, activity=activity,
        ))
        await started.wait()
        clock[0] += 2.5
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert activity.elapsed_ms == pytest.approx(2500)
    assert not activity.response_received


@pytest.mark.parametrize("query", ["", " " * 10, "a" * 1001])
async def test_invalid_queries_not_dispatched(query):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("dispatched"))) as client:
        with pytest.raises(PublicError) as error:
            await WebIQ(client, "https://api.microsoft.ai/v3/auto").search(query, 1)
    assert error.value.code == "invalid_query"
