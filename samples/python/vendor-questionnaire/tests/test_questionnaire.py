# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import time

import httpx
import pytest

from webiq_vendor_questionnaire.client import BrowseClient, BrowseError, Page, PageProblem
from webiq_vendor_questionnaire.config import Vendor, VendorPage
from webiq_vendor_questionnaire.questionnaire import (
    best_quote,
    build_results,
    collect,
    keywords,
    rank_sources,
)

RESIDENCY = Page(
    "https://contoso.example/residency",
    "Data residency",
    "Contoso stores customer data in the region you choose. ... Backups stay in the same geography.",
    None,
    None,
)
ENCRYPTION = Page(
    "https://contoso.example/encryption",
    "Encryption",
    "Customer data is encrypted at rest with AES-256 and in transit with TLS 1.2 or later.",
    None,
    None,
)
CAREERS = Page(
    "https://contoso.example/careers",
    "Careers",
    "Join our team. We value customer data and curiosity.",
    None,
    None,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def browse(self, url, query, *, live_crawl=False):
        self.calls.append((url, query, live_crawl))
        return self.responses[(url, live_crawl)]

    def stop(self):
        pass


def test_collect_cancels_queued_requests_after_a_run_stopping_error():
    calls = []

    def handler(request):
        calls.append(request)
        time.sleep(0.02)
        return httpx.Response(401, json={"errorCode": "AuthInvalidApiKey"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    browse = BrowseClient("k", client=http, min_interval=0)
    vendor = Vendor("A", tuple(VendorPage("", f"https://a.example/{i}") for i in range(10)))
    questions = tuple(f"Question {i}?" for i in range(10))
    with pytest.raises(BrowseError):
        collect(browse, vendor, questions)
    assert len(calls) < 20


def test_collect_stops_on_a_fatal_error_behind_a_slow_request():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["url"] == "https://a.example/0" and body["query"] == "Question 0?":
            time.sleep(0.5)
        elif body["url"] == "https://a.example/1" and body["query"] == "Question 0?":
            return httpx.Response(401, json={"errorCode": "AuthInvalidApiKey"})
        else:
            time.sleep(0.01)
        return httpx.Response(200, json={"url": body["url"], "title": "", "content": "Synthetic."})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    browse = BrowseClient("k", client=http, min_interval=0)
    vendor = Vendor("A", tuple(VendorPage("", f"https://a.example/{i}") for i in range(10)))
    questions = tuple(f"Question {i}?" for i in range(10))
    with pytest.raises(BrowseError):
        collect(browse, vendor, questions)
    assert len(calls) < 20


def test_keywords_drop_stopwords_and_stem():
    assert keywords("Where is customer data stored?") == {"stor"}
    assert "encrypt" in keywords("Is data encrypted?")
    assert "encrypt" in keywords("encryption keys")


def test_rank_sources_prefers_distinctive_words():
    ranked = rank_sources("Is customer data encrypted in transit?", (RESIDENCY, ENCRYPTION, CAREERS))
    assert [source.page for source in ranked] == [ENCRYPTION]


@pytest.mark.parametrize(
    ("question", "contents"),
    [
        (
            "Is customer information encrypted?",
            ("Customer information is encrypted.", "All customer records are encrypted."),
        ),
        (
            "Is customer data encrypted at rest?",
            ("Customer information is encrypted at rest.", "All records are encrypted at rest."),
        ),
    ],
)
def test_rank_sources_keeps_pages_whose_matching_words_are_on_every_page(question, contents):
    pages = tuple(
        Page(f"https://a.example/{i}", "", content, None, None)
        for i, content in enumerate(contents)
    )
    assert [source.page for source in rank_sources(question, pages)] == list(pages)


def test_rank_sources_ranks_shared_words_below_distinctive_ones():
    shared = Page("https://a.example/shared", "", "All records are encrypted.", None, None)
    distinctive = Page(
        "https://a.example/keys", "", "Records are encrypted with rotated keys.", None, None
    )
    ranked = rank_sources("Are records encrypted with rotated keys?", (shared, distinctive))
    assert [source.page for source in ranked] == [distinctive]


def test_rank_sources_returns_nothing_without_a_match():
    assert rank_sources("Which certifications do you hold?", (RESIDENCY, ENCRYPTION)) == ()


def test_rank_sources_keeps_at_most_two():
    pages = tuple(
        Page(f"https://a.example/{i}", "", f"Region {i} storage options.", None, None)
        for i in range(4)
    )
    pages += (CAREERS,)
    assert len(rank_sources("Can the customer choose the storage region?", pages)) == 2


def test_best_quote_finds_the_matching_part_within_the_limit():
    page = Page("https://a.example", "", "Unrelated opening text here. ... Backups stay in the same geography as the primary copy.", None, None)
    quote, before, after = best_quote("Where are backups kept in the same geography?", page, 60)
    assert quote.startswith("Backups stay in the same geography")
    assert len(quote) <= 60
    assert before


def test_best_quote_skips_the_page_title_and_stays_in_one_segment():
    page = Page(
        "https://a.example",
        "Data deletion overview",
        "Data deletion overview Some navigation. ... To request deletion, open a support request.",
        None,
        None,
    )
    quote, _, after = best_quote("How can a customer request deletion?", page, 200)
    assert quote == "To request deletion, open a support request."
    assert not after


def test_best_quote_with_no_room_returns_nothing():
    assert best_quote("q", RESIDENCY, 0) == ("", False, False)


def test_collect_retries_unindexed_pages_with_live_crawl():
    vendor = Vendor("Contoso", (VendorPage("", RESIDENCY.url), VendorPage("", ENCRYPTION.url)))
    missing = PageProblem(ENCRYPTION.url, "not indexed", not_indexed=True)
    fake = FakeClient(
        {
            (RESIDENCY.url, False): RESIDENCY,
            (ENCRYPTION.url, False): missing,
            (ENCRYPTION.url, True): ENCRYPTION,
        }
    )
    rows = collect(fake, vendor, ("Is data encrypted?",), live_crawl=True)
    assert rows == [[RESIDENCY, ENCRYPTION]]
    assert (ENCRYPTION.url, "Is data encrypted?", True) in fake.calls
    assert (RESIDENCY.url, "Is data encrypted?", True) not in fake.calls


def test_collect_without_live_crawl_keeps_the_problem():
    vendor = Vendor("Contoso", (VendorPage("", ENCRYPTION.url),))
    missing = PageProblem(ENCRYPTION.url, "not indexed", not_indexed=True)
    fake = FakeClient({(ENCRYPTION.url, False): missing})
    assert collect(fake, vendor, ("q",)) == [[missing]]
    assert all(not live for _, _, live in fake.calls)


def test_build_results_counts_passages_and_problems():
    problem = PageProblem(CAREERS.url, "blocked")
    results = build_results(
        ("Is data encrypted at rest?", "Where is data stored?"),
        [[ENCRYPTION, RESIDENCY, problem], [ENCRYPTION, RESIDENCY, problem]],
    )
    assert results.passages == 4
    assert results.characters == 2 * (len(ENCRYPTION.content) + len(RESIDENCY.content))
    assert results.problems == (problem,)
    assert results.answers[0].sources[0].page == ENCRYPTION
    assert results.answers[1].sources[0].page == RESIDENCY
