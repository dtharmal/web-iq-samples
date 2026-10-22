// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

import { afterEach, describe, expect, it, vi } from "vitest";
import { BrowserPerformance, outputStartPerformanceTime } from "../src/performance";

describe("browser-only latency clocks", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
  it("maps Web Audio output positions using the browser-provided clock pair", () => {
    expect(outputStartPerformanceTime({ contextTime: 2, performanceTime: 5000 }, 1.9)).toBeCloseTo(4900);
    expect(outputStartPerformanceTime({ contextTime: 1, performanceTime: 5000 }, 1.9)).toBeNull();
    expect(outputStartPerformanceTime({ contextTime: 0, performanceTime: 0 }, .015)).toBeNull();
    expect(outputStartPerformanceTime({}, .015)).toBeNull();
  });

  it("does not render or send timing when logging is disabled", () => {
    const send = vi.fn();
    const trace = new BrowserPerformance(send);
    trace.beginTurn(1, null);
    trace.render({ type: "tool_activity", turn: 1, call_id: "c", state: "completed" }, 2);
    expect(send).not.toHaveBeenCalled();
  });

  it("excludes startup greetings from answer timing and turn-one-only telemetry", () => {
    const send = vi.fn();
    const trace = new BrowserPerformance(send);
    trace.enabled = true;
    trace.firstAudio("hello", {} as AudioContext, { contextStart: 1, leadMs: 15 }, 1000);
    trace.render({ type: "answer", turn: 0, id: "hello" }, 1);
    expect(send).not.toHaveBeenCalled();
    trace.beginTurn(1, null);
    trace.firstAudio("answer", {} as AudioContext, { contextStart: 2, leadMs: 15 }, 2000);
    expect(send.mock.calls.map(call => call[0].phase)).toEqual(["audio_received", "playback_unavailable"]);
    expect(send.mock.calls.every(call => call[0].turn === 1)).toBe(true);
  });

  it("reports bounded playback milestones once per answer, separately from failures", () => {
    const send = vi.fn();
    const trace = new BrowserPerformance(send);
    trace.enabled = true;
    trace.playbackDiagnostics = true;
    trace.beginTurn(1, null);
    const values = { browser_queue_ms: 31000, browser_audio_context_ms: 1000,
      browser_clock_stall_ms: 0, browser_received_audio_ms: 32000, context_state: "running" as const };
    trace.playbackDiagnostic("r", values);
    trace.playbackDiagnostic("r", values);
    trace.playbackDiagnostic("r", { ...values, browser_clock_stall_ms: 5000 }, "playback_stalled");
    expect(send.mock.calls.map(call => call[0].phase)).toEqual(["playback_buffered", "playback_error"]);
    expect(send.mock.calls[1][0]).toMatchObject({ code: "playback_stalled", turn: 1 });
  });

  it("keeps new diagnostics off for older servers and clears negotiated capability on reset", () => {
    const send = vi.fn();
    const trace = new BrowserPerformance(send);
    const values = { browser_queue_ms: 31000, browser_audio_context_ms: 1000,
      browser_clock_stall_ms: 0, browser_received_audio_ms: 32000, context_state: "running" as const };
    trace.enabled = true;
    trace.beginTurn(1, null);
    trace.playbackDiagnostic("old-server", values);
    expect(send).not.toHaveBeenCalled();
    trace.playbackDiagnostics = true;
    trace.playbackDiagnostic("new-server", values);
    expect(send).toHaveBeenCalledTimes(1);
    trace.reset();
    trace.enabled = true;
    trace.beginTurn(1, null);
    trace.playbackDiagnostic("next-old-server", values);
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("correlates submissions by ID rather than the next arriving speech turn", () => {
    const send = vi.fn();
    const trace = new BrowserPerformance(send);
    trace.enabled = true;
    const id = trace.submitted(100);
    trace.beginTurn(1, null);
    trace.beginTurn(2, id);
    trace.render({ type: "tool_activity", turn: 1, call_id: "stale", state: "completed" }, 3);
    trace.render({ type: "tool_activity", turn: 2, call_id: "current", state: "completed" }, 4);
    trace.render({ type: "tool_activity", turn: 2, call_id: "current", state: "completed" }, 8);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0][0]).toMatchObject({ turn: 2, browser_render_ms: 4 });
  });

  it("interruption cancels pending output observation and rejects stale callbacks", () => {
    let callback: FrameRequestCallback | undefined;
    vi.stubGlobal("requestAnimationFrame", (fn: FrameRequestCallback) => { callback = fn; return 1; });
    const cancel = vi.fn();
    vi.stubGlobal("cancelAnimationFrame", cancel);
    vi.spyOn(performance, "now").mockReturnValue(1000);
    vi.stubGlobal("AudioContext", class {
      state = "running";
      getOutputTimestamp() { return { contextTime: 0, performanceTime: 0 }; }
    });
    const send = vi.fn();
    const trace = new BrowserPerformance(send);
    trace.enabled = true;
    trace.beginTurn(1, null);
    trace.firstAudio("r1", new AudioContext(), { contextStart: .015, leadMs: 15 }, 1000);
    trace.beginTurn(2, null);
    expect(cancel).toHaveBeenCalledWith(1);
    callback?.(1100);
    expect(send.mock.calls.map(call => call[0].phase)).toEqual(["audio_received"]);
  });

  it("unsupported output timestamps are explicitly unavailable, not zero-latency success", () => {
    vi.stubGlobal("AudioContext", class {
      state = "running";
      getOutputTimestamp() { throw new Error("Unsupported output timing"); }
    });
    const send = vi.fn();
    const trace = new BrowserPerformance(send);
    trace.enabled = true;
    trace.beginTurn(1, null);
    trace.firstAudio("r1", new AudioContext(), { contextStart: .015, leadMs: 15 }, performance.now());
    expect(send.mock.calls.map(call => call[0].phase)).toEqual(["audio_received", "playback_unavailable"]);
    expect(send.mock.calls[1][0]).not.toHaveProperty("browser_receipt_to_playback_ms");
  });
});
