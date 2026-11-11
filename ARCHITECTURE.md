# Architecture — how everything connects

This document explains how the voice agent works end to end: the Azure
resources, the SDKs, the two runtime modes, what happens on every turn, and why
the current setup is fast. For setup and run commands see [README.md](README.md).

---

## 1. The big picture

There are three layers:

| Layer | What it is | Where it lives |
|---|---|---|
| **Client** | Python app on your Mac: mic capture, speaker playback, event handling | [`src/voice_client.py`](src/voice_client.py) |
| **Voice Live** | Azure's realtime voice service. A WebSocket endpoint that handles turn detection, noise/echo cancellation, transcription and voice, and routes audio to a model or an agent | `https://modelssweden-resource.services.ai.azure.com/` |
| **Intelligence** | Either a speech-to-speech model (`gpt-realtime-2.1`) **or** a Foundry prompt agent (`gpt-5.6-terra` + tools) | Deployments / agents in the `modelssweden` Foundry project |

The client only ever talks to **Voice Live**. Voice Live talks to the model or
agent on the server side.

---

## 2. Azure resources

| Resource | Value | Used by |
|---|---|---|
| Subscription | Visual Studio Enterprise Subscription – MPN (Synapx tenant) | everything |
| Foundry resource | `modelssweden-resource` (Sweden region) | everything |
| Foundry project | `modelssweden` | agent mode |
| Deployment `gpt-realtime-2.1` | Speech-to-speech realtime model | **realtime mode** |
| Deployment `gpt-5.6-terra` | Text + tool-calling reasoning model | **agent mode** |
| Deployments `gpt-6-sol`, `grok-4.7` | Available, not used | — |
| Foundry agent `voice-agent` | Prompt agent (versioned) | **agent mode** |

### "Deployed" models vs "service" models

Only the models in the table above are **your deployments**. The speech models
used in agent mode — `mai-transcribe-2`, `azure-speech`, the Azure neural voices
like `en-US-Ava:DragonHDLatestNeural` — are **built into the Voice Live
service**. You select them by name in the session config; you don't deploy or
host them, and they don't need deployment permissions.

---

## 3. SDKs and packages

From [`requirements.txt`](requirements.txt):

| Package | Role in this project |
|---|---|
| **`azure-ai-voicelive[aiohttp]`** | Voice Live client. `connect()` opens the WebSocket; models such as `RequestSession`, `AzureSemanticVad`, `OpenAIVoice`, `AzureStandardVoice`, `AudioInputTranscriptionOptions`; `ServerEventType` for events. Used by `voice_client.py`. |
| **`azure-ai-projects`** | The Foundry SDK. `AIProjectClient.agents.create_version()` creates the prompt agent; `PromptAgentDefinition`, `MCPTool`, `WorkIQPreviewTool`. Used by `create_agent.py` (agent mode only). |
| **`azure-identity`** | Entra ID auth: `AzureCliCredential` (voice client, agent mode) and `DefaultAzureCredential` (create_agent). Both reuse your `az login`. |
| `azure-core` | `AzureKeyCredential` for API-key auth in realtime mode. |
| `aiohttp` | The WebSocket transport under the Voice Live SDK. |
| `sounddevice` + [`src/pyaudio.py`](src/pyaudio.py) | Audio I/O. `sounddevice` bundles PortAudio (no sudo/Homebrew); the shim exposes the PyAudio API the official sample expects. |
| `python-dotenv` | Loads `.env`. |
| `openai` | Used by the helper/test scripts. |

---

## 4. The two modes

`VOICE_MODE` in `.env` picks the path inside `main()` in `voice_client.py`.

### 4a. Realtime mode (default) — one model does everything

```text
 🎤 Mic ──PCM16 24kHz──▶ Voice Live ──▶ gpt-realtime-2.1 ──audio──▶ Voice Live ──▶ 🔊 Speaker
                              │                                       │
                              └─ VAD, noise + echo cancel             └─ azure-speech transcript
                                                                         (only for "You said")
```

- **Auth:** `AzureKeyCredential(VOICELIVE_API_KEY)`
- **Connect:** `connect(endpoint, credential, model="gpt-realtime-2.1", ...)`
- **Session config is sent by the client** (there's no agent to store it):
  instructions, `OpenAIVoice("alloy")`, `AzureSemanticVad`, noise suppression,
  echo cancellation, `azure-speech` transcription.
- **No Work IQ / MCP.** Optional local Python function tools (`src/local_tools.py`)
  with `REALTIME_TOOLS=1`.

### 4b. Agent mode — cascaded pipeline through a Foundry agent

```text
 🎤 Mic ─▶ Voice Live ─▶ [1] mai-transcribe-2 (speech → text)
                     ─▶ [2] Foundry agent "voice-agent" → gpt-5.6-terra (+ toolbox → Work IQ / MCP)
                     ─▶ [3] Azure neural TTS, Ava Dragon HD (text → speech)
                     ─▶ 🔊 Speaker
```

- **Auth:** `AzureCliCredential()` — your `az login` token. Agent mode does not accept keys.
- **Connect:** `connect(endpoint, credential, agent_name="voice-agent", project_name="modelssweden", ...)`
- **Session config is stored on the agent**, not sent by the client.
  `create_agent.py` writes it into the agent's metadata under
  `microsoft.voice-live.configuration` (split into 512-character chunks because
  of the metadata value limit). Voice Live reads it when the session starts.
- **Tools:** whatever `create_agent.py` attached (toolbox MCP or direct Work IQ).

### 4c. Realtime via the AI gateway (optional)

Set `AI_GATEWAY_BASE_URL` + `AI_GATEWAY_KEY` and realtime mode connects to the
gateway (LiteLLM) at `wss://<gateway>/v1/realtime?model=gpt-realtime-2.1` with
`Authorization: Bearer <key>` instead of Voice Live. Same SDK, same events and
audio path ([`GatewayConnectionManager`](src/voice_client.py) only swaps the URL
and auth header). The gateway speaks the plain OpenAI realtime protocol, so the
`azure_*` options are replaced: `server_vad`, `near_field` noise reduction,
`whisper-1` transcription (`GATEWAY_TRANSCRIPTION_MODEL`), no echo cancellation.
Agent mode, Work IQ and the toolbox are unaffected (they use Foundry, not the
gateway). Live check: `python scripts/check_gateway.py`.

### Side by side

| | Realtime | Agent |
|---|---|---|
| Model(s) per turn | 1 (`gpt-realtime-2.1`) | 3 (transcribe → LLM → TTS) |
| Extra network hop | none | Voice Live → Foundry agent service |
| Speed | ⚡ fastest | slower |
| Voice | OpenAI voice, generated by the model itself | Azure neural voice |
| Hears tone / pauses | yes (raw audio) | no (sees text only) |
| Tools (Work IQ / MCP) | ❌ | ✅ |
| Auth | API key | Entra ID |
| Config source | client (`_setup_session`) | agent metadata (`create_agent.py`) |
| Scripts to run | `voice_client.py` | `create_agent.py` then `voice_client.py` |

---

## 5. What happens on one turn (realtime mode)

All of this is in [`src/voice_client.py`](src/voice_client.py).

1. **Start-up — `main()`.** Loads `.env` (`src/.env` first, then the project
   root). Reads `VOICE_MODE`, builds the credential, and creates a
   `BasicVoiceAssistant`. Instructions get today's date appended because the
   model has no clock.
2. **Connect — `BasicVoiceAssistant.start()`.** Calls `connect(**connect_kwargs)`
   with `connection_options={"heartbeat": 60, "receive_timeout": 90}`. This
   opens a `wss://` WebSocket to Voice Live.
3. **Configure — `_setup_session()`.** Sends a `session.update` containing:
   - `model`, `instructions`, modalities `TEXT` + `AUDIO`
   - audio format PCM16 in and out
   - `voice` — `OpenAIVoice` if the name is an OpenAI voice, otherwise `AzureStandardVoice`
   - `input_audio_transcription` — `azure-speech`
   - `turn_detection` — `AzureSemanticVad(threshold=0.5, silence_duration_ms=400, prefix_padding_ms=300, speech_duration_ms=200, interrupt_response=True)`
   - `input_audio_noise_reduction` — `azure_deep_noise_suppression`
   - `input_audio_echo_cancellation` — server-side
4. **Session ready — `SESSION_UPDATED` event.** Logs the session details, then
   asks for one greeting with
   `conn.response.create(additional_instructions="Greet the user briefly…")`.
   `additional_instructions` applies to that one response only, so the greeting
   doesn't become a permanent rule. Then mic capture starts.
5. **Mic streaming — `AudioProcessor.start_capture()`.** The audio callback fires
   every 20 ms (480 frames at 24 kHz, `AUDIO_CHUNK_MS`). The raw PCM is handed
   to the event loop, base64-encoded there, and sent with
   `input_audio_buffer.append`. If the loop falls behind, the backlog goes out as
   one coalesced message (and beyond ~2 s the oldest audio is dropped).
6. **Turn detection (server side).**
   - `INPUT_AUDIO_BUFFER_SPEECH_STARTED` → prints `🎤 Listening...` and calls
     `skip_pending_audio()`, which stops any reply that's playing (**barge-in**).
     The server cancels the reply itself (`interrupt_response`); audio deltas
     of that reply still in flight are dropped by `response_id`, so no tail of
     the old answer plays after you start talking.
   - `INPUT_AUDIO_BUFFER_SPEECH_STOPPED` → fires after 400 ms of silence (`VAD_SILENCE_MS`). Prints
     `🤔 Processing...` and starts the latency stopwatch.
7. **Model replies.** `RESPONSE_CREATED`, then a stream of
   `RESPONSE_AUDIO_DELTA` chunks. The first chunk prints
   `⏱️ First audio after N ms`. Every chunk goes into the playback queue and
   plays immediately — the model is still generating the rest while you hear
   the start.
8. **Playback — `AudioProcessor.read_playback()`.** The speaker callback reads
   from a lock-protected jitter buffer in [`src/audio.py`](src/audio.py). A reply
   starts once ~100 ms (`AUDIO_PREBUFFER_MS`) has arrived, re-buffers after an
   underrun, and barge-in clears the whole buffer at once.
9. **Transcripts.**
   - `CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED` → `👤 You said: …`
   - `RESPONSE_AUDIO_TRANSCRIPT_DONE` → `🤖 Agent responded with audio transcript: …`
   The "You said" text comes from `azure-speech`, separately from what the
   model heard. A wrong transcript doesn't mean the model misheard.
10. **Turn done.** `RESPONSE_AUDIO_DONE` → `🎤 Ready for next input...`;
    `RESPONSE_DONE` → logged. Loop back to step 6.
11. **Shutdown.** Ctrl+C → `KeyboardInterrupt` → `AudioProcessor.shutdown()`
    closes the streams.

### Agent-mode differences

- Step 2 passes `agent_name` / `project_name` instead of `model`.
- Step 3 sends only modalities, audio formats and (if `VOICE_INTERIM=1`) the
  interim filler config. Everything else comes from the agent metadata.
- Step 7 is the three-stage cascade, and may include tool calls.

---

## 6. Creating the agent (agent mode only)

[`src/create_agent.py`](src/create_agent.py):

1. `AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=DefaultAzureCredential())`
2. `build_tools()` picks, in order:
   - **Toolbox** if `TOOLBOX_SERVER_URL` + `TOOLBOX_PROJECT_CONNECTION_ID` are set →
     `MCPTool(server_label="toolbox", require_approval="never", ...)`
   - **Direct Work IQ** if `WORK_IQ_PROJECT_CONNECTION_ID` is set → `WorkIQPreviewTool(...)`
   - otherwise no tools
3. Builds the Voice Live session config:
   - voice `en-US-Ava:DragonHDLatestNeural`, temperature 0.5
   - transcription `TRANSCRIPTION_MODEL` (default `mai-transcribe-2`)
   - `azure_semantic_vad`, threshold 0.5, silence `VAD_SILENCE_MS` (400 ms), prefix padding 300 ms
   - deep noise suppression, server echo cancellation
4. `project_client.agents.create_version(agent_name, PromptAgentDefinition(model, instructions, tools), metadata=chunk_config(...))`
   — every run creates a new version.
5. Reads the agent back and prints the reassembled config to confirm it saved.

`require_approval="never"` keeps voice conversations from stalling on an approval
prompt. Tighten it for write-capable tools.

---

## 7. Work IQ and MCP (agent mode)

```text
 Foundry agent ──MCPTool──▶ Foundry toolbox (MCP endpoint) ──▶ Work IQ (Microsoft 365 data)
                                                         └──▶ other MCP servers
```

1. **One-time tenant setup (admin):** provision the Work IQ service principal,
   register an Entra app with delegated `WorkIQAgent.Ask` + admin consent +
   client secret, enable Copilot Credits billing.
2. **Create connection + toolbox:** fill `WORKIQ_TENANT_ID`, `WORKIQ_CLIENT_ID`,
   `WORKIQ_CLIENT_SECRET`, then run
   [`scripts/workiq_toolbox.py`](scripts/workiq_toolbox.py) (wraps `azd ai`).
   It prints `TOOLBOX_PROJECT_CONNECTION_ID` and `TOOLBOX_SERVER_URL`.
3. **Attach:** paste those into `.env`, set `VOICE_MODE=agent`, run
   `create_agent.py`, then `voice_client.py`.
4. **Add more MCP servers later** by adding them to the same toolbox. No code
   change is needed.

Tool calls add seconds. Set `VOICE_INTERIM=1` so the agent says "one moment…"
while a tool runs.

---

## 8. Why the current setup is fast

The big change was **architecture**, not tuning:

1. **One model instead of three.** In agent mode each stage waits for the
   previous one. `gpt-realtime-2.1` takes audio in and streams audio out.
2. **No agent hop.** Realtime mode connects straight to the model deployment.
3. **Streaming playback.** Audio chunks play as they arrive.
4. **Native voice.** The same model that reasons also speaks, so the voice
   matches the content and sounds natural.
5. **Short answers.** Instructions ask for one sentence of up to 12 words unless
   detail is requested. Spoken length is a big part of how slow it feels.

### Bugs fixed along the way

| Symptom | Cause | Fix |
|---|---|---|
| Greeting repeated on every turn | Greeting was a **permanent system message** in the conversation | One-off `response.create(additional_instructions=...)` |
| `'alloy' is not a valid azure-standard voice` | OpenAI voices need `OpenAIVoice` | Pick the class from the voice name |
| `Only 'azure-speech', … are supported in cascaded pipelines` | `whisper-1` not allowed in agent mode | `mai-transcribe-2` |
| Agent mode stuck after "Processing…" | `mai-transcribe-2-streaming` never finished the turn | Non-streaming `mai-transcribe-2` |
| `No PONG received after 15.0 seconds` | SDK default heartbeat too tight | `heartbeat=60`, `receive_timeout=90` |
| Slow turn-taking in agent mode | Multilingual end-of-utterance model waited for semantic confirmation | Plain `azure_semantic_vad` |
| "What's the day today?" got a vague reply | Model has no clock | Today's date appended to instructions |
| Bad API key retried 5× (~45 s) before exiting | Reconnect loop treated 401/403 as a network drop | Auth failures fail fast with a hint |
| Old reply's tail played after barge-in; "no active response" errors | In-flight deltas re-filled the buffer; client `response.cancel()` raced the server's own interrupt | Mute the interrupted `response_id`; rely on `interrupt_response=True` |
| Local tool replies failed | `response.create` sent while the tool-calling response was still active | Tool runs immediately in a thread; output + `response.create` sent on `RESPONSE_DONE` |
| Extra ~30-60 ms per turn in the client | 50 ms audio blocks, PortAudio "high" latency, 500 ms VAD silence | 20 ms blocks, "low" latency, 400 ms silence |

---

## 9. Logs and measuring latency

Every run writes two files to `src/logs/`:

| File | Contents |
|---|---|
| `<timestamp>_voicelive.log` | Technical events with millisecond timestamps: connect, session ready, speech started/stopped, response created, first audio, finished speaking, errors |
| `<timestamp>_conversation.log` | Session details plus the dialogue: user transcripts and agent replies |

The key number is the console line `⏱️ First audio after N ms` — time from the
end of your speech to the first audio chunk arriving. It doesn't include the
400 ms silence window (that comes before "speech stopped") or speaker
buffering on your Mac.

Quick timing view of the latest run:

```bash
grep -E "started|stopped speaking|First audio|finished speaking" \
  "$(ls -t src/logs/*voicelive.log | head -1)"
```

---

## 10. Tuning knobs

| Setting | Where | Effect |
|---|---|---|
| `VAD_SILENCE_MS` | `.env` (both modes; agent mode needs `create_agent.py` re-run) | Lower = replies sooner but may cut you off mid-sentence |
| `AUDIO_CHUNK_MS` / `AUDIO_PREBUFFER_MS` | `.env` | Audio block size and playback jitter buffer; raise if audio crackles or is choppy |
| `speech_duration_ms` | `_setup_session` | Minimum speech length; ignores coughs and taps |
| `threshold` | same | VAD sensitivity, 0.0–1.0 |
| `VOICE_NAME` | `.env` | Voice; OpenAI names in realtime mode, Azure names in agent mode |
| `REALTIME_INSTRUCTIONS` | `.env` | System prompt for realtime mode |
| Agent instructions | `create_agent.py` | System prompt for agent mode (re-run the script after editing) |
| `VOICE_INTERIM` | `.env` | Spoken filler while tools run (agent mode) |

---

## 11. File map

```text
voiceAgent-VsCode/
├── README.md               setup + run guide
├── ARCHITECTURE.md         this file
├── AGENTS.md               guidance for coding agents
├── .env / .env.example     configuration (.env is git-ignored)
├── requirements.txt        Python dependencies
├── src/
│   ├── voice_client.py     session + event handling, both modes
│   ├── audio.py            mic capture, jitter-buffered playback
│   ├── local_tools.py      opt-in realtime function tools
│   ├── create_agent.py     creates the Foundry agent (agent mode)
│   ├── pyaudio.py          sounddevice-backed PyAudio shim
│   └── logs/               per-run logs
├── scripts/
│   ├── check_setup.py      pre-flight checks
│   └── workiq_toolbox.py   creates the Work IQ connection + toolbox
├── tests/                  unit tests (unittest)
├── tools/
│   └── test_tools.py       text-mode tool smoke test
├── .vscode/                launch configs (F5) and MCP config
└── _vendor/voicelive-samples/   read-only copy of the official samples
```
