// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

type Tool = "webiq.news" | "cite_sources";
type BackendState = "running" | "completed" | "failed" | "cancelled";
type Scope = "mcp_round_trip" | "local_citation_validation";
export type ActivityEvent = {
  session_id: string; call_id: string; turn: number; tool: Tool; state: BackendState;
  scope: Scope; elapsed_ms: number | null; source_count: number | null; response_received: boolean;
};
type Entry = Omit<ActivityEvent, "state"> & {
  state: BackendState | "disconnected"; localTermination?: boolean;
};

const descriptions: Record<Scope, string> = {
  mcp_round_trip: "Backend MCP call dispatch to SDK response receipt (or abort). Includes transport and MCP decoding. Excludes connection setup, model generation, citation processing and audio.",
  local_citation_validation: "Local source-ID validation only; not WebIQ search latency. Excludes model generation, returning the tool output to Foundry and audio.",
};
const names: Record<Tool, string> = { "webiq.news": "WebIQ news", cite_sources: "Select citations" };
const key = (event: Pick<ActivityEvent, "session_id" | "call_id">) => `${event.session_id}:${event.call_id}`;

export function parseActivity(value: Record<string, unknown>): ActivityEvent {
  const { session_id, call_id, turn, tool, state, scope, elapsed_ms, source_count, response_received } = value;
  if (
    typeof session_id !== "string" || !session_id || session_id.length > 128 ||
    typeof call_id !== "string" || !call_id || call_id.length > 200 ||
    typeof turn !== "number" || !Number.isInteger(turn) || turn < 1 || turn > 40 ||
    (tool !== "webiq.news" && tool !== "cite_sources") ||
    (state !== "running" && state !== "completed" && state !== "failed" && state !== "cancelled") ||
    (scope !== "mcp_round_trip" && scope !== "local_citation_validation") ||
    scope !== (tool === "webiq.news" ? "mcp_round_trip" : "local_citation_validation") ||
    (elapsed_ms !== null && (typeof elapsed_ms !== "number" || !Number.isFinite(elapsed_ms) || elapsed_ms < 0)) ||
    (source_count !== null && (typeof source_count !== "number" || !Number.isInteger(source_count) || source_count < 0 || source_count > 5)) ||
    typeof response_received !== "boolean"
  ) throw new Error("Invalid tool-activity event.");
  return { session_id, call_id, turn, tool, state, scope, elapsed_ms, source_count, response_received };
}

export function formatMilliseconds(value: number | null): string {
  if (value === null) return "—";
  if (value < .1) return "<0.1 ms";
  return `${value.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} ms`;
}

export class ActivityHistory {
  entries: Entry[] = [];

  update(event: ActivityEvent, currentTurn: number): boolean {
    const index = this.entries.findIndex(entry => key(entry) === key(event));
    const previous = this.entries[index];
    if (event.turn < currentTurn && event.state !== "cancelled") return false;
    if (previous && previous.state !== "running" &&
        !(previous.localTermination && event.state === "cancelled")) return false;
    if (previous?.state === "running" && event.state === "running") return false;
    if (index >= 0) this.entries[index] = event;
    else this.entries.unshift(event);
    this.entries = this.entries.slice(0, 8);
    return true;
  }

  closePending(state: "cancelled" | "disconnected", beforeTurn = Infinity): boolean {
    let changed = false;
    this.entries = this.entries.map(entry => {
      if (entry.state !== "running" || entry.turn >= beforeTurn) return entry;
      changed = true;
      // No final backend timing arrived; never turn browser elapsed time into a measured result.
      return { ...entry, state, elapsed_ms: null, source_count: null, localTermination: true };
    });
    return changed;
  }
}

function outcome(entry: Entry): string {
  if (entry.state === "running") return "Dispatched · Running";
  if (entry.state === "disconnected") return "Disconnected · Final timing unavailable";
  if (entry.state === "cancelled") return entry.response_received ?
    "Cancelled · Returned result discarded" :
    entry.elapsed_ms === null ? "Cancelled · Final timing unavailable" : "Cancelled · Time until abort";
  if (entry.state === "failed") return entry.elapsed_ms === null ? "Failed · Not dispatched" : "Failed";
  const count = entry.source_count;
  return `${entry.tool === "webiq.news" ? "Returned" : "Completed"}${count === null ? "" :
    ` · ${count} ${entry.tool === "webiq.news" ? "usable sources" : "sources selected"}`}`;
}

export class ToolActivityPanel {
  readonly history = new ActivityHistory();
  constructor(
    private readonly list: HTMLOListElement,
    private readonly announcement: HTMLElement,
  ) {}

  receive(value: Record<string, unknown>, currentTurn: number): void {
    const event = parseActivity(value);
    if (!this.history.update(event, currentTurn)) return;
    this.render();
    this.announcement.textContent = `${names[event.tool]}, turn ${event.turn}. ${outcome(event)}.${
      event.elapsed_ms === null ? "" : ` ${formatMilliseconds(event.elapsed_ms)}.`
    }`;
  }

  closePending(state: "cancelled" | "disconnected", beforeTurn = Infinity): void {
    if (!this.history.closePending(state, beforeTurn)) return;
    this.render();
    this.announcement.textContent = state === "disconnected" ?
      "Pending tool call disconnected. Final backend timing unavailable." :
      "Pending tool call cancelled. Final backend timing unavailable.";
  }

  private render(): void {
    this.list.replaceChildren();
    for (const entry of this.history.entries) {
      const row = document.createElement("li");
      row.className = `tool-row ${entry.tool === "webiq.news" ? "search-tool" : "citation-tool"}`;
      row.dataset.state = entry.state;
      row.dataset.callId = entry.call_id;
      row.dataset.turn = String(entry.turn);
      row.title = descriptions[entry.scope];
      const detail = document.createElement("div");
      const heading = document.createElement("div");
      heading.className = "tool-name";
      const dot = document.createElement("span");
      dot.className = "tool-dot";
      dot.setAttribute("aria-hidden", "true");
      const name = document.createElement("span");
      name.textContent = names[entry.tool];
      const turn = document.createElement("span");
      turn.className = "tool-turn";
      turn.textContent = `Turn ${entry.turn}`;
      heading.append(dot, name, turn);
      const status = document.createElement("p");
      status.className = "tool-outcome";
      status.textContent = outcome(entry);
      const scope = document.createElement("span");
      scope.className = "tool-scope";
      scope.textContent = entry.tool === "webiq.news" ? "MCP round-trip" : "Local validation · not search";
      detail.append(heading, status, scope);
      const duration = document.createElement("strong");
      duration.className = "tool-duration";
      duration.textContent = entry.state === "running" ? "In flight" : formatMilliseconds(entry.elapsed_ms);
      duration.setAttribute("aria-label", entry.state === "running" ? "Tool call running" :
        entry.elapsed_ms === null ? "No authoritative duration received" : `Backend measured ${duration.textContent}`);
      row.append(detail, duration);
      this.list.append(row);
    }
  }
}
