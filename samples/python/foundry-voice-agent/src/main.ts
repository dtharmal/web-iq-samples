// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

import { AudioPlaybackError, AudioPlayer, Microphone } from "./audio";
import { ToolActivityPanel } from "./activity";
import { BrowserPerformance } from "./performance";

type Source = {
  id: string; title: string; url: string; domain: string;
  publisher: string | null; published_at: string | null; updated_at: string | null; crawled_at: string | null;
};
type CitationState = "pending" | "selecting" | "completed" | "unavailable" | "cancelled" | "failed" | "not_searched";
type Message = { container: HTMLElement; body: HTMLParagraphElement; role: string; turn: number; citations?: CitationState };
type State = "idle" | "connecting" | "listening" | "searching" | "thinking" | "speaking" | "error";

function element<T extends HTMLElement>(id: string, type: { new(): T }): T {
  const result = document.getElementById(id);
  if (!(result instanceof type)) throw new Error(`Missing UI element ${id}`);
  return result;
}
const startButton = element("start", HTMLButtonElement);
const endButton = element("end", HTMLButtonElement);
const muteButton = element("mute", HTMLButtonElement);
const interruptButton = element("interrupt", HTMLButtonElement);
const sendButton = element("send", HTMLButtonElement);
const input = element("question", HTMLInputElement);
const statusLabel = element("status-label", HTMLSpanElement);
const caption = element("voice-caption", HTMLParagraphElement);
const errorBox = element("error", HTMLDivElement);
const setupBox = element("setup", HTMLDivElement);
const transcript = element("transcript", HTMLDivElement);
const sourceList = element("source-list", HTMLDivElement);
const searchStatus = element("search-status", HTMLDivElement);
const controls = element("live-controls", HTMLDivElement);
const sessionActions = element("session-actions", HTMLDivElement);
const connectionPending = element("connection-pending", HTMLDivElement);
const connectionStage = element("connection-stage", HTMLSpanElement);
const cancelConnectionButton = element("cancel-connection", HTMLButtonElement);
const clock = element("session-clock", HTMLSpanElement);
const signOutButton = element("sign-out", HTMLButtonElement);
let logoutCsrf: string | null = null;
let lastAudioResponseId: string | null = null;
let playbackTimer: ReturnType<typeof setInterval> | undefined;
const activityPanel = new ToolActivityPanel(
  element("tool-activity-list", HTMLOListElement),
  element("tool-activity-announcement", HTMLParagraphElement),
);
const messages = new Map<string, Message>();
let socket: WebSocket | null = null;
let player: AudioPlayer | null = null;
let mic: Microphone | null = null;
let generation = 0;
let ready = false;
let muted = false;
let speechMode = false;
let active = false;
let state: State = "idle";
let currentTurn = 0;
let sourceEvidence: Source[] = [];
let selectedSources = new Set<string>();
let connectTimer: ReturnType<typeof setTimeout> | undefined;
let clockTimer: ReturnType<typeof setInterval> | undefined;
let sessionStarted = 0;
let pendingText: string | null = null;
let pendingTextId: string | null = null;
let modelDone = false;
let answerPlayback: { id: string; turn: number; done: boolean } | null = null;
let awaitingTurn = false;
const performanceTrace = new BrowserPerformance(send);

function finishPlayback(): void {
  if (!ready || awaitingTurn) return;
  if (!answerPlayback?.done) {
    if (modelDone && !player?.playing) setState("listening");
    return;
  }
  const outcome = player?.completion() ?? (player ? null : "unavailable");
  if (outcome === null) return;
  setState("listening");
  const { id, turn } = answerPlayback;
  answerPlayback = null;
  send({ type: "playback_complete", response_id: id, turn, outcome });
}

function cancelPendingReferences(): void {
  answerPlayback = null;
  for (const [key, message] of messages) {
    if (key.startsWith(`${generation}:`) && ["pending", "selecting"].includes(message.citations ?? "")) {
      referenceStatus(message, "cancelled");
      searchStatus.textContent = "Reference selection cancelled.";
    }
  }
}

function referenceStatus(message: Message, next: CitationState): void {
  message.citations = next;
  let note = message.container.querySelector<HTMLElement>(".citation-status");
  if (!note) {
    note = document.createElement("span");
    note.className = "citation-status interrupted-label";
    note.setAttribute("role", "status");
    message.container.append(note);
  }
  const labels: Record<CitationState, string> = {
    pending: "References will be selected after audio playback.",
    selecting: "Selecting references for the answer just spoken...",
    completed: "Model-selected references, not independent fact-checking.",
    unavailable: "No supporting references selected for this answer.",
    cancelled: "Reference selection cancelled; this answer has no completed source selection.",
    failed: "References unavailable for the spoken answer.",
    not_searched: "Answered without WebIQ search; not verified against live sources.",
  };
  note.textContent = labels[next];
}

function setState(next: State): void {
  state = next;
  document.body.dataset.state = next;
  const labels: Record<State, string> = {
    idle: "", connecting: "Connecting", listening: muted ? "Microphone muted" : speechMode ? "Listening" : "Ready for a question",
    searching: "Searching WebIQ", thinking: "Preparing your briefing", speaking: "Speaking", error: "Needs attention",
  };
  const captions: Record<State, string> = {
    idle: "Good questions deserve well-sourced answers.",
    connecting: "Opening a native Foundry voice session...",
    listening: muted ? "Microphone paused. You can still type a question." : speechMode ? "I'm listening. Ask about the news." : "Type a question below to hear your briefing.",
    searching: "Checking live reporting, not relying on memory.",
    thinking: "Preparing a response; checking whether live search is needed.",
    speaking: speechMode && !muted ? "Speak to interrupt, or select Stop speaking." : "Select Stop speaking to interrupt the briefing.",
    error: "Something needs attention. See the message above.",
  };
  statusLabel.textContent = labels[next];
  element("status", HTMLSpanElement).hidden = next === "idle";
  caption.textContent = captions[next];
  sendButton.disabled = active && !ready;
  muteButton.disabled = !ready || !speechMode;
  interruptButton.disabled = !ready || !["speaking", "thinking", "searching"].includes(next);
}

function showError(message: string): void {
  errorBox.textContent = message;
  errorBox.hidden = false;
}
function clearError(): void { errorBox.hidden = true; errorBox.textContent = ""; }
function send(message: object): void {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

function setConnectionStage(message: string): void {
  caption.textContent = message;
  connectionStage.textContent = message;
}

function stop(failed = false): void {
  const restoreFocus = document.activeElement === cancelConnectionButton || controls.contains(document.activeElement);
  performanceTrace.cancelPending();
  activityPanel.closePending(failed ? "disconnected" : "cancelled");
  cancelPendingReferences();
  generation++;
  const stoppedGeneration = generation;
  clearTimeout(connectTimer);
  clearInterval(clockTimer);
  clearInterval(playbackTimer);
  lastAudioResponseId = null;
  if (socket) {
    if (socket.readyState === WebSocket.OPEN) send({ type: "end" });
    socket.close();
    socket = null;
  }
  mic?.close();
  mic = null;
  player?.clear();
  if (player && player.context.state !== "closed") {
    void player.context.close().catch(() => {
      if (generation === stoppedGeneration) showError("The audio device could not close cleanly. Reload this tab.");
    });
  }
  player = null;
  active = ready = muted = speechMode = false;
  pendingText = null;
  pendingTextId = null;
  startButton.hidden = false;
  controls.hidden = true;
  connectionPending.hidden = true;
  sessionActions.setAttribute("aria-busy", "false");
  muteButton.textContent = "Mute microphone";
  muteButton.setAttribute("aria-pressed", "false");
  clock.textContent = "Not recording";
  element("micro-note", HTMLParagraphElement).textContent = "Microphone permission required. Headphones recommended.";
  setState(failed ? "error" : "idle");
  if (restoreFocus) startButton.focus();
}

async function start(withMicrophone: boolean, text: string | null = null, submittedAt = performance.now()): Promise<void> {
  if (active) return;
  clearError();
  active = true;
  speechMode = withMicrophone;
  const session = ++generation;
  currentTurn = 0;
  pendingText = text;
  performanceTrace.reset();
  pendingTextId = text === null ? null : performanceTrace.submitted(submittedAt);
  modelDone = false;
  answerPlayback = null;
  awaitingTurn = false;
  const moveStartFocus = document.activeElement === startButton;
  if (document.activeElement === sendButton) input.focus();
  startButton.hidden = true;
  controls.hidden = true;
  connectionPending.hidden = false;
  sessionActions.setAttribute("aria-busy", "true");
  clock.textContent = "Connecting…";
  setState("connecting");
  setConnectionStage("Preparing audio...");
  element("micro-note", HTMLParagraphElement).textContent = withMicrophone ?
    "Audio is not sent until the session is ready." : "Text mode: microphone stays off.";
  if (moveStartFocus) cancelConnectionButton.focus();
  connectTimer = setTimeout(() => {
    if (session !== generation || ready) return;
    showError("Connecting took too long. Check Azure login and WebIQ access, then try again.");
    stop(true);
  }, 65_000);
  try {
    const context = new AudioContext({ sampleRate: 24000, latencyHint: "interactive" });
    player = new AudioPlayer(context);
    player.onDrained = () => {
      if (session !== generation || !ready) return;
      try { finishPlayback(); }
      catch (error) { playbackFailed(error); }
    };
    playbackTimer = setInterval(() => {
      if (session !== generation) return;
      try { player?.checkProgress(); finishPlayback(); }
      catch (error) { playbackFailed(error); }
    }, 100);
    await context.resume();
    if (session !== generation) return;
    if (withMicrophone) {
      setConnectionStage("Opening microphone. Allow access if your browser asks.");
      mic = new Microphone();
      await mic.start(context, (buffer) => {
        if (session !== generation || !ready || muted || socket?.readyState !== WebSocket.OPEN) return;
        if (socket.bufferedAmount > 96_000) {
          showError("The microphone connection fell behind. Reconnect to avoid delayed audio.");
          stop(true);
          return;
        }
        socket.send(buffer);
      });
    }
    if (session !== generation) return;
    setConnectionStage("Opening a native Foundry voice session...");
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/voice`);
    socket = ws;
    ws.onmessage = (message: MessageEvent<string>) => {
      if (session !== generation) return;
      try {
        const event = parseObject(JSON.parse(message.data));
        const stamp = performance.now();
        onEvent(event);
        if (["tool_activity", "evidence", "answer", "citations"].includes(String(event.type))) {
          performanceTrace.render(event, performance.now() - stamp);
        }
      }
      catch (error) {
        playbackFailed(error);
      }
    };
    ws.onerror = () => {
      if (session !== generation) return;
      showError("Could not connect to the voice server. Check your access and connection.");
      stop(true);
    };
    ws.onclose = (event) => {
      if (session !== generation) return;
      showError(event.code === 1013 ? "The demo is at capacity or starting too quickly. End your other session or retry shortly." :
        "The voice connection closed. Your transcript is still here; start a new session to continue.");
      stop(true);
    };
  } catch (error) {
    if (session !== generation) return;
    const denied = error instanceof DOMException && error.name === "NotAllowedError";
    showError(denied ? "Microphone permission was denied. Allow it in your browser, or type a question instead." :
      error instanceof Error ? error.message : "Could not open the audio device. Try text mode.");
    stop(true);
  }
}

function playbackFailed(error: unknown): void {
  if (error instanceof AudioPlaybackError && lastAudioResponseId) {
    performanceTrace.playbackDiagnostic(lastAudioResponseId, error.diagnostics, error.code);
  }
  showError(error instanceof Error ? error.message : "Invalid voice response received.");
  stop(true);
}

function parseObject(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid server event.");
  return value as Record<string, unknown>;
}
function textField(value: Record<string, unknown>, key: string): string {
  if (typeof value[key] !== "string") throw new Error(`Invalid server field: ${key}`);
  return value[key];
}
function numberField(value: Record<string, unknown>, key: string): number {
  if (typeof value[key] !== "number" || !Number.isFinite(value[key])) throw new Error(`Invalid server field: ${key}`);
  return value[key];
}
function nullableText(value: unknown): string | null { return typeof value === "string" ? value : null; }
export function safeLink(value: string): string | null {
  try {
    const url = new URL(value);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password ||
        url.hostname === "localhost" || url.hostname.endsWith(".local") ||
        /^[\d.]+$/.test(url.hostname) || url.hostname.startsWith("[")) return null;
    return url.href;
  } catch { return null; }
}
function readSources(value: unknown): Source[] {
  if (!Array.isArray(value) || value.length > 5) throw new Error("Invalid source list.");
  return value.map((entry: unknown) => {
    const record = parseObject(entry);
    const url = safeLink(textField(record, "url"));
    if (!url) throw new Error("An unsafe source link was blocked.");
    return {
      id: textField(record, "id"), title: textField(record, "title"), url, domain: textField(record, "domain"),
      publisher: nullableText(record.publisher), published_at: nullableText(record.published_at),
      updated_at: nullableText(record.updated_at), crawled_at: nullableText(record.crawled_at),
    };
  });
}

function onEvent(event: Record<string, unknown>): void {
  const type = textField(event, "type");
  if (type === "ready") {
    if (!active || ready) return;
    const moveCancelFocus = document.activeElement === cancelConnectionButton;
    ready = true;
    performanceTrace.enabled = event.performance_logging === true;
    performanceTrace.playbackDiagnostics = event.playback_diagnostics === true;
    clearTimeout(connectTimer);
    sessionStarted = Date.now();
    clock.textContent = "0:00 / 10:00 · Not recording";
    clockTimer = setInterval(() => {
      const seconds = Math.floor((Date.now() - sessionStarted) / 1000);
      clock.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")} / 10:00 · Not recording`;
    }, 1000);
    element("micro-note", HTMLParagraphElement).textContent = withModeDescription();
    setState("listening");
    connectionPending.hidden = true;
    controls.hidden = false;
    sessionActions.setAttribute("aria-busy", "false");
    if (moveCancelFocus) (speechMode ? muteButton : endButton).focus();
    if (pendingText) {
      awaitingTurn = true;
      send({ type: "text", text: pendingText,
        client_turn_id: performanceTrace.enabled ? pendingTextId : undefined });
      pendingText = null;
      pendingTextId = null;
    }
  } else if (type === "state") {
    const next = textField(event, "state");
    if (ready && ["listening", "searching", "thinking"].includes(next)) setState(next as State);
  } else if (type === "interrupt") {
    if (state === "searching" || state === "thinking") searchStatus.textContent = "Previous briefing interrupted.";
    currentTurn = numberField(event, "turn");
    cancelPendingReferences();
    sourceEvidence = [];
    selectedSources.clear();
    renderSources();
    searchStatus.textContent = "The model will decide whether live search is needed.";
    awaitingTurn = false;
    lastAudioResponseId = null;
    performanceTrace.beginTurn(currentTurn, event.client_turn_id);
    activityPanel.closePending("cancelled", currentTurn);
    const hadPlayback = player?.playing;
    const positions = player?.clear() ?? [];
    if (hadPlayback) {
      const prior = [...messages.values()].reverse().find(m => m.role === "assistant" && m.turn < currentTurn);
      if (prior && !prior.container.querySelector(".playback-interrupted")) {
        const note = document.createElement("span");
        note.className = "interrupted-label playback-interrupted";
        note.textContent = "Interrupted · transcript may include words not played.";
        prior.container.append(note);
      }
    }
    modelDone = false;
    send({ type: "playback", turn: currentTurn, items: positions });
    setState("listening");
  } else if (type === "tool_activity") {
    activityPanel.receive(event, currentTurn);
  } else if (type === "search") {
    sourceEvidence = [];
    selectedSources.clear();
    sourceList.replaceChildren();
    element("source-count", HTMLSpanElement).textContent = "0";
    searchStatus.textContent = `Searching WebIQ news: ${textField(event, "query")}`;
    setState("searching");
  } else if (type === "evidence") {
    sourceEvidence = readSources(event.sources);
    renderSources();
    searchStatus.textContent = sourceEvidence.length ? `Retrieved ${sourceEvidence.length} sources. References follow audio playback.` : "No usable evidence found for this question.";
  } else if (type === "answer") {
    if (awaitingTurn || numberField(event, "turn") !== currentTurn) return;
    const sources = readSources(event.sources);
    const message = getMessage(textField(event, "id"), "assistant", numberField(event, "turn"));
    const pending = event.citation_state === "pending";
    const notSearched = event.citation_state === "not_searched";
    referenceStatus(message, notSearched ? "not_searched" : pending ? "pending" : sources.length ? "completed" : "unavailable");
    answerPlayback = pending ? { id: textField(event, "id"), turn: currentTurn, done: false } : null;
    modelDone = false;
    if (pending || !sources.length) {
      selectedSources.clear();
      renderSources();
      searchStatus.textContent = notSearched ? "The model answered without searching WebIQ. No live references." :
        pending ? "Speaking from retrieved evidence. References follow playback." : "No supporting references selected.";
      return;
    }
    attachReferences(message, sources);
  } else if (type === "citations") {
    if (awaitingTurn || numberField(event, "turn") !== currentTurn) return;
    const message = messages.get(`${generation}:${textField(event, "id")}`);
    if (!message || message.turn !== currentTurn ||
        !["pending", "selecting"].includes(message.citations ?? "")) return;
    const next = textField(event, "state");
    if (!["selecting", "completed", "failed", "unavailable", "cancelled"].includes(next)) {
      throw new Error("Invalid citation state.");
    }
    referenceStatus(message, next as CitationState);
    if (next === "completed") {
      const sources = readSources(event.sources);
      if (!sources.length) throw new Error("Completed references must include selected sources.");
      attachReferences(message, sources);
    } else {
      searchStatus.textContent = message.container.querySelector(".citation-status")?.textContent ?? "";
    }
  } else if (type === "transcript") {
    const role = textField(event, "role");
    const turn = numberField(event, "turn");
    if (role === "assistant" && (awaitingTurn || turn !== currentTurn)) return;
    const message = getMessage(textField(event, "id"), role, turn);
    const content = textField(event, "text");
    const nearBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 100;
    message.body.textContent = event.final ? content : message.body.textContent + content;
    if (nearBottom) transcript.scrollTop = transcript.scrollHeight;
  } else if (type === "audio") {
    if (awaitingTurn || numberField(event, "turn") !== currentTurn) return;
    const receivedAt = performance.now();
    lastAudioResponseId = textField(event, "id");
    if (player) {
      const scheduled = player.enqueue(textField(event, "data"), textField(event, "item_id"), numberField(event, "content_index"));
      performanceTrace.firstAudio(textField(event, "id"), player.context, scheduled, receivedAt);
      const diagnostics = player.diagnostics();
      if (diagnostics.browser_queue_ms > 30_000) {
        performanceTrace.playbackDiagnostic(lastAudioResponseId, diagnostics);
      }
    }
    setState("speaking");
  } else if (type === "done") {
    if (awaitingTurn || numberField(event, "turn") !== currentTurn) return;
    modelDone = true;
    if (answerPlayback && answerPlayback.id === event.id) {
      if (event.completed === false) answerPlayback = null;
      else answerPlayback.done = true;
    }
    finishPlayback();
  } else if (type === "muted") {
    if (state === "listening") setState("listening");
  } else if (type === "error") {
    showError(textField(event, "message"));
    if (event.fatal === true || !ready) stop(true);
  }
}

function attachReferences(message: Message, sources: Source[]): void {
    selectedSources = new Set(sources.map(source => source.id));
    renderSources();
    searchStatus.textContent = `${sources.length} model-selected references for the spoken answer. Not independent fact-checking.`;
    message.container.querySelector(".answer-links")?.remove();
    const links = document.createElement("div");
    links.className = "answer-links";
    links.setAttribute("aria-label", "Model-selected references for this answer");
    sources.forEach((source, i) => {
      const a = sourceAnchor(source);
      a.textContent = `${i + 1} · ${source.publisher ?? source.domain}`;
      a.title = source.title;
      links.append(a);
    });
    message.container.append(links);
}

function withModeDescription(): string {
  return speechMode ? "Microphone streams only in this session. Audio is not stored." : "Text mode: microphone is off. Answers still stream as native voice.";
}
function getMessage(id: string, role: string, turn: number): Message {
  const key = `${generation}:${id}`;
  const existing = messages.get(key);
  if (existing) return existing;
  document.getElementById("empty-transcript")?.remove();
  const container = document.createElement("article");
  container.className = `message ${role === "user" ? "user" : "assistant"}`;
  const label = document.createElement("div");
  label.className = "message-label";
  label.textContent = role === "user" ? "YOU" : "WEBIQ BRIEFING";
  const body = document.createElement("p");
  container.append(label, body);
  const message = { container, body, role, turn };
  const sameTurnAnswer = [...messages.entries()].find(([k, m]) =>
    k.startsWith(`${generation}:`) && m.turn === turn && m.role === "assistant");
  if (role === "user" && sameTurnAnswer) transcript.insertBefore(container, sameTurnAnswer[1].container);
  else transcript.append(container);
  messages.set(key, message);
  // Keep only bounded in-tab history. Nothing is written to browser storage.
  if (messages.size > 100) {
    const oldest = messages.entries().next().value;
    if (oldest) { oldest[1].container.remove(); messages.delete(oldest[0]); }
  }
  return message;
}
function sourceAnchor(source: Source): HTMLAnchorElement {
  const anchor = document.createElement("a");
  anchor.href = source.url;
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  return anchor;
}
function displayDate(raw: string): string {
  const date = new Date(raw);
  return Number.isNaN(date.valueOf()) ? raw.slice(0, 80) :
    date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
}
function renderSources(): void {
  sourceList.replaceChildren();
  element("source-count", HTMLSpanElement).textContent = String(sourceEvidence.length);
  for (const source of sourceEvidence) {
    const card = sourceAnchor(source);
    card.className = "source-card";
    const meta = document.createElement("div");
    meta.className = "source-meta";
    const publisher = document.createElement("span");
    publisher.textContent = source.publisher ?? source.domain;
    const tag = document.createElement("span");
    tag.className = "source-tag";
    tag.textContent = selectedSources.has(source.id) ? "Model-selected ↗" : "Retrieved ↗";
    meta.append(publisher, tag);
    const title = document.createElement("h3");
    title.className = "source-title";
    title.textContent = source.title;
    const date = document.createElement("p");
    date.className = "source-date";
    date.textContent = source.published_at ? `Published ${displayDate(source.published_at)}` :
      source.updated_at ? `Updated ${displayDate(source.updated_at)}` :
      source.crawled_at ? `Crawled ${displayDate(source.crawled_at)}` : "Date not supplied";
    card.append(meta, title, date);
    sourceList.append(card);
  }
}

async function refreshHealth(): Promise<void> {
  try {
    const response = await fetch("/api/health");
    if (response.status === 401) {
      stop();
      location.replace("/login");
      return;
    }
    if (!response.ok) throw new Error("The voice server is not ready.");
    const health = parseObject(await response.json());
    if (health.access && typeof health.access === "object") {
      const access = parseObject(health.access);
      signOutButton.hidden = access.hosted !== true;
      logoutCsrf = typeof access.csrf === "string" ? access.csrf : null;
    }
    const logging = health.performance_logging;
    const telemetryNotice = element("telemetry-notice", HTMLDivElement);
    if (logging && typeof logging === "object" && "state" in logging && logging.state === "warning") {
      telemetryNotice.textContent = "Local performance logging is incomplete: a write, queue or schema error occurred. Check /api/health for the safe error code and dropped-event count.";
      telemetryNotice.hidden = false;
    } else telemetryNotice.hidden = true;
    const missing = [];
    if (!health.agent_configured) missing.push("Create the native voice agent using the README setup command.");
    if (!health.webiq_configured) missing.push("Add WEBIQ_API_KEY privately to this project's .env file.");
    setupBox.textContent = missing.join(" ");
    setupBox.hidden = !missing.length;
  } catch {
    setupBox.textContent = "Cannot reach the backend. Check your connection or contact the demo owner.";
    setupBox.hidden = false;
  }
}

signOutButton.addEventListener("click", async () => {
  if (!logoutCsrf) return;
  stop();
  try {
    const response = await fetch("/api/logout", { method: "POST", headers: { "X-CSRF-Token": logoutCsrf } });
    if (!response.ok) throw new Error("Could not sign out. Reload and retry.");
    location.replace("/login");
  } catch {
    showError("Could not sign out. Reload and retry; access expires after one hour.");
  }
});

startButton.addEventListener("click", () => { void start(true); });
cancelConnectionButton.addEventListener("click", () => {
  if (!active || ready) return;
  showError("Connection cancelled. You can start again or type a question.");
  stop();
});
endButton.addEventListener("click", () => stop());
interruptButton.addEventListener("click", () => {
  cancelPendingReferences();
  awaitingTurn = true;
  send({ type: "interrupt" });
});
muteButton.addEventListener("click", () => {
  muted = !muted;
  mic?.mute(muted);
  send({ type: "mute", muted });
  muteButton.textContent = muted ? "Unmute microphone" : "Mute microphone";
  muteButton.setAttribute("aria-pressed", String(muted));
  if (state === "listening") setState("listening");
});
element("text-form", HTMLFormElement).addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text || (active && !ready)) return;
  clearError();
  input.value = "";
  const submittedAt = performance.now();
  if (active) {
    cancelPendingReferences();
    awaitingTurn = true;
    send({ type: "text", text,
      client_turn_id: performanceTrace.enabled ? performanceTrace.submitted(submittedAt) : undefined });
  }
  else void start(false, text, submittedAt);
});
document.querySelectorAll<HTMLButtonElement>("[data-prompt]").forEach(button => {
  button.addEventListener("click", () => { input.value = button.dataset.prompt ?? ""; input.focus(); });
});
window.addEventListener("beforeunload", () => stop());
window.addEventListener("focus", () => { if (!active) void refreshHealth(); });
setState("idle");
void refreshHealth();
setInterval(() => { void refreshHealth(); }, 5000);
