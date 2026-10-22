# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult

from app.agent import definition
from app.bridge import Bridge
from app.performance import PerformanceLogger, PerformanceTrace
from app.protocol import ClientTiming, End, Interrupt, Mute, Playback, PlaybackComplete, Played, Text
from app.webiq import PublicError, Source, WebIQ


@pytest.fixture
def bridge():
    conn = SimpleNamespace(
        response=SimpleNamespace(create=AsyncMock(), cancel=AsyncMock()),
        conversation=SimpleNamespace(item=SimpleNamespace(create=AsyncMock(), truncate=AsyncMock())),
        input_audio_buffer=SimpleNamespace(append=AsyncMock(), clear=AsyncMock()),
    )
    ws = SimpleNamespace(send_json=AsyncMock())
    search = SimpleNamespace(search=AsyncMock())
    result = Bridge(ws, conn, search)
    result.search_tool = definition("test-only").as_dict()["tools"][0]
    result.ready = True
    return result


def emitted(bridge, kind):
    return [call.args[0] for call in bridge.ws.send_json.call_args_list if call.args[0]["type"] == kind]


async def begin(bridge):
    await bridge.client_event(Text(type="text", text="Latest news"))
    await bridge.client_event(Playback(type="playback", turn=bridge.turn, items=[]))


async def created(bridge, rid="r1"):
    await bridge.server_event({"type": "response.created", "response": {"id": rid}})


def call_response(rid, tool, args, call_id="c1", status="completed"):
    return {
        "id": rid, "status": status, "output": [{
            "type": "function_call", "call_id": call_id, "name": tool, "arguments": json.dumps(args),
        }],
    }


def evidence():
    return Source("t1-s1", "Title", "https://example.org/a", "example.org", "True evidence", "Publisher")

async def test_configured_greeting_streams_without_search_or_citation_claim(bridge, tmp_path):
    logger = PerformanceLogger(tmp_path / "greeting" / "performance.jsonl")
    logger.start()
    bridge = Bridge(bridge.ws, bridge.conn, bridge.webiq, greeting_mode="template",
                    trace=PerformanceTrace(logger))
    session = definition("test-only").as_dict()
    await bridge.server_event({"type": "session.updated", "session": session})
    assert bridge.ready and bridge.response_deadline > 0
    await created(bridge, "hello")
    await bridge.server_event({
        "type": "response.output_audio.delta", "response_id": "hello", "item_id": "hello-audio",
        "content_index": 0, "delta": base64.b64encode(b"\x00\x00" * 240).decode(),
    })
    await bridge.server_event({
        "type": "response.output_audio_transcript.done", "response_id": "hello",
        "transcript": "TEST ONLY GREETING",
    })
    await bridge.response_done({"id": "hello", "status": "completed", "output": []})
    assert emitted(bridge, "audio")[0]["turn"] == 0
    assert emitted(bridge, "transcript")[0]["turn"] == 0
    assert emitted(bridge, "done")[0]["turn"] == 0
    assert not emitted(bridge, "answer") and not emitted(bridge, "search")
    bridge.conn.response.create.assert_not_awaited()
    bridge.webiq.search.assert_not_awaited()
    logger.close()
    events = [json.loads(line) for line in logger.path.read_text().splitlines()]
    assert any(e["event"] == "first_audio" and e["stage"] == "greeting" and e["turn"] == 0 for e in events)
    assert "TEST ONLY GREETING" not in logger.path.read_text()
    await begin(bridge)
    assert bridge.pending == (1, "respond")
    with pytest.raises(PublicError, match="unsolicited"):
        await created(Bridge(bridge.ws, bridge.conn, bridge.webiq), "unconfigured")


@pytest.mark.parametrize("created_before_interrupt", [False, True])
@pytest.mark.parametrize("greeting_mode", ["template", "llm_generated"])
async def test_greeting_interrupt_race_still_allows_model_search_choice(bridge, created_before_interrupt, greeting_mode):
    bridge = Bridge(bridge.ws, bridge.conn, bridge.webiq, greeting_mode=greeting_mode)
    bridge.search_tool = definition("test-only").as_dict()["tools"][0]
    bridge.ready = True
    if created_before_interrupt:
        await created(bridge, "hello")
    await begin(bridge)
    bridge.conn.response.create.assert_not_awaited()
    if not created_before_interrupt:
        await created(bridge, "hello")
    if greeting_mode == "template":
        bridge.conn.response.cancel.assert_not_awaited()
    else:
        bridge.conn.response.cancel.assert_awaited_once_with(response_id="hello", event_id="cancel_hello")
    await bridge.server_event({
        "type": "response.output_audio.delta", "response_id": "hello", "item_id": "hello-audio",
        "content_index": 0, "delta": base64.b64encode(b"\x00\x00").decode(),
    })
    assert not emitted(bridge, "audio")
    await bridge.response_done({"id": "hello", "status": "cancelled", "output": []})
    assert bridge.pending == (1, "respond")
    assert bridge.conn.response.create.call_args.kwargs["response"]["tool_choice"] == "auto"


async def test_greeting_cannot_call_tools_or_authorize_a_second_unsolicited_response(bridge):
    bridge = Bridge(bridge.ws, bridge.conn, bridge.webiq, greeting_mode="template")
    bridge.search_tool = definition("test-only").as_dict()["tools"][0]
    await created(bridge, "hello")
    with pytest.raises(PublicError, match="startup greeting"):
        await bridge.response_done(call_response("hello", "search_web", {"query": "TEST ONLY"}))
    bridge.webiq.search.assert_not_awaited()
    with pytest.raises(PublicError, match="unsolicited"):
        await created(bridge, "second-hello")

async def test_template_playback_clears_locally_but_real_answer_still_truncates(bridge):
    bridge = Bridge(bridge.ws, bridge.conn, bridge.webiq, greeting_mode="template")
    bridge.search_tool = definition("test-only").as_dict()["tools"][0]
    bridge.ready = True
    bridge.pending = None
    bridge.audio[("template-item", 0)] = (0, 24000)
    await begin(bridge)
    bridge.playback_ack = 1
    await bridge.client_event(Playback(type="playback", turn=1, items=[
        Played(item_id="template-item", content_index=0, audio_end_ms=100),
    ]))
    bridge.conn.conversation.item.truncate.assert_not_awaited()
    bridge.pending = None
    bridge.audio[("real-answer", 0)] = (1, 24000)
    await bridge.client_event(Interrupt(type="interrupt"))
    await bridge.client_event(Playback(type="playback", turn=2, items=[
        Played(item_id="real-answer", content_index=0, audio_end_ms=200),
    ]))
    bridge.conn.conversation.item.truncate.assert_awaited_once_with(
        item_id="real-answer", content_index=0, audio_end_ms=200,
    )


async def test_typed_turn_allows_search_or_audio_only_after_playback_ack(bridge):
    await bridge.client_event(Text(type="text", text="Latest news"))
    bridge.conn.response.create.assert_not_awaited()
    await bridge.client_event(Playback(type="playback", turn=1, items=[]))
    request = bridge.conn.response.create.call_args.kwargs["response"]
    assert request["tool_choice"] == "auto"
    assert request["output_modalities"] == ["audio"]
    assert [tool["name"] for tool in request["tools"]] == ["search_web"]
    assert emitted(bridge, "transcript")[0]["role"] == "user"


@pytest.mark.parametrize("model", ["gpt-realtime-2.1-global-standard", "gpt-realtime-1.5-global-standard"])
async def test_search_speak_playback_select_has_actual_answer_citation_link(bridge, model):
    bridge.ready = False
    session = definition(model).as_dict()
    await bridge.server_event({"type": "session.updated", "session": session})
    bridge.webiq.search.return_value = [evidence()]
    await begin(bridge)
    search_request = bridge.conn.response.create.call_args.kwargs["response"]
    assert search_request["tool_choice"] == "auto"
    assert search_request["tools"] == [session["tools"][0]]
    assert search_request["output_modalities"] == ["audio"]
    await created(bridge)
    await bridge.response_done(call_response("r1", "search_web", {"query": "news"}))
    await bridge.tool_task
    kind, args = await bridge.queue.get()
    assert kind == "tool"
    await bridge.tool_finished(*args)
    assert bridge.pending == (1, "answer")
    answer_request = bridge.conn.response.create.call_args.kwargs["response"]
    assert answer_request["tool_choice"] == "none"
    assert "do not provide news facts" not in answer_request["instructions"]
    assert "tools" not in answer_request
    await created(bridge, "r2")
    answer = emitted(bridge, "answer")[0]
    assert answer == {"type": "answer", "id": "r2", "turn": 1, "sources": [], "citation_state": "pending"}
    await bridge.server_event({
        "type": "response.output_audio.delta", "response_id": "r2", "item_id": "audio1",
        "content_index": 0, "delta": base64.b64encode(b"\x00\x00" * 240).decode(),
    })
    complete = PlaybackComplete(type="playback_complete", turn=1, response_id="r2", outcome="played")
    await bridge.client_event(complete)
    assert bridge.conn.response.create.await_count == 2
    await bridge.response_done({"id": "r2", "status": "completed", "output": []})
    assert bridge.conn.response.create.await_count == 2
    assert bridge.pending is None and bridge.response_deadline > 0
    await bridge.client_event(complete)
    assert bridge.pending == (1, "cite")
    cite_request = bridge.conn.response.create.call_args.kwargs["response"]
    if model == "gpt-realtime-1.5-global-standard":
        assert cite_request["tool_choice"] == "required"
        assert cite_request["tools"] == [session["tools"][1]]
    else:
        assert cite_request["tool_choice"] == {"type": "function", "name": "cite_sources"}
    assert cite_request["output_modalities"] == ["text"]
    assert "already finished playback" in cite_request["instructions"]
    await bridge.client_event(complete)
    await created(bridge, "r3")
    response = call_response("r3", "cite_sources", {"source_ids": ["t1-s1"]}, "c2")
    await bridge.response_done(response)
    await bridge.response_done(response)
    await bridge.client_event(complete)
    cited = emitted(bridge, "citations")[-1]
    assert cited["state"] == "completed" and cited["id"] == answer["id"]
    assert cited["sources"][0]["url"] == "https://example.org/a"
    assert "content" not in cited["sources"][0]
    assert bridge.conn.response.create.await_count == 3
    assert len(emitted(bridge, "answer")) == 1 and len(emitted(bridge, "done")) == 1
    assert bridge.pending is None and bridge.response_deadline == 0
    assert emitted(bridge, "audio")[0]["id"] == answer["id"]

async def test_native_15_can_complete_a_direct_answer_without_search(bridge):
    bridge.ready = False
    await bridge.server_event({
        "type": "session.updated", "session": definition("gpt-realtime-1.5").as_dict(),
    })
    await begin(bridge)
    await created(bridge)
    await bridge.response_done({"id": "r1", "status": "completed", "output": [{"type": "message"}]})
    bridge.webiq.search.assert_not_awaited()
    assert emitted(bridge, "answer")[0]["citation_state"] == "not_searched"
    assert emitted(bridge, "done")[0]["completed"] is True
    assert bridge.pending is None and bridge.response_deadline == 0


@pytest.mark.parametrize("first_event", ["response.output_audio.delta", "response.output_audio_transcript.delta"])
async def test_direct_answer_streams_without_search_citations_or_a_second_pass(bridge, first_event, tmp_path):
    logger = PerformanceLogger(tmp_path / "performance.jsonl")
    bridge.trace = PerformanceTrace(logger)
    await begin(bridge)
    await created(bridge, "direct")
    assert not emitted(bridge, "answer")
    audio = {
        "type": "response.output_audio.delta", "response_id": "direct", "item_id": "direct-pcm",
        "content_index": 0, "delta": base64.b64encode(b"\x00\x00" * 240).decode(),
    }
    transcript = {
        "type": "response.output_audio_transcript.delta", "response_id": "direct", "delta": "Test-only greeting.",
    }
    await bridge.server_event(audio if first_event.endswith("audio.delta") else transcript)
    await bridge.server_event(transcript if first_event.endswith("audio.delta") else audio)
    assert emitted(bridge, "answer") == [{
        "type": "answer", "id": "direct", "turn": 1, "sources": [], "citation_state": "not_searched",
    }]
    bridge.client_timing(ClientTiming(
        type="client_timing", turn=1, response_id="direct", phase="playback_started",
        browser_receipt_to_playback_ms=20,
    ))
    bridge.client_timing(ClientTiming(
        type="client_timing", turn=1, response_id="direct", phase="ui_render", ui="answer", browser_render_ms=1,
    ))
    response = {"id": "direct", "status": "completed", "output": [{"type": "message"}]}
    await bridge.response_done(response)
    await bridge.response_done(response)
    await bridge.client_event(PlaybackComplete(
        type="playback_complete", turn=1, response_id="direct", outcome="played",
    ))
    bridge.webiq.search.assert_not_awaited()
    assert bridge.conn.response.create.await_count == 1
    assert len(emitted(bridge, "done")) == 1
    assert not emitted(bridge, "citations") and not emitted(bridge, "tool_activity")
    rows = list(logger.queue.queue)
    assert any(r["event"] == "first_audio" and r["stage"] == "respond" for r in rows)
    assert any(r["event"] == "client_playback_started" for r in rows)
    assert any(r["event"] == "client_ui_render" for r in rows)
    assert logger.status()["state"] == "ok"
    assert "Test-only greeting" not in json.dumps(rows)


async def test_empty_auto_response_is_explicit_nonfatal_error(bridge):
    await begin(bridge)
    await created(bridge)
    await bridge.response_done({"id": "r1", "status": "completed", "output": []})
    assert emitted(bridge, "error")[-1]["code"] == "response_incomplete"
    assert emitted(bridge, "error")[-1]["fatal"] is False
    assert not emitted(bridge, "answer")
    assert bridge.response_deadline == 0
    await begin(bridge)
    assert bridge.pending == (2, "respond")


@pytest.mark.parametrize("tool", ["cite_sources", "unrecognized_tool"])
async def test_auto_choice_cannot_run_citations_or_unknown_tools_before_answer(bridge, tool):
    await begin(bridge)
    await created(bridge)
    with pytest.raises(PublicError) as error:
        await bridge.response_done(call_response("r1", tool, {"source_ids": ["old-source"]}))
    assert error.value.code == "tool_protocol"
    bridge.webiq.search.assert_not_awaited()
    assert not emitted(bridge, "answer") and not emitted(bridge, "citations")


async def test_optional_search_still_rejects_parallel_calls(bridge):
    await begin(bridge)
    await created(bridge)
    response = call_response("r1", "search_web", {"query": "test-only"})
    response["output"].append({**response["output"][0], "call_id": "second"})
    with pytest.raises(PublicError) as error:
        await bridge.response_done(response)
    assert error.value.code == "tool_protocol"
    bridge.webiq.search.assert_not_awaited()


async def test_model_may_not_mix_direct_speech_with_search(bridge):
    await begin(bridge)
    await created(bridge)
    await bridge.server_event({
        "type": "response.output_audio_transcript.delta", "response_id": "r1", "delta": "Test-only text.",
    })
    with pytest.raises(PublicError, match="mixed"):
        await bridge.response_done(call_response("r1", "search_web", {"query": "test-only"}))
    bridge.webiq.search.assert_not_awaited()


async def test_direct_answer_interrupt_keeps_native_truncation_and_discards_late_media(bridge):
    await begin(bridge)
    await created(bridge, "direct")
    audio = {
        "type": "response.output_audio.delta", "response_id": "direct", "item_id": "pcm",
        "content_index": 0, "delta": base64.b64encode(b"\x00\x00" * 24000).decode(),
    }
    await bridge.server_event(audio)
    await bridge.client_event(Interrupt(type="interrupt"))
    await bridge.client_event(Playback(type="playback", turn=2, items=[
        Played(item_id="pcm", content_index=0, audio_end_ms=100),
    ]))
    await bridge.server_event(audio)
    bridge.conn.response.cancel.assert_awaited_once_with(response_id="direct", event_id="cancel_direct")
    bridge.conn.conversation.item.truncate.assert_awaited_once_with(
        item_id="pcm", content_index=0, audio_end_ms=100,
    )
    assert len(emitted(bridge, "audio")) == 1
    assert not emitted(bridge, "citations")


async def test_v7_required_search_description_is_overridden_locally(bridge):
    bridge.ready = False
    session = definition("gpt-realtime-1.5-global-standard").as_dict()
    session["tools"][0]["description"] = "Search current news. Required each turn."
    await bridge.server_event({"type": "session.updated", "session": session})
    await begin(bridge)
    request = bridge.conn.response.create.call_args.kwargs["response"]
    assert request["tool_choice"] == "auto"
    assert request["output_modalities"] == ["audio"]
    assert [t["name"] for t in request["tools"]] == ["search_web"]
    assert "Optional" in request["tools"][0]["description"]
    assert "Required each turn" in session["tools"][0]["description"]


async def test_cancel_ignores_late_audio_and_truncates_only_sent_items(bridge):
    bridge.turn = 1
    bridge.active = "r1"
    bridge.responses["r1"] = (1, "answer")
    bridge.audio[("item1", 0)] = (1, 24000)
    await bridge.client_event(Interrupt(type="interrupt"))
    bridge.conn.response.cancel.assert_awaited_once_with(response_id="r1", event_id="cancel_r1")
    await bridge.server_event({
        "type": "response.output_audio.delta", "response_id": "r1", "item_id": "item1",
        "content_index": 0, "delta": base64.b64encode(b"\x00\x00").decode(),
    })
    assert not emitted(bridge, "audio")
    await bridge.client_event(Playback(type="playback", turn=2, items=[
        Played(item_id="item1", content_index=0, audio_end_ms=300),
        Played(item_id="forged", content_index=0, audio_end_ms=10),
    ]))
    bridge.conn.conversation.item.truncate.assert_awaited_once_with(
        item_id="item1", content_index=0, audio_end_ms=300,
    )


async def test_cancellation_race_before_response_created(bridge):
    await begin(bridge)
    await bridge.client_event(Text(type="text", text="A different question"))
    await bridge.client_event(Playback(type="playback", turn=2, items=[]))
    assert bridge.conn.response.create.await_count == 1
    await created(bridge, "old")
    bridge.conn.response.cancel.assert_awaited_once_with(response_id="old", event_id="cancel_old")
    await bridge.response_done({"id": "old", "status": "cancelled", "output": []})
    assert bridge.conn.response.create.await_count == 2
    assert bridge.pending == (2, "respond")


async def test_cancelled_search_gets_tool_output_and_stale_result_is_discarded(bridge):
    bridge.webiq.search.side_effect = lambda *_: asyncio.sleep(60)
    bridge.turn = 1
    bridge.search_call = {"call_id": "old"}
    bridge.tool_task = asyncio.create_task(asyncio.sleep(60))
    await bridge.new_turn()
    bridge.conn.conversation.item.create.assert_awaited_once()
    output = bridge.conn.conversation.item.create.call_args.kwargs["item"]
    assert output.call_id == "old"
    assert "cancelled" in output.output
    await bridge.tool_finished(1, {"call_id": "old"}, [evidence()])
    assert not bridge.sources
    bridge.conn.response.create.assert_not_awaited()


async def test_function_calls_on_incomplete_response_not_executed(bridge):
    await begin(bridge)
    await created(bridge)
    await bridge.response_done(call_response("r1", "search_web", {"query": "news"}, status="incomplete"))
    bridge.webiq.search.assert_not_awaited()
    assert emitted(bridge, "error")[0]["code"] == "response_incomplete"


async def test_repeated_done_event_is_idempotent(bridge):
    bridge.webiq.search.return_value = []
    await begin(bridge)
    await created(bridge)
    response = call_response("r1", "search_web", {"query": "news"})
    await bridge.response_done(response)
    task = bridge.tool_task
    await bridge.response_done(response)
    await task
    assert bridge.webiq.search.await_count == 1


async def test_forged_citation_is_rejected(bridge):
    bridge.turn = 1
    bridge.responses["cite"] = (1, "cite")
    bridge.active = "cite"
    bridge.sources = [evidence()]
    await bridge.response_done(call_response("cite", "cite_sources", {"source_ids": ["invented"]}))
    assert emitted(bridge, "error")[-1]["code"] == "citation_failed"
    assert emitted(bridge, "error")[-1]["fatal"] is False
    assert not emitted(bridge, "answer")


async def test_webiq_failure_cannot_become_factual_answer(bridge):
    bridge.turn = 1
    await bridge.tool_finished(1, {"call_id": "c"}, PublicError("webiq_timeout", "WebIQ timed out."))
    request = bridge.conn.response.create.call_args.kwargs["response"]
    assert "do not provide news facts" in request["instructions"]
    assert not bridge.selected
    assert emitted(bridge, "error")[0]["code"] == "webiq_timeout"


async def test_delayed_transcription_keeps_original_turn(bridge):
    await bridge.server_event({"type": "input_audio_buffer.speech_started", "item_id": "speech1"})
    await bridge.new_turn()
    await bridge.server_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "item_id": "speech1", "transcript": "Earlier utterance",
    })
    assert emitted(bridge, "transcript")[0]["turn"] == 1
    await bridge.server_event({"type": "input_audio_buffer.committed", "item_id": "speech1"})
    assert not bridge.waiting_turn


async def test_mute_clears_input_and_blocks_audio(bridge):
    await bridge.client_event(Mute(type="mute", muted=True))
    await bridge.client_event(b"\x00\x00" * 480)
    bridge.conn.input_audio_buffer.clear.assert_awaited_once()
    bridge.conn.input_audio_buffer.append.assert_not_awaited()


async def test_manual_stop_returns_to_idle_without_spurious_timeout(bridge):
    bridge.turn = 1
    bridge.active = "r1"
    bridge.responses["r1"] = (1, "answer")
    await bridge.client_event(Interrupt(type="interrupt"))
    await bridge.client_event(Playback(type="playback", turn=2, items=[]))
    assert bridge.response_deadline > 0
    await bridge.response_done({"id": "r1", "status": "cancelled", "output": []})
    assert bridge.response_deadline == 0
    assert not bridge.pending


async def test_only_correlated_already_completed_cancellation_is_benign(bridge):
    bridge.cancel_events.add("cancel_r1")
    await bridge.server_event({
        "type": "error", "error": {"code": "response_cancel_not_active", "event_id": "cancel_r1"},
    })
    with pytest.raises(PublicError):
        await bridge.server_event({
            "type": "error", "error": {"code": "response_cancel_not_active", "event_id": "unrelated"},
        })


async def test_startup_waits_for_effective_config(bridge):
    bridge.ready = False
    await bridge.server_event({"type": "session.created"})
    assert not bridge.ready
    config = definition("test-only").as_dict()
    await bridge.server_event({"type": "session.updated", "session": config})
    assert bridge.ready
    assert emitted(bridge, "ready")
    bridge.ready = False
    config["audio"]["input"]["turn_detection"]["create_response"] = True
    with pytest.raises(PublicError, match="configuration"):
        await bridge.server_event({"type": "session.updated", "session": config})


async def test_unexpected_tool_transport_failure_is_reported_immediately(bridge):
    bridge.webiq.search.side_effect = RuntimeError("PRIVATE_PAYLOAD")
    await bridge.search({"call_id": "c"}, "news", 1)
    kind, (turn, _, error) = await bridge.queue.get()
    assert kind == "tool" and turn == 1
    assert error.code == "webiq_failure"
    assert "PRIVATE_PAYLOAD" not in error.message


async def test_service_reader_failure_wakes_idle_session(bridge):
    class BrokenConnection:
        async def __aiter__(self):
            raise RuntimeError("PRIVATE_PAYLOAD")
            yield  # pragma: no cover
    bridge.conn = BrokenConnection()
    await bridge.read_service()
    kind, error = await bridge.queue.get()
    assert kind == "error"
    assert error.code == "voice_disconnected"
    assert "PRIVATE_PAYLOAD" not in error.message


@pytest.mark.parametrize("data", [b"\x00" * 4802, b"\x00", b""])
async def test_invalid_pcm_frame_is_rejected_at_boundary(bridge, data):
    bridge.ws.receive = AsyncMock(return_value={"type": "websocket.receive", "bytes": data})
    await bridge.read_browser()
    kind, error = await bridge.queue.get()
    assert kind == "error"
    assert error.code == "invalid_audio"


async def dispatch_search(bridge, call_id="timed"):
    await begin(bridge)
    await created(bridge)
    await bridge.response_done(call_response("r1", "search_web", {"query": "PRIVATE QUERY"}, call_id))


async def publish_running(bridge):
    kind, event = await asyncio.wait_for(bridge.queue.get(), 1)
    assert kind == "tool_activity"
    await bridge.publish_activity(event)
    return event


def mcp_result():
    valid = {"title": "Title", "url": "https://example.org/a", "content": "PRIVATE EVIDENCE"}
    return CallToolResult(content=[], structuredContent={
        "newsResults": [valid, valid, {**valid, "url": "javascript:invalid"}],
    })


async def test_live_tool_lifecycle_dispatch_before_return_and_usable_source_count(bridge, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("app.activity.perf_counter", lambda: clock[0])
    release = asyncio.Event()

    async def call_tool(*_):
        await release.wait()
        clock[0] = 10.321
        return mcp_result()

    session = AsyncMock()
    session.call_tool.side_effect = call_tool
    bridge.webiq = WebIQ(session)
    await dispatch_search(bridge)
    await publish_running(bridge)
    events = emitted(bridge, "tool_activity")
    assert [event["state"] for event in events] == ["running"]
    assert events[0]["elapsed_ms"] is None
    release.set()
    await bridge.tool_task
    kind, args = await bridge.queue.get()
    assert kind == "tool"
    await bridge.tool_finished(*args)
    events = emitted(bridge, "tool_activity")
    assert [event["state"] for event in events] == ["running", "completed"]
    assert events[-1]["elapsed_ms"] == pytest.approx(321)
    assert events[-1]["source_count"] == 1
    assert events[-1]["response_received"]
    assert events[-1]["scope"] == "mcp_round_trip"
    assert {(e["session_id"], e["call_id"], e["turn"]) for e in events} == {
        (bridge.session_id, "timed", 1),
    }
    assert "PRIVATE" not in json.dumps(events)
    await bridge.finish_activity("timed", "completed", 5)
    assert len(emitted(bridge, "tool_activity")) == 2


@pytest.mark.parametrize("returned", [False, True])
async def test_interrupt_cannot_resurrect_stale_dispatch_or_completion(bridge, monkeypatch, returned):
    clock = [10.0]
    monkeypatch.setattr("app.activity.perf_counter", lambda: clock[0])
    started = asyncio.Event()

    async def call_tool(*_):
        started.set()
        if returned:
            clock[0] = 10.2
            return mcp_result()
        await asyncio.Event().wait()

    session = AsyncMock()
    session.call_tool.side_effect = call_tool
    bridge.webiq = WebIQ(session)
    await dispatch_search(bridge)
    await asyncio.wait_for(started.wait(), 1)
    if returned:
        await bridge.tool_task
    clock[0] = 10.4
    await bridge.new_turn()
    while not bridge.queue.empty():
        kind, value = await bridge.queue.get()
        if kind == "tool_activity":
            await bridge.publish_activity(value)
        elif kind == "tool":
            await bridge.tool_finished(*value)
    events = emitted(bridge, "tool_activity")
    assert [e["state"] for e in events] == ["cancelled"]
    assert events[0]["elapsed_ms"] == pytest.approx(200 if returned else 400)
    assert events[0]["response_received"] == returned
    assert not emitted(bridge, "evidence")


async def test_failed_search_emits_failed_timing_not_completion(bridge, monkeypatch):
    clock = [1.0]
    monkeypatch.setattr("app.activity.perf_counter", lambda: clock[0])

    async def call_tool(*_):
        clock[0] = 1.5
        raise TimeoutError

    session = AsyncMock()
    session.call_tool.side_effect = call_tool
    bridge.webiq = WebIQ(session)
    await dispatch_search(bridge)
    await bridge.tool_task
    await publish_running(bridge)
    _, result = await bridge.queue.get()
    await bridge.tool_finished(*result)
    events = emitted(bridge, "tool_activity")
    assert [e["state"] for e in events] == ["running", "failed"]
    assert events[-1]["elapsed_ms"] == pytest.approx(500)
    assert events[-1]["source_count"] is None
    assert emitted(bridge, "error")[0]["code"] == "webiq_timeout"


@pytest.mark.parametrize("valid", [True, False])
async def test_citation_latency_is_separate_local_validation(bridge, monkeypatch, valid):
    stamps = iter([5.0, 5.000125])
    monkeypatch.setattr("app.activity.perf_counter", lambda: next(stamps))
    bridge.turn = 1
    bridge.responses["cite"] = (1, "cite")
    bridge.sources = [evidence()]
    response = call_response("cite", "cite_sources", {"source_ids": ["t1-s1" if valid else "forged"]})
    await bridge.response_done(response)
    events = emitted(bridge, "tool_activity")
    assert [e["state"] for e in events] == ["running", "completed" if valid else "failed"]
    assert events[-1]["scope"] == "local_citation_validation"
    assert events[-1]["elapsed_ms"] == pytest.approx(.125)
    assert events[-1]["source_count"] == (1 if valid else None)


async def test_turn_milestones_and_clock_domains_are_distinct(bridge, tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.performance.time.perf_counter", lambda: clock[0])
    logger = PerformanceLogger(tmp_path / "performance.jsonl")
    bridge.trace = PerformanceTrace(logger)
    await bridge.server_event({"type": "input_audio_buffer.speech_started", "item_id": "input"})
    await bridge.client_event(Playback(type="playback", turn=1, items=[]))
    clock[0] = 101.0
    await bridge.server_event({"type": "input_audio_buffer.speech_stopped", "item_id": "input"})
    clock[0] = 101.2
    await bridge.server_event({"type": "input_audio_buffer.committed", "item_id": "input"})
    clock[0] = 101.3
    await bridge.server_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "item_id": "input", "transcript": "PRIVATE TRANSCRIPT",
    })
    records = list(logger.queue.queue)
    assert [r["event"] for r in records[:5]] == [
        "turn_start", "playback_ack", "speech_stopped", "input_committed", "model_requested",
    ]
    assert next(r for r in records if r["event"] == "model_requested")["since_speech_stop_ms"] == 200
    assert next(r for r in records if r["event"] == "transcription_ready")["since_speech_stop_ms"] == 300
    assert "PRIVATE" not in json.dumps(records)


async def test_first_audio_logged_once_and_client_reports_require_current_forwarded_response(bridge, tmp_path):
    logger = PerformanceLogger(tmp_path / "performance.jsonl")
    bridge.trace = PerformanceTrace(logger)
    bridge.turn = 1
    bridge.responses["answer"] = (1, "answer")
    bridge.trace.mark("turn_start", turn=1, input_kind="text")
    bridge.trace.mark("model_requested", turn=1, stage="answer")
    report = ClientTiming(type="client_timing", turn=1, phase="playback_started",
                          response_id="answer", browser_receipt_to_playback_ms=31,
                          browser_submit_to_playback_ms=8765, browser_schedule_lead_ms=15)
    bridge.client_timing(report)
    audio = {
        "type": "response.output_audio.delta", "response_id": "answer", "item_id": "item",
        "content_index": 0, "delta": base64.b64encode(b"\x00\x00" * 240).decode(),
    }
    await bridge.server_event({**audio, "delta": ""})
    assert not any(r["event"] == "first_audio" for r in logger.queue.queue)
    await bridge.server_event(audio)
    await bridge.server_event(audio)
    bridge.client_timing(report)
    bridge.client_timing(report)
    records = list(logger.queue.queue)
    assert sum(r["event"] == "first_audio" for r in records) == 1
    assert sum(r["event"] == "first_audio_forwarded" for r in records) == 1
    client_records = [r for r in records if r["event"] == "client_playback_started"]
    assert len(client_records) == 1
    assert client_records[0]["browser_submit_to_playback_ms"] == 8765
    assert "since_turn_ms" not in client_records[0]
    bridge.turn = 2
    bridge.client_timing(report)
    await bridge.server_event(audio)
    assert sum(r["event"] == "first_audio" for r in logger.queue.queue) == 1
    assert sum(r["event"] == "client_playback_started" for r in logger.queue.queue) == 1
    assert any(r["event"] == "stale_event" for r in logger.queue.queue)


async def test_cancelled_tool_output_keeps_original_turn_in_log(bridge, tmp_path):
    logger = PerformanceLogger(tmp_path / "performance.jsonl")
    bridge.trace = PerformanceTrace(logger)
    bridge.turn = 1
    bridge.call_turns["old"] = 1
    bridge.search_call = {"call_id": "old"}
    bridge.tool_task = asyncio.create_task(asyncio.sleep(60))
    await bridge.new_turn()
    result = next(r for r in logger.queue.queue if r["event"] == "tool_output_sent")
    assert result["turn"] == 1


async def completed_answer(bridge, *, audio=True, sources=True, status="completed"):
    bridge.turn = 1
    bridge.sources = [evidence()] if sources else []
    await bridge.request_response("answer")
    await created(bridge, "spoken")
    if audio:
        await bridge.server_event({
            "type": "response.output_audio.delta", "response_id": "spoken", "item_id": "audio1",
            "content_index": 0, "delta": base64.b64encode(b"\x00\x00" * 240).decode(),
        })
    await bridge.response_done({"id": "spoken", "status": status, "output": []})


def playback_complete():
    return PlaybackComplete(type="playback_complete", turn=1, response_id="spoken", outcome="played")


async def wait_forever():
    await asyncio.Event().wait()


@pytest.mark.parametrize("outcome,audio", [("no_audio", False), ("unavailable", True), ("played", False)])
async def test_no_observed_playback_does_not_select_references_or_disconnect(bridge, outcome, audio):
    await completed_answer(bridge, audio=audio)
    await bridge.client_event(PlaybackComplete(
        type="playback_complete", turn=1, response_id="spoken", outcome=outcome,
    ))
    assert bridge.conn.response.create.await_count == 1
    assert emitted(bridge, "citations")[-1]["state"] == "failed"
    assert emitted(bridge, "error")[-1]["fatal"] is False
    assert bridge.response_deadline == 0
    await begin(bridge)
    assert bridge.pending == (2, "respond")


@pytest.mark.parametrize("status,sources", [("incomplete", True), ("completed", False)])
async def test_incomplete_answer_or_empty_evidence_never_cites(bridge, status, sources):
    await completed_answer(bridge, status=status, sources=sources)
    await bridge.client_event(playback_complete())
    assert bridge.conn.response.create.await_count == 1
    assert not emitted(bridge, "citations") or emitted(bridge, "citations")[-1]["state"] == "unavailable"
    assert bridge.response_deadline == 0


@pytest.mark.parametrize("phase", ["playback", "requested", "created"])
async def test_new_question_cancels_deferred_citations_and_late_results(bridge, phase):
    await completed_answer(bridge)
    if phase != "playback":
        await bridge.client_event(playback_complete())
    if phase == "created":
        await created(bridge, "cite-old")
    await begin(bridge)
    await bridge.client_event(playback_complete())
    assert any(e["state"] == "cancelled" and e["id"] == "spoken" for e in emitted(bridge, "citations"))
    if phase == "requested":
        await created(bridge, "cite-old")
    if phase != "playback":
        bridge.conn.response.cancel.assert_awaited_once_with(
            response_id="cite-old", event_id="cancel_cite-old",
        )
        await bridge.response_done(call_response(
            "cite-old", "cite_sources", {"source_ids": ["t1-s1"]}, "late-call",
        ))
        assert "cancelled" in bridge.conn.conversation.item.create.call_args.kwargs["item"].output
    assert bridge.pending == (2, "respond")
    assert not any(e["state"] == "completed" for e in emitted(bridge, "citations"))
    assert len(emitted(bridge, "answer")) == 1


@pytest.mark.parametrize("turn,rid", [(2, "spoken"), (1, "forged")])
async def test_playback_ack_must_match_completed_answer(bridge, turn, rid):
    await completed_answer(bridge)
    await bridge.client_event(PlaybackComplete(type="playback_complete", turn=turn, response_id=rid, outcome="played"))
    assert bridge.conn.response.create.await_count == 1
    assert bridge.citation_state == "pending"


@pytest.mark.parametrize("args", [
    {"source_ids": []}, {"source_ids": ["forged"]}, {"source_ids": ["t1-s1", "t1-s1"]},
    {"source_ids": "t1-s1"}, {"source_ids": ["t1-s1"], "extra": "untrusted"},
])
async def test_citation_failures_keep_spoken_answer_without_extra_speech(bridge, args):
    await completed_answer(bridge)
    await bridge.client_event(playback_complete())
    await created(bridge, "cite")
    await bridge.response_done(call_response("cite", "cite_sources", args))
    assert bridge.conn.response.create.await_count == 2
    assert len(emitted(bridge, "answer")) == 1 and len(emitted(bridge, "done")) == 1
    assert emitted(bridge, "citations")[-1]["id"] == "spoken"
    assert emitted(bridge, "citations")[-1]["state"] == "failed"
    assert emitted(bridge, "error")[-1]["fatal"] is False
    assert not bridge.selected
    await begin(bridge)
    assert bridge.pending == (2, "respond")


@pytest.mark.parametrize("status", ["incomplete", "completed"])
async def test_failed_or_plain_text_citation_response_is_nonfatal(bridge, status):
    await completed_answer(bridge)
    await bridge.client_event(playback_complete())
    await created(bridge, "cite")
    await bridge.response_done({"id": "cite", "status": status, "output": [{"type": "message"}]})
    assert emitted(bridge, "error")[-1]["fatal"] is False
    assert bridge.citation_state == "failed"
    assert len(emitted(bridge, "done")) == 1


async def test_correlated_citation_request_error_allows_next_question(bridge):
    await completed_answer(bridge)
    await bridge.client_event(playback_complete())
    await bridge.server_event({
        "type": "error", "error": {"event_id": "request_t1_cite", "code": "invalid_request_error"},
    })
    assert bridge.pending is None and bridge.citation_state == "failed"
    await begin(bridge)
    assert bridge.pending == (2, "respond")


@pytest.mark.parametrize("selecting", [False, True])
async def test_end_cancels_pending_citation_work(bridge, selecting):
    await completed_answer(bridge)
    if selecting:
        await bridge.client_event(playback_complete())
    bridge.read_browser = wait_forever
    bridge.read_service = wait_forever
    await bridge.queue.put(("client", End(type="end")))
    await bridge.run()
    assert bridge.conn.response.create.await_count == (2 if selecting else 1)
    assert not any(e["state"] == "completed" for e in emitted(bridge, "citations"))


async def test_playback_wait_timeout_is_explicit_and_nonfatal(bridge):
    await completed_answer(bridge)
    bridge.response_deadline = 1

    async def browser():
        while not emitted(bridge, "error"):
            await asyncio.sleep(.01)
        await bridge.queue.put(("client", End(type="end")))

    bridge.read_browser = browser
    bridge.read_service = wait_forever
    await asyncio.wait_for(bridge.run(), 1)
    assert emitted(bridge, "error")[-1]["code"] == "citation_timeout"
    assert emitted(bridge, "error")[-1]["fatal"] is False
    assert bridge.conn.response.create.await_count == 1


async def test_citation_timeout_cancels_native_response_and_stays_available(bridge):
    await completed_answer(bridge)
    await bridge.client_event(playback_complete())
    await created(bridge, "slow-cite")
    bridge.response_deadline = 1

    async def browser():
        while not bridge.conn.response.cancel.await_count:
            await asyncio.sleep(.01)
        await bridge.queue.put(("server", {
            "type": "response.done", "response": {"id": "slow-cite", "status": "cancelled", "output": []},
        }))
        await bridge.queue.put(("client", End(type="end")))

    bridge.read_browser = browser
    bridge.read_service = wait_forever
    await asyncio.wait_for(bridge.run(), 1)
    bridge.conn.response.cancel.assert_awaited_once_with(response_id="slow-cite", event_id="cancel_slow-cite")
    assert emitted(bridge, "error")[-1]["fatal"] is False
    assert bridge.response_deadline == 0
    assert bridge.citation_state == "failed"


async def test_local_override_corrects_v7_future_answer_tool_without_changing_session(bridge):
    bridge.ready = False
    session = definition("gpt-realtime-1.5-global-standard").as_dict()
    session["tools"][1]["description"] = "Select sources for the answer you will speak next."
    session["tools"][1]["parameters"]["properties"]["source_ids"]["minItems"] = 1
    await bridge.server_event({"type": "session.updated", "session": session})
    await completed_answer(bridge)
    await bridge.client_event(playback_complete())
    request = bridge.conn.response.create.call_args.kwargs["response"]
    assert request["tool_choice"] == "required"
    assert request["tools"][0]["parameters"]["properties"]["source_ids"]["minItems"] == 0
    assert "already spoken" in request["tools"][0]["description"]
    assert session["tools"][1]["parameters"]["properties"]["source_ids"]["minItems"] == 1
