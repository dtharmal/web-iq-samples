# Web IQ + Foundry Voice Agent

A browser voice assistant for current news. Ask a question out loud (or type it), hear a short
spoken answer from a native **Microsoft Foundry voice agent**, and see the **Web IQ** news sources
behind it.

**Audience:** developers building real-time voice agents who need to ground answers in fresh web
content with citations.

What this sample demonstrates:

- A native Foundry voice agent (`kind: voice`, `gpt-realtime-1.5`) created with
  `azure-ai-projects[voice]` and streamed through
  `project_client.beta.voice_agents.realtime.connect`.
- **Model-selected search.** The model decides on each turn whether to answer directly or call
  `search_web`. The tool is backed by the Web IQ MCP `news` tool.
- **Post-playback citations.** For a searched answer the order is: search, then speak, then let
  the model select sources after the browser confirms the audio finished playing. Speech
  starts sooner because citation selection is no longer in its way.
- Interruption ("barge-in") that cancels generation and truncates the server conversation to the
  audio that was actually heard.
- A live tool-activity panel with backend-measured Web IQ latency.
- An optional private Azure App Service deployment with a shared-passphrase gate and managed
  identity.

> Foundry voice agents and `gpt-realtime-1.5` are **preview** features. Model and region
> availability depend on your Foundry project. See [Configure a voice agent][configure-voice].

## Architecture

```text
Browser (Vite + TypeScript, AudioWorklet)
   │  WebSocket: PCM16 24 kHz audio, transcript, tool activity, sources
   ▼
Python backend (FastAPI + Uvicorn)  ──MCP (x-apikey)──►  Web IQ  https://api.microsoft.ai/v3/mcp
   │  azure-ai-projects realtime connection (Entra ID)
   ▼
Microsoft Foundry native voice agent (kind: voice, store=false)
```

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI app, WebSocket endpoint, Host/Origin checks, hosted-mode login gate |
| `app/bridge.py` | Turn state machine: optional search, spoken answer, post-playback citations, interruption |
| `app/agent.py` | Voice agent definition, instructions and tool schemas |
| `app/webiq.py` | Web IQ MCP client: session setup, `news` calls, result validation |
| `app/setup_agent.py` | One-time explicit agent creation; records `.foundry/agent.json` |
| `app/setup_access.py` | Generates the hosted-mode passphrase and verifier into `.private-demo/` |
| `src/` | Browser UI, audio capture/playback, activity and timing panels |
| `infra/` | Optional Bicep for App Service hosting and an allowlisted packaging script |
| `tests/` | Offline Python, unit and browser tests, plus opt-in live probes |

## Prerequisites

- An Azure subscription and a [Microsoft Foundry project][foundry-docs] in a region that supports
  native voice agents and `gpt-realtime-1.5`.
- The **Azure AI User** role (or higher) on that Foundry project for your own account.
- A **Web IQ API key** with access to the MCP endpoint `https://api.microsoft.ai/v3/mcp`.
- [Python 3.11+](https://www.python.org/downloads/),
  [Node.js 22+](https://nodejs.org/), and the
  [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli).
- A current Chrome or Edge browser. Headphones are recommended so the agent does not hear itself.

Running the sample incurs Foundry realtime-model and Web IQ usage charges. The offline tests
make no service calls.

## Get started

From this directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
npm ci
npm run build
```

On Windows, use `.venv\Scripts\python` instead of `.venv/bin/python`.

### 1. Configure

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` in a local editor. Don't paste keys into shell commands, chat or the browser.

| Variable | Required | Description |
|---|---|---|
| `WEBIQ_API_KEY` | Yes | Your Web IQ API key. Used only by the backend. |
| `WEBIQ_ENDPOINT` | Yes | Must be `https://api.microsoft.ai/v3/mcp`; other hosts are refused. |
| `FOUNDRY_PROJECT_ENDPOINT` | Yes | `https://<resource>.services.ai.azure.com/api/projects/<project>` |
| `AZURE_TENANT_ID` | No | Tenant for Azure CLI tokens if your account spans tenants. |
| `FOUNDRY_VOICE_AGENT_NAME` / `_VERSION` | No | Override the agent recorded in `.foundry/agent.json`. |
| `PORT` | No | Local port, default `8765`. |
| `PERF_LOG_ENABLED` | No | `1` (default) writes local timing logs to `.logs/`; `0` disables them. |

The configuration is read again for each new voice session, so after changing the Web IQ key you
only need to start a new session, not restart the server.

### 2. Sign in to Azure

```bash
az login            # add --tenant <your-tenant-id> if needed
```

Locally, the app uses `AzureCliCredential`. It never stores Azure tokens.

### 3. Create the voice agent (once)

```bash
.venv/bin/python -m app.setup_agent --name <your-agent-name> --model gpt-realtime-1.5
```

This creates an immutable agent version from `app/agent.py` with `store=False` and records its
name and version in the ignored `.foundry/agent.json`. It never runs automatically. It refuses to
create a second agent while one is recorded. To publish changed instructions, audio settings or
tools as a new version, run it again with the same name and `--new-version`.

At connect time the app checks that the recorded version is still the latest version and that
conversation storage is off. If someone edits the agent elsewhere, for example in the portal,
review the change and then update the recorded version on purpose.

### 4. Run

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

## How it works

1. A browser AudioWorklet captures microphone audio as 24 kHz PCM16 in 20 ms frames. Nothing is
   recorded to disk.
2. The backend opens a native voice session with `store=false` and waits for `session.updated`
   before sending audio.
3. Azure semantic voice activity detection (VAD) detects the end of a turn. The backend starts a
   response with `tool_choice="auto"` and only `search_web` available, so the model either speaks
   directly or calls the tool.
4. `search_web` calls the Web IQ MCP `news` tool (five passages, English/US). Results are treated
   as untrusted evidence. A failed or empty search lets the model explain the limitation, never
   invent news.
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

`gpt-realtime-1.5` didn't reliably honor a forced named-function `tool_choice` in testing. The
citation pass therefore uses `tool_choice="required"` with exactly one tool.

## Validation

The offline tests never call Azure or Web IQ:

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
npm test
npm run build
npx playwright install chromium --only-shell
npm run test:browser
```

Browser tests start their own server on port `8766` (override with `TEST_PORT`) and use a
synthetic microphone.

With the server running and `.env` configured, these **opt-in live checks incur usage**. They
print counts, IDs and timings only, never audio, transcripts or credentials:

```bash
.venv/bin/python -m app.smoke                       # one typed question end to end
.venv/bin/python -m app.voice_input_smoke           # synthetic spoken input + interruption
node tests/live-timing.mjs                          # browser timing panel
node tests/live-post-speech.mjs                     # post-playback citations + interruption
node tests/live-post-speech.mjs --optional-search   # direct answer, then searched answer
```

## Optional: deploy to Azure App Service

Hosted mode (`APP_MODE=hosted`) never reads `.env` or `.foundry/`. It authenticates to Foundry
with the web app's managed identity, puts every page except `/login` behind a shared passphrase,
and allows up to 10 concurrent conversations in a single process. The deployment creates a Linux
B1 App Service plan and web app, and grants the app's identity **Azure AI User** on your Foundry
project only.

1. Generate the access material. Nothing is printed or uploaded.

   ```bash
   .venv/bin/python -m app.setup_access
   ```

   This creates `.private-demo/access.json` (the passphrase to share privately) and
   `.private-demo/verifier` (the value the server needs).

2. Create a private parameters file:

   ```bash
   mkdir -p .deploy && chmod 700 .deploy
   cp infra/main.parameters.example.json .deploy/main.parameters.json
   chmod 600 .deploy/main.parameters.json
   ```

   Fill in the placeholders in a local editor. Add `webiqApiKey` and `authVerifier`
   parameters, setting `authVerifier` to the contents of `.private-demo/verifier`.
   `.deploy/` is ignored by Git. To restrict access by network, set `allowedIpCidrs` to your
   client IP ranges.

3. Provision into an existing resource group:

   ```bash
   az deployment group create --resource-group <your-resource-group> \
     --template-file infra/main.bicep --parameters @.deploy/main.parameters.json
   ```

   If the Foundry resource lives in another resource group, set `foundryResourceGroupName`.

4. Package and deploy the code. Only runtime files and the built UI are included:

   ```bash
   npm run build
   .venv/bin/python -m infra.package_app "$PWD/.deploy/app.zip"
   az webapp deploy --resource-group <your-resource-group> --name <your-app-name> \
     --src-path .deploy/app.zip --type zip
   ```

5. Open `https://<your-app-name>.azurewebsites.net/login` and sign in with the passphrase.

To rotate the passphrase, generate new material in a fresh directory, update the
`DEMO_AUTH_VERIFIER` app setting and restart the app. A restart signs out every session. Delete
the resource group resources when you're finished so charges stop.

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

This sample doesn't send telemetry to Microsoft. Local timing logs (`.logs/performance.jsonl`,
capped at 3 MiB) contain durations and counts only. Set `PERF_LOG_ENABLED=0` to turn them off.
Azure and Web IQ process requests under their own service terms.

## Troubleshooting

| Symptom | Action |
|---|---|
| `FOUNDRY_PROJECT_ENDPOINT must look like ...` | Copy the project endpoint from the Foundry portal **Overview** page. |
| "Web IQ MCP rejected the API key" | Update `WEBIQ_API_KEY` in `.env`, then start a new session. |
| Azure authentication error | Run `az login` again, using `--tenant` if needed, then reconnect. |
| Native voice HTTP 400/403 | Check that your region supports voice agents, and check your role on the project. |
| "Agent version changed" | Review the agent's latest version, then update `.foundry/agent.json` or run `setup_agent --new-version`. |
| No audio | End the session and start again with a click. Check the site's audio permission and your output device. |
| Unwanted interruptions | Use headphones. Echo cancellation depends on your hardware. |
| "Another active session" | End the conversation in the other tab. Local mode allows one. |

## Resources

- [Quickstart: Create a voice-based prompt agent][voice-quickstart]
- [Configure a voice agent][configure-voice]
- [Use the GPT Realtime API for speech and audio](https://learn.microsoft.com/azure/foundry/openai/how-to/realtime-audio)
- [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk)

[foundry-docs]: https://learn.microsoft.com/azure/foundry/
[voice-quickstart]: https://learn.microsoft.com/azure/foundry/agents/quickstarts/prompt-voice-agent
[configure-voice]: https://learn.microsoft.com/azure/foundry/agents/how-to/configure-voice-agent

## Contribute

See the repository [contributing guide](../../../CONTRIBUTING.md).
