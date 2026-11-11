# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Send each question to each page, then pick the best sources for each answer."""

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .client import BrowseClient, Page, PageProblem
from .config import Vendor

MAX_SOURCES_PER_ANSWER = 2
MIN_MATCHED_WORDS = 2
SECOND_SOURCE_RATIO = 0.75
WORKERS = 4

STOPWORDS = {
    "about", "after", "and", "any", "are", "before", "can", "does", "for", "from",
    "has", "have", "how", "into", "its", "our", "the", "their", "them", "there",
    "they", "this", "that", "used", "use", "uses", "what", "when", "where", "which",
    "who", "will", "with", "would", "you", "your",
    # Words in almost every questionnaire question, which don't help pick a page.
    "customer", "customers", "data", "information", "vendor", "vendors",
}
SUFFIXES = ("ations", "ation", "ions", "ion", "ing", "ed", "es", "s", "age")


@dataclass(frozen=True)
class Source:
    page: Page
    score: float


@dataclass(frozen=True)
class Answer:
    question: str
    sources: tuple[Source, ...]
    # Every page that returned passages for this question: the input for a model.
    pages: tuple[Page, ...]


@dataclass(frozen=True)
class Results:
    answers: tuple[Answer, ...]
    problems: tuple[PageProblem, ...]
    passages: int
    characters: int


def collect(
    client: BrowseClient,
    vendor: Vendor,
    questions: tuple[str, ...],
    *,
    live_crawl: bool = False,
    progress: Callable[[str], None] | None = None,
) -> list[list[Page | PageProblem]]:
    """Return results[question][page]. Indexed pages run in parallel; live crawls one at a time."""
    pairs = [(q, p) for q in range(len(questions)) for p in range(len(vendor.pages))]
    results: list[list[Page | PageProblem | None]] = [
        [None] * len(vendor.pages) for _ in questions
    ]

    def run(pair: tuple[int, int], crawl: bool) -> None:
        question, page = pair
        results[question][page] = client.browse(
            vendor.pages[page].url, questions[question], live_crawl=crawl
        )

    if progress:
        progress(f"Sending {len(pairs)} Browse requests ({len(questions)} questions x "
                 f"{len(vendor.pages)} pages)...")
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        # Check requests as they finish, so a failure that stops the run isn't hidden
        # behind a slow earlier request.
        for future in as_completed([pool.submit(run, pair, False) for pair in pairs]):
            future.result()
    except BaseException:
        # A failure that stops the run (or Ctrl+C) cancels the queued requests.
        client.stop()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown()

    if live_crawl:
        missing = [
            pair for pair in pairs
            if isinstance(results[pair[0]][pair[1]], PageProblem)
            and results[pair[0]][pair[1]].not_indexed
        ]
        if missing and progress:
            progress(f"Requesting live crawls for {len(missing)} requests, one at a time...")
        # Live crawl has a lower limit than indexed requests, so these run one at a time.
        for pair in missing:
            run(pair, True)

    return [[item for item in row if item is not None] for row in results]


def build_results(
    questions: tuple[str, ...], results: list[list[Page | PageProblem]]
) -> Results:
    answers: list[Answer] = []
    problems: dict[str, PageProblem] = {}
    passages = 0
    characters = 0
    for question, row in zip(questions, results):
        pages = tuple(item for item in row if isinstance(item, Page) and item.content.strip())
        for item in row:
            if isinstance(item, PageProblem):
                problems.setdefault(item.url, item)
        passages += len(pages)
        characters += sum(len(page.content) for page in pages)
        answers.append(Answer(question, rank_sources(question, pages), pages))
    return Results(tuple(answers), tuple(problems.values()), passages, characters)


def rank_sources(question: str, pages: tuple[Page, ...]) -> tuple[Source, ...]:
    """Rank pages by the question's distinctive words found in their passages.

    A simple keyword match, so the sample runs without a model. A page needs at least
    two of the question's words (one if the question has only one). Words found on
    every page still count toward that, but they score less, so they help less in
    choosing between pages. A second source is kept only when it scores close to the
    first.
    """
    terms = keywords(question)
    if not terms or not pages:
        return ()
    needed = min(MIN_MATCHED_WORDS, len(terms))
    page_terms = [keywords(page.content) for page in pages]
    document_frequency = {term: sum(term in found for found in page_terms) for term in terms}
    scored: list[Source] = []
    for page, found in zip(pages, page_terms):
        matched = terms & found
        if len(matched) < needed:
            continue
        scored.append(Source(page, sum(1 / document_frequency[term] for term in matched)))
    scored.sort(key=lambda source: source.score, reverse=True)
    if not scored:
        return ()
    top = scored[0].score
    return tuple(
        source
        for index, source in enumerate(scored[:MAX_SOURCES_PER_ANSWER])
        if index == 0 or source.score >= SECOND_SOURCE_RATIO * top
    )


def best_quote(question: str, page: Page, limit: int) -> tuple[str, bool, bool]:
    """Return the window of at most `limit` characters with the most question words.

    Returns the quote and whether text was cut before and after it. The window stays
    inside one passage segment, starts and ends on whole words, and skips the page title
    when the passage starts with it.
    """
    if limit <= 0:
        return "", False, False
    terms = keywords(question)
    content = page.content
    skip = len(page.title) if page.title and content.startswith(page.title) else 0
    best: tuple[int, int, int] | None = None
    for segment in re.finditer(r"(?:(?!\s\.\.\.\s).)+", content, flags=re.DOTALL):
        words = [
            (match.start(), match.end(), stem(match.group().lower()))
            for match in re.finditer(r"[A-Za-z0-9][A-Za-z0-9'-]*", segment.group())
        ]
        words = [(segment.start() + s, segment.start() + e, w) for s, e, w in words]
        words = [word for word in words if word[0] >= skip]
        end_index = 0
        for start_index, (start, _, _) in enumerate(words):
            end_index = max(end_index, start_index)
            while end_index + 1 < len(words) and words[end_index + 1][1] - start <= limit:
                end_index += 1
            if words[end_index][1] - start > limit:
                continue
            hits = len(terms & {word[2] for word in words[start_index : end_index + 1]})
            if best is None or hits > best[0]:
                best = (hits, start, words[end_index][1])
    if best is None:
        return "", False, False
    _, start, end = best
    # Keep closing punctuation right after the last word when it fits.
    while end < len(content) and content[end] in ".!?;:,)" and end + 1 - start <= limit:
        end += 1
    return content[start:end], start > 0, end < len(content.rstrip())


def keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {stem(word) for word in words if len(word) >= 3 and word not in STOPWORDS}


def stem(word: str) -> str:
    for suffix in SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word
