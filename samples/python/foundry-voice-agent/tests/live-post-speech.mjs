// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

// Opt-in: two local native-voice/WebIQ turns. Only IDs, counts and timings are reported.
import assert from "node:assert/strict";
import { chromium } from "@playwright/test";

const base = "http://127.0.0.1:8765";
const optionalSearch = process.argv.includes("--optional-search");
const health = await (await fetch(`${base}/api/health`)).json();
assert.equal(health.active_sessions, 0, "A user conversation is active; do not run the probe.");
assert.equal(health.version, "7");
const browser = await chromium.launch();
const turns = new Map();
const errors = [];
let sessionId;
let truncated = 0;
let interruptAcknowledged = false;
function turn(number) {
  if (!turns.has(number)) turns.set(number, { answers: 0, audio_bytes: 0 });
  return turns.get(number);
}
async function until(condition, label, milliseconds = 110_000) {
  const deadline = Date.now() + milliseconds;
  while (!condition() && !errors.length && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert.deepEqual(errors, [], "Native/browser probe reported fixed-code errors.");
  assert.ok(condition(), label);
}
try {
  const page = await browser.newPage();
  page.on("pageerror", () => errors.push("browser_error"));
  page.on("websocket", socket => {
    socket.on("framereceived", ({ payload }) => {
      if (typeof payload !== "string") return;
      const event = JSON.parse(payload);
      if (event.type === "ready") sessionId = event.session_id;
      if (event.type === "error") errors.push(event.code);
      if (event.type === "playback_truncated") truncated++;
      if (!event.turn) return; // Configured greeting is not a question/answer.
      const row = turn(event.turn);
      if (event.type === "answer") {
        row.answers++;
        row.answer_id = event.id;
        row.initial_sources = event.sources.length;
        row.citation_state = event.citation_state;
      }
      if (event.type === "search") row.search_requests = (row.search_requests ?? 0) + 1;
      if (event.type === "audio") {
        row.first_audio_ms ??= Date.now();
        row.audio_bytes += Buffer.from(event.data, "base64").length;
      }
      if (event.type === "done") row.generation_done_ms = Date.now();
      if (event.type === "citations" && event.state === "selecting") row.citation_selecting_ms = Date.now();
      if (event.type === "citations" && event.state === "completed") {
        row.citation_completed_ms = Date.now();
        row.citation_answer_id = event.id;
        row.selected_sources = event.sources.length;
      }
    });
    socket.on("framesent", ({ payload }) => {
      if (typeof payload !== "string") return;
      const event = JSON.parse(payload);
      if (event.type === "client_timing" && event.phase === "playback_started") {
        turn(event.turn).playback_started = true;
      }
      if (event.type === "playback_complete" && event.outcome === "played") {
        const row = turn(event.turn);
        row.playback_complete_ms = Date.now();
        row.playback_answer_id = event.response_id;
      }
      if (event.type === "playback" && event.turn === 3) interruptAcknowledged = true;
    });
  });
  await page.goto(base);
  if (optionalSearch) {
    await page.locator("#question").fill("Hello! Please just greet me in one short sentence.");
    await page.locator("#send").click();
    await until(() => turn(1).generation_done_ms && turn(1).playback_started,
      "Direct conversational answer did not stream and complete.");
    await page.locator("#status-label").filter({ hasText: "Ready for a question" }).waitFor({ timeout: 30_000 });
    const direct = turn(1);
    assert.equal(direct.answers, 1);
    assert.ok(direct.audio_bytes > 0);
    assert.equal(direct.citation_state, "not_searched");
    assert.equal(direct.search_requests, undefined);
    assert.equal(direct.citation_selecting_ms, undefined);
    assert.equal(direct.playback_complete_ms, undefined);
    await page.locator(".citation-status").filter({ hasText: "without WebIQ search" }).waitFor();
  }
  const groundedTurn = optionalSearch ? 2 : 1;
  await page.locator("#question").fill("In one short sentence, what is one recent Microsoft Foundry announcement?");
  await page.locator("#send").click();
  await until(() => turn(groundedTurn).citation_completed_ms, "Post-playback references did not complete.");
  const first = turn(groundedTurn);
  assert.equal(first.answers, 1);
  assert.equal(first.initial_sources, 0);
  assert.equal(first.search_requests, 1);
  assert.ok(first.audio_bytes > 0 && first.selected_sources > 0);
  assert.equal(first.answer_id, first.playback_answer_id);
  assert.equal(first.answer_id, first.citation_answer_id);
  assert.ok(first.first_audio_ms < first.playback_complete_ms);
  assert.ok(first.generation_done_ms <= first.playback_complete_ms);
  assert.ok(first.playback_complete_ms <= first.citation_selecting_ms);
  assert.ok(first.citation_selecting_ms <= first.citation_completed_ms);
  assert.equal(await page.locator(".answer-links a").count(), first.selected_sources);
  if (!optionalSearch) {
    await page.locator("#question").fill("Give a short briefing on one recent space news story.");
    await page.locator("#send").click();
    await until(() => turn(2).first_audio_ms, "Second turn did not start audio.");
    await page.locator("#interrupt").click();
    await until(() => interruptAcknowledged && truncated > 0, "Native interruption/truncation was not acknowledged.", 15_000);
    assert.equal(turn(2).citation_selecting_ms, undefined);
    assert.equal(turn(2).citation_completed_ms, undefined);
  }
  await page.locator("#end").click();
  console.log(JSON.stringify({
    status: "passed", session_id: sessionId, live_turns: 2, completed: first,
    ...(optionalSearch ? { direct_turn: turn(1) } :
      { interrupted_turn: { audio_bytes: turn(2).audio_bytes, references_selected: false } }),
    native_truncations: truncated, errors, audio_or_content_saved: false,
  }, null, 2));
} finally {
  await browser.close();
}
