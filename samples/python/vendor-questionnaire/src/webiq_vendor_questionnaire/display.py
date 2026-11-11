# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Terminal output: short quotes, links, and how much Browse returned."""

import unicodedata
from datetime import datetime, timezone
from typing import TextIO

from .config import Vendor
from .questionnaire import Results, best_quote

# The Web IQ Terms of Use limit displayed content to 200 characters per source, or less if
# the website owner sets a lower maximum (which this sample can't detect). The sample applies
# the limit to the whole run: a page cited in several answers shares its 200 characters.
DISPLAY_CHARACTER_LIMIT = 200
MIN_QUOTE_CHARACTERS = 60
ELLIPSIS = "..."


def render(
    vendor: Vendor, results: Results, output: TextIO, *, now: datetime | None = None
) -> int:
    """Print the pre-filled answers and return the number of characters shown."""
    now = now if now is not None else datetime.now(timezone.utc)
    labels = {page.url: page.title for page in vendor.pages}
    uses: dict[str, int] = {}
    for answer in results.answers:
        for source in answer.sources:
            uses[source.page.url] = uses.get(source.page.url, 0) + 1
    remaining = {url: DISPLAY_CHARACTER_LIMIT for url in uses}

    print(
        f"Vendor: {terminal_text(vendor.name)} ({len(vendor.pages)} pages, "
        f"{len(results.answers)} questions)",
        file=output,
    )
    print(
        "Pre-filled answers for human review. Check each source before you rely on it.",
        file=output,
    )

    shown_total = 0
    for index, answer in enumerate(results.answers, start=1):
        print(f"\n[{index}] {terminal_text(answer.question)}", file=output)
        if not answer.sources:
            print("  Not found on these pages.", file=output)
            continue
        for number, source in enumerate(answer.sources, start=1):
            page = source.page
            share = max(MIN_QUOTE_CHARACTERS, DISPLAY_CHARACTER_LIMIT // uses[page.url])
            available = min(share, remaining[page.url])
            quote, cut_before, cut_after = (
                best_quote(answer.question, page, available)
                if available >= MIN_QUOTE_CHARACTERS
                else ("", False, False)
            )
            quote = terminal_text(quote)
            remaining[page.url] -= len(quote)
            shown_total += len(quote)

            title = terminal_text(page.title) or terminal_text(labels.get(page.url, ""))
            print(f"  Source {number}: {title or '(title not provided)'}", file=output)
            if quote:
                prefix = ELLIPSIS if cut_before else ""
                suffix = ELLIPSIS if cut_after else ""
                print(f'    "{prefix}{quote}{suffix}"', file=output)
            else:
                print(
                    "    [No quote: this run already showed the most text allowed from this "
                    "page. Open the page to read it.]",
                    file=output,
                )
            print(f"    {page.url}", file=output)
            print(
                f"    {len(page.content):,} characters returned, ready for your model; "
                f"{len(quote):,} shown",
                file=output,
            )
            print(
                f"    Crawled: {timestamp_label(page.crawled_at, now)}; "
                f"last updated: {timestamp_label(page.last_updated_at, now)}",
                file=output,
            )

    if results.problems:
        print("\nPages that returned no passages for some questions:", file=output)
        for problem in results.problems:
            print(f"  {problem.url}", file=output)
            print(f"    {problem.reason}", file=output)

    pages_with_content = len({page.url for answer in results.answers for page in answer.pages})
    print(
        f"\nBrowse returned {results.passages} passages ({results.characters:,} characters) "
        f"from {pages_with_content} page(s), ready as model input in this run; "
        f"{shown_total:,} characters are shown here.",
        file=output,
    )
    print(
        "Nothing was saved. Read the original pages before you answer the questionnaire.",
        file=output,
    )
    return shown_total


def timestamp_label(value: str | None, now: datetime) -> str:
    if not value:
        return "not provided"
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "unrecognized timestamp"
    if instant.tzinfo is None or instant.utcoffset() is None:
        return f"{terminal_text(value)} (no time zone; age unknown)"
    try:
        instant = instant.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return "unrecognized timestamp"
    stamp = instant.isoformat().replace("+00:00", "Z")
    age = (now - instant).total_seconds()
    if age < 0:
        return f"{stamp} (future timestamp)"
    for seconds, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if age >= seconds:
            count = int(age // seconds)
            return f"{stamp} ({count} {unit}{'' if count == 1 else 's'} ago)"
    return f"{stamp} (less than 1 minute ago)"


def terminal_text(value: str) -> str:
    """Drop control characters and collapse whitespace to a single line."""
    text = "".join(
        character
        for character in value
        if not unicodedata.category(character).startswith("C") or character in "\n\t"
    )
    return " ".join(text.split())
