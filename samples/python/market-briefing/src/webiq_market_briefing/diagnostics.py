# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import logging
import re
from collections.abc import Mapping

from webiq.telemetry import TelemetryEvent

logger = logging.getLogger("webiq_market_briefing")


def safe_identifier(value: object) -> str:
    if not isinstance(value, str):
        return "-"
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", value[:128])


def error_code(body: object) -> str:
    # Keep only the machine-readable code, never messages or arbitrary bodies.
    if not isinstance(body, Mapping):
        return "-"
    error = body.get("error")
    if isinstance(error, Mapping):
        return safe_identifier(error.get("code"))
    return safe_identifier(body.get("errorCode") or body.get("code"))


def log_webiq_attempt(event: TelemetryEvent) -> None:
    level = logging.WARNING if event.error or (event.status_code or 0) >= 400 else logging.INFO
    logger.log(
        level,
        "service=webiq operation=%s status=%s request_id=%s trace_id=%s "
        "error_code=%s exception=%s",
        safe_identifier(event.path),
        event.status_code,
        safe_identifier(event.request_id),
        safe_identifier(event.trace_id),
        error_code(event.error_body),
        type(event.error).__name__ if event.error else "-",
    )
