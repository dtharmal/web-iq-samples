# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from webiq_vendor_questionnaire import client
from webiq_vendor_questionnaire.client import (
    BrowseClient,
    BrowseError,
    Page,
    PageProblem,
    build_request,
)

URL = "https://www.example.com/trust"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    delays = []
    monkeypatch.setattr(client, "_sleep", lambda seconds, stopped: delays.append(seconds))
    return delays


def browse_client(*responses):
    calls = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return BrowseClient("test-key", client=http, min_interval=0), calls


def ok(content="Synthetic passage.", **extra):
    return httpx.Response(200, json={"url": URL, "title": "Trust", "content": content, **extra})


def in_seconds(seconds):
    instant = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return instant.isoformat().replace("+00:00", "Z")


def test_build_request_asks_for_passages_without_live_crawl():
    assert build_request(URL, "  Where is data stored? ") == {
        "url": URL,
        "query": "Where is data stored?",
        "contentFormat": "passage",
        "maxLength": 3000,
        "liveCrawl": "none",
    }


def test_build_request_uses_fallback_for_live_crawl():
    assert build_request(URL, "q", live_crawl=True)["liveCrawl"] == "fallback"


@pytest.mark.parametrize(
    "url, query",
    [("http://www.example.com", "q"), (URL, ""), (URL, "   "), (URL, "x" * 1001)],
)
def test_build_request_rejects_invalid_input(url, query):
    with pytest.raises(ValueError):
        build_request(url, query)


def test_key_with_whitespace_is_rejected():
    with pytest.raises(ValueError):
        BrowseClient("bad key", client=httpx.Client())


def test_browse_sends_key_header_and_body():
    browse, calls = browse_client(
        ok(lastUpdatedAt="2026-11-01T00:00:00Z", crawledAt="2026-11-02T00:00:00Z")
    )
    page = browse.browse(URL, "Where is data stored?")
    assert page == Page(URL, "Trust", "Synthetic passage.", "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z")
    request = calls[0]
    assert str(request.url) == client.BROWSE_URL
    assert request.headers["x-apikey"] == "test-key"
    assert json.loads(request.content)["query"] == "Where is data stored?"


def test_404_is_a_page_problem_that_suggests_live_crawl():
    browse, _ = browse_client(httpx.Response(404, json={"errorCode": "DocNotFound"}))
    problem = browse.browse(URL, "q")
    assert isinstance(problem, PageProblem)
    assert problem.not_indexed
    assert "--live-crawl" in problem.reason


def test_404_with_live_crawl_does_not_suggest_it_again():
    browse, _ = browse_client(httpx.Response(404, json={"errorCode": "DocNotFound"}))
    problem = browse.browse(URL, "q", live_crawl=True)
    assert "--live-crawl" not in problem.reason


@pytest.mark.parametrize(
    "code",
    [
        "BrowseApiIsUrlExcluded",
        "URLFilteredOutEncumbrance",
        "URLFilteredOutSource403Forbidden",
        "BrowseApiIsFilteredOut",
    ],
)
def test_blocked_pages_are_page_problems(code):
    browse, calls = browse_client(httpx.Response(403, json={"errorCode": code}))
    problem = browse.browse(URL, "q")
    assert isinstance(problem, PageProblem)
    assert "policy" in problem.reason
    assert len(calls) == 1


@pytest.mark.parametrize("status", [520, 523, 524])
def test_crawl_failures_are_page_problems_without_retry(status):
    browse, calls = browse_client(httpx.Response(status, json={"retryAfter": in_seconds(300)}))
    assert isinstance(browse.browse(URL, "q"), PageProblem)
    assert len(calls) == 1


def test_202_waits_for_retry_after_then_returns_the_page(no_sleep):
    browse, calls = browse_client(
        httpx.Response(202, json={"url": URL, "retryAfter": in_seconds(5)}),
        ok(),
    )
    assert isinstance(browse.browse(URL, "q", live_crawl=True), Page)
    assert len(calls) == 2
    assert 3 <= no_sleep[0] <= 6


def test_202_stops_polling_after_the_limit():
    pending = [httpx.Response(202, json={"retryAfter": in_seconds(1)}) for _ in range(4)]
    browse, calls = browse_client(*pending)
    problem = browse.browse(URL, "q", live_crawl=True)
    assert isinstance(problem, PageProblem)
    assert len(calls) == client.MAX_CRAWL_POLLS + 1


def test_202_with_a_long_wait_is_a_page_problem():
    browse, calls = browse_client(httpx.Response(202, json={"retryAfter": in_seconds(600)}))
    assert isinstance(browse.browse(URL, "q", live_crawl=True), PageProblem)
    assert len(calls) == 1


def test_too_many_live_crawls_is_retried_then_reported_for_the_page():
    limit = httpx.Response(
        429, json={"errorCode": "TooManyOnDemandCrawls", "retryAfter": in_seconds(2)}
    )
    browse, calls = browse_client(limit, limit, limit)
    problem = browse.browse(URL, "q", live_crawl=True)
    assert isinstance(problem, PageProblem)
    assert "live crawl" in problem.reason
    assert len(calls) == 3


def test_429_is_retried_with_service_guidance(no_sleep):
    browse, calls = browse_client(
        httpx.Response(429, json={"errorCode": "AuthUserThrottled", "retryAfter": "30s"}),
        ok(),
    )
    browse.browse(URL, "q")
    assert len(calls) == 2
    assert no_sleep == [30.0]


def test_daily_quota_stops_the_run():
    browse, calls = browse_client(
        httpx.Response(429, json={"errorCode": "AuthUserDailyUsageExceeded"})
    )
    with pytest.raises(BrowseError, match="00:00 UTC"):
        browse.browse(URL, "q")
    assert len(calls) == 1


def test_401_reports_code_and_trace_id_but_no_body():
    trace = "0123456789abcdef0123456789abcdef"
    browse, _ = browse_client(
        httpx.Response(
            401, json={"errorCode": "AuthInvalidApiKey", "traceId": trace, "userMessage": "secret"}
        )
    )
    with pytest.raises(BrowseError) as error:
        browse.browse(URL, "q")
    message = str(error.value)
    assert "Code: AuthInvalidApiKey" in message
    assert trace in message
    assert "secret" not in message


def test_403_for_the_application_stops_the_run():
    browse, _ = browse_client(httpx.Response(403, json={"errorCode": "AuthUnauthorizedService"}))
    with pytest.raises(BrowseError, match="enabled for Browse"):
        browse.browse(URL, "q")


def test_transient_errors_stop_after_three_attempts():
    browse, calls = browse_client(httpx.Response(503), httpx.Response(503), httpx.Response(503))
    with pytest.raises(BrowseError, match="3 attempt"):
        browse.browse(URL, "q")
    assert len(calls) == 3


def test_timeouts_are_reported_after_retries():
    browse, calls = browse_client(*(httpx.ReadTimeout("timeout") for _ in range(3)))
    with pytest.raises(BrowseError, match="timed out"):
        browse.browse(URL, "q")
    assert len(calls) == 3


def test_redirects_are_not_followed():
    browse, calls = browse_client(
        httpx.Response(302, headers={"location": "https://elsewhere.example/"})
    )
    with pytest.raises(BrowseError, match="Redirects are not followed"):
        browse.browse(URL, "q")
    assert len(calls) == 1


def test_invalid_json_is_an_error():
    browse, _ = browse_client(httpx.Response(200, content=b"not json"))
    with pytest.raises(BrowseError, match="invalid JSON"):
        browse.browse(URL, "q")


@pytest.mark.parametrize("status", [400, 403, 404, 500])
def test_error_code_that_is_not_text_is_handled(status):
    responses = [httpx.Response(status, json={"errorCode": ["x"]}) for _ in range(3)]
    browse, _ = browse_client(*responses)
    try:
        result = browse.browse(URL, "q")
    except BrowseError:
        return
    assert isinstance(result, PageProblem)


def test_stopped_client_sends_nothing():
    browse, calls = browse_client()
    browse.stop()
    with pytest.raises(BrowseError, match="Stopped"):
        browse.browse(URL, "q")
    assert calls == []


def test_requests_are_spaced(monkeypatch, no_sleep):
    http = httpx.Client(transport=httpx.MockTransport(lambda request: ok()))
    browse = BrowseClient("k", client=http, min_interval=0.5)
    browse.browse(URL, "q")
    browse.browse(URL, "q")
    assert no_sleep and 0 < no_sleep[-1] <= 0.5
