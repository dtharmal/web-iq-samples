# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Browse REST requests, retries, live crawl polling, and error messages."""

import json
import math
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

BROWSE_URL = "https://api.microsoft.ai/v3/browse"
MAX_LENGTH = 3_000
MAX_QUERY_LENGTH = 1_000
MAX_RETRIES = 2
MAX_RETRY_DELAY_SECONDS = 60
MAX_CRAWL_POLLS = 3
# Spacing between request starts keeps the run below the default per-application rate limit.
MIN_REQUEST_INTERVAL_SECONDS = 0.1
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
PAGE_BLOCKED_CODES = {
    "BrowseApiIsFilteredOut",
    "BrowseApiIsUrlExcluded",
    "URLFilteredOutSpamAdult",
    "URLFilteredOutEncumbrance",
    "URLFilteredOutSource403Forbidden",
}
LIVE_CRAWL_LIMIT_CODES = {"TooManyOnDemandCrawls", "BrowseApiTooManyOnDemandCrawls"}
KNOWN_ERROR_CODES = {
    "AuthNoToken",
    "AuthInvalidApiKey",
    "AuthExpiredTrial",
    "AuthenticationFailed",
    "AuthUnauthorizedService",
    "AuthContentFormatForbidden",
    "AuthUserThrottled",
    "AuthUserDailyUsageExceeded",
    "HandlerInvalidInput",
    "InternalServerError",
    "RequestInvalidFormat",
    "RequestUnsupportedMediaType",
}


class BrowseError(RuntimeError):
    """A failure that stops the run. The message contains no request or response content."""


@dataclass(frozen=True)
class Page:
    """Passages Browse returned for one question and one page."""

    url: str
    title: str
    content: str
    last_updated_at: str | None
    crawled_at: str | None


@dataclass(frozen=True)
class PageProblem:
    """A page that returned no passages for this question, with the reason."""

    url: str
    reason: str
    not_indexed: bool = False


def build_request(url: str, query: str, *, live_crawl: bool = False) -> dict[str, object]:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("Page URLs must start with https://.")
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= MAX_QUERY_LENGTH:
        raise ValueError("Each question must contain between 1 and 1,000 characters.")
    return {
        "url": url,
        "query": query.strip(),
        "contentFormat": "passage",
        "maxLength": MAX_LENGTH,
        "liveCrawl": "fallback" if live_crawl else "none",
    }


class BrowseClient:
    """Calls Browse with an API key. Safe to share across threads."""

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        min_interval: float = MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Set WEBIQ_API_KEY to a Web IQ API key with Browse access.")
        api_key = api_key.strip()
        if not all(33 <= ord(character) <= 126 for character in api_key):
            raise ValueError("WEBIQ_API_KEY contains whitespace or invalid header characters.")
        self._api_key = api_key
        self._http = client if client is not None else httpx.Client()
        self._owns_client = client is None
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next_start = 0.0
        self._stopped = threading.Event()

    def __enter__(self) -> "BrowseClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def stop(self) -> None:
        """Make requests in other threads stop at their next wait instead of retrying."""
        self._stopped.set()

    def browse(self, url: str, query: str, *, live_crawl: bool = False) -> Page | PageProblem:
        payload = build_request(url, query, live_crawl=live_crawl)
        attempt = 0
        polls = 0
        while True:
            self._wait_for_slot()
            if self._stopped.is_set():
                raise BrowseError("Stopped because the run ended.")
            try:
                response = self._http.post(
                    BROWSE_URL,
                    headers={
                        "x-apikey": self._api_key,
                        "content-type": "application/json",
                        "accept": "application/json",
                    },
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                    follow_redirects=False,
                )
            except httpx.RequestError as error:
                if attempt == MAX_RETRIES:
                    message = (
                        "The Web IQ request timed out."
                        if isinstance(error, httpx.TimeoutException)
                        else "Could not reach Web IQ. Check network, TLS, and proxy settings."
                    )
                    raise BrowseError(f"{message} Stopped after {attempt + 1} attempts.") from None
                _sleep(_backoff(attempt), self._stopped)
                attempt += 1
                continue

            status = response.status_code
            if status == 200:
                return _page_from(url, response)

            body = _json_object(response)
            if status == 202:
                # A live crawl is in progress. Wait until retryAfter, then send the same request.
                if polls >= MAX_CRAWL_POLLS:
                    return PageProblem(url, "The live crawl didn't finish during this run. Try again later.")
                delay = _optional_delay(body.get("retryAfter"), attempt)
                if delay is None or delay > MAX_RETRY_DELAY_SECONDS:
                    return PageProblem(url, "The live crawl didn't finish during this run. Try again later.")
                retry_url = body.get("url")
                if isinstance(retry_url, str) and retry_url.startswith("https://"):
                    payload["url"] = retry_url
                _sleep(delay, self._stopped)
                polls += 1
                continue

            code = body.get("errorCode")
            code = code if isinstance(code, str) else None
            problem = _page_problem(url, status, code, live_crawl)
            if problem is not None:
                return problem

            if status == 430 or code in LIVE_CRAWL_LIMIT_CODES:
                delay = _optional_delay(body.get("retryAfter"), attempt)
                if attempt == MAX_RETRIES or delay is None or delay > MAX_RETRY_DELAY_SECONDS:
                    return PageProblem(
                        url, "Too many live crawl requests right now. Try this page again later."
                    )
                _sleep(delay, self._stopped)
                attempt += 1
                continue

            failure = _failure_message(status, body, self._api_key)
            if (
                status not in RETRYABLE_STATUSES
                or code == "AuthUserDailyUsageExceeded"
                or attempt == MAX_RETRIES
            ):
                raise BrowseError(f"{failure} Stopped after {attempt + 1} attempt(s).")
            try:
                delay = _retry_delay(
                    response.headers.get("retry-after"),
                    body.get("retryAfter"),
                    attempt,
                    datetime.now(timezone.utc),
                )
            except ValueError:
                raise BrowseError(
                    f"{failure} Retry guidance was invalid; no automatic retry was made."
                ) from None
            if delay > MAX_RETRY_DELAY_SECONDS:
                raise BrowseError(
                    f"{failure} The service requires waiting more than "
                    f"{MAX_RETRY_DELAY_SECONDS} seconds. Try later; this run will not retry early."
                )
            _sleep(delay, self._stopped)
            attempt += 1

    def _wait_for_slot(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_start)
            self._next_start = start + self._min_interval
        if start > now:
            _sleep(start - now, self._stopped)


def _sleep(seconds: float, stopped: threading.Event) -> None:
    """Wait, but return early when the run is stopped."""
    stopped.wait(seconds)


def _page_from(url: str, response: httpx.Response) -> Page:
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise BrowseError("Web IQ returned HTTP 200 with invalid JSON.") from None
    if not isinstance(body, dict) or not isinstance(body.get("content"), str):
        raise BrowseError("Web IQ returned an unexpected response object.")
    title = body.get("title")
    return Page(
        url=url,
        title=title if isinstance(title, str) else "",
        content=body["content"],
        last_updated_at=_optional_text(body.get("lastUpdatedAt")),
        crawled_at=_optional_text(body.get("crawledAt")),
    )


def _page_problem(url: str, status: int, code: str | None, live_crawl: bool) -> PageProblem | None:
    """Return a per-page outcome for errors that affect only this page, or None."""
    if status == 404:
        hint = "" if live_crawl else " Run again with --live-crawl to request a crawl."
        return PageProblem(url, f"The page isn't in the Web IQ index.{hint}", not_indexed=True)
    if status == 403 and code in PAGE_BLOCKED_CODES:
        return PageProblem(
            url, "The site's or Web IQ's content policy doesn't allow this page to be returned."
        )
    if status in {520, 523, 524}:
        return PageProblem(url, "The page couldn't be crawled or processed right now. Try again later.")
    if status == 400 and code in {"InvalidUrl", "NotSupported"}:
        return PageProblem(url, "Browse doesn't support this URL.")
    return None


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _optional_delay(value: object, attempt: int) -> float | None:
    if value is None:
        return _backoff(attempt)
    try:
        return max(
            _backoff(attempt),
            _parse_retry_after(value, datetime.now(timezone.utc), http_header=False),
        )
    except ValueError:
        return None


def _json_object(response: httpx.Response) -> dict[str, object]:
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def _failure_message(status: int, body: dict[str, object], api_key: str) -> str:
    hints = {
        400: "Check the page URLs and questions.",
        401: "Check WEBIQ_API_KEY and whether it has expired.",
        403: "Confirm the key's application is enabled for Browse.",
        415: "The request content type was rejected.",
        429: "The service is throttling requests or the application has reached its quota.",
    }
    hint = hints.get(status, "The service could not complete the request. Try again later.")
    if 300 <= status < 400:
        hint = "Redirects are not followed, to avoid forwarding credentials."
    if body.get("errorCode") == "AuthUserDailyUsageExceeded":
        hint = "The daily quota is exhausted. It resets at 00:00 UTC; no automatic retry was made."
    message = f"Web IQ returned HTTP {status}. {hint}"
    code = body.get("errorCode")
    if isinstance(code, str) and code in KNOWN_ERROR_CODES and api_key not in code:
        message += f" Code: {code}."
    trace_id = body.get("traceId")
    if (
        isinstance(trace_id, str)
        and re.fullmatch(
            r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
            trace_id,
        )
        and api_key not in trace_id
    ):
        message += f" Trace ID: {trace_id}."
    return message


def _backoff(attempt: int) -> float:
    return random.uniform(0.5, 1.0) * (2 ** attempt)


def _retry_delay(header: str | None, body_value: object, attempt: int, now: datetime) -> float:
    delays = [_backoff(attempt)]
    if header is not None:
        delays.append(_parse_retry_after(header, now, http_header=True))
    if body_value is not None:
        delays.append(_parse_retry_after(body_value, now, http_header=False))
    return max(delays)


def _parse_retry_after(value: object, now: datetime, *, http_header: bool) -> float:
    """Parse seconds, a duration such as "60s", or a timestamp, as Web IQ documents them."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid retry guidance.")
    value = value.strip()
    pattern = r"[0-9]+(?:\.[0-9]+)?" if http_header else r"[0-9]+(?:\.[0-9]+)?s"
    if re.fullmatch(pattern, value):
        seconds = float(value if http_header else value[:-1])
        if not math.isfinite(seconds):
            raise ValueError("Non-finite retry delay.")
        return seconds
    try:
        instant = (
            parsedate_to_datetime(value)
            if http_header
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Invalid retry timestamp.") from None
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Retry timestamps must include a timezone.")
    return max(0.0, (instant - now).total_seconds())
