// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

// Opt-in: two real typed turns and one native PCM-input turn. No utterance is saved.
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { chromium } from "@playwright/test";

const health = await (await fetch("http://127.0.0.1:8765/api/health")).json();
assert.equal(health.active_sessions, 0, "A user session is active; do not run the probe.");
assert.equal(health.performance_logging?.state, "ok", "Performance logger is not healthy.");
const coldTextOnly = process.argv.includes("--cold-text");
let pcm;
if (!coldTextOnly) {
  try {
    const result = await promisify(execFile)(".venv/bin/python", ["-c",
      "import asyncio,sys; from app.config import Settings; from app.voice_input_smoke import make_test_utterance; sys.stdout.buffer.write(asyncio.run(make_test_utterance(Settings.load())))",
    ], { encoding: "buffer", timeout: 45_000, maxBuffer: 2_000_000 });
    pcm = result.stdout;
  } catch {
    throw new Error("Native test-utterance generation failed; subprocess payloads were not logged.");
  }
  assert.ok(pcm.length > 0 && pcm.length < 2_000_000 && pcm.length % 2 === 0);
}
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  const errors = [];
  const done = new Set();
  const playback = new Set();
  const citations = new Set();
  let sessionId;
  page.on("pageerror", () => errors.push("browser_error"));
  page.on("websocket", socket => {
    socket.on("framereceived", ({ payload }) => {
      if (typeof payload !== "string") return;
      const event = JSON.parse(payload);
      if (event.type === "ready") sessionId = event.session_id;
      if (event.type === "error") errors.push(event.code);
      if (event.type === "done") done.add(event.turn);
      if (event.type === "citations" && event.state === "completed") citations.add(event.turn);
    });
    socket.on("framesent", ({ payload }) => {
      if (typeof payload !== "string") return;
      const event = JSON.parse(payload);
      if (event.type === "client_timing" && event.phase === "playback_started") playback.add(event.turn);
    });
  });
  await page.addInitScript(() => {
    const Original = window.WebSocket;
    window.WebSocket = class extends Original {
      constructor(...args) { super(...args); Reflect.set(window, "__latencySocket", this); }
    };
  });
  await page.goto("http://127.0.0.1:8765/");
  async function finish(turn) {
    const deadline = Date.now() + 100_000;
    while ((!done.has(turn) || !playback.has(turn) || !citations.has(turn)) && !errors.length && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.deepEqual(errors, []);
    assert.ok(done.has(turn) && playback.has(turn) && citations.has(turn),
      `Turn ${turn} lacked completion, output-timestamp playback or post-speech citations.`);
    await page.locator("#status-label").filter({ hasText: "Ready for a question" }).waitFor({ timeout: 60_000 });
  }
  for (const [index, question] of [
    "In one short sentence, what is one recent Microsoft Foundry voice agent announcement?",
    "In one short sentence, what is another recent Microsoft AI announcement?",
  ].slice(0, coldTextOnly ? 1 : 2).entries()) {
    await page.locator("#question").fill(question);
    await page.locator("#send").click();
    await finish(index + 1);
  }
  // IPC and browser memory only: this is native Foundry-generated PCM, not a mocked response.
  if (!coldTextOnly) {
    await page.evaluate(async base64 => {
    const socket = Reflect.get(window, "__latencySocket");
    if (!(socket instanceof WebSocket) || socket.readyState !== WebSocket.OPEN) throw new Error("No live socket");
    const raw = atob(base64);
    const audio = new Uint8Array(raw.length + 48_000);
    for (let i = 0; i < raw.length; i++) audio[i] = raw.charCodeAt(i);
    for (let offset = 0; offset < audio.length; offset += 960) {
      socket.send(audio.slice(offset, offset + 960));
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    }, pcm.toString("base64"));
    await finish(3);
  }
  await page.locator("#end").click();
  console.log(JSON.stringify({
    session_id: sessionId, turns: coldTextOnly ? 1 : 3,
    inputs: coldTextOnly ? ["typed-cold"] : ["typed-cold", "typed-warm", "native-pcm-warm"],
    playback_verified_turns: [...playback], saved_audio: false, browser_errors: errors,
  }, null, 2));
} finally {
  await browser.close();
}
