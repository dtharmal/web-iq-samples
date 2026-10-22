// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

type Report = Record<string, string | number>;
import type { PlaybackDiagnostics, PlaybackErrorCode } from "./audio";
export type ScheduledAudio = { contextStart: number; leadMs: number };

// Both sides of this mapping are supplied by Web Audio, not guessed from server timestamps.
export function outputStartPerformanceTime(timestamp: AudioTimestamp, contextStart: number): number | null {
  const { contextTime, performanceTime } = timestamp;
  if (contextTime === undefined || performanceTime === undefined ||
      !Number.isFinite(contextTime) || !Number.isFinite(performanceTime) ||
      contextTime < contextStart || performanceTime <= 0) return null;
  return performanceTime - (contextTime - contextStart) * 1000;
}

export class BrowserPerformance {
  enabled = false;
  playbackDiagnostics = false;
  private submissions = new Map<string, number>();
  private submittedAt: number | null = null;
  private turn = 0;
  private seen = new Set<string>();
  private frames = new Set<number>();
  private epoch = 0;
  constructor(private readonly send: (report: Report) => void) {}

  reset(): void {
    this.cancelPending();
    this.enabled = false;
    this.playbackDiagnostics = false;
    this.turn = 0;
    this.submittedAt = null;
    this.submissions.clear();
    this.seen.clear();
  }

  submitted(at = performance.now()): string {
    const id = crypto.randomUUID();
    this.submissions.set(id, at);
    if (this.submissions.size > 40) {
      const oldest = this.submissions.keys().next().value;
      if (oldest !== undefined) this.submissions.delete(oldest);
    }
    return id;
  }

  beginTurn(turn: number, id: unknown): void {
    this.cancelPending();
    this.turn = turn;
    this.submittedAt = typeof id === "string" ? this.submissions.get(id) ?? null : null;
    if (typeof id === "string") this.submissions.delete(id);
  }

  cancelPending(): void {
    this.epoch++;
    this.frames.forEach(frame => cancelAnimationFrame(frame));
    this.frames.clear();
  }

  playbackDiagnostic(responseId: string, details: PlaybackDiagnostics, code?: PlaybackErrorCode): void {
    if (!this.enabled || !this.playbackDiagnostics || !this.turn) return;
    const phase = code ? "playback_error" : "playback_buffered";
    this.reportOnce(`${phase}:${responseId}`, {
      type: "client_timing", phase, turn: this.turn, response_id: responseId,
      ...details, ...(code ? { code } : {}),
    });
  }

  render(event: Record<string, unknown>, elapsed: number): void {
    if (!this.enabled || !this.turn || event.turn !== this.turn || !Number.isFinite(elapsed)) return;
    const ui = event.type === "evidence" ? "evidence" : event.type === "answer" ? "answer" :
      event.type === "citations" ? "citations" : "activity";
    if (ui === "citations" && event.state !== "completed") return;
    if (ui === "activity" && event.state === "running") return;
    const answer = ui === "answer" || ui === "citations";
    const id = answer ? event.id : event.call_id;
    if (typeof id !== "string") return;
    this.reportOnce(`render:${ui}:${id}`, {
      type: "client_timing", phase: "ui_render", turn: this.turn, ui,
      [answer ? "response_id" : "call_id"]: id, browser_render_ms: elapsed,
    });
  }

  firstAudio(responseId: string, context: AudioContext, scheduled: ScheduledAudio, receivedAt: number): void {
    if (!this.enabled || !this.turn || this.seen.has(`audio:${responseId}`)) return;
    const turn = this.turn;
    const epoch = this.epoch;
    const common = { type: "client_timing", turn, response_id: responseId };
    const receipt: Report = { ...common, phase: "audio_received" };
    if (this.submittedAt !== null) receipt.browser_submit_to_receipt_ms = receivedAt - this.submittedAt;
    this.reportOnce(`audio:${responseId}`, receipt);
    const unavailable = () => this.reportOnce(`playback:${responseId}`, { ...common, phase: "playback_unavailable" });
    if (typeof context.getOutputTimestamp !== "function") { unavailable(); return; }
    const submittedAt = this.submittedAt;
    const check = () => {
      if (epoch !== this.epoch || turn !== this.turn) return;
      if (context.state !== "running") { unavailable(); return; }
      let stamp: AudioTimestamp;
      try { stamp = context.getOutputTimestamp(); }
      catch { unavailable(); return; }
      const at = outputStartPerformanceTime(stamp, scheduled.contextStart);
      if (at !== null) {
        if (at < receivedAt || at > performance.now() + 5) { unavailable(); return; }
        const report: Report = {
          ...common, phase: "playback_started", browser_receipt_to_playback_ms: at - receivedAt,
          browser_schedule_lead_ms: scheduled.leadMs,
        };
        if (submittedAt !== null) report.browser_submit_to_playback_ms = at - submittedAt;
        if (Number.isFinite(context.outputLatency) && context.outputLatency >= 0) {
          report.browser_output_latency_ms = context.outputLatency * 1000;
        }
        this.reportOnce(`playback:${responseId}`, report);
      } else if (performance.now() - receivedAt > 3000) unavailable();
      else {
        const frame = requestAnimationFrame(() => { this.frames.delete(frame); check(); });
        this.frames.add(frame);
      }
    };
    check();
  }

  private reportOnce(key: string, report: Report): void {
    if (this.seen.has(key)) return;
    this.seen.add(key);
    this.send(report);
  }
}
