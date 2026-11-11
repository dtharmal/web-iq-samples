# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Load and validate the vendor pages and the question list."""

import json
from dataclasses import dataclass
from pathlib import Path

MAX_PAGES = 10
MAX_QUESTIONS = 20
# Each question is sent to each page, so this caps the billable requests in one run.
MAX_REQUESTS = 100


@dataclass(frozen=True)
class VendorPage:
    title: str
    url: str


@dataclass(frozen=True)
class Vendor:
    name: str
    pages: tuple[VendorPage, ...]


def load_vendor(path: Path) -> Vendor:
    data = _read_json(path)
    name = data.get("name")
    pages = data.get("pages")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{path.name}: set \"name\" to the vendor's name.")
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        raise ValueError(f"{path.name}: list between 1 and {MAX_PAGES} pages.")
    result: list[VendorPage] = []
    seen: set[str] = set()
    for index, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            raise ValueError(f"{path.name}: page {index} must be an object with a url.")
        url = page.get("url")
        title = page.get("title", "")
        if not isinstance(url, str) or not url.startswith("https://") or any(
            character.isspace() for character in url
        ):
            raise ValueError(f"{path.name}: page {index} needs an https:// url.")
        url = url.split("#", 1)[0]
        if url in seen:
            raise ValueError(f"{path.name}: page {index} repeats {url}.")
        if not isinstance(title, str):
            raise ValueError(f"{path.name}: page {index} title must be text.")
        seen.add(url)
        result.append(VendorPage(title=title.strip(), url=url))
    return Vendor(name=name.strip(), pages=tuple(result))


def load_questions(path: Path) -> tuple[str, ...]:
    data = _read_json(path)
    questions = data.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ValueError(f"{path.name}: list between 1 and {MAX_QUESTIONS} questions.")
    result: list[str] = []
    for index, question in enumerate(questions, start=1):
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 1_000:
            raise ValueError(
                f"{path.name}: question {index} must contain between 1 and 1,000 characters."
            )
        result.append(question.strip())
    return tuple(result)


def check_request_count(vendor: Vendor, questions: tuple[str, ...]) -> int:
    count = len(vendor.pages) * len(questions)
    if count > MAX_REQUESTS:
        raise ValueError(
            f"{len(questions)} questions x {len(vendor.pages)} pages is {count} Browse requests; "
            f"this sample allows at most {MAX_REQUESTS} per run."
        )
    return count


def _read_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"{path} was not found. Run from the sample's directory.") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"{path.name} is not valid JSON.") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return data
