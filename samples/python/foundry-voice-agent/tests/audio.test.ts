// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

import { describe, expect, it, vi } from "vitest";
import { AudioPlayer, AudioPlaybackError, decodePCM, playedPositions } from "../src/audio";
import { PCMResampler } from "../public/audio-worklet.js";

describe("PCM capture and playback", () => {
  it.each([16000, 24000, 44100, 48000])("resamples %i Hz to exactly 24 kHz across block boundaries", (rate) => {
    const resampler = new PCMResampler(rate);
    let count = 0;
    for (let offset = 0; offset < rate; offset += 128) {
      const block = new Float32Array(Math.min(128, rate - offset)).fill(.5);
      const output = resampler.process(block);
      count += output.length;
      expect(output.every((n: number) => n === 16384)).toBe(true);
    }
    expect(count).toBe(24000);
  });

  function playbackFixture() {
    const nodes: { stop: ReturnType<typeof vi.fn>; start: ReturnType<typeof vi.fn> }[] = [];
    const context = {
      currentTime: 0, state: "running", destination: {},
      createBuffer(_channels: number, length: number, rate: number) {
        return { duration: length / rate, copyToChannel: vi.fn() };
      },
      createBufferSource() {
        const node = { buffer: null, connect: vi.fn(), disconnect: vi.fn(), start: vi.fn(), stop: vi.fn(), onended: null };
        nodes.push(node);
        return node;
      },
    };
    return { context, nodes, player: new AudioPlayer(context as unknown as AudioContext) };
  }

  it("buffers a valid 90s answer delivered instantly, including the former 30s cutoff", () => {
    const { player, context, nodes } = playbackFixture();
    const second = btoa("\0\0".repeat(24000));
    for (let i = 0; i < 90; i++) player.enqueue(second, "answer", 0);
    expect(player.diagnostics().browser_received_audio_ms).toBe(90_000);
    expect(player.diagnostics().browser_queue_ms).toBeCloseTo(90_015);
    expect(() => player.enqueue(second, "answer", 0)).toThrow("90-second");
    context.currentTime = 2.8;
    expect(player.clear()[0].audio_end_ms).toBe(2785);
    expect(nodes).toHaveLength(90);
    for (const node of nodes) expect(node.stop).toHaveBeenCalledOnce();
    expect(() => player.enqueue(second, "next", 0)).not.toThrow();
  });

  it("detects a genuinely stalled clock, not a fast-growing queue", () => {
    const { player, context } = playbackFixture();
    const now = vi.spyOn(performance, "now").mockReturnValue(0);
    player.enqueue(btoa("\0\0".repeat(24000)), "answer", 0);
    now.mockReturnValue(4999);
    expect(() => player.checkProgress()).not.toThrow();
    context.currentTime = .1;
    player.checkProgress();
    now.mockReturnValue(9999);
    try { player.checkProgress(); expect.fail("Expected stalled clock"); }
    catch (error) {
      expect(error).toBeInstanceOf(AudioPlaybackError);
      expect(error).toMatchObject({ code: "playback_stalled", diagnostics: { browser_clock_stall_ms: 5000 } });
    }
    now.mockRestore();
  });

  it("distinguishes suspension and caps tiny-chunk resource exhaustion", () => {
    const { player, context } = playbackFixture();
    for (let i = 0; i < 5000; i++) player.enqueue("AAA=", "answer", 0);
    expect(() => player.enqueue("AAA=", "answer", 0)).toThrow("bounded");
    player.clear();
    context.state = "suspended";
    expect(() => player.enqueue("AAA=", "answer", 0)).toThrow("suspended");
  });

  it("clips input and decodes signed PCM16 little-endian", () => {
    const resampler = new PCMResampler(24000);
    expect(resampler.process([2, -2, 0])).toEqual([32767, -32768, 0]);
    expect([...decodePCM(btoa("\0\x80\xff\x7f\0\0"))]).toEqual([-1, 32767 / 32768, 0]);
    expect(() => decodePCM("AA==")).toThrow("Invalid PCM");
  });

  it("waits for the output device timeline, not just rendered node completion", () => {
    const { context, nodes, player } = playbackFixture();
    const stamp = { contextTime: 0, performanceTime: 1 };
    Object.assign(context, { getOutputTimestamp: () => stamp });
    expect(player.completion()).toBe("no_audio");
    player.enqueue(btoa("\0\0".repeat(24000)), "answer", 0);
    expect(player.completion()).toBeNull();
    const node = nodes[0] as typeof nodes[0] & { onended: () => void };
    context.currentTime = 2;
    node.onended();
    expect(player.playing).toBe(false);
    expect(player.completion()).toBeNull();
    stamp.contextTime = 1.02;
    expect(player.completion()).toBe("played");
  });

  it("reports missing output timestamps rather than inventing playback completion", () => {
    const { context, nodes, player } = playbackFixture();
    player.enqueue("AAA=", "answer", 0);
    (nodes[0] as typeof nodes[0] & { onended: () => void }).onended();
    expect(player.completion()).toBe("unavailable");
    context.state = "suspended";
    expect(() => player.completion()).toThrow("suspended");
  });

  it("truncates using played time, not received or scheduled audio length", () => {
    const segments = [
      { item: "a", index: 0, start: 10, duration: .5 },
      { item: "a", index: 0, start: 10.5, duration: .5 },
      { item: "b", index: 0, start: 11, duration: .5 },
    ];
    expect(playedPositions(segments, 10.7)).toEqual([
      { item_id: "a", content_index: 0, audio_end_ms: 699 },
      { item_id: "b", content_index: 0, audio_end_ms: 0 },
    ]);
  });

  it("interruption stops every scheduled node and permits clean new playback", () => {
    const nodes: { stop: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn> }[] = [];
    vi.stubGlobal("AudioContext", class {
      currentTime = 0;
      state = "running";
      destination = {};
      createBuffer(_channels: number, length: number, rate: number) {
        return { duration: length / rate, copyToChannel: vi.fn() };
      }
      createBufferSource() {
        const node = { buffer: null, connect: vi.fn(), disconnect: vi.fn(), start: vi.fn(), stop: vi.fn(), onended: null };
        nodes.push(node);
        return node;
      }
    });
    const player = new AudioPlayer(new AudioContext());
    player.enqueue(btoa("\0\0".repeat(480)), "a", 0);
    player.enqueue(btoa("\0\0".repeat(480)), "a", 0);
    expect(player.playing).toBe(true);
    expect(player.clear()[0].audio_end_ms).toBe(0);
    expect(player.playing).toBe(false);
    nodes.forEach(node => expect(node.stop).toHaveBeenCalledOnce());
    player.enqueue(btoa("\0\0".repeat(480)), "b", 0);
    expect(player.playing).toBe(true);
    vi.unstubAllGlobals();
  });
});
