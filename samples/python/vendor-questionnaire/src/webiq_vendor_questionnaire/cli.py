# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Command-line entry point for the vendor questionnaire sample."""

import argparse
import os
import sys
from pathlib import Path

from .client import BrowseClient, BrowseError
from .config import check_request_count, load_questions, load_vendor
from .display import render
from .questionnaire import build_results, collect


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="webiq-vendor-questionnaire",
        description=(
            "Pre-fill a vendor security questionnaire from the vendor's public pages "
            "with Web IQ Browse passages. No model is called and nothing is saved."
        ),
    )
    parser.add_argument(
        "--vendor",
        type=Path,
        default=Path("vendor.json"),
        help="Vendor name and public page URLs (default: vendor.json).",
    )
    parser.add_argument(
        "--questions",
        type=Path,
        default=Path("questions.json"),
        help="Questionnaire questions (default: questions.json).",
    )
    parser.add_argument(
        "--live-crawl",
        action="store_true",
        help="For pages that aren't in the Web IQ index, request a live crawl (liveCrawl fallback).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if not sys.stdout.isatty():
            raise ValueError(
                "Run in an interactive terminal. Redirecting or piping the output is "
                "disabled; this sample does not save results."
            )
        api_key = os.environ.get("WEBIQ_API_KEY")
        if not api_key or not api_key.strip():
            raise ValueError("Set WEBIQ_API_KEY to a Web IQ API key with Browse access.")
        vendor = load_vendor(args.vendor)
        questions = load_questions(args.questions)
        check_request_count(vendor, questions)

        with BrowseClient(api_key) as client:
            rows = collect(
                client,
                vendor,
                questions,
                live_crawl=args.live_crawl,
                progress=lambda message: print(message, file=sys.stderr),
            )
        results = build_results(questions, rows)

        # To draft answers with your own model, do it here, in this run, for each answer:
        #   prompt = build_model_input(answer.question, answer.pages)
        #   draft = <call your model with prompt>
        #   if check_model_answer(draft, answer.pages): rewrite or drop the draft.
        # Show the draft next to its numbered sources and links. Don't store passages or drafts.
        render(vendor, results, sys.stdout)
        return 0
    except (BrowseError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 1


def entrypoint() -> None:
    raise SystemExit(main())
