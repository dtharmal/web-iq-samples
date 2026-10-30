# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import math
import os
import random
import re
import time
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from itertools import zip_longest
from typing import TypeVar
from urllib.parse import urlsplit

from azure.identity import CertificateCredential
from webiq import RetryPolicy, WebIQClient
from webiq.errors import APIConnectionError, APIStatusError
from webiq.types import ContentFormat

from .diagnostics import log_webiq_attempt, logger
from .models import Evidence

MAX_CONTENT_CHARACTERS = 3_000
MAX_EVIDENCE_ITEMS = 18
MAX_RETRIES = 2
MAX_RETRY_DELAY_SECONDS = 60
T = TypeVar("T")


def retrieve_evidence(
    api_key: str | None,
    queries: Iterable[str],
    max_results: int,
    region: str,
    *,
    auth_mode: str = "api-key",
) -> list[Evidence]:
    if auth_mode not in {"api-key", "entra"}:
        raise ValueError("Web IQ authentication must be api-key or entra.")
    result_groups: list[list[Evidence]] = []

    with ExitStack() as stack:
        credential = stack.enter_context(_certificate_credential()) if auth_mode == "entra" else None
        client = stack.enter_context(
            WebIQClient(
                api_key=api_key if auth_mode == "api-key" else None,
                credential=credential,
                scope="https://api.microsoft.ai/.default",
                timeout=30.0,
                retry=RetryPolicy(max_retries=0),
                telemetry_hook=log_webiq_attempt,
            )
        )
        for query in queries:
            logger.info("service=webiq operation=_search_web query=%s", json.dumps(query))
            web_response = _search_with_retry(
                lambda: client.web.search(
                    query,
                    max_results=max_results,
                    language="en",
                    region=region,
                    content_format=ContentFormat.passage,
                    max_length=MAX_CONTENT_CHARACTERS,
                )
            )
            web_items = []
            for result in web_response.webResults or []:
                if result.url:
                    logger.info(
                        "service=webiq operation=_search_web query=%s source_url=%s",
                        json.dumps(query), json.dumps(result.url),
                    )
                item = _to_evidence(
                    title=result.title,
                    url=result.url,
                    content=result.content,
                    query=query,
                    result_type="web",
                    updated_at=result.lastUpdatedAt,
                )
                if item:
                    web_items.append(item)
            result_groups.append(web_items)

            logger.info("service=webiq operation=_search_news query=%s", json.dumps(query))
            news_response = _search_with_retry(
                lambda: client.news.search(
                    query,
                    max_results=max_results,
                    language="en",
                    region=region,
                    content_format=ContentFormat.passage,
                    max_length=MAX_CONTENT_CHARACTERS,
                )
            )
            news_items = []
            for result in news_response.newsResults or []:
                if result.url:
                    logger.info(
                        "service=webiq operation=_search_news query=%s source_url=%s",
                        json.dumps(query), json.dumps(result.url),
                    )
                item = _to_evidence(
                    title=result.title,
                    url=result.url,
                    content=result.content,
                    query=query,
                    result_type="news",
                    publisher=result.source,
                    updated_at=result.lastUpdatedAt,
                )
                if item:
                    news_items.append(item)
            result_groups.append(news_items)

    evidence_by_url: dict[str, Evidence] = {}
    for round_items in zip_longest(*result_groups):
        for item in round_items:
            if item is not None:
                evidence_by_url.setdefault(item.url, item)
                if len(evidence_by_url) == MAX_EVIDENCE_ITEMS:
                    return list(evidence_by_url.values())
    return list(evidence_by_url.values())


def _certificate_credential() -> CertificateCredential:
    settings = {}
    for parameter, variable in (
        ("tenant_id", "AZURE_TENANT_ID"),
        ("client_id", "AZURE_CLIENT_ID"),
        ("certificate_path", "AZURE_CLIENT_CERTIFICATE_PATH"),
    ):
        value = os.environ.get(variable)
        if not value or not value.strip():
            raise ValueError(f"Set the {variable} environment variable before using --webiq-auth entra.")
        settings[parameter] = value
    return CertificateCredential(
        **settings,
        password=os.environ.get("AZURE_CLIENT_CERTIFICATE_PASSWORD"),
    )


def _search_with_retry(search: Callable[[], T]) -> T:
    for attempt in range(MAX_RETRIES + 1):
        try:
            return search()
        except (APIConnectionError, APIStatusError) as error:
            transient = isinstance(error, APIConnectionError) or error.status_code in {
                408, 429, 430, 500, 502, 503, 504,
            }
            if not transient or attempt == MAX_RETRIES:
                raise
            delay = random.uniform(0.5, 1.0) * (2 ** attempt)
            if isinstance(error, APIStatusError) and isinstance(error.body, dict):
                guidance = error.body.get("retryAfter")
                if isinstance(guidance, str) and re.fullmatch(r"\d+(?:\.\d+)?s", guidance):
                    retry_after = float(guidance[:-1])
                    if not math.isfinite(retry_after):
                        raise
                    delay = max(delay, retry_after)
            # Do not retry earlier than long service guidance to keep the CLI bounded.
            if delay > MAX_RETRY_DELAY_SECONDS:
                raise
            logger.warning(
                "service=webiq retry=%s delay_seconds=%.2f exception=%s",
                attempt + 1, delay, type(error).__name__,
            )
            time.sleep(delay)
    raise AssertionError("Unreachable retry state")


def _to_evidence(
    *,
    title: str | None,
    url: str | None,
    content: str | None,
    query: str,
    result_type: str,
    publisher: str | None = None,
    updated_at: str | None = None,
) -> Evidence | None:
    if not title or not url or not content:
        return None
    title, url, content = title.strip(), url.strip(), content.strip()
    parsed_url = urlsplit(url)
    if not title or not content or parsed_url.scheme not in {"https", "http"} or not parsed_url.netloc:
        return None

    return Evidence(
        title=title,
        url=url,
        content=content[:MAX_CONTENT_CHARACTERS],
        query=query,
        result_type=result_type,
        publisher=publisher.strip() if publisher else None,
        updated_at=updated_at,
    )
