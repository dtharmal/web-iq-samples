# Web IQ + Foundry Voice Agent

A browser voice assistant for current news. Ask a question out loud (or type it), hear a short
spoken answer from a native **Microsoft Foundry voice agent**, and see the **Web IQ** news sources
behind it.

**Audience:** developers building real-time voice agents who need to ground answers in fresh web
content with citations.

What this sample demonstrates:

- A native Foundry voice agent (`kind: voice`, for example `gpt-realtime-1.5`) created with
  `azure-ai-projects[voice]` and streamed through
  `project_client.beta.voice_agents.realtime.connect`.
- **Model-selected search.** The model decides on each turn whether to answer directly or call
  `search_web`. The tool is backed by the Web IQ Auto REST API.
- **Post-playback citations.** For a searched answer the order is: search, then speak, then let
  the model select sources after the browser confirms the audio finished playing. Speech
  starts sooner because citation selection is no longer in its way.
- Interruption ("barge-in") that cancels generation and truncates the server conversation to the
  audio that was actually heard.
- A live tool-activity panel with backend-measured Web IQ latency.

> Foundry voice agents and some compatible models are **preview** features. Model and region
> availability depend on your Foundry project. See [Configure a voice agent][configure-voice].
> This is an evaluation sample, not a production reference architecture. An Azure
> subscription alone does not guarantee voice-preview or Web IQ access.

## Architecture

```text
Browser (Vite + TypeScript, AudioWorklet)
   │  WebSocket: PCM16 24 kHz audio, transcript, tool activity, sources
   ▼
Python backend (FastAPI + Uvicorn)  ──HTTPS (x-apikey)──►  Web IQ  https://api.microsoft.ai/v3/auto
   │  azure-ai-projects realtime connection (Entra ID)
   ▼
Microsoft Foundry native voice agent (kind: voice, store=false)
```

| Path | Purpose |
|---|---|
| `app/main.py` | Local FastAPI app, WebSocket endpoint and Host/Origin checks |
| `app/bridge.py` | Turn state machine: optional search, spoken answer, post-playback citations, interruption |
| `app/agent.py` | Voice agent definition, instructions and tool schemas |
| `app/webiq.py` | Web IQ Auto REST client, evidence and citation validation |
| `app/setup_agent.py` | One-time explicit agent creation; records `.foundry/agent.json` |
| `app/preflight.py` | Offline setup checks and opt-in read-only service checks |
| `src/` | Browser UI, audio capture/playback, activity and timing panels |
| `tests/` | Offline Python and frontend unit tests |

## Prerequisites

- An Azure subscription and a [Microsoft Foundry project][create-project] in a region that supports
  native voice agents and a compatible model of your choice. `gpt-realtime-1.5` is one example.
- The **Foundry User** role (previously **Azure AI User**, same role ID), or equivalent
  agent create/read permission, on that Foundry project for your own account.
- A **Web IQ API key** from [Profile Management][webiq-profiles] with an AppID enabled for
  the Auto beta. If you haven't onboarded Web IQ, follow the [Web IQ quickstart][webiq-quickstart]
  and [resource setup][webiq-setup] first. Web IQ requires a paid Azure subscription;
  Azure credits do not apply. See [access and onboarding][webiq-access] for current eligibility.
- [Python 3.11+](https://www.python.org/downloads/),
  [Node.js 22.12+](https://nodejs.org/), and the
  [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli).
- A current Chrome or Edge browser. Headphones are recommended so the agent does not hear itself.

Running the sample incurs separate Foundry model and Web IQ usage charges. The offline tests
make no service calls.

### Choose the simplest path

Use the **local quickstart** below first. It reuses your Foundry project and Web IQ key; it
doesn't deploy a model, APIM, App Service, Content Safety, or monitoring resources. The model
is service-managed, not an Azure OpenAI deployment you must create separately. Foundry owns
the voice session; Python runs the bridge locally.

| Service | Local authentication | What access proves |
|---|---|---|
| Microsoft Foundry | Your `az login` identity, optionally pinned by `AZURE_TENANT_ID` | Azure sign-in alone does not prove project permissions or voice-model availability. |
| Web IQ | `WEBIQ_API_KEY` sent only to the official endpoint as `x-apikey` | Foundry RBAC does not grant Web IQ or Auto access. |

No Azure OpenAI key, Entra app registration, client secret, certificate, or APIM subscription
key is required for this local path.

## Get started

Clone your fork and enter `samples/python/foundry-voice-agent`. Run all commands from that
directory, not the repository root.

### Install (macOS/Linux)

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
npm ci
npm run build
cp .env.example .env
chmod 600 .env
```

### Install (Windows PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -e .
npm ci
npm run build
Copy-Item .env.example .env
notepad .env
```

Use a private working directory and restrict the `.env` file's Windows permissions to your
account using your organization's approved ACL tooling. No activation script or PowerShell
execution-policy change is required. In later commands, replace `.venv/bin/python` with
`.\.venv\Scripts\python` on Windows.
Voice and on-screen timing do not depend on disk logging. On platforms without `O_NOFOLLOW`,
the optional logger creates new files exclusively and refuses to reopen an existing log; move
existing logs aside before restarting with logging enabled. Restrict the log directory's ACLs
to your account on Windows.

### 1. Configure and check locally

Edit `.env` in a local editor. Don't paste keys into shell commands, chat or the browser.

| Variable | Required | Description |
|---|---|---|
| `WEBIQ_API_KEY` | Yes | Your Web IQ API key. Used only by the backend. |
| `WEBIQ_ENDPOINT` | Yes | Must be `https://api.microsoft.ai/v3/auto`; other hosts are refused. |
| `FOUNDRY_PROJECT_ENDPOINT` | Yes | `https://<resource>.services.ai.azure.com/api/projects/<project>` |
| `AZURE_TENANT_ID` | No | Tenant for Azure CLI tokens if your account spans tenants. |
| `FOUNDRY_VOICE_AGENT_NAME` / `FOUNDRY_VOICE_AGENT_VERSION` | No | Set both or neither. Override the agent recorded in `.foundry/agent.json`. |
| `PORT` | No | Local port, default `8765`. |
| `PERF_LOG_ENABLED` | No | `0` (default) disables local timing logs; `1` writes them to `.logs/`. |

The configuration is read again for each new voice session. **Process environment variables
override `.env`**, including an old key exported in your terminal. Update/remove that override
and restart the process if necessary; editing `.env` alone cannot replace it. When no override
exists, editing the key in `.env` only requires a new voice session, not a server restart.

```bash
.venv/bin/python -m app.preflight
```

This checks configuration, prerequisite executables, Node's version, a non-placeholder key,
and the built UI without network requests or printing configured values. A `NEXT Create`
message is expected before agent setup. Exit code `1` means fix a prerequisite first.

### 2. Sign in to Azure

```bash
az login            # add --tenant <your-tenant-id> if needed
az account set --subscription "<your-subscription-id>"
az account show --query "{subscription:name, tenant:tenantId}" --output table
```

Verify that this is the intended subscription and tenant before creating the agent. Set
`AZURE_TENANT_ID` in `.env` when you need to select a tenant explicitly. Locally, the app uses
`AzureCliCredential` rather than a credential chain; the sample itself never writes tokens.
Azure CLI manages its own sign-in cache.

### 3. Create the voice agent (once)

```bash
.venv/bin/python -m app.setup_agent --name <your-agent-name> --model gpt-realtime-1.5
```

The model shown is an example. Choose a compatible model available to your project; its usage
is billed separately from Web IQ.

This creates an immutable agent version from `app/agent.py` with `store=False` and records its
name and version in the ignored `.foundry/agent.json`. It never runs automatically. It refuses to
create a second agent while one is recorded. To publish changed instructions, audio settings or
tools as a new version, run it again with the same name and `--new-version`.

The app checks that the recorded version is still the latest version and that conversation
storage is off. Successful checks can be reused for at most 30 seconds. If someone edits the agent elsewhere, for example in the portal,
review the change and then update the recorded version on purpose.

If you change `FOUNDRY_PROJECT_ENDPOINT`, the sample refuses to silently reuse metadata from a
different project. Set both explicit agent overrides for the new project, or move the old
`.foundry/agent.json` aside before creating a new agent. No old remote agent is deleted.

### 4. Verify service access without a conversation

```bash
.venv/bin/python -m app.preflight --online
```

This uses your configured credentials for a bounded, read-only Foundry agent lookup. It checks
the latest version, `kind=voice`, and `store=False`. It does **not** create resources, submit
searches, verify Auto entitlement, open voice sessions, or verify realtime model capacity.
Errors print only sanitized codes and
guidance, never keys, tokens, configuration values, or response bodies.

### 5. Run

```bash
.venv/bin/python -m app.main
```

Open <http://127.0.0.1:8765>. Select **Start speech mode** and allow microphone access, or type a
question without using the microphone. Try a greeting first (the agent answers without searching),
then ask "What's new with Microsoft Foundry this week?"

Direct answers are labeled **not verified against live sources**. Searched answers show the
retrieved sources right away. The references the model selects are attached to the same answer
after its audio finishes playing.

The server binds to loopback only, accepts one active conversation and caps each session at
10 minutes. `npm run dev` serves the static UI only. Live voice needs the built app served by
Python.

### First-run acceptance check (incurs usage)

Try these in one browser tab:

| Action | Expected result |
|---|---|
| Say or type "Hello" | Direct answer; no Web IQ call is required. A configured greeting may play first. |
| Ask "What are today's Microsoft Foundry announcements?" | The model normally chooses `search_web`; watch the tool panel. Selection is model-driven, not guaranteed by a keyword rule. |
| Let a searched answer finish | Audio starts before citation selection; selected references attach afterward without another spoken answer. |
| Talk over an answer, or press **Stop speaking** | Playback stops; canceled turns do not gain stale post-playback citations. |
| Select **End session** | Backend connections close and the conversation slot is released. |

Typing skips microphone permission but still produces audio. If no search occurs, ask explicitly
to search current news; a direct answer is visibly marked as not live-verified.

## How it works

1. A browser AudioWorklet captures microphone audio as 24 kHz PCM16 in 20 ms frames. Nothing is
   recorded to disk.
2. The backend opens a native voice session with `store=false` and waits for `session.updated`
   before sending audio.
3. Azure semantic voice activity detection (VAD) detects the end of a turn. The backend starts a
   response with `tool_choice="auto"` and only `search_web` available, so the model either speaks
   directly or calls the tool.
4. `search_web` calls Web IQ Auto in fast mode with web and news filters. Generated text evidence
   and its result-scoped citations are treated as untrusted. A failed or empty search lets the
   model explain the limitation, never invent news.
5. With tools disabled, the model speaks an answer from this turn's evidence. The browser plays
   audio as it streams. Talking over the answer or pressing **Stop speaking** cancels generation,
   and the backend truncates the conversation to the audio actually played.
6. Once generation has finished and the audio output clock has played the final sample, the
   browser sends `playback_complete`. A text-only `cite_sources` pass then selects IDs from the
   retrieved results that support the answer that was spoken. The validated links attach to that
   same answer. A new question, an interruption or an ended session cancels any pending
   selection, and late results are discarded.

Citation selection is declared by the model. It isn't an independent, claim-by-claim fact check.
Read the original articles when accuracy matters.

### Timing and scope

The tool panel's milliseconds measure **one Auto HTTP round trip**, including network
and upstream execution, for the complete result set of that request. They exclude connection
setup, source normalization, model generation, citation selection, and browser playback.
They are not per article, gateway-only processing time, or end-to-end answer latency.
Separate timing stages expose the broader answer lifecycle.

### Faster session startup

Web IQ client setup runs concurrently with agent validation. After validation succeeds, native voice
connection starts without waiting for client setup to finish. Both services and the effective
`session.updated` audio/tool configuration must be ready before microphone frames are sent.
Early greeting/configuration events remain in the native transport's flow-controlled queue;
the bridge is the sole reader after handoff. A structured sibling task owns voice;
disconnect, cancellation or failure
closes both upstreams and releases capacity. No voice sessions are pre-opened or pooled.

Local mode reuses the Azure CLI token between the REST check and voice handshake within
one session, avoiding duplicate acquisition for the same scope/tenant. Tokens refresh within
five minutes of expiry; claims challenges and unknown options bypass reuse. Token state is
cleared on session exit, including failure/cancellation, and never written to disk.

By default, the local server performs a read-only agent check at startup and refreshes it
20 seconds after each check. Successful native-kind, `store=False`, latest pinned version
and greeting validation can be reused for at most **30 seconds from check start**. The cache
is keyed by endpoint, tenant, and agent name/version; it stores only that key, expiry
and greeting mode, not the full definition or credentials. Changed configuration, expiry,
refresh errors or failed sessions invalidate or bypass reuse. A cold/expired cache falls
back to synchronous validation, never stale data. Shutdown cancels the background task.
Health reports nonsecret validation freshness/error state.

This is a deliberate local freshness tradeoff: a remote version/privacy change may take
up to 30 seconds to block a new local connection. Set `LOCAL_AGENT_VALIDATION_ENABLED=0`
for fresh per-connection checks and no background Azure calls. Unpinned agents bypass caching.
Background refresh adds periodic Foundry REST requests but creates no voice sessions and performs no searches.

`python -m app.perf_summary` reports `agent_cache` separately from remote `agent_lookup`,
`azure_auth_ms` for actual token acquisition, and `webiq_voice_connect_overlap_ms` alongside
`webiq_agent_setup_overlap_ms`. Nested auth/setup durations and concurrent durations must not
be added as sequential latency. Missing measurements stay null. Click-to-ready and greeting
playback completion are different milestones; no fixed startup-latency guarantee is implied.

The harness permits one search call per turn, not parallel search fan-out. Each call returns
up to five citations. Auto generates text evidence, not necessarily verbatim article passages;
the sample does not retrieve full articles. The agent is instructed to summarize in its own
words, not read retrieved text aloud, and quote at most one short phrase per source. The request
uses `maxResponseLength=3000` (Auto's total
response limit), not the separate News API's `maxLength` parameter. Titles are capped at
200 characters and each evidence text passed to the agent at 3,000 characters. An empty
`results` array means no evidence. See the [Auto REST reference][webiq-auto].

`gpt-realtime-1.5` didn't reliably honor a forced named-function `tool_choice` in testing. The
citation pass therefore uses `tool_choice="required"` with exactly one tool.

## Validation

The offline tests never call Azure or Web IQ:

```bash
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
npm test
npm run build
```

 ### Enterprise gateway option (not wired into this sample)

For centralized consumer attribution and Web IQ authentication, see the repository's
[APIM managed-identity sample](../../terraform/apim-managed-identity/README.md).
It is not a drop-in endpoint replacement for this sample.

This voice sample intentionally permits **only** `https://api.microsoft.ai/v3/auto`. Do not
point `WEBIQ_ENDPOINT` at APIM or forward your Web IQ key to an arbitrary host. Supporting a
gateway requires an explicit adapter with its own endpoint allowlist, downstream credential,
identity binding, tool contract, timeouts, and tests. An APIM subscription key identifies a
consumer; it is not a Web IQ key or an Azure subscription ID. Neither APIM nor Content Safety
is deployed here.

## Cleanup

End the browser session, then press Ctrl+C in the terminal running Python. This stops local
service use, but does not delete any Azure resource. Keep private `.env` and `.foundry` files
only as long as needed and never share them with a support bundle.

If you created an agent just for this sample, remove that agent through the Foundry portal
when finished; don't delete a shared project or model deployment. Web IQ billing resources and
Foundry projects remain. Review Azure Cost Management for ongoing charges.

## Privacy and safety

- `store=False` is set on the agent definition, and `store=false` is set on every connection. No
  audio, transcripts or requests are logged or saved, and the browser keeps at most 100 messages
  in memory.
- Credentials never reach the agent definition, prompts, tool arguments, browser JavaScript or
  health responses. Service errors are sanitized before they reach the browser.
- Requests must match the expected Host and Origin. Sessions are limited in turns, tool calls,
  message rate and audio size, and every service call has a timeout.
- Citation links must be public HTTP(S) URLs without credentials. The backend never fetches them,
  and all text is rendered with `textContent`.
- Never expose local mode through a public tunnel. It has no authentication.

### Telemetry

This sample doesn't export application telemetry to a monitoring service or its maintainers.
Optional timing logs (`.logs/performance.jsonl`, capped at 3 MiB) contain durations,
counts, opaque correlation IDs and sanitized status/error
codes, not queries, source URLs, passages, transcripts, audio or credentials.
Set `PERF_LOG_ENABLED=1` to enable them. These logs are not Web IQ billing or gateway
metrics. Azure and Web IQ process service requests under their own terms; review
[Web IQ privacy and data handling][webiq-privacy] and your Foundry data-processing requirements.
`store=False` controls conversation persistence, not all service-side processing or retention.

## Troubleshooting

| Symptom | Action |
|---|---|
| `FOUNDRY_PROJECT_ENDPOINT must look like ...` | Copy the project endpoint from the Foundry portal **Overview** page. |
| "Web IQ Auto rejected the API key" | Update `WEBIQ_API_KEY` in `.env`, then start a new session. |
| Key still fails after editing `.env` | Check for a stale terminal/process `WEBIQ_API_KEY` override. Never dump environment variables or keys for diagnosis. |
| `webiq_schema` | Auto returned an unexpected response. Check the current Auto REST contract. |
| Web IQ HTTP 403 | The AppID is not enabled for Auto; contact your Web IQ provider. Foundry RBAC does not grant access. |
| Web IQ HTTP 429 | Respect service retry guidance and check your AppID rate/daily quota. The voice path has bounded timeouts and does not automatically retry searches. |
| Azure authentication error | Run `az login` again, using `--tenant` if needed, then reconnect. |
| Native voice HTTP 400/403 | Check that your region supports voice agents, and check your role on the project. |
| "Agent version changed" | Review the agent's latest version, then update `.foundry/agent.json` or run `setup_agent --new-version`. |
| No audio | End the session and start again with a click. Check the site's audio permission and your output device. |
| Unwanted interruptions | Use headphones. Echo cancellation depends on your hardware. |
| "Another active session" | End the conversation in the other tab. Local mode allows one. |
| Preflight `FAIL configuration` | Compare `.env` with `.env.example`: replace endpoint/tenant placeholders, set both agent overrides or neither, and check that recorded metadata belongs to the project. |
| Port already in use | End your old instance or set `PORT` in `.env` and use the exact resulting loopback URL. Do not terminate an unrelated process. |
| No microphone on a remote browser / public tunnel | Run the browser on the same machine at the loopback URL. Local mode is not safe for public exposure. |

## Resources

- [Quickstart: Create a voice-based prompt agent][voice-quickstart]
- [Configure a voice agent][configure-voice]
- [Use the GPT Realtime API for speech and audio](https://learn.microsoft.com/azure/foundry/openai/how-to/realtime-audio)
- [Web IQ quickstart][webiq-quickstart] and [Auto REST reference][webiq-auto]

[foundry-docs]: https://learn.microsoft.com/azure/foundry/
[create-project]: https://learn.microsoft.com/azure/foundry/how-to/create-projects
[voice-quickstart]: https://learn.microsoft.com/azure/foundry/agents/quickstarts/prompt-voice-agent
[configure-voice]: https://learn.microsoft.com/azure/foundry/agents/how-to/configure-voice-agent
[webiq-profiles]: https://webiq.microsoft.ai/profiles
[webiq-quickstart]: https://learn.microsoft.com/en-us/web-iq/get-started/quickstart
[webiq-setup]: https://learn.microsoft.com/en-us/web-iq/get-started/deploy-azure-portal
[webiq-access]: https://learn.microsoft.com/en-us/web-iq/resources/faq#access-and-onboarding
[webiq-auto]: https://learn.microsoft.com/en-us/web-iq/reference/auto
[webiq-privacy]: https://learn.microsoft.com/en-us/web-iq/resources/faq#privacy-and-data-handling

## Contribute

See the repository [contributing guide](../../../CONTRIBUTING.md).
