# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import logging
import stat
from unittest.mock import patch

import pytest

from app.perf_summary import summarize
from app.performance import PerformanceLogger, PerformanceTrace


def read_records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_allowlist_hashes_ids_and_never_records_content(tmp_path):
    path = tmp_path / "private" / "performance.jsonl"
    logger = PerformanceLogger(path)
    logger.start()
    trace = PerformanceTrace(logger)
    trace.mark("first_audio", turn=1, response_id="SECRET_ID", duration_ms=12.34567)
    trace.mark("error", code="SECRET_EXCEPTION")
    trace.mark("first_text", transcript="SECRET_TRANSCRIPT")
    trace.mark("tool_request", query="SECRET_QUESTION", audio="SECRET_BASE64", headers="SECRET_KEY")
    trace.mark("first_audio", source_url="https://private.example/SECRET_URL")
    logger.close()
    records = read_records(path)
    assert len(records) == 2
    assert "SECRET" not in path.read_text()
    assert records[0]["duration_ms"] == 12.346
    assert records[0]["response_id"] != "SECRET_ID"
    assert len(records[0]["response_id"]) == 24
    assert records[1]["code"] == "other_error"
    assert logger.status()["error_code"] == "schema_rejected"
    assert logger.status()["dropped"] == 3
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_rotation_bounds_retention_and_preserves_private_permissions(tmp_path):
    path = tmp_path / "private" / "performance.jsonl"
    logger = PerformanceLogger(path, max_bytes=1000, backups=2)
    logger.start()
    trace = PerformanceTrace(logger)
    for _ in range(100):
        trace.mark("session_open")
    logger.close()
    files = list(path.parent.iterdir())
    assert {p.name for p in files} == {"performance.jsonl", "performance.jsonl.1", "performance.jsonl.2"}
    assert all(p.stat().st_size <= 1000 and stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files)
    assert logger.status()["dropped"] == 0


def test_queue_is_bounded_and_drops_are_visible_without_blocking(tmp_path):
    logger = PerformanceLogger(tmp_path / "private" / "performance.jsonl", capacity=1)
    trace = PerformanceTrace(logger)
    trace.mark("session_open")
    trace.mark("ready")
    assert logger.queue.qsize() == 1
    assert logger.status() == {"enabled": True, "state": "warning", "dropped": 1, "error_code": "queue_full"}
    assert not logger.path.exists()


def test_write_failure_is_visible_and_does_not_dump_exception_or_record(tmp_path, caplog):
    logger = PerformanceLogger(tmp_path / "private" / "performance.jsonl")
    logger.start()
    trace = PerformanceTrace(logger)
    with (
        patch.object(logger.handler, "shouldRollover", side_effect=OSError("SECRET_WRITE_ERROR")),
        caplog.at_level(logging.WARNING),
    ):
        trace.mark("session_open")
        logger.queue.join()
    logger.close()
    assert logger.status()["error_code"] == "write_failed"
    assert logger.status()["dropped"] == 1
    assert "SECRET" not in caplog.text


def test_startup_failure_does_not_follow_symlink(tmp_path):
    target = tmp_path / "untouched"
    target.write_text("PRIVATE_EXISTING_FILE")
    directory = tmp_path / "private"
    directory.mkdir()
    path = directory / "performance.jsonl"
    path.symlink_to(target)
    logger = PerformanceLogger(path)
    logger.start()
    PerformanceTrace(logger).mark("session_open")
    logger.close()
    assert logger.status()["error_code"] == "startup_failed"
    assert target.read_text() == "PRIVATE_EXISTING_FILE"


def test_disabled_logging_creates_no_file_or_queue_entries(tmp_path):
    logger = PerformanceLogger(tmp_path / "private" / "performance.jsonl", enabled=False)
    logger.start()
    PerformanceTrace(logger).mark("session_open")
    logger.close()
    assert not logger.path.parent.exists()
    assert logger.queue.empty()
    assert logger.status()["state"] == "disabled"


def test_playback_diagnostics_are_content_free_bounded_and_enum_checked(tmp_path):
    logger = PerformanceLogger(tmp_path / "private" / "performance.jsonl")
    trace = PerformanceTrace(logger)
    trace.first("client_playback_error", turn=1, response_id="r", code="playback_stalled",
                browser_queue_ms=89_000, browser_received_audio_ms=90_000,
                browser_audio_context_ms=1000, browser_clock_stall_ms=5000, context_state="running")
    trace.first("client_playback_error", turn=1, response_id="r", code="playback_stalled")
    assert logger.queue.qsize() == 1
    row = logger.queue.get_nowait()
    assert row["code"] == "playback_stalled" and row["browser_queue_ms"] == 89_000
    trace.mark("client_playback_error", context_state="SECRET")
    assert logger.queue.empty()
    assert logger.status()["error_code"] == "schema_rejected"


def test_trace_duration_uses_monotonic_clock_and_missing_is_not_zero(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.performance.time.perf_counter", lambda: clock[0])
    logger = PerformanceLogger(tmp_path / "private" / "performance.jsonl")
    trace = PerformanceTrace(logger)
    assert trace.since("speech_stopped", 1) is None
    with trace.span("voice_connect"):
        clock[0] = 100.250
    trace.mark("turn_start", turn=1, input_kind="speech")
    clock[0] = 101.0
    assert trace.since("turn_start", 1) == 750
    assert trace.first("first_audio", turn=1, response_id="r1")
    assert not trace.first("first_audio", turn=1, response_id="r1")
    records = list(logger.queue.queue)
    assert [r["event"] for r in records] == ["stage_start", "stage_end", "turn_start", "first_audio"]
    assert records[1]["duration_ms"] == 250
    assert records[1]["outcome"] == "success"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, "SECRET", True])
def test_invalid_metrics_cannot_reach_json(tmp_path, value):
    logger = PerformanceLogger(tmp_path / "private" / "performance.jsonl")
    trace = PerformanceTrace(logger)
    trace.mark("first_audio", duration_ms=value)
    assert logger.queue.empty()
    assert logger.status()["error_code"] == "schema_rejected"


def test_summary_reports_actual_overlap_and_keeps_failed_turn_missing():
    rows = [
        {"event": "stage_start", "stage": "mcp_setup", "monotonic_ms": 0},
        {"event": "stage_start", "stage": "agent_lookup", "monotonic_ms": 20},
        {"event": "stage_end", "stage": "mcp_setup", "monotonic_ms": 200, "duration_ms": 200},
        {"event": "stage_end", "stage": "agent_lookup", "monotonic_ms": 500, "duration_ms": 480},
        {"event": "turn_start", "turn": 1, "input_kind": "speech", "monotonic_ms": 1000},
        {"event": "error", "turn": 1, "code": "voice_disconnected", "monotonic_ms": 2000},
    ]
    result = summarize([{"session_id": "test", "turn": 0, **row} for row in rows])[0]
    assert result["mcp_agent_setup_overlap_ms"] == 180
    assert result["mcp_voice_connect_overlap_ms"] is None
    assert result["ready_ms"] is None
    assert result["azure_auth_ms"] is None
    turn = result["turns"][0]
    assert turn["native_speech_stop_to_server_audio_ms"] is None
    assert turn["browser_submit_to_playback_ms"] is None
    assert not turn["browser_playback_observed"]
    assert turn["errors"] == [{"code": "voice_disconnected"}]


def test_summary_totals_auth_acquisitions_without_changing_parent_durations():
    rows = [
        {"event": "stage_start", "stage": "azure_auth", "monotonic_ms": 0},
        {"event": "stage_end", "stage": "azure_auth", "duration_ms": 150},
        {"event": "stage_end", "stage": "azure_auth", "duration_ms": 100},
        {"event": "stage_end", "stage": "agent_lookup", "duration_ms": 500},
    ]
    result = summarize([{"session_id": "test", "turn": 0, **row} for row in rows])[0]
    assert result["azure_auth_ms"] == 250
    assert result["setup_ms"]["agent_lookup"] == 500


def test_cache_hit_is_distinct_from_missing_remote_lookup(tmp_path):
    logger = PerformanceLogger(tmp_path / "private" / "performance.jsonl")
    trace = PerformanceTrace(logger)
    with trace.span("agent_cache"):
        pass
    rows = list(logger.queue.queue)
    assert logger.status()["error_code"] is None
    result = summarize(rows)[0]
    assert result["setup_ms"]["agent_cache"] >= 0
    assert result["setup_ms"]["agent_lookup"] is None


def test_summary_direct_turn_has_one_pass_and_no_invented_search_or_citation_time():
    rows = [
        {"event": "turn_start", "input_kind": "text"},
        {"event": "model_requested", "stage": "respond"},
        {"event": "first_audio", "stage": "respond", "since_model_request_ms": 30, "since_turn_ms": 40},
        {"event": "model_done", "stage": "respond"},
    ]
    turn = summarize([{"session_id": "test", "turn": 1, **row} for row in rows])[0]["turns"][0]
    assert turn["model_passes"] == 1
    assert turn["search_selected"] is False
    assert turn["answer_model_to_first_audio_ms"] == 30
    assert turn["webiq_round_trip_ms"] is None
    assert turn["search_model_ms"] is None
    assert turn["citation_model_ms"] is None


@pytest.mark.parametrize("voice_start,expected", [(100, 100), (250, 0)])
def test_summary_measures_mcp_voice_overlap_without_double_counting(voice_start, expected):
    rows = [
        {"event": "stage_start", "stage": "mcp_setup", "monotonic_ms": 0},
        {"event": "stage_end", "stage": "mcp_setup", "monotonic_ms": 200, "duration_ms": 200},
        {"event": "stage_start", "stage": "voice_connect", "monotonic_ms": voice_start},
        {"event": "stage_end", "stage": "voice_connect", "monotonic_ms": 400, "duration_ms": 400 - voice_start},
    ]
    result = summarize([{"session_id": "test", "turn": 0, **row} for row in rows])[0]
    assert result["mcp_voice_connect_overlap_ms"] == expected
    assert result["mcp_agent_setup_overlap_ms"] is None
