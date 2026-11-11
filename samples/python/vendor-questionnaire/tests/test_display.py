# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import io
import re
from datetime import datetime, timezone

from webiq_vendor_questionnaire.client import Page, PageProblem
from webiq_vendor_questionnaire.config import Vendor, VendorPage
from webiq_vendor_questionnaire.display import (
    DISPLAY_CHARACTER_LIMIT,
    render,
    terminal_text,
    timestamp_label,
)
from webiq_vendor_questionnaire.questionnaire import build_results

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
SYNTHETIC = "Synthetic customer data region storage text for demonstration only. " * 60
PAGE = Page(
    "https://contoso.example/trust",
    "Contoso Trust Center",
    SYNTHETIC,
    None,
    "2026-11-05T12:00:00Z",
)
VENDOR = Vendor("Contoso", (VendorPage("Trust", PAGE.url), VendorPage("", "https://contoso.example/x")))


def run(questions, rows):
    output = io.StringIO()
    shown = render(VENDOR, build_results(questions, rows), output, now=NOW)
    return output.getvalue(), shown


def quotes(text):
    return re.findall(r'^    "(?:\.\.\.)?(.*?)(?:\.\.\.)?"$', text, flags=re.MULTILINE)


def test_quote_is_at_most_200_characters_and_passages_are_not_printed():
    text, shown = run(("Where is customer data stored?",), [[PAGE]])
    assert len(quotes(text)) == 1
    assert 0 < shown <= DISPLAY_CHARACTER_LIMIT
    assert SYNTHETIC.strip() not in text
    assert f"{len(SYNTHETIC):,} characters returned, ready for your model; {shown:,} shown" in text
    assert "Crawled: 2026-11-05T12:00:00Z (1 day ago)" in text


def test_a_source_shares_200_characters_across_the_run():
    questions = tuple(f"Where is customer data stored by region, case {i}?" for i in range(3))
    text, shown = run(questions, [[PAGE]] * 3)
    assert len(quotes(text)) == 3
    assert shown <= DISPLAY_CHARACTER_LIMIT
    assert all(len(quote) <= DISPLAY_CHARACTER_LIMIT // 3 for quote in quotes(text))


def test_a_source_used_too_often_gets_no_more_quotes():
    questions = tuple(f"Where is customer data stored by region, case {i}?" for i in range(8))
    text, shown = run(questions, [[PAGE]] * 8)
    assert shown <= DISPLAY_CHARACTER_LIMIT
    assert "No quote: this run already showed the most text allowed" in text
    assert text.count(PAGE.url) == 8


def test_not_found_and_page_problems_are_reported():
    problem = PageProblem("https://contoso.example/x", "The page isn't in the Web IQ index.")
    text, shown = run(("Which certifications apply?",), [[problem]])
    assert "Not found on these pages." in text
    assert "The page isn't in the Web IQ index." in text
    assert shown == 0


def test_summary_line_reports_returned_and_shown_characters():
    text, shown = run(("Where is customer data stored?",), [[PAGE]])
    assert (
        f"Browse returned 1 passages ({len(SYNTHETIC):,} characters) from 1 page(s), "
        f"ready as model input in this run; {shown:,} characters are shown here."
    ) in text
    assert "Nothing was saved." in text


def test_terminal_text_removes_control_characters():
    assert terminal_text("a\x1b[31m\n b\u200b") == "a[31m b"


def test_timestamp_labels():
    assert timestamp_label(None, NOW) == "not provided"
    assert timestamp_label("2026-11-06T09:00:00Z", NOW) == "2026-11-06T09:00:00Z (3 hours ago)"
    assert "age unknown" in timestamp_label("2026-10-18T00:00:00", NOW)
    assert "future" in timestamp_label("2026-11-07T00:00:00Z", NOW)
