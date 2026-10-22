// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

// Opt-in genuine native voice + WebIQ query, never run by the offline test suites.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { chromium } from "@playwright/test";

const browser = await chromium.launch();
try {
  const health = await (await fetch("http://127.0.0.1:8765/api/health")).json();
  assert.equal(health.active_sessions, 0, "A user session is active; do not run the probe.");
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const activity = [];
  const errors = [];
  let audioBytes = 0;
  let selectedSources = 0;
  let done = false;
  page.on("pageerror", () => errors.push("browser_error"));
  page.on("websocket", socket => socket.on("framereceived", ({ payload }) => {
    if (typeof payload !== "string") return;
    const event = JSON.parse(payload);
    if (event.type === "tool_activity") activity.push(event);
    if (event.type === "audio" && event.turn > 0) audioBytes += Buffer.from(event.data, "base64").length;
    if (event.type === "citations" && event.state === "completed") selectedSources = event.sources.length;
    if (event.type === "error") errors.push(event.code);
    if (event.type === "done" && event.turn > 0) done = true;
  }));
  await page.goto("http://127.0.0.1:8765/?scoutTheme=light");
  await page.locator("#question").fill("What is one recent Microsoft Foundry voice agent announcement? Be concise.");
  await page.locator("#send").click();
  const deadline = Date.now() + 100_000;
  while ((!done || !selectedSources) && errors.length === 0 && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.deepEqual(errors, []);
  assert.ok(done, "Native voice answer did not finish in 100 seconds.");
  const searches = activity.filter(event => event.tool === "webiq.news");
  assert.deepEqual(searches.map(event => event.state), ["running", "completed"]);
  const search = searches[1];
  assert.ok(search.elapsed_ms > 0 && search.source_count > 0 && search.response_received);
  assert.equal(search.call_id, searches[0].call_id);
  assert.equal(search.session_id, searches[0].session_id);
  assert.equal(search.scope, "mcp_round_trip");
  const displayed = await page.locator(".search-tool .tool-duration").innerText();
  assert.equal(displayed, `${search.elapsed_ms.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} ms`);
  const citations = activity.filter(event => event.tool === "cite_sources");
  assert.deepEqual(citations.map(event => event.state), ["running", "completed"]);
  assert.equal(citations[1].scope, "local_citation_validation");
  assert.ok(selectedSources > 0 && audioBytes > 0);
  assert.ok((await page.locator(".message.assistant p").innerText()).length > 0);
  assert.equal(await page.locator(".answer-links a").count(), selectedSources);
  await page.locator("#end").click();
  assert.equal(await page.locator(".tool-row").count(), 2);
  await mkdir("test-results", { recursive: true });
  await page.screenshot({ path: "test-results/live-tool-timing.png", fullPage: true });
  console.log(JSON.stringify({
    url: page.url(), search_ms: search.elapsed_ms, displayed,
    scope: "MCP dispatch to SDK response receipt; excludes setup, model, source parsing and audio",
    usable_sources: search.source_count, selected_sources: selectedSources,
    local_citation_validation_ms: citations[1].elapsed_ms, native_audio_bytes: audioBytes,
    dispatch_before_return: true, retained_after_end: true, browser_errors: errors,
  }, null, 2));
} finally {
  await browser.close();
}
