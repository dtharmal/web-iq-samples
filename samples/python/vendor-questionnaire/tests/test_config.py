# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
from pathlib import Path

import pytest

from webiq_vendor_questionnaire.config import (
    MAX_REQUESTS,
    Vendor,
    VendorPage,
    check_request_count,
    load_questions,
    load_vendor,
)

SAMPLE_DIR = Path(__file__).resolve().parents[1]


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_checked_in_files_are_valid():
    vendor = load_vendor(SAMPLE_DIR / "vendor.json")
    questions = load_questions(SAMPLE_DIR / "questions.json")
    assert vendor.pages and questions
    assert check_request_count(vendor, questions) == len(vendor.pages) * len(questions)


def test_vendor_pages_are_loaded(tmp_path):
    path = write(
        tmp_path,
        "vendor.json",
        {"name": " Contoso ", "pages": [{"title": "Trust", "url": "https://contoso.example/trust"}]},
    )
    assert load_vendor(path) == Vendor("Contoso", (VendorPage("Trust", "https://contoso.example/trust"),))


@pytest.mark.parametrize(
    "data",
    [
        {"pages": [{"url": "https://a.example"}]},
        {"name": "A", "pages": []},
        {"name": "A", "pages": [{"url": "http://a.example"}]},
        {"name": "A", "pages": [{"url": "https://a.example/x y"}]},
        {"name": "A", "pages": [{"url": "https://a.example"}, {"url": "https://a.example"}]},
        {"name": "A", "pages": ["https://a.example"]},
    ],
)
def test_invalid_vendor_files_are_rejected(tmp_path, data):
    with pytest.raises(ValueError):
        load_vendor(write(tmp_path, "vendor.json", data))


@pytest.mark.parametrize(
    "data", [{}, {"questions": []}, {"questions": [""]}, {"questions": ["x" * 1001]}]
)
def test_invalid_question_files_are_rejected(tmp_path, data):
    with pytest.raises(ValueError):
        load_questions(write(tmp_path, "questions.json", data))


def test_missing_and_invalid_json_are_reported(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_vendor(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_questions(bad)


def test_request_count_is_capped():
    pages = tuple(VendorPage("", f"https://a.example/{i}") for i in range(10))
    questions = tuple(f"Question {i}?" for i in range(MAX_REQUESTS // 10 + 1))
    with pytest.raises(ValueError, match="at most"):
        check_request_count(Vendor("A", pages), questions)
