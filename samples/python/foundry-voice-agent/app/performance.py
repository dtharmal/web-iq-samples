# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Content-free local milestones. Disk I/O runs on one bounded, background writer."""

import asyncio
import hashlib
import json
import logging
import math
import os
import queue
import re
import stat
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

EVENTS = frozenset(["session_open", "session_end", "stage_start", "stage_end", "ready", "turn_start", "turn_end", "speech_stopped", "input_committed", "transcription_ready", "model_requested", "model_sent", "model_created", "function_ready", "model_done", "tool_request", "tool_dispatched", "tool_returned", "tool_finished", "tool_output_sent", "first_text", "first_audio", "first_audio_forwarded", "notification_sent", "client_audio_received", "client_playback_started", "client_playback_unavailable", "client_ui_render", "playback_ack", "stale_event", "error"])
STAGES = frozenset(["mcp_setup", "mcp_initialize", "mcp_discovery", "agent_lookup", "voice_connect", "input_submit", "search", "cite", "answer", "greeting", "activity_ui", "evidence_ui", "answer_ui"])
OUTCOMES = frozenset(["success", "failed", "cancelled", "discarded", "ended", "disconnected", "unavailable"])
ERROR_CODES = frozenset(["agent_not_configured", "agent_config", "agent_version", "azure_auth", "azure_http", "azure_connection", "session_timeout", "session_error", "voice_timeout", "invalid_audio", "invalid_input", "browser_disconnected", "voice_closed", "voice_disconnected", "turn_limit", "audio_rate", "message_rate", "not_ready", "empty_text", "foundry_event", "voice_config", "unexpected_response", "voice_audio", "audio_limit", "tool_protocol", "tool_loop", "tool_arguments", "webiq_failure", "webiq_schema", "webiq_size", "webiq_timeout", "webiq_mcp", "webiq_not_configured", "invalid_query", "webiq_tool", "webiq_http_401", "webiq_http_403", "webiq_http_429", "no_evidence", "transcription_failed", "response_incomplete", "session_busy", "other_error"])
METRICS = frozenset(["duration_ms", "since_turn_ms", "since_speech_stop_ms", "since_model_request_ms", "browser_submit_to_receipt_ms", "browser_submit_to_playback_ms", "browser_receipt_to_playback_ms", "browser_schedule_lead_ms", "browser_output_latency_ms", "browser_render_ms"])
ERROR_KINDS = frozenset({
    "TimeoutError", "ClientConnectionError", "ServerTimeoutError", "ClientConnectionResetError",
    "ClientOSError", "ConnectionResetError", "RuntimeError", "ValueError", "ExceptionGroup", "Other",
})
FIELDS = METRICS | {"turn", "stage", "outcome", "input_kind", "response_id", "call_id", "source_count", "code", "error_kind"}
EVENTS = EVENTS | {"client_playback_error", "client_playback_buffered", "answer_playback_complete"}
STAGES = STAGES | {"citation_ui", "respond", "azure_auth", "agent_cache"}
ERROR_CODES = ERROR_CODES | {"playback_suspended", "playback_stalled", "playback_limit", "playback_invalid",
                             "playback_unavailable", "citation_failed", "citation_timeout", "citation_unavailable"}
METRICS = METRICS | {"browser_queue_ms", "browser_audio_context_ms", "browser_clock_stall_ms",
                     "browser_received_audio_ms"}
FIELDS = FIELDS | METRICS | {"context_state"}


class PrivateRotatingHandler(RotatingFileHandler):
    def __init__(self, path: Path, failure, max_bytes: int, backups: int):
        self.failure = failure
        if path.parent.is_symlink():
            raise ValueError("Unsafe log directory")
        path.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
        os.chmod(path.parent, 0o700)
        for candidate in [path, *(Path(f"{path}.{n}") for n in range(1, backups + 1))]:
            if candidate.is_symlink():
                raise ValueError("Unsafe log file")
            if candidate.exists():
                info = candidate.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
                    raise ValueError("Unexpected existing log file")
                candidate.chmod(0o600)
        super().__init__(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        self.setFormatter(logging.Formatter("%(message)s"))

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Unsafe log file")
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, "a", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise

    def handleError(self, record):
        # Never print the record or an exception/traceback; health exposes this fixed code.
        self.failure("write_failed")


class PerformanceLogger:
    def __init__(self, path: Path, *, enabled=True, max_bytes=1_048_576, backups=2, capacity=256):
        self.path = path
        self.enabled = enabled
        self.max_bytes = max_bytes
        self.backups = backups
        self.queue: queue.Queue[dict] = queue.Queue(maxsize=capacity)
        self.handler: PrivateRotatingHandler | None = None
        self.thread: threading.Thread | None = None
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.dropped = 0
        self.error_code: str | None = None

    def failure(self, code: str, *, dropped=1) -> None:
        with self.lock:
            self.dropped += dropped
            self.error_code = code

    def status(self) -> dict:
        with self.lock:
            return {
                "enabled": self.enabled,
                "state": "disabled" if not self.enabled else "warning" if self.error_code else "ok",
                "dropped": self.dropped, "error_code": self.error_code,
            }

    def start(self) -> None:
        if not self.enabled:
            return
        try:
            self.handler = PrivateRotatingHandler(self.path, self.failure, self.max_bytes, self.backups)
        except (OSError, ValueError):
            self.failure("startup_failed", dropped=0)
        self.thread = threading.Thread(target=self._write_loop, name="local-performance-log", daemon=True)
        self.thread.start()

    def record(self, event: str, session_id: str, monotonic_ms: float, **fields) -> None:
        if not self.enabled:
            return
        try:
            if event not in EVENTS or not re.fullmatch(r"[0-9a-f]{32}", session_id):
                raise ValueError
            if not fields.keys() <= FIELDS or not math.isfinite(monotonic_ms) or monotonic_ms < 0:
                raise ValueError
            safe = {}
            for key, value in fields.items():
                if value is None:
                    continue
                if key in METRICS:
                    if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 900_000:
                        raise ValueError
                    safe[key] = round(value, 3)
                elif key in {"turn", "source_count"}:
                    if type(value) is not int or not 0 <= value <= (40 if key == "turn" else 5):
                        raise ValueError
                    safe[key] = value
                elif key in {"response_id", "call_id"}:
                    if not isinstance(value, str) or not 1 <= len(value) <= 200:
                        raise ValueError
                    # Correlatable aliases prevent an unexpected SDK identifier from recording content.
                    safe[key] = hashlib.sha256(value.encode()).hexdigest()[:24]
                elif key == "code":
                    safe[key] = value if value in ERROR_CODES else "other_error"
                elif key == "error_kind":
                    safe[key] = value if value in ERROR_KINDS else "Other"
                elif key == "context_state":
                    if value not in {"running", "suspended", "closed", "interrupted"}:
                        raise ValueError
                    safe[key] = value
                else:
                    allowed = STAGES if key == "stage" else OUTCOMES if key == "outcome" else {"text", "speech", "none"}
                    if value not in allowed:
                        raise ValueError
                    safe[key] = value
            record = {
                "schema": 1, "utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "session_id": session_id, "monotonic_ms": round(monotonic_ms, 3), "event": event, **safe,
            }
        except (ValueError, TypeError):
            self.failure("schema_rejected")
            return
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.failure("queue_full")

    def _write_loop(self) -> None:
        reported = None
        while not self.stopping.is_set() or not self.queue.empty():
            state = self.status()
            if state["error_code"] and state["error_code"] != reported:
                reported = state["error_code"]
                logging.getLogger("webiq_demo").warning(
                    "Local performance logging degraded: %s; dropped=%d", reported, state["dropped"],
                )
            try:
                record = self.queue.get(timeout=.25)
            except queue.Empty:
                continue
            try:
                if self.handler:
                    line = json.dumps(record, separators=(",", ":"), allow_nan=False)
                    self.handler.handle(logging.LogRecord("performance", logging.INFO, "", 0, line, (), None))
                else:
                    self.failure("startup_failed")
            except (OSError, ValueError):
                self.failure("write_failed")
            finally:
                self.queue.task_done()
        if self.handler:
            self.handler.close()

    def close(self) -> None:
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                self.failure("shutdown_timeout", dropped=self.queue.qsize())
                logging.getLogger("webiq_demo").warning("Local performance logging: shutdown_timeout; pending events may be lost.")


class PerformanceTrace:
    def __init__(self, logger: PerformanceLogger | None = None):
        self.logger = logger
        self.session_id = uuid4().hex
        self.started = time.perf_counter()
        self.anchors: dict[tuple, float] = {}
        self.once: set[tuple] = set()

    def mark(self, event: str, *, turn=0, stage=None, **fields) -> None:
        now = time.perf_counter()
        self.anchors.setdefault((event, turn, stage), now)
        if self.logger:
            self.logger.record(
                event, self.session_id, (now - self.started) * 1000, turn=turn, stage=stage, **fields,
            )

    def first(self, event: str, *, turn=0, stage=None, response_id=None, call_id=None, **fields) -> bool:
        key = (event, turn, stage, response_id, call_id)
        if key in self.once:
            return False
        self.once.add(key)
        self.mark(event, turn=turn, stage=stage, response_id=response_id, call_id=call_id, **fields)
        return True

    def recorded(self, event: str, turn: int, stage=None, response_id=None, call_id=None) -> bool:
        return (event, turn, stage, response_id, call_id) in self.once

    def since(self, event: str, turn=0, stage=None) -> float | None:
        stamp = self.anchors.get((event, turn, stage))
        return (time.perf_counter() - stamp) * 1000 if stamp is not None else None

    @contextmanager
    def span(self, stage: str, *, turn=0):
        self.mark("stage_start", turn=turn, stage=stage)
        start = time.perf_counter()
        outcome = "success"
        try:
            yield
        except BaseException as exc:
            outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            raise
        finally:
            self.mark("stage_end", turn=turn, stage=stage, outcome=outcome,
                      duration_ms=(time.perf_counter() - start) * 1000)
