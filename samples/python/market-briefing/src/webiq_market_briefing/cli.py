# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from azure.core.exceptions import AzureError
from openai import OpenAIError
from webiq.errors import WebIQError

from .config import load_azure_openai_settings
from .diagnostics import logger
from .queries import build_queries
from .report import build_markdown, generate_report_body
from .retrieval import retrieve_evidence

DEFAULT_TOPIC = "North American energy market"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a cited Markdown briefing with Web IQ and Azure OpenAI."
    )
    parser.add_argument("topic", nargs="?", default=DEFAULT_TOPIC)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--query", action="append", dest="queries")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
    )
    parser.add_argument("--region", default="US")
    parser.add_argument(
        "--webiq-auth",
        choices=("api-key", "entra"),
        default="api-key",
        help="Web IQ authentication; entra uses CertificateCredential (default: api-key).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("market-briefing.md"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _validate_args(args)

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    logger.setLevel(logging.INFO)
    webiq_api_key = (
        _required_environment_variable("WEBIQ_API_KEY") if args.webiq_auth == "api-key" else None
    )
    azure_openai_api_key = _required_environment_variable("AZURE_OPENAI_API_KEY")
    azure_openai = load_azure_openai_settings(args.config)
    end_date = date.today()
    start_date = end_date - timedelta(days=args.lookback_days)
    queries = args.queries or build_queries(args.topic, args.lookback_days, end_date)

    print(f"Retrieving evidence for {len(queries)} queries...")
    evidence = retrieve_evidence(
        api_key=webiq_api_key,
        queries=queries,
        max_results=args.max_results,
        region=args.region,
        auth_mode=args.webiq_auth,
    )
    print(f"Retrieved {len(evidence)} unique sources. Generating report...")

    body = generate_report_body(
        azure_openai_api_key=azure_openai_api_key,
        azure_openai_endpoint=azure_openai.endpoint,
        azure_openai_api_version=azure_openai.api_version,
        deployment=azure_openai.deployment,
        topic=args.topic,
        start_date=start_date,
        end_date=end_date,
        evidence=evidence,
    )
    markdown = build_markdown(
        topic=args.topic,
        start_date=start_date,
        end_date=end_date,
        body=body,
        evidence=evidence,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")
    print(f"Wrote {args.output.resolve()}")
    return 0


def entrypoint() -> None:
    try:
        raise SystemExit(main())
    except (WebIQError, OpenAIError, AzureError) as error:
        logger.error("Request failed: exception=%s", type(error).__name__)
        print(
            "Error: A service or authentication request failed. Check the status, error code, "
            "and correlation IDs above; verify credentials and service access before retrying.",
            file=sys.stderr,
        )
        raise SystemExit(1) from error
    except (ValueError, RuntimeError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


def _validate_args(args: argparse.Namespace) -> None:
    if args.lookback_days < 1:
        raise ValueError("--lookback-days must be at least 1.")
    if not 1 <= args.max_results <= 10:
        raise ValueError("--max-results must be between 1 and 10.")
    if not args.topic.strip():
        raise ValueError("The topic cannot be empty.")


def _required_environment_variable(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Set the {name} environment variable before running.")
    return value
