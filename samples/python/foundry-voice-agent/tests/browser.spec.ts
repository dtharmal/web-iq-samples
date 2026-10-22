// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";
import { readFile } from "node:fs/promises";

test("private login page clears failed credentials, supports retry and fits mobile", async ({ page }) => {
  const html = (await readFile("app/login.html", "utf8")).replace("<!--CSRF-->", "test-only-csrf");
  let attempts = 0;
  await page.route("**/login", route => route.fulfill({ contentType: "text/html", body: html }));
  await page.route("**/api/login", route => {
    attempts++;
    return attempts === 1
      ? route.fulfill({ status: 401, json: { error: "Passphrase not accepted." } })
      : route.fulfill({ status: 303, headers: { location: "/" }, body: "" });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/login");
  await expect(page.getByRole("heading")).toHaveText("Your briefing starts here.");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByLabel("Demo passphrase").fill("wrong-test-only");
  await page.getByRole("button", { name: "Enter private demo" }).click();
  await expect(page.getByRole("alert")).toHaveText("Passphrase not accepted.");
  await expect(page.getByLabel("Demo passphrase")).toHaveValue("");
  await expect(page.getByRole("button")).toBeEnabled();
  await page.screenshot({ path: "test-results/private-login-mobile.png", fullPage: true });
  await page.getByLabel("Demo passphrase").fill("correct-test-only");
  await page.getByRole("button", { name: "Enter private demo" }).click();
  await expect(page).toHaveURL("/");
  await expect(page.locator("#start")).toBeVisible();
  expect(attempts).toBe(2);
});

for (const diagnosticCapability of [false, true]) {
  test(`forty-second fast playback stays interruptible with diagnostics capability ${diagnosticCapability}`, async ({ page }) => {
    const reports: Record<string, unknown>[] = [];
    let playedMs: number | undefined;
    await page.routeWebSocket("**/api/voice", ws => {
      const send = (event: object) => ws.send(JSON.stringify(event));
      send({ type: "ready", model: "TEST ONLY", sample_rate: 24000, performance_logging: true,
        ...(diagnosticCapability ? { playback_diagnostics: true } : {}) });
      ws.onMessage(message => {
        if (typeof message !== "string") return;
        const event = JSON.parse(message);
        if (event.type === "text") send({ type: "interrupt", turn: 1, client_turn_id: event.client_turn_id });
        if (event.type === "client_timing") reports.push(event);
        if (event.type === "playback" && event.turn === 1) {
          send({ type: "answer", sources: [], id: "r-long", turn: 1 });
          for (let chunk = 0; chunk < 20; chunk++) {
            send({ type: "audio", id: "r-long", item_id: "a-long", content_index: 0, turn: 1,
              data: Buffer.alloc(48000 * 2).toString("base64") });
          }
          send({ type: "done", id: "r-long", turn: 1 });
        }
        if (event.type === "interrupt") send({ type: "interrupt", turn: 2 });
        if (event.type === "playback" && event.turn === 2) playedMs = event.items[0]?.audio_end_ms;
      });
    });
    await page.goto("/");
    await page.locator("#question").fill("Test-only long playback");
    await page.locator("#send").click();
    await expect(page.locator("#status-label")).toHaveText("Speaking");
    if (diagnosticCapability) {
      await expect.poll(() => reports.filter(event => event.phase === "playback_buffered").length).toBe(1);
    }
    await expect(page.locator("#error")).toBeHidden();
    await page.locator("#interrupt").click();
    await expect.poll(() => playedMs).toBeDefined();
    expect(playedMs).toBeLessThan(40000);
    await expect(page.locator("#status-label")).toHaveText("Ready for a question");
    expect(reports.filter(event => event.phase === "playback_buffered")).toHaveLength(diagnosticCapability ? 1 : 0);
    expect(reports.filter(event => event.phase === "playback_error")).toHaveLength(0);
    await page.locator("#end").click();
  });
}

test("configured greeting plays without a grounding claim or question latency and remains interruptible", async ({ page }) => {
  const reports: Record<string, unknown>[] = [];
  let playedMs: number | undefined;
  await page.routeWebSocket("**/api/voice", ws => {
    const send = (event: object) => ws.send(JSON.stringify(event));
    send({ type: "ready", model: "TEST ONLY", sample_rate: 24000, performance_logging: true });
    send({ type: "transcript", id: "hello", turn: 0, role: "assistant", text: "Test-only welcome", final: true });
    send({ type: "audio", id: "hello", item_id: "hello-audio", content_index: 0, turn: 0,
      data: Buffer.alloc(48000 * 5).toString("base64") });
    send({ type: "done", id: "hello", turn: 0 });
    ws.onMessage(message => {
      if (typeof message !== "string") return;
      const event = JSON.parse(message);
      if (event.type === "client_timing") reports.push(event);
      if (event.type === "interrupt") send({ type: "interrupt", turn: 1 });
      if (event.type === "playback" && event.turn === 1) playedMs = event.items[0]?.audio_end_ms;
    });
  });
  await page.goto("/");
  await page.locator("#start").click();
  await expect(page.locator("#status-label")).toHaveText("Speaking");
  await expect(page.locator(".message.assistant p")).toHaveText("Test-only welcome");
  await expect(page.locator(".answer-links")).toHaveCount(0);
  await expect(page.locator("#error")).toBeHidden();
  expect(reports).toHaveLength(0);
  await page.locator("#interrupt").click();
  await expect.poll(() => playedMs).toBeDefined();
  expect(playedMs).toBeLessThan(5000);
  await expect(page.locator("#error")).toBeHidden();
  await page.locator("#end").click();
});

test("polished local landing page, theme and mobile layout", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/?scoutTheme=light");
  await expect(page).toHaveTitle("Web IQ + Foundry Voice Agent");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Web IQ + Foundry Voice Agent");
  for (const removed of [
    "FOUNDRY VOICE / PREVIEW", "Local-first", "A MORE NATURAL WAY TO CATCH UP",
    "Hear the latest.", "See the sources.", "YOUR BRIEFING", "Let's talk about what's new.",
    "Ready when you are", "Watch a search dispatch and return in milliseconds.",
    "Last 8 calls · kept in this tab", "THE EVIDENCE, NOT JUST THE ANSWER",
    "Native Microsoft Foundry Voice + WebIQ MCP",
  ]) expect(await page.locator("body").innerText()).not.toContain(removed);
  await expect(page.locator(".panel-heading")).toBeHidden();
  await expect(page.locator("#tool-activity-empty, .activity-history-note, .topbar-right")).toHaveCount(0);
  await expect(page.locator("footer")).toContainText("No recordings saved.");
  await expect(page.locator("#start")).toBeVisible();
  await expect(page.locator("#setup")).toBeHidden();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.screenshot({ path: "test-results/landing-light.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.goto("/?scoutTheme=dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: "test-results/landing-mobile-dark.png", fullPage: true });
  expect(errors).toEqual([]);
});

test("real browser mic produces bounded PCM; mute and teardown release device", async ({ page }) => {
  let audioFrames = 0;
  let maxBytes = 0;
  let muted = false;
  await page.addInitScript(() => {
    const getMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      const stream = await getMedia(constraints);
      Reflect.set(window, "__testStream", stream);
      return stream;
    };
  });
  await page.routeWebSocket("**/api/voice", ws => {
    ws.send(JSON.stringify({ type: "ready", model: "TEST ONLY", sample_rate: 24000 }));
    ws.onMessage(message => {
      if (typeof message !== "string") { audioFrames++; maxBytes = Math.max(maxBytes, message.length); }
      else {
        const event = JSON.parse(message);
        if (event.type === "mute") {
          muted = event.muted;
          ws.send(JSON.stringify({ type: "muted", muted }));
        }
      }
    });
  });
  await page.goto("/");
  expect(await page.evaluate(() => Reflect.get(window, "__testStream"))).toBeUndefined();
  await page.locator("#start").click();
  await expect(page.locator("#status-label")).toHaveText("Listening");
  await expect.poll(() => audioFrames).toBeGreaterThan(2);
  expect(maxBytes).toBe(960);
  await page.locator("#mute").click();
  await expect.poll(() => muted).toBe(true);
  expect(await page.evaluate(() => {
    const stream = Reflect.get(window, "__testStream");
    return stream instanceof MediaStream && !stream.getAudioTracks()[0].enabled;
  })).toBe(true);
  await page.locator("#end").click();
  await expect(page.locator("#start")).toBeVisible();
  expect(await page.evaluate(() => {
    const stream = Reflect.get(window, "__testStream");
    return stream instanceof MediaStream && stream.getTracks().every(track => track.readyState === "ended");
  })).toBe(true);
});

test("microphone denial is explicit and leaves text fallback usable", async ({ page }) => {
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException("Denied", "NotAllowedError"));
  });
  await page.goto("/");
  await page.locator("#start").click();
  await expect(page.locator("#error")).toContainText("Microphone permission was denied");
  await expect(page.locator("#send")).toBeEnabled();
  await expect(page.locator("#start")).toBeVisible();
  await expect(page.locator("#connection-pending")).toBeHidden();
  await expect(page.locator("#live-controls")).toBeHidden();
  await expect(page.locator("#session-actions")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#start")).toBeFocused();
});

test("typed grounding, streaming audio, safe citations and interruption acknowledgement", async ({ page }) => {
  let playback = false;
  let noMic = true;
  const source = {
    id: "t1-s1", title: "<img src=x onerror=alert(1)> Test source", url: "https://example.org/news",
    domain: "example.org", publisher: "Test publisher", published_at: null, updated_at: "2026-10-01", crawled_at: null,
  };
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = () => {
      Reflect.set(window, "__unexpectedMic", true);
      return Promise.reject(new Error("Text mode must not request microphone"));
    };
  });
  await page.routeWebSocket("**/api/voice", ws => {
    const send = (event: object) => ws.send(JSON.stringify(event));
    send({ type: "ready", model: "TEST ONLY", sample_rate: 24000 });
    ws.onMessage(message => {
      if (typeof message !== "string") return;
      const event = JSON.parse(message);
      if (event.type === "text") send({ type: "interrupt", turn: 1 });
      if (event.type === "playback" && event.turn === 1) {
        send({ type: "transcript", role: "user", id: "u1", turn: 1, text: "News?", final: true });
        send({ type: "search", query: "test news", turn: 1 });
        send({ type: "evidence", sources: [source], turn: 1 });
        send({ type: "answer", sources: [], id: "r1", turn: 1, citation_state: "pending" });
        send({ type: "transcript", role: "assistant", id: "r1", turn: 1, text: "Test-only ", final: false });
        send({ type: "transcript", role: "assistant", id: "r1", turn: 1, text: "answer.", final: false });
        send({ type: "audio", id: "r1", item_id: "a1", content_index: 0, turn: 1, data: Buffer.alloc(48000 * 3).toString("base64") });
        send({ type: "done", id: "r1", turn: 1 });
      }
      if (event.type === "interrupt") send({ type: "interrupt", turn: 2 });
      if (event.type === "playback" && event.turn === 2) {
        playback = event.items.length === 1 && event.items[0].audio_end_ms < 3000;
      }
    });
  });
  await page.goto("/");
  await page.locator("#question").fill("News?");
  await page.locator("#send").click();
  await expect(page.locator(".message.assistant p")).toHaveText("Test-only answer.");
  await expect(page.locator(".answer-links a")).toHaveCount(0);
  await expect(page.locator(".citation-status")).toContainText("after audio playback");
  await expect(page.locator(".source-title")).toHaveText(source.title);
  await expect(page.locator(".source-card img")).toHaveCount(0);
  await expect(page.locator(".source-date")).toContainText("Updated");
  await expect(page.locator("#status-label")).toHaveText("Speaking");
  await page.locator("#interrupt").click();
  await expect.poll(() => playback).toBe(true);
  await expect(page.locator(".playback-interrupted")).toBeVisible();
  await expect(page.locator(".citation-status")).toContainText("cancelled");
  await expect(page.locator("#status-label")).toHaveText("Ready for a question");
  noMic = await page.evaluate(() => !Reflect.get(window, "__unexpectedMic"));
  expect(noMic).toBe(true);
  await page.locator("#end").click();
});

test("disconnect is visible and transcript is not silently replayed", async ({ page }) => {
  await page.routeWebSocket("**/api/voice", ws => {
    ws.send(JSON.stringify({ type: "ready", model: "TEST ONLY", sample_rate: 24000 }));
    ws.onMessage(message => {
      if (typeof message === "string" && JSON.parse(message).type === "text") ws.close({ code: 1011 });
    });
  });
  await page.goto("/");
  await page.locator("#question").fill("News?");
  await page.locator("#send").click();
  await expect(page.locator("#error")).toContainText("connection closed");
  await expect(page.locator("#start")).toBeVisible();
});

const postSpeechSource = {
  id: "t1-s1", title: "Test-only evidence", url: "https://example.org/news",
  domain: "example.org", publisher: "Test publisher",
};

test("an unsearched answer clears prior evidence, streams speech and never selects references", async ({ page }) => {
  let questions = 0;
  const completions: number[] = [];
  await page.routeWebSocket("**/api/voice", ws => {
    const send = (event: object) => ws.send(JSON.stringify(event));
    send({ type: "ready" });
    ws.onMessage(raw => {
      if (typeof raw !== "string") return;
      const event = JSON.parse(raw);
      if (event.type === "text") send({ type: "interrupt", turn: ++questions });
      if (event.type === "playback" && event.turn === 1) {
        send({ type: "search", turn: 1, query: "Test-only news query" });
        send({ type: "evidence", turn: 1, sources: [postSpeechSource] });
        send({ type: "answer", id: "searched", turn: 1, citation_state: "pending", sources: [] });
        send({ type: "audio", id: "searched", turn: 1, item_id: "searched-pcm", content_index: 0,
          data: Buffer.alloc(4800).toString("base64") });
        send({ type: "done", id: "searched", turn: 1, completed: true });
      }
      if (event.type === "playback" && event.turn === 2) {
        send({ type: "answer", id: "direct", turn: 2, citation_state: "not_searched", sources: [] });
        send({ type: "transcript", id: "direct", role: "assistant", turn: 2, text: "Test-only direct answer.", final: true });
        send({ type: "audio", id: "direct", turn: 2, item_id: "direct-pcm", content_index: 0,
          data: Buffer.alloc(4800).toString("base64") });
        send({ type: "done", id: "direct", turn: 2, completed: true });
      }
      if (event.type === "playback_complete") {
        completions.push(event.turn);
        if (event.turn === 1) send({ type: "citations", id: "searched", turn: 1, state: "completed", sources: [postSpeechSource] });
      }
    });
  });
  await page.goto("/");
  await page.locator("#question").fill("Test-only current news");
  await page.locator("#send").click();
  await expect(page.locator(".answer-links a")).toHaveCount(1);
  await expect(page.locator("#source-count")).toHaveText("1");
  await page.locator("#question").fill("Thanks");
  await page.locator("#send").click();
  await expect(page.locator(".message.assistant").last()).toContainText("Test-only direct answer.");
  await expect(page.locator(".message.assistant").last().locator(".citation-status")).toContainText("without WebIQ search");
  await expect(page.locator("#source-count")).toHaveText("0");
  await expect(page.locator("#source-list a")).toHaveCount(0);
  await expect(page.locator("#search-status")).toContainText("without searching");
  await expect(page.locator("#status-label")).toHaveText("Ready for a question");
  expect(completions).toEqual([1]);
  await expect(page.locator(".message.assistant").last().locator(".answer-links")).toHaveCount(0);
  await expect(page.locator(".answer-links a")).toHaveCount(1); // Historical links stay with their own answer.
  await expect(page.locator("#error")).toBeHidden();
  await page.locator("#end").click();
});

test("draining between audio chunks cannot finish playback before generation done", async ({ page }) => {
  let route: WebSocketRoute | undefined;
  const completions: object[] = [];
  await page.routeWebSocket("**/api/voice", ws => {
    route = ws;
    const send = (event: object) => ws.send(JSON.stringify(event));
    send({ type: "ready" });
    ws.onMessage(raw => {
      if (typeof raw !== "string") return;
      const event = JSON.parse(raw);
      if (event.type === "text") send({ type: "interrupt", turn: 1 });
      if (event.type === "playback") {
        send({ type: "answer", id: "answer", turn: 1, sources: [], citation_state: "pending" });
        send({ type: "audio", id: "answer", turn: 1, item_id: "pcm", content_index: 0,
          data: Buffer.alloc(4800).toString("base64") });
      }
      if (event.type === "playback_complete") completions.push(event);
    });
  });
  await page.goto("/");
  await page.locator("#question").fill("Test-only separated chunks");
  await page.locator("#send").click();
  await expect(page.locator("#status-label")).toHaveText("Speaking");
  await page.waitForTimeout(300);
  expect(completions).toHaveLength(0);
  route?.send(JSON.stringify({ type: "audio", id: "answer", turn: 1, item_id: "pcm", content_index: 0,
    data: Buffer.alloc(4800).toString("base64") }));
  route?.send(JSON.stringify({ type: "done", id: "answer", turn: 1, completed: true }));
  await expect.poll(() => completions.length).toBe(1);
  await page.locator("#end").click();
});

test("a new question does not wait for selecting references or accept their late result", async ({ page }) => {
  let route: WebSocketRoute | undefined;
  let questions = 0;
  await page.routeWebSocket("**/api/voice", ws => {
    route = ws;
    const send = (event: object) => ws.send(JSON.stringify(event));
    send({ type: "ready" });
    ws.onMessage(raw => {
      if (typeof raw !== "string") return;
      const event = JSON.parse(raw);
      if (event.type === "text") send({ type: "interrupt", turn: ++questions });
      if (event.type === "playback" && event.turn === 1) {
        send({ type: "answer", id: "old", turn: 1, sources: [], citation_state: "pending" });
        send({ type: "audio", id: "old", turn: 1, item_id: "pcm", content_index: 0,
          data: Buffer.alloc(4800).toString("base64") });
        send({ type: "done", id: "old", turn: 1, completed: true });
      }
      if (event.type === "playback_complete") send({ type: "citations", id: "old", turn: 1, state: "selecting", sources: [] });
    });
  });
  await page.goto("/");
  await page.locator("#question").fill("First test question");
  await page.locator("#send").click();
  await expect(page.locator(".citation-status")).toContainText("Selecting");
  await expect(page.locator("#status-label")).toHaveText("Ready for a question");
  await page.locator("#question").fill("Next test question");
  await page.locator("#send").click();
  await expect.poll(() => questions).toBe(2);
  route?.send(JSON.stringify({ type: "citations", id: "old", turn: 1, state: "completed", sources: [postSpeechSource] }));
  await expect(page.locator(".citation-status")).toContainText("cancelled");
  await expect(page.locator(".answer-links a")).toHaveCount(0);
  await page.locator("#end").click();
});

for (const selected of [true, false]) {
  test(`references follow actual text-mode playback, selected=${selected}`, async ({ page }) => {
    const completed: Record<string, unknown>[] = [];
    let sentAt = 0;
    let completedAt = 0;
    await page.routeWebSocket("**/api/voice", ws => {
      const send = (event: object) => ws.send(JSON.stringify(event));
      send({ type: "ready", sample_rate: 24000 });
      ws.onMessage(raw => {
        if (typeof raw !== "string") return;
        const event = JSON.parse(raw);
        if (event.type === "text") send({ type: "interrupt", turn: 1 });
        if (event.type === "playback" && event.turn === 1) {
          send({ type: "evidence", turn: 1, sources: [postSpeechSource] });
          send({ type: "answer", id: "spoken", turn: 1, sources: [], citation_state: "pending" });
          send({ type: "transcript", id: "spoken", turn: 1, role: "assistant", text: "Test-only answer.", final: true });
          sentAt = Date.now();
          send({ type: "audio", id: "spoken", item_id: "pcm", content_index: 0, turn: 1,
            data: Buffer.alloc(48000).toString("base64") });
          send({ type: "done", id: "spoken", turn: 1, completed: true });
          send({ type: "done", id: "spoken", turn: 1, completed: true });
        }
        if (event.type === "playback_complete") {
          completed.push(event);
          completedAt = Date.now();
          send({ type: "citations", id: "spoken", turn: 1, state: "selecting", sources: [] });
          send({ type: "citations", id: "wrong-answer", turn: 1, state: "completed", sources: [postSpeechSource] });
          send({ type: "citations", id: "spoken", turn: 1, state: selected ? "completed" : "failed",
            sources: selected ? [postSpeechSource] : [] });
          if (!selected) send({ type: "error", code: "citation_failed", message: "References unavailable.", fatal: false });
        }
      });
    });
    await page.goto("/");
    await page.locator("#question").fill("Test-only news");
    await page.locator("#send").click();
    await expect(page.locator(".message.assistant p")).toHaveText("Test-only answer.");
    await expect(page.locator("#status-label")).toHaveText("Speaking");
    await expect(page.locator(".answer-links a")).toHaveCount(0);
    expect(completed).toHaveLength(0);
    await expect.poll(() => completed.length).toBe(1);
    expect(completed[0]).toMatchObject({ turn: 1, response_id: "spoken", outcome: "played" });
    expect(completedAt - sentAt).toBeGreaterThanOrEqual(1000);
    await expect(page.locator(".message.assistant")).toHaveCount(1);
    await expect(page.locator("#status-label")).toHaveText("Ready for a question");
    await expect(page.locator("#send")).toBeEnabled();
    if (selected) {
      await expect(page.locator(".answer-links a")).toHaveAttribute("href", postSpeechSource.url);
      await expect(page.locator(".answer-links a")).toHaveAttribute("rel", "noopener noreferrer");
      await expect(page.locator(".citation-status")).toContainText("not independent fact-checking");
      await expect(page.locator(".source-tag")).toContainText("Model-selected");
    } else {
      await expect(page.locator(".answer-links a")).toHaveCount(0);
      await expect(page.locator("#error")).toHaveText("References unavailable.");
    }
    await page.locator("#end").click();
    expect(completed).toHaveLength(1);
  });
}

for (const action of ["interrupt", "new-question", "end", "suspend"]) {
  test(`pending references cannot survive ${action}`, async ({ page }) => {
    let route: WebSocketRoute | undefined;
    let turn = 0;
    const completed: object[] = [];
    await page.addInitScript(() => {
      const Original = AudioContext;
      window.AudioContext = class extends Original {
        constructor(options?: AudioContextOptions) {
          super(options);
          Reflect.set(window, "__testOutput", this);
        }
      };
    });
    await page.routeWebSocket("**/api/voice", ws => {
      route = ws;
      const send = (event: object) => ws.send(JSON.stringify(event));
      send({ type: "ready", sample_rate: 24000 });
      ws.onMessage(raw => {
        if (typeof raw !== "string") return;
        const event = JSON.parse(raw);
        if (event.type === "text" || event.type === "interrupt") send({ type: "interrupt", turn: ++turn });
        if (event.type === "playback" && event.turn === 1) {
          send({ type: "answer", id: "old", turn: 1, sources: [], citation_state: "pending" });
          send({ type: "transcript", id: "old", turn: 1, role: "assistant", text: "Test answer.", final: true });
          send({ type: "audio", id: "old", item_id: "old-audio", content_index: 0, turn: 1,
            data: Buffer.alloc(48000 * 3).toString("base64") });
          send({ type: "done", id: "old", turn: 1, completed: true });
        }
        if (event.type === "playback_complete") completed.push(event);
      });
    });
    await page.goto("/");
    await page.locator("#question").fill("Test-only first question");
    await page.locator("#send").click();
    await expect(page.locator("#status-label")).toHaveText("Speaking");
    if (action === "new-question") {
      await page.locator("#question").fill("Test-only replacement");
      await page.locator("#send").click();
    } else if (action === "suspend") {
      await page.evaluate(() => Reflect.get(window, "__testOutput").suspend());
      await expect(page.locator("#error")).toContainText("suspended");
    } else await page.locator(action === "end" ? "#end" : "#interrupt").click();
    await expect(page.locator(".citation-status")).toContainText("cancelled");
    if (action === "interrupt" || action === "new-question") {
      route?.send(JSON.stringify({ type: "citations", turn: 1, id: "old", state: "completed", sources: [postSpeechSource] }));
      await expect(page.locator("#status-label")).toHaveText("Ready for a question");
      await page.locator("#end").click();
    }
    await expect(page.locator(".answer-links a")).toHaveCount(0);
    expect(completed).toHaveLength(0);
  });
}

for (const audio of [false, true]) {
  test(`no fabricated playback with ${audio ? "unsupported output timestamps" : "zero audio"}`, async ({ page }) => {
    const completed: Record<string, unknown>[] = [];
    if (audio) await page.addInitScript(() => Object.defineProperty(AudioContext.prototype, "getOutputTimestamp", { value: undefined }));
    await page.routeWebSocket("**/api/voice", ws => {
      const send = (event: object) => ws.send(JSON.stringify(event));
      send({ type: "ready", sample_rate: 24000 });
      ws.onMessage(raw => {
        if (typeof raw !== "string") return;
        const event = JSON.parse(raw);
        if (event.type === "text") send({ type: "interrupt", turn: 1 });
        if (event.type === "playback") {
          send({ type: "answer", id: "r", turn: 1, sources: [], citation_state: "pending" });
          if (audio) send({ type: "audio", id: "r", turn: 1, item_id: "pcm", content_index: 0,
            data: Buffer.alloc(4800).toString("base64") });
          send({ type: "done", id: "r", turn: 1, completed: true });
        }
        if (event.type === "playback_complete") completed.push(event);
      });
    });
    await page.goto("/");
    await page.locator("#question").fill("Test-only no playback");
    await page.locator("#send").click();
    await expect.poll(() => completed.length).toBe(1);
    expect(completed[0].outcome).toBe(audio ? "unavailable" : "no_audio");
    await expect(page.locator(".answer-links a")).toHaveCount(0);
    await page.locator("#end").click();
  });
}

function activity(overrides: Record<string, unknown> = {}) {
  return {
    type: "tool_activity", session_id: "test-session", call_id: "search1", turn: 1,
    tool: "webiq.news", state: "running", scope: "mcp_round_trip", elapsed_ms: null,
    source_count: null, response_received: false, ...overrides,
  };
}

async function activitySession(page: Page): Promise<WebSocketRoute> {
  let route: WebSocketRoute | undefined;
  await page.routeWebSocket("**/api/voice", ws => {
    route = ws;
    ws.send(JSON.stringify({ type: "ready", model: "TEST ONLY", sample_rate: 24000 }));
    ws.onMessage(message => {
      if (typeof message === "string" && JSON.parse(message).type === "text") {
        ws.send(JSON.stringify({ type: "interrupt", turn: 1 }));
      }
    });
  });
  await page.goto("/");
  await page.locator("#question").fill("Test-only news?");
  await page.locator("#send").click();
  await expect(page.locator("#status-label")).toHaveText("Ready for a question");
  if (!route) throw new Error("Test WebSocket did not open.");
  return route;
}

test("live tool panel shows dispatch, measured return and distinct citation time", async ({ page }) => {
  const ws = await activitySession(page);
  ws.send(JSON.stringify(activity()));
  const search = page.locator(".search-tool");
  await expect(search).toContainText("Dispatched · Running");
  await expect(search.locator(".tool-duration")).toHaveText("In flight");
  await expect(page.locator("#tool-activity-announcement")).toContainText("WebIQ news, turn 1");
  ws.send(JSON.stringify(activity({
    state: "completed", elapsed_ms: 321.456, source_count: 3, response_received: true,
  })));
  await expect(search.locator(".tool-duration")).toHaveText("321.5 ms");
  await expect(search).toContainText("Returned · 3 usable sources");
  await expect(search).toHaveAttribute("title", /Excludes connection setup, model generation/);
  ws.send(JSON.stringify(activity({
    call_id: "cite1", tool: "cite_sources", scope: "local_citation_validation",
    state: "completed", elapsed_ms: .032, source_count: 2,
  })));
  await expect(page.locator(".citation-tool")).toContainText("Local validation · not search");
  await expect(page.locator(".citation-tool .tool-duration")).toHaveText("<0.1 ms");
  await expect(page.locator(".citation-tool")).toContainText("2 sources selected");
  ws.send(JSON.stringify({ type: "done", id: "test-answer", turn: 1 }));
  await page.locator("#end").click();
  await expect(page.locator(".tool-row")).toHaveCount(2);
  await page.screenshot({ path: "test-results/tool-timing-light.png", fullPage: true });
  for (const width of [820, 390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await search.evaluate(row => row.scrollWidth <= row.clientWidth)).toBe(true);
  }
  await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
  await page.screenshot({ path: "test-results/tool-timing-mobile-dark.png", fullPage: true });
});

test("tool failures, cancellation and stale completions never look successful", async ({ page }) => {
  const ws = await activitySession(page);
  ws.send(JSON.stringify(activity()));
  await expect(page.locator(".tool-row")).toHaveAttribute("data-state", "running");
  ws.send(JSON.stringify({ type: "interrupt", turn: 2 }));
  await expect(page.locator(".tool-row")).toContainText("Cancelled · Final timing unavailable");
  ws.send(JSON.stringify(activity({ state: "completed", elapsed_ms: 17, source_count: 4 })));
  await expect(page.locator(".tool-row")).toHaveAttribute("data-state", "cancelled");
  ws.send(JSON.stringify(activity({ state: "cancelled", elapsed_ms: 44.8 })));
  await expect(page.locator(".tool-duration")).toHaveText("44.8 ms");
  await expect(page.locator(".tool-row")).toContainText("Time until abort");
  ws.send(JSON.stringify(activity({
    call_id: "failed2", turn: 2, state: "failed", elapsed_ms: 1000.4,
  })));
  await expect(page.locator('[data-call-id="failed2"]')).toContainText("Failed");
  await expect(page.locator('[data-call-id="failed2"] .tool-duration')).toHaveText("1,000.4 ms");
  await expect(page.locator('.tool-row[data-state="completed"]')).toHaveCount(0);
  await page.locator("#end").click();
});

test("disconnected tool outcome is unknown and activity history stays bounded", async ({ page }) => {
  const ws = await activitySession(page);
  for (let turn = 1; turn <= 10; turn++) {
    ws.send(JSON.stringify(activity({
      call_id: `done${turn}`, turn, state: "completed", elapsed_ms: 100 + turn, source_count: 1,
    })));
  }
  ws.send(JSON.stringify(activity({ call_id: "in-flight", turn: 11 })));
  await expect(page.locator(".tool-row")).toHaveCount(8);
  await expect(page.locator('[data-call-id="done1"]')).toHaveCount(0);
  await expect(page.locator('[data-call-id="in-flight"]')).toHaveAttribute("data-state", "running");
  ws.close({ code: 1011 });
  const pending = page.locator('[data-call-id="in-flight"]');
  await expect(pending).toContainText("Disconnected · Final timing unavailable");
  await expect(pending.locator(".tool-duration")).toHaveText("—");
  await expect(page.locator(".tool-row")).toHaveCount(8);
});

test("performance logging failures are visible without stopping the demo", async ({ page }) => {
  await page.route("**/api/health", route => route.fulfill({ json: {
    agent_configured: true, webiq_configured: true,
    performance_logging: { enabled: true, state: "warning", dropped: 2, error_code: "write_failed" },
  } }));
  await page.goto("/");
  await expect(page.locator("#telemetry-notice")).toContainText("logging is incomplete");
  await expect(page.locator("#send")).toBeEnabled();
});

test("first playback reports use browser clocks and contain no question or audio", async ({ page }) => {
  const reports: Record<string, unknown>[] = [];
  await page.routeWebSocket("**/api/voice", ws => {
    const send = (event: object) => ws.send(JSON.stringify(event));
    send({ type: "ready", model: "TEST ONLY", sample_rate: 24000, performance_logging: true });
    ws.onMessage(message => {
      if (typeof message !== "string") return;
      const event = JSON.parse(message);
      if (event.type === "text") send({ type: "interrupt", turn: 1, client_turn_id: event.client_turn_id });
      if (event.type === "playback") {
        send({ type: "answer", id: "r1", turn: 1, sources: [] });
        for (let i = 0; i < 2; i++) send({
          type: "audio", id: "r1", item_id: "item", turn: 1, content_index: 0,
          data: Buffer.alloc(24000).toString("base64"),
        });
        send({ type: "done", id: "r1", turn: 1 });
      }
      if (event.type === "client_timing") reports.push(event);
    });
  });
  await page.goto("/");
  await page.locator("#question").fill("PRIVATE QUESTION MARKER");
  await page.locator("#send").click();
  await expect.poll(() => reports.filter(r => r.phase === "playback_started").length).toBe(1);
  const receipt = reports.find(r => r.phase === "audio_received");
  const playback = reports.find(r => r.phase === "playback_started");
  expect(receipt).toBeDefined();
  expect(playback).toBeDefined();
  expect(Number(playback?.browser_submit_to_playback_ms)).toBeGreaterThan(Number(receipt?.browser_submit_to_receipt_ms));
  expect(Number(playback?.browser_receipt_to_playback_ms)).toBeGreaterThan(0);
  expect(reports.filter(r => r.phase === "audio_received")).toHaveLength(1);
  expect(JSON.stringify(reports)).not.toContain("PRIVATE");
  expect(reports.every(r => !("text" in r) && !("data" in r))).toBe(true);
  await page.locator("#end").click();
});

test("startup indicator is immediate, stable and cancellable before audio resume or native readiness", async ({ page }) => {
  let route: WebSocketRoute | undefined;
  let frames = 0;
  await page.addInitScript(() => {
    const resume = AudioContext.prototype.resume;
    AudioContext.prototype.resume = function () {
      Reflect.set(window, "__startupSnapshot", {
        loaderVisible: !document.getElementById("connection-pending")?.hidden,
        controlsHidden: document.getElementById("live-controls")?.hidden,
        clock: document.getElementById("session-clock")?.textContent,
      });
      return new Promise<void>((resolve, reject) => {
        Reflect.set(window, "__resumeAudio", () => { void resume.call(this).then(resolve, reject); });
      });
    };
  });
  await page.routeWebSocket("**/api/voice", ws => {
    route = ws;
    ws.onMessage(message => { if (typeof message !== "string") frames++; });
  });
  await page.goto("/?scoutTheme=light");
  const idleBox = await page.locator("#voice-stage").boundingBox();
  await page.locator("#start").focus();
  await page.keyboard.press("Enter");
  expect(await page.evaluate(() => Reflect.get(window, "__startupSnapshot"))).toEqual({
    loaderVisible: true, controlsHidden: true, clock: "Connecting…",
  });
  await expect(page.locator("#connection-status")).toContainText("Connecting…");
  await expect(page.locator("#connection-status")).toHaveAttribute("role", "status");
  await expect(page.locator("#connection-status")).toHaveAttribute("aria-live", "polite");
  await expect(page.locator(".connecting-dots")).toHaveAttribute("aria-hidden", "true");
  await expect(page.locator(".connecting-dots i")).toHaveCount(3);
  await expect(page.locator("#session-actions")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#cancel-connection")).toBeFocused();
  await expect(page.locator("#start")).toBeHidden();
  await expect(page.locator("#live-controls")).toBeHidden();
  expect((await page.locator("#voice-stage").boundingBox())?.height).toBe(idleBox?.height);
  await page.screenshot({ path: "test-results/connecting-light.png", fullPage: true });
  await page.evaluate(() => Reflect.get(window, "__resumeAudio")());
  await expect.poll(() => Boolean(route)).toBe(true);
  if (!route) throw new Error("Expected pending connection");
  route.send(JSON.stringify({ type: "state", state: "listening" }));
  await expect(page.locator("#status-label")).toHaveText("Connecting");
  await expect(page.locator("#live-controls")).toBeHidden();
  expect(frames).toBe(0);
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.emulateMedia({ reducedMotion: "reduce" });
  expect(await page.locator(".connecting-dots i").evaluateAll(dots =>
    dots.every(dot => getComputedStyle(dot).animationName === "none"),
  )).toBe(true);
  await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
  await page.screenshot({ path: "test-results/connecting-mobile-dark.png", fullPage: true });
  const pendingBox = await page.locator("#voice-stage").boundingBox();
  route.send(JSON.stringify({ type: "ready", model: "TEST ONLY", sample_rate: 24000 }));
  await expect(page.locator("#connection-pending")).toBeHidden();
  await expect(page.locator("#live-controls")).toBeVisible();
  await expect(page.locator("#session-actions")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#session-clock")).toContainText("0:00 / 10:00");
  await expect(page.locator("#mute")).toBeFocused();
  await expect.poll(() => frames).toBeGreaterThan(0);
  expect((await page.locator("#voice-stage").boundingBox())?.height).toBe(pendingBox?.height);
  await page.screenshot({ path: "test-results/connected-mobile-dark.png", fullPage: true });
  await page.locator("#end").click();
});

test("pending text startup neither requests microphone nor steals composer focus on readiness", async ({ page }) => {
  let route: WebSocketRoute | undefined;
  const texts: string[] = [];
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = () => {
      Reflect.set(window, "__unexpectedMic", true);
      return Promise.reject(new Error("Text mode must not request microphone"));
    };
  });
  await page.routeWebSocket("**/api/voice", ws => {
    route = ws;
    ws.onMessage(message => {
      if (typeof message !== "string") return;
      const event = JSON.parse(message);
      if (event.type === "text") texts.push(event.text);
    });
  });
  await page.goto("/");
  await page.locator("#question").fill("First question");
  await page.locator("#send").click();
  await expect(page.locator("#connection-pending")).toBeVisible();
  await expect(page.locator("#micro-note")).toHaveText("Text mode: microphone stays off.");
  await expect(page.locator("#question")).toBeFocused();
  await expect(page.locator("#send")).toBeDisabled();
  await page.evaluate(() => {
    document.getElementById("start")?.click();
    document.getElementById("start")?.click();
  });
  await expect.poll(() => Boolean(route)).toBe(true);
  if (!route) throw new Error("Expected connection");
  route.send(JSON.stringify({ type: "ready", model: "TEST ONLY", sample_rate: 24000 }));
  await expect(page.locator("#live-controls")).toBeVisible();
  await expect(page.locator("#question")).toBeFocused();
  await expect.poll(() => texts.length).toBe(1);
  expect(texts).toEqual(["First question"]);
  expect(await page.evaluate(() => Reflect.get(window, "__unexpectedMic"))).toBeUndefined();
  await page.locator("#end").click();
});

test("cancelling microphone permission cleans a late stream without reviving the old attempt", async ({ page }) => {
  let route: WebSocketRoute | undefined;
  await page.addInitScript(() => {
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async constraints => {
      const stream = await original(constraints);
      Reflect.set(window, "__lateStream", stream);
      return new Promise<MediaStream>(resolve => {
        Reflect.set(window, "__grantLatePermission", () => resolve(stream));
      });
    };
  });
  await page.routeWebSocket("**/api/voice", ws => { route = ws; });
  await page.goto("/");
  await page.locator("#start").click();
  await expect(page.locator("#voice-caption")).toContainText("Allow access");
  await expect.poll(() => page.evaluate(() => typeof Reflect.get(window, "__grantLatePermission"))).toBe("function");
  await page.locator("#cancel-connection").click();
  await expect(page.locator("#error")).toContainText("Connection cancelled");
  await expect(page.locator("#connection-pending")).toBeHidden();
  await expect(page.locator("#start")).toBeFocused();
  await page.locator("#question").fill("Retry using text");
  await page.locator("#send").click();
  await expect(page.locator("#error")).toBeHidden();
  await expect.poll(() => Boolean(route)).toBe(true);
  await page.evaluate(() => Reflect.get(window, "__grantLatePermission")());
  await expect.poll(() => page.evaluate(() => {
    const stream = Reflect.get(window, "__lateStream");
    return stream instanceof MediaStream && stream.getTracks().every(track => track.readyState === "ended");
  })).toBe(true);
  await expect(page.locator("#connection-pending")).toBeVisible();
  await expect(page.locator("#live-controls")).toBeHidden();
  if (!route) throw new Error("Expected retry connection");
  route.send(JSON.stringify({ type: "ready", model: "TEST ONLY", sample_rate: 24000 }));
  await expect(page.locator("#status-label")).toHaveText("Ready for a question");
  await page.locator("#end").click();
});

test("cancelling service setup promptly releases mic and audio context and ignores stale ready", async ({ page }) => {
  let connected = false;
  await page.routeWebSocket("**/api/voice", () => { connected = true; });
  await page.goto("/");
  await page.evaluate(() => {
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async constraints => {
      const stream = await original(constraints);
      Reflect.set(window, "__pendingMic", stream);
      return stream;
    };
    const resume = AudioContext.prototype.resume;
    AudioContext.prototype.resume = function () {
      Reflect.set(window, "__pendingContext", this);
      return resume.call(this);
    };
    const Original = window.WebSocket;
    window.WebSocket = class extends Original {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        Reflect.set(window, "__pendingSocket", this);
      }
    };
  });
  await page.locator("#start").click();
  await expect.poll(() => connected).toBe(true);
  await expect(page.locator("#live-controls")).toBeHidden();
  await page.locator("#cancel-connection").click();
  await expect.poll(() => page.evaluate(() => {
    const stream = Reflect.get(window, "__pendingMic");
    const context = Reflect.get(window, "__pendingContext");
    return stream instanceof MediaStream && stream.getTracks().every(track => track.readyState === "ended") &&
      context instanceof AudioContext && context.state === "closed";
  })).toBe(true);
  await page.evaluate(() => {
    const socket = Reflect.get(window, "__pendingSocket");
    socket.onmessage?.call(socket, new MessageEvent("message", {
      data: JSON.stringify({ type: "ready", model: "TEST ONLY", sample_rate: 24000 }),
    }));
  });
  await expect(page.locator("#start")).toBeVisible();
  await expect(page.locator("#start")).toBeFocused();
  await expect(page.locator("#connection-pending")).toBeHidden();
  await expect(page.locator("#live-controls")).toBeHidden();
  await expect(page.locator("#error")).toContainText("Connection cancelled");
});

test("audio startup rejection restores the CTA before any network connection", async ({ page }) => {
  await page.addInitScript(() => {
    AudioContext.prototype.resume = () => Promise.reject(new Error("Test audio startup failure."));
  });
  await page.goto("/");
  await page.locator("#start").click();
  await expect(page.locator("#error")).toContainText("Test audio startup failure");
  await expect(page.locator("#start")).toBeVisible();
  await expect(page.locator("#start")).toBeFocused();
  await expect(page.locator("#connection-pending")).toBeHidden();
  await expect(page.locator("#live-controls")).toBeHidden();
});

for (const failure of ["error", "disconnect", "timeout"] as const) {
  test(`startup ${failure} restores actionable controls and retry ignores late readiness`, async ({ page }) => {
    const routes: WebSocketRoute[] = [];
    await page.routeWebSocket("**/api/voice", ws => { routes.push(ws); });
    await page.clock.install();
    await page.goto("/");
    await page.evaluate(() => {
      const Original = window.WebSocket;
      const sockets: WebSocket[] = [];
      Reflect.set(window, "__startupSockets", sockets);
      window.WebSocket = class extends Original {
        constructor(url: string | URL, protocols?: string | string[]) {
          super(url, protocols);
          sockets.push(this);
        }
      };
    });
    await page.locator("#question").fill("Test startup");
    await page.locator("#send").click();
    await expect.poll(() => routes.length).toBe(1);
    await page.evaluate(() => {
      const old = Reflect.get(window, "__startupSockets")[0];
      Reflect.set(window, "__lateReady", () => old.onmessage?.call(old, new MessageEvent("message", {
        data: JSON.stringify({ type: "ready", model: "TEST ONLY", sample_rate: 24000 }),
      })));
    });
    if (failure === "error") routes[0].send(JSON.stringify({ type: "error", message: "Test connection failed.", fatal: false }));
    else if (failure === "disconnect") routes[0].close({ code: 1011 });
    else await page.clock.fastForward(65_001);
    await expect(page.locator("#error")).toContainText(failure === "error" ?
      "Test connection failed" : failure === "disconnect" ? "connection closed" : "Connecting took too long");
    await expect(page.locator("#start")).toBeVisible();
    await expect(page.locator("#connection-pending")).toBeHidden();
    await expect(page.locator("#live-controls")).toBeHidden();
    await expect(page.locator("#send")).toBeEnabled();
    await page.locator("#question").fill("Retry startup");
    await page.locator("#send").click();
    await expect.poll(() => routes.length).toBe(2);
    await page.evaluate(() => Reflect.get(window, "__lateReady")());
    await expect(page.locator("#connection-pending")).toBeVisible();
    await expect(page.locator("#live-controls")).toBeHidden();
    routes[1].send(JSON.stringify({ type: "ready", model: "TEST ONLY", sample_rate: 24000 }));
    await expect(page.locator("#live-controls")).toBeVisible();
    await expect(page.locator("#error")).toBeHidden();
    await page.locator("#end").click();
  });
}
