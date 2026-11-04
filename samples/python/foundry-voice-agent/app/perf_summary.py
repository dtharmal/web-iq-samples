# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Summarize content-free local traces; missing stages stay null, not invented zeros."""

import json

from app.config import ROOT


def summarize(records: list[dict]) -> list[dict]:
    summaries = []
    for session in dict.fromkeys(row["session_id"] for row in records):
        rows = [row for row in records if row["session_id"] == session]

        def first(event, turn=0, stage=None, *, rows=rows):
            return next((r for r in rows if r["event"] == event and r.get("turn") == turn
                         and (stage is None or r.get("stage") == stage)), {})

        def overlap_with(stage):
            intervals = [
                (first("stage_start", stage=s).get("monotonic_ms"), first("stage_end", stage=s).get("monotonic_ms"))
                for s in ("mcp_setup", stage)
            ]
            if not all(start is not None and end is not None for start, end in intervals):
                return None
            return round(max(0, min(end for _, end in intervals) -
                             max(start for start, _ in intervals)), 3)
        summary = {
            "session_id": session,
            "ready_ms": first("ready").get("duration_ms"),
            "azure_auth_ms": round(sum(r["duration_ms"] for r in rows
                                       if r["event"] == "stage_end" and r.get("stage") == "azure_auth"), 3)
            if any(r.get("stage") == "azure_auth" for r in rows) else None,
            "mcp_agent_setup_overlap_ms": overlap_with("agent_lookup"),
            "mcp_voice_connect_overlap_ms": overlap_with("voice_connect"),
            "setup_ms": {
                stage: first("stage_end", stage=stage).get("duration_ms")
                for stage in ("mcp_setup", "mcp_initialize", "mcp_discovery", "agent_lookup", "agent_cache", "voice_connect")
            },
            "turns": [],
        }
        for turn in dict.fromkeys(r["turn"] for r in rows if r["event"] == "turn_start"):
            start = first("turn_start", turn)
            if start.get("input_kind") not in {"text", "speech"}:
                continue
            audio = first("first_audio", turn)
            browser = first("client_playback_started", turn)
            notification = [r["duration_ms"] for r in rows if r["event"] == "notification_sent"
                            and r.get("turn") == turn and "duration_ms" in r]
            render = [r["browser_render_ms"] for r in rows if r["event"] == "client_ui_render"
                      and r.get("turn") == turn and "browser_render_ms" in r]
            function_wait = {}
            for stage in ("search", "cite"):
                arguments = first("function_ready", turn, stage).get("monotonic_ms")
                requested = first("tool_request", turn, stage).get("monotonic_ms")
                function_wait[stage] = round(requested - arguments, 3) if arguments is not None and requested is not None else None
            summary["turns"].append({
                "turn": turn, "input_kind": start["input_kind"],
                "errors": [
                    {key: r[key] for key in ("code", "error_kind") if key in r}
                    for r in rows if r["event"] == "error" and r.get("turn") == turn
                ],
                "model_passes": sum(r["event"] == "model_requested" and r.get("turn") == turn for r in rows),
                "search_selected": bool(first("tool_request", turn, "search")),
                "server_turn_to_first_audio_ms": audio.get("since_turn_ms"),
                "native_speech_stop_to_server_audio_ms": audio.get("since_speech_stop_ms"),
                "speech_stop_to_commit_ms": first("input_committed", turn).get("since_speech_stop_ms"),
                "speech_stop_to_transcription_ms": first("transcription_ready", turn).get("since_speech_stop_ms"),
                "search_model_ms": first("tool_request", turn, "search").get("since_model_request_ms"),
                "webiq_round_trip_ms": first("tool_returned", turn, "search").get("duration_ms"),
                "citation_model_ms": first("tool_request", turn, "cite").get("since_model_request_ms"),
                "local_citation_ms": first("tool_finished", turn, "cite").get("duration_ms"),
                "answer_model_to_first_audio_ms": audio.get("since_model_request_ms"),
                "function_args_to_completed_response_ms": function_wait,
                "server_ui_notification_total_ms": round(sum(notification), 3) if notification else None,
                "browser_ui_render_total_ms": round(sum(render), 3) if render else None,
                "browser_submit_to_playback_ms": browser.get("browser_submit_to_playback_ms"),
                "browser_receipt_to_playback_ms": browser.get("browser_receipt_to_playback_ms"),
                "browser_playback_observed": bool(browser),
                "completed_playback_ack_ms": first("answer_playback_complete", turn).get("monotonic_ms"),
                "citation_requested_ms": first("model_requested", turn, "cite").get("monotonic_ms"),
            })
        summaries.append(summary)
    return summaries


def main():
    records = []
    for name in ("performance.jsonl.2", "performance.jsonl.1", "performance.jsonl"):
        path = ROOT / ".logs" / name
        if path.exists():
            records.extend(json.loads(line) for line in path.read_text().splitlines())
    print(json.dumps(summarize(records), indent=2))


if __name__ == "__main__":
    main()
