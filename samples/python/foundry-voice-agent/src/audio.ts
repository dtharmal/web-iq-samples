// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

export type PlaybackPosition = { item_id: string; content_index: number; audio_end_ms: number };
type Segment = { item: string; index: number; start: number; duration: number; node: AudioBufferSourceNode };
export type PlaybackDiagnostics = {
  browser_queue_ms: number; browser_audio_context_ms: number; browser_clock_stall_ms: number;
  browser_received_audio_ms: number; context_state: AudioContextState;
};
export type PlaybackErrorCode = "playback_suspended" | "playback_stalled" | "playback_limit" | "playback_invalid";
export class AudioPlaybackError extends Error {
  constructor(readonly code: PlaybackErrorCode, message: string, readonly diagnostics: PlaybackDiagnostics) {
    super(message);
  }
}

export function decodePCM(base64: string): Float32Array<ArrayBuffer> {
  const raw = atob(base64);
  if (raw.length % 2) throw new Error("Invalid PCM audio received.");
  const result = new Float32Array(raw.length / 2);
  for (let i = 0; i < result.length; i++) {
    const word = raw.charCodeAt(i * 2) | (raw.charCodeAt(i * 2 + 1) << 8);
    result[i] = (word > 32767 ? word - 65536 : word) / 32768;
  }
  return result;
}

export function playedPositions(segments: Pick<Segment, "item" | "index" | "start" | "duration">[], now: number): PlaybackPosition[] {
  const positions = new Map<string, PlaybackPosition>();
  for (const part of segments) {
    const key = `${part.item}:${part.index}`;
    const current = positions.get(key) ?? { item_id: part.item, content_index: part.index, audio_end_ms: 0 };
    current.audio_end_ms += Math.max(0, Math.min(part.duration, now - part.start)) * 1000;
    positions.set(key, current);
  }
  return [...positions.values()].map(p => ({ ...p, audio_end_ms: Math.floor(p.audio_end_ms) }));
}

export class AudioPlayer {
  private segments: Segment[] = [];
  private nextTime = 0;
  private active = new Set<AudioBufferSourceNode>();
  private receivedSamples = 0;
  private progressTime = 0;
  private progressClock = 0;
  private drainedAt = 0;
  onDrained: (() => void) | null = null;
  constructor(readonly context: AudioContext) {}

  get playing(): boolean { return this.active.size > 0; }

  completion(): "played" | "no_audio" | "unavailable" | null {
    if (!this.receivedSamples) return "no_audio";
    if (this.playing) return null;
    if (this.context.state !== "running") {
      throw new AudioPlaybackError("playback_suspended", "Audio playback was suspended. End and restart the session.", this.diagnostics());
    }
    // onended tracks rendering, which can run ahead of the output device.
    if (typeof this.context.getOutputTimestamp !== "function") return "unavailable";
    const stamp = this.context.getOutputTimestamp();
    if (stamp.contextTime !== undefined && stamp.performanceTime !== undefined &&
        Number.isFinite(stamp.contextTime) && Number.isFinite(stamp.performanceTime) &&
        stamp.contextTime >= this.nextTime && stamp.performanceTime > 0 &&
        stamp.performanceTime <= performance.now() + 5) return "played";
    return performance.now() - this.drainedAt >= 5000 ? "unavailable" : null;
  }

  diagnostics(): PlaybackDiagnostics {
    return {
      browser_queue_ms: Math.max(0, this.nextTime - this.context.currentTime) * 1000,
      browser_audio_context_ms: this.context.currentTime * 1000,
      browser_clock_stall_ms: this.playing ? Math.max(0, performance.now() - this.progressTime) : 0,
      browser_received_audio_ms: this.receivedSamples / 24,
      context_state: this.context.state,
    };
  }

  checkProgress(): void {
    if (!this.playing) return;
    if (this.context.state !== "running") {
      throw new AudioPlaybackError("playback_suspended", "Audio playback was suspended. End and restart the session.", this.diagnostics());
    }
    if (this.context.currentTime - this.progressClock >= .05) {
      this.progressClock = this.context.currentTime;
      this.progressTime = performance.now();
    } else if (performance.now() - this.progressTime >= 5000) {
      throw new AudioPlaybackError("playback_stalled", "The audio playback clock stopped advancing. Please reconnect.", this.diagnostics());
    }
  }

  enqueue(data: string, item: string, index: number): ScheduledAudio {
    if (this.context.state !== "running") {
      throw new AudioPlaybackError("playback_suspended", "Audio playback is suspended. End and restart the session.", this.diagnostics());
    }
    this.checkProgress();
    if (data.length > 512_000) {
      throw new AudioPlaybackError("playback_invalid", "The audio chunk exceeded the safe limit.", this.diagnostics());
    }
    let samples: Float32Array<ArrayBuffer>;
    try { samples = decodePCM(data); }
    catch {
      throw new AudioPlaybackError("playback_invalid", "Invalid PCM audio received.", this.diagnostics());
    }
    // Foundry can generate faster than playback. Bound received audio, not generation speed.
    if (this.receivedSamples + samples.length > 90 * 24000 || this.segments.length >= 5000) {
      throw new AudioPlaybackError("playback_limit", "The response exceeded the bounded 90-second playback limit.", this.diagnostics());
    }
    if (!this.playing) {
      this.progressClock = this.context.currentTime;
      this.progressTime = performance.now();
    }
    this.receivedSamples += samples.length;
    const buffer = this.context.createBuffer(1, samples.length, 24000);
    buffer.copyToChannel(samples, 0);
    const node = this.context.createBufferSource();
    node.buffer = buffer;
    node.connect(this.context.destination);
    const now = this.context.currentTime;
    const start = Math.max(now + .015, this.nextTime);
    this.nextTime = start + buffer.duration;
    this.segments.push({ item, index, node, start, duration: buffer.duration });
    this.active.add(node);
    node.onended = () => {
      this.active.delete(node);
      node.disconnect();
      if (!this.active.size) {
        this.drainedAt = performance.now();
        this.onDrained?.();
      }
    };
    node.start(start);
    return { contextStart: start, leadMs: (start - now) * 1000 };
  }

  clear(): PlaybackPosition[] {
    const positions = playedPositions(this.segments, this.context.currentTime);
    for (const node of this.active) {
      node.onended = null;
      node.stop();
      node.disconnect();
    }
    this.active.clear();
    this.segments = [];
    this.receivedSamples = 0;
    this.nextTime = this.context.currentTime;
    return positions;
  }
}

export class Microphone {
  private stream: MediaStream | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private worklet: AudioWorkletNode | null = null;
  private closed = false;

  async start(context: AudioContext, send: (buffer: ArrayBuffer) => void): Promise<void> {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("Microphone access requires localhost or HTTPS.");
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    if (this.closed) { stream.getTracks().forEach(track => track.stop()); return; }
    this.stream = stream;
    await context.audioWorklet.addModule("/audio-worklet.js");
    if (this.closed) return;
    this.source = context.createMediaStreamSource(stream);
    this.worklet = new AudioWorkletNode(context, "pcm-capture");
    this.worklet.port.onmessage = (event: MessageEvent<ArrayBuffer>) => send(event.data);
    this.source.connect(this.worklet);
    // The processor emits silence; connecting keeps the render graph alive without mic echo.
    this.worklet.connect(context.destination);
  }

  mute(muted: boolean): void {
    this.stream?.getAudioTracks().forEach(track => { track.enabled = !muted; });
    this.worklet?.port.postMessage({ muted });
  }

  close(): void {
    this.closed = true;
    this.stream?.getTracks().forEach(track => track.stop());
    if (this.worklet) { this.worklet.port.onmessage = null; this.worklet.port.close(); this.worklet.disconnect(); }
    this.source?.disconnect();
    this.stream = null;
  }
}
import type { ScheduledAudio } from "./performance";
