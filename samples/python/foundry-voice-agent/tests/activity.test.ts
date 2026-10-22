// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

import { describe, expect, it } from "vitest";
import { ActivityHistory, formatMilliseconds, parseActivity, type ActivityEvent } from "../src/activity";

const event = (overrides: Partial<ActivityEvent> = {}): ActivityEvent => ({
  session_id: "session1", call_id: "call1", turn: 1, tool: "webiq.news", state: "running",
  scope: "mcp_round_trip", elapsed_ms: null, source_count: null, response_received: false, ...overrides,
});

describe("authoritative activity history", () => {
  it("keeps one correlated row and the exact backend duration", () => {
    const history = new ActivityHistory();
    history.update(event(), 1);
    history.update(event({ state: "completed", elapsed_ms: 321.456, source_count: 3, response_received: true }), 1);
    expect(history.entries).toHaveLength(1);
    expect(history.entries[0]).toMatchObject({ state: "completed", elapsed_ms: 321.456, source_count: 3 });
    expect(history.update(event(), 1)).toBe(false);
    expect(history.update(event({ state: "failed" }), 1)).toBe(false);
    expect(history.entries[0].state).toBe("completed");
  });

  it("does not confuse repeated native call IDs across sessions", () => {
    const history = new ActivityHistory();
    history.update(event(), 1);
    history.closePending("cancelled");
    history.update(event({ session_id: "session2" }), 1);
    expect(history.entries).toHaveLength(2);
    expect(history.entries.map(entry => entry.state)).toEqual(["running", "cancelled"]);
  });

  it("bounds history and preserves it after session end", () => {
    const history = new ActivityHistory();
    for (let turn = 1; turn <= 12; turn++) {
      history.update(event({ call_id: `call${turn}`, turn, state: "completed", elapsed_ms: turn }), turn);
    }
    history.closePending("cancelled");
    expect(history.entries).toHaveLength(8);
    expect(history.entries.map(entry => entry.turn)).toEqual([12, 11, 10, 9, 8, 7, 6, 5]);
    expect(history.update(event({ turn: 1, state: "completed" }), 12)).toBe(false);
  });

  it("marks interrupted work without fabricating timing or resurrecting success", () => {
    const history = new ActivityHistory();
    history.update(event(), 1);
    history.closePending("cancelled", 2);
    expect(history.entries[0]).toMatchObject({ state: "cancelled", elapsed_ms: null });
    expect(history.update(event({ state: "completed", elapsed_ms: 20 }), 2)).toBe(false);
    expect(history.update(event({ state: "cancelled", elapsed_ms: 30 }), 2)).toBe(true);
    expect(history.entries[0]).toMatchObject({ state: "cancelled", elapsed_ms: 30 });
  });

  it("shows disconnection as an unknown outcome, never a success", () => {
    const history = new ActivityHistory();
    history.update(event(), 1);
    history.closePending("disconnected");
    expect(history.entries[0]).toMatchObject({ state: "disconnected", elapsed_ms: null });
    expect(history.update(event({ state: "completed", elapsed_ms: 20 }), 1)).toBe(false);
  });

  it("separates local validation from MCP round-trip and validates metadata", () => {
    expect(parseActivity(event({ tool: "cite_sources", scope: "local_citation_validation" })).tool).toBe("cite_sources");
    for (const patch of [
      { tool: "unknown" }, { scope: "local_citation_validation" }, { turn: 0 },
      { elapsed_ms: -1 }, { elapsed_ms: Infinity }, { source_count: 6 }, { source_count: 1.5 },
      { session_id: "" }, { state: "success" },
    ]) expect(() => parseActivity({ ...event(), ...patch })).toThrow("Invalid tool-activity");
  });

  it("renders sub-millisecond timing honestly and never converts missing timing to zero", () => {
    expect(formatMilliseconds(null)).toBe("—");
    expect(formatMilliseconds(.032)).toBe("<0.1 ms");
    expect(formatMilliseconds(321.456)).toBe("321.5 ms");
  });
});
