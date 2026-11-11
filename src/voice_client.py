"""Realtime voice client for Azure Voice Live (realtime model mode or Foundry agent mode).

Adapted from the official AgentsNewQuickstart sample. See ARCHITECTURE.md.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import signal
import sys
import time
from datetime import datetime
from typing import Any, Optional, Union, TYPE_CHECKING, cast

from azure.core.credentials import AzureKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import AzureCliCredential

from urllib.parse import urlencode, urlparse, urlunparse

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.aio._patch import _VoiceLiveConnectionManager
from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioInputTranscriptionOptions,
    AudioNoiseReduction,
    AzureSemanticVad,
    AzureStandardVoice,
    FunctionCallOutputItem,
    FunctionTool,
    InputAudioFormat,
    InterimResponseTrigger,
    LlmInterimResponseConfig,
    Modality,
    OpenAIVoice,
    OutputAudioFormat,
    RequestSession,
    ResponseStatus,
    ServerEventType,
    ServerVad,
)
from dotenv import load_dotenv

import local_tools
import pyaudio
from audio import AudioDeviceError, AudioProcessor

if TYPE_CHECKING:
    from azure.ai.voicelive.aio import VoiceLiveConnection

# Config: prefer a local src/.env, then the project-root .env.
_script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_script_dir, ".env"), override=True)
load_dotenv(os.path.join(_script_dir, os.pardir, ".env"), override=False)

DEFAULT_API_VERSION = "2026-04-10"
DEFAULT_VAD_SILENCE_MS = 400
OPENAI_VOICES = {"alloy", "ash", "ballad", "coral", "echo", "sage",
                 "shimmer", "verse", "marin", "cedar"}
DEFAULT_INSTRUCTIONS = (
    "You are a voice assistant. Reply in ONE short sentence (max 12 words) "
    "unless the user explicitly asks for detail. Answer the user's actual "
    "question directly. No markdown, plain spoken words only."
)

logger = logging.getLogger(__name__)
conversation_logger = logging.getLogger("conversation")


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# GA realtime renamed some server events; map them back to the names handled below.
GA_EVENT_ALIASES = {
    "response.output_audio.delta": "response.audio.delta",
    "response.output_audio.done": "response.audio.done",
    "response.output_audio_transcript.done": "response.audio_transcript.done",
    "response.output_text.done": "response.text.done",
}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read `key` from a typed SDK model or a plain dict (unknown/GA events)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    value = getattr(obj, key, None)
    if value is None and hasattr(obj, "get"):
        value = obj.get(key)
    return default if value is None else value


def ga_session_config(*, model: str, instructions: Optional[str], voice: str,
                      transcription_model: str, silence_ms: int,
                      tools: Optional[list[dict]] = None,
                      language: str = "", threshold: float = 0.5,
                      noise_reduction: str = "near_field") -> dict:
    """`session.update` payload in the GA realtime shape (requires session.type)."""
    pcm = {"type": "audio/pcm", "rate": 24000}
    config: dict[str, Any] = {
        "type": "realtime",
        "model": model,
        "output_modalities": ["audio"],
        "instructions": instructions or "",
        "audio": {
            "input": {
                "format": pcm,
                "transcription": ({"model": transcription_model, "language": language}
                                  if language else {"model": transcription_model}),
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": threshold,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": silence_ms,
                    "create_response": True,
                    "interrupt_response": True,
                },
                **({"noise_reduction": {"type": noise_reduction}} if noise_reduction else {}),
            },
            "output": {"format": pcm, "voice": voice},
        },
    }
    if tools:
        config["tools"] = tools
    return config


def gateway_realtime_url(base_url: str, model: str) -> str:
    """wss URL of an OpenAI-compatible realtime endpoint behind the AI gateway.

    Accepts the base URL with or without a trailing slash / `/v1`:
    `https://gw`, `https://gw/`, `https://gw/v1` -> `wss://gw/v1/realtime?model=...`.
    """
    parsed = urlparse(base_url.strip())
    scheme = "wss" if parsed.scheme in ("https", "wss") else "ws"
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return urlunparse((scheme, parsed.netloc, path + "/v1/realtime", "",
                       urlencode({"model": model}), ""))


class GatewayConnectionManager(_VoiceLiveConnectionManager):
    """Voice Live SDK connection pointed at the AI gateway's `/v1/realtime`.

    The SDK hardcodes the Voice Live path and api-key header; the gateway
    (LiteLLM) wants `/v1/realtime?model=...` and `Authorization: Bearer <key>`.
    Everything after the handshake (events, audio, session.update) is reused.
    """

    def __init__(self, *, base_url: str, api_key: str, model: str, **kwargs: Any) -> None:
        super().__init__(credential=AzureKeyCredential(api_key), endpoint=base_url,
                         model=model, extra_query={}, extra_headers={}, **kwargs)
        self._gw_url = gateway_realtime_url(base_url, model)
        self._gw_key = api_key

    def _prepare_url(self) -> str:
        return self._gw_url

    async def _get_auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._gw_key}"}


def _is_auth_error(exc: BaseException) -> bool:
    """True if `exc` (or anything in its cause chain) is a 401/403 or credential failure.

    Retrying these never helps, so run() fails fast instead of backing off.
    """
    seen: set[int] = set()
    e: Optional[BaseException] = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, ClientAuthenticationError):
            return True
        if getattr(e, "status", None) in (401, 403) or getattr(e, "status_code", None) in (401, 403):
            return True
        e = e.__cause__ or e.__context__
    return False


# --------------------------------------------------------------------- logging
def setup_logging(keep: int = 20) -> None:
    """Create per-run log files and prune old ones. Safe to call once from main()."""
    logs_dir = os.path.join(_script_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    logging.basicConfig(
        filename=os.path.join(logs_dir, f"{stamp}_voicelive.log"),
        filemode="w",
        format="%(asctime)s:%(name)s:%(levelname)s:%(message)s",
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    )

    # Transcripts can contain personal data; set CONVERSATION_LOG=0 to disable.
    conversation_logger.propagate = False
    if _env_flag("CONVERSATION_LOG", True):
        handler = logging.FileHandler(
            os.path.join(logs_dir, f"{stamp}_conversation.log"), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        conversation_logger.addHandler(handler)
        conversation_logger.setLevel(logging.INFO)

    for suffix in ("_voicelive.log", "_conversation.log"):
        old = sorted(f for f in os.listdir(logs_dir) if f.endswith(suffix))[:-keep]
        for name in old:
            try:
                os.remove(os.path.join(logs_dir, name))
            except OSError:
                pass


class BasicVoiceAssistant:
    """Voice assistant over the Voice Live SDK with two modes.

    - realtime (`model` set): direct speech-to-speech with an API key. Fastest.
      Optional local function tools via REALTIME_TOOLS=1.
    - agent (`model` unset): Entra ID + Foundry agent (cascaded pipeline). Session
      config (voice, VAD, transcription) lives in the agent's metadata.
    """

    def __init__(
        self,
        endpoint: str,
        credential: Union[AzureKeyCredential, AsyncTokenCredential],
        voice: str,
        agent_name: Optional[str] = None,
        project_name: Optional[str] = None,
        agent_version: Optional[str] = None,
        conversation_id: Optional[str] = None,
        foundry_resource_override: Optional[str] = None,
        agent_authentication_identity_client_id: Optional[str] = None,
        model: Optional[str] = None,
        instructions: Optional[str] = None,
        gateway: bool = False,
    ):
        self.endpoint = endpoint
        self.credential = credential
        self.voice = voice
        self.agent_name = agent_name
        self.project_name = project_name
        self.agent_version = agent_version
        self.conversation_id = conversation_id
        self.foundry_resource_override = foundry_resource_override
        self.agent_authentication_identity_client_id = agent_authentication_identity_client_id
        self.model = model
        self.instructions = instructions
        # Realtime via the AI gateway (OpenAI-compatible) instead of Voice Live.
        self.gateway = gateway and bool(model)

        self.connection: Optional["VoiceLiveConnection"] = None
        self.audio_processor: Optional[AudioProcessor] = None
        self.session_ready = False
        self.greeting_sent = False
        self._unhandled_seen: set[str] = set()

        self.use_local_tools = bool(model) and _env_flag("REALTIME_TOOLS")
        self.response_timeout = _env_int("RESPONSE_TIMEOUT_S", 20)
        self.max_retries = _env_int("MAX_RECONNECTS", 5)

        self._watchdog: Optional[asyncio.Task] = None
        # Strong refs to tool-output tasks: asyncio itself only keeps weak ones,
        # so an unreferenced task can be garbage-collected mid-flight.
        self._tool_flushes: set[asyncio.Task] = set()
        self._reset_turn_state()

    def _reset_turn_state(self) -> None:
        """Per-connection state; cleared on every (re)connect."""
        self._speech_stopped_at: Optional[float] = None
        self._first_audio_seen = True
        self._response_active = False
        self._current_response_id: Optional[str] = None
        # Response interrupted by barge-in: its late audio deltas are dropped.
        self._muted_response_id: Optional[str] = None
        self._call_names: dict[str, str] = {}
        # Local tool calls started during the current response; their outputs
        # are sent once RESPONSE_DONE arrives (the service rejects
        # response.create while a response is still active).
        self._pending_tools: list[tuple[str, asyncio.Future[str]]] = []

    # ------------------------------------------------------------- lifecycle
    async def run(self) -> None:
        """Run a session, reconnecting with backoff if the connection drops."""
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                await self.start()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Session ended unexpectedly")
                if _is_auth_error(exc):
                    hint = ("AI_GATEWAY_KEY was rejected by the gateway. Check AI_GATEWAY_KEY "
                            "and AI_GATEWAY_BASE_URL in .env." if self.gateway else
                            "VOICELIVE_API_KEY was rejected. Copy the key from the same "
                            "Foundry resource as VOICELIVE_ENDPOINT (Keys and Endpoint)."
                            if self.model else
                            "Entra ID auth failed. Run `az login` (and check your role "
                            "on the Foundry project).")
                    print(f"❌ Authentication failed: {hint}")
                    raise
                if isinstance(exc, AudioDeviceError):
                    print(f"❌ Audio device problem: {exc}. Check your microphone/"
                          "speakers (and macOS microphone permission for this app).")
                    raise
                if time.monotonic() - started > 60:
                    attempt = 0  # it was healthy for a while; reset the budget
                attempt += 1
                if attempt > self.max_retries:
                    raise
                delay = min(2 ** attempt, 15)
                print(f"⚠️  Connection lost ({exc}). Reconnecting in {delay}s "
                      f"({attempt}/{self.max_retries})...")
                await asyncio.sleep(delay)

    async def start(self) -> None:
        """Open one Voice Live session and process events until it ends."""
        self.session_ready = False
        self._cancel_watchdog()
        self._reset_turn_state()
        try:
            logger.info("Connecting to VoiceLive (agent=%s, project=%s, model=%s)",
                        self.agent_name, self.project_name, self.model)

            # A longer heartbeat + receive_timeout avoids spurious "No PONG
            # received" drops when audio callbacks briefly delay the loop.
            connect_kwargs: dict = {
                "endpoint": self.endpoint,
                "credential": self.credential,
                "api_version": os.environ.get("VOICELIVE_API_VERSION", DEFAULT_API_VERSION),
                "connection_options": {"heartbeat": 60, "receive_timeout": 90},
            }
            if self.model:
                connect_kwargs["model"] = self.model
            else:
                connect_kwargs.update({
                    "agent_name": self.agent_name,
                    "project_name": self.project_name,
                    "agent_version": self.agent_version,
                    "conversation_id": self.conversation_id,
                    "foundry_resource_override": self.foundry_resource_override,
                    "authentication_identity_client_id": self.agent_authentication_identity_client_id,
                })

            if self.gateway:
                manager = GatewayConnectionManager(
                    base_url=self.endpoint,
                    api_key=cast(AzureKeyCredential, self.credential).key,
                    model=cast(str, self.model),
                    connection_options=connect_kwargs["connection_options"],
                )
            else:
                manager = connect(**connect_kwargs)

            async with manager as connection:
                self.connection = connection
                self.audio_processor = AudioProcessor(connection)
                if self.gateway:
                    # No server-side echo cancellation on the gateway path.
                    self.audio_processor.half_duplex = _env_flag("GATEWAY_HALF_DUPLEX", True)
                    self.audio_processor.echo_tail_s = _env_int("GATEWAY_ECHO_TAIL_MS", 500) / 1000
                await self._setup_session()
                self.audio_processor.start_playback()

                print("\n" + "=" * 65)
                print("🎤 VOICE ASSISTANT READY")
                print("Start speaking to begin conversation")
                print("Press Ctrl+C to exit")
                print("=" * 65 + "\n")

                await self._process_events()
        finally:
            self._cancel_watchdog()
            for task in self._tool_flushes:
                task.cancel()
            self._tool_flushes.clear()
            for _call_id, fut in self._pending_tools:
                fut.cancel()
            self._pending_tools = []
            if self.audio_processor:
                self.audio_processor.shutdown()
                self.audio_processor = None
            self.connection = None

    # ---------------------------------------------------------------- session
    def _build_interim_config(self) -> Optional[LlmInterimResponseConfig]:
        # Filler ("one moment...") is off by default: with fast replies it fires
        # on every turn and overlaps the real answer. Enable with VOICE_INTERIM=1
        # once a tool such as Work IQ adds real latency.
        if not _env_flag("VOICE_INTERIM"):
            return None
        return LlmInterimResponseConfig(
            triggers=[InterimResponseTrigger.TOOL, InterimResponseTrigger.LATENCY],
            latency_threshold_ms=100,
            instructions=("Create friendly interim responses indicating wait time due to "
                          "ongoing processing, if any. Do not include in all responses! "
                          "Do not say you don't have real-time access to information "
                          "when calling tools!"),
        )

    async def _setup_session(self) -> None:
        """Configure the Voice Live session for audio conversation."""
        logger.info("Setting up voice conversation session...")

        if self.model and self.gateway:
            # The gateway speaks GA OpenAI realtime, not Voice Live's preview
            # shape: send a raw GA session (no azure_* options, no echo cancel).
            ga_tools = [{"type": "function", "name": name, "description": desc,
                         "parameters": params}
                        for name, (_fn, desc, params) in local_tools.active_tools().items()
                        ] if self.use_local_tools else None
            session_config: Any = ga_session_config(
                model=self.model, instructions=self.instructions, voice=self.voice,
                transcription_model=os.environ.get("GATEWAY_TRANSCRIPTION_MODEL", "whisper-1"),
                silence_ms=_env_int("VAD_SILENCE_MS", DEFAULT_VAD_SILENCE_MS),
                tools=ga_tools,
                # whisper-1 guesses the language from noise/echo ("Hindi" for English
                # speech) unless told; far_field suits a laptop mic with speakers.
                language=os.environ.get("GATEWAY_TRANSCRIPTION_LANGUAGE", "en").strip(),
                threshold=float(os.environ.get("GATEWAY_VAD_THRESHOLD", "0.6") or 0.6),
                noise_reduction=os.environ.get("GATEWAY_NOISE_REDUCTION", "far_field").strip())
        elif self.model:
            voice_cfg = (OpenAIVoice(name=self.voice)
                         if self.voice.lower() in OPENAI_VOICES
                         else AzureStandardVoice(name=self.voice))
            tools = None
            if self.use_local_tools:
                tools = [FunctionTool(name=name, description=desc, parameters=params)
                         for name, (_fn, desc, params) in local_tools.active_tools().items()]
            session_config = RequestSession(
                model=self.model,
                modalities=[Modality.TEXT, Modality.AUDIO],
                instructions=self.instructions,
                input_audio_format=InputAudioFormat.PCM16,
                output_audio_format=OutputAudioFormat.PCM16,
                voice=voice_cfg,
                tools=tools,
                input_audio_transcription=AudioInputTranscriptionOptions(
                    model=os.environ.get("REALTIME_TRANSCRIPTION_MODEL", "azure-speech")),
                turn_detection=AzureSemanticVad(
                    threshold=0.5,
                    # The silence window is added to EVERY turn before the model
                    # even starts; semantic VAD keeps 400 ms from cutting you off.
                    silence_duration_ms=_env_int("VAD_SILENCE_MS", DEFAULT_VAD_SILENCE_MS),
                    prefix_padding_ms=300,
                    speech_duration_ms=200,  # ignore sub-200ms noise blips
                    # The server cancels the reply the moment you speak; the
                    # client only has to drop audio it already received.
                    interrupt_response=True,
                ),
                input_audio_noise_reduction=AudioNoiseReduction(type="azure_deep_noise_suppression"),
                input_audio_echo_cancellation=AudioEchoCancellation(),
            )
        else:
            # Voice, VAD and transcription are stored on the agent by create_agent.py.
            session_config = RequestSession(
                modalities=[Modality.TEXT, Modality.AUDIO],
                input_audio_format=InputAudioFormat.PCM16,
                output_audio_format=OutputAudioFormat.PCM16,
                interim_response=self._build_interim_config(),
            )

        conn = self.connection
        if conn is None:
            raise RuntimeError("Connection must be established before setting up session")
        await conn.session.update(session=session_config)
        logger.info("Session configuration sent")

    # ----------------------------------------------------------------- events
    async def _process_events(self) -> None:
        conn = self.connection
        if conn is None:
            raise RuntimeError("Connection must be established before processing events")
        async for event in conn:
            await self._handle_event(event)
        # The server closed the stream without an error: treat it as a drop so
        # run() reconnects instead of silently exiting.
        raise ConnectionError("Voice Live closed the connection")

    # Watchdog: if no audio arrives after you stop speaking, say so instead of
    # leaving the console on "Processing..." forever.
    def _arm_watchdog(self) -> None:
        self._cancel_watchdog()
        if self.response_timeout > 0:
            self._watchdog = asyncio.get_running_loop().create_task(self._watchdog_wait())

    def _cancel_watchdog(self) -> None:
        if self._watchdog and not self._watchdog.done():
            self._watchdog.cancel()
        self._watchdog = None

    async def _watchdog_wait(self) -> None:
        await asyncio.sleep(self.response_timeout)
        logger.warning("No response audio %ds after speech stopped", self.response_timeout)
        print(f"⚠️  No reply after {self.response_timeout}s — still waiting. "
              "Speak again to retry.")

    async def _handle_event(self, event: Any) -> None:
        logger.debug("Received event: %s", event.type)
        ap = self.audio_processor
        conn = self.connection
        if ap is None or conn is None:
            raise RuntimeError("AudioProcessor and Connection must be initialized")

        etype = GA_EVENT_ALIASES.get(event.type, event.type)

        if etype == ServerEventType.SESSION_UPDATED:
            self._log_session(event)
            self.session_ready = True
            ap.start_capture()  # idempotent; mic is live before the greeting round-trip
            if not self.greeting_sent:
                self.greeting_sent = True
                try:
                    # Per-response instructions: the greeting does not become a
                    # permanent rule for later turns.
                    greeting = ("Greet the user briefly in English, "
                                "e.g. 'Hi, how can I help?'")
                    if self.gateway:
                        await conn.send({"type": "response.create", "response": {
                            "instructions": f"{self.instructions or ''} {greeting}".strip()}})
                    else:
                        await conn.response.create(additional_instructions=greeting)
                except Exception:
                    logger.exception("Failed to send proactive greeting request")

        elif etype == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            text = _get(event, "transcript", "")
            print(f"👤 You said:\t{text}")
            conversation_logger.info("User Input:\t%s", text)

        elif etype == ServerEventType.RESPONSE_TEXT_DONE:
            text = event.get("text", "")
            print(f"🤖 Agent responded with text:\t{text}")
            conversation_logger.info("Agent Text Response:\t%s", text)

        elif etype == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            text = event.get("transcript", "")
            print(f"🤖 Agent responded with audio transcript:\t{text}")
            conversation_logger.info("Agent Audio Response:\t%s", text)

        elif etype == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            logger.info("User started speaking - stopping playback")
            print("🎤 Listening...")
            self._cancel_watchdog()
            ap.skip_pending_audio()
            # The server cancels the active reply itself (interrupt_response);
            # a client-side response.cancel() would race it and only produce
            # "no active response" errors. Deltas of that reply may still be in
            # flight, so mute them instead of letting them re-fill the buffer.
            if self._response_active:
                self._muted_response_id = self._current_response_id

        elif etype == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            logger.info("User stopped speaking")
            print("🤔 Processing...")
            self._speech_stopped_at = time.monotonic()
            self._first_audio_seen = False
            self._arm_watchdog()

        elif etype == ServerEventType.RESPONSE_CREATED:
            self._response_active = True
            self._current_response_id = _get(_get(event, "response"), "id")
            logger.info("Assistant response created")

        elif etype == ServerEventType.RESPONSE_AUDIO_DELTA:
            if self._is_muted(event):
                return
            if not self._first_audio_seen and self._speech_stopped_at is not None:
                self._first_audio_seen = True
                self._cancel_watchdog()
                ms = (time.monotonic() - self._speech_stopped_at) * 1000
                logger.info("First audio after %.0f ms", ms)
                print(f"⏱️  First audio after {ms:.0f} ms")
            chunk = _get(event, "delta")
            if isinstance(chunk, str):  # GA-named events arrive undecoded (base64)
                chunk = base64.b64decode(chunk)
            ap.queue_audio(chunk)

        elif etype == ServerEventType.RESPONSE_AUDIO_DONE:
            if self._is_muted(event):
                return  # a stale "flush" would make the next reply skip prebuffering
            ap.finish_response()
            logger.info("Assistant finished speaking")
            print("🎤 Ready for next input...")

        elif etype == ServerEventType.RESPONSE_OUTPUT_ITEM_ADDED:
            item = _get(event, "item")
            call_id, name = _get(item, "call_id"), _get(item, "name")
            if call_id and name:
                self._call_names[call_id] = name

        elif etype == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
            self._start_function_call(event)

        elif etype == ServerEventType.RESPONSE_DONE:
            self._response_active = False
            self._muted_response_id = None
            status = _get(_get(event, "response"), "status")
            logger.info("Response complete (status=%s)", status)
            if self._pending_tools:
                # Tool turn: the spoken reply comes from the follow-up response,
                # so keep the watchdog running across the tool call.
                pending, self._pending_tools = self._pending_tools, []
                cancelled = status == ResponseStatus.CANCELLED
                task = asyncio.get_running_loop().create_task(
                    self._send_tool_outputs(pending, request_response=not cancelled))
                self._tool_flushes.add(task)
                task.add_done_callback(self._tool_flushes.discard)
            else:
                self._cancel_watchdog()

        elif etype == ServerEventType.ERROR:
            msg = event.error.message
            if "already has an active response" in (msg or ""):
                # Something upstream (the gateway) asked for a second reply while
                # the server's own auto-reply was running. The server rejects the
                # duplicate and the real reply is unaffected, so keep it off the console.
                logger.info("Duplicate response.create rejected by server (harmless): %s", msg)
                return
            self._cancel_watchdog()
            logger.error("VoiceLive error: %s", msg)
            print(f"Error: {msg}")

        else:
            if etype not in self._unhandled_seen:
                self._unhandled_seen.add(etype)
                logger.info("Unhandled event type (first seen): %s", etype)

    def _log_session(self, event: Any) -> None:
        s = _get(event, "session")
        a, v = _get(s, "agent"), _get(s, "voice")
        logger.info("Session ready: %s", _get(s, "id"))
        lines = [f"SessionID: {_get(s, 'id')}"]
        if a is not None:
            lines += [f"Agent Name: {a.name}", f"Agent Description: {a.description}",
                      f"Agent ID: {a.agent_id}"]
        if v is not None:
            def field(key: str) -> Any:
                return v.get(key) if isinstance(v, dict) else getattr(v, key, None)
            lines += [f"Voice Name: {field('name')}", f"Voice Type: {field('type')}",
                      f"Voice Temperature: {field('temperature')}"]
        conversation_logger.info("\n".join(lines) + "\n")

    def _is_muted(self, event: Any) -> bool:
        """True for audio events of a reply the user already interrupted."""
        muted = self._muted_response_id
        return muted is not None and _get(event, "response_id") == muted

    def _start_function_call(self, event: Any) -> None:
        """Start a local tool as soon as its arguments are known (realtime mode).

        The tool runs in a worker thread while the model finishes the current
        response; its output is sent on RESPONSE_DONE (see _send_tool_outputs).
        """
        # The arguments-done event carries only call_id; the tool name was
        # recorded when the function_call output item was added.
        name = self._call_names.pop(_get(event, "call_id"), None)
        if not self.use_local_tools or not name:
            return
        args, call_id = _get(event, "arguments"), _get(event, "call_id")
        logger.info("Tool call: %s(%s)", name, args)
        print(f"🔧 Tool: {name}")
        future = asyncio.ensure_future(
            asyncio.to_thread(local_tools.run_tool, name, args))
        self._pending_tools.append((call_id, future))

    async def _send_tool_outputs(
        self, pending: list[tuple[str, "asyncio.Future[str]"]], request_response: bool
    ) -> None:
        """Return tool outputs to the model, then ask for the spoken follow-up."""
        conn = self.connection
        if conn is None:
            return
        try:
            for call_id, future in pending:
                output = await future
                await conn.conversation.item.create(
                    item=FunctionCallOutputItem(call_id=call_id, output=output))
            # Skipped when the user barged in: their new turn triggers a reply.
            if request_response:
                await conn.response.create()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Failed to return tool output")


# ------------------------------------------------------------------- main
def build_assistant() -> BasicVoiceAssistant:
    endpoint = os.environ.get("VOICELIVE_ENDPOINT", "")
    voice_name = os.environ.get("VOICE_NAME", "alloy")
    mode = os.environ.get("VOICE_MODE", "realtime").strip().lower()
    api_key = os.environ.get("VOICELIVE_API_KEY", "").strip()
    model = os.environ.get("VOICELIVE_MODEL", "gpt-realtime-2.1")
    agent_name = os.environ.get("AGENT_NAME", "")
    project_name = os.environ.get("PROJECT_NAME", "")

    print("Environment variables:")
    print(f"VOICE_MODE: {mode}")
    print(f"VOICELIVE_ENDPOINT: {endpoint}")
    print(f"VOICELIVE_MODEL: {model}")
    print(f"VOICELIVE_API_KEY: {'***set***' if api_key else '(not set)'}")
    print(f"VOICE_NAME: {voice_name}")
    print(f"AGENT_NAME: {agent_name}")
    print(f"PROJECT_NAME: {project_name}")

    if mode not in ("realtime", "agent"):
        sys.exit(f"VOICE_MODE must be 'realtime' or 'agent', got '{mode}'.")

    gw_url = os.environ.get("AI_GATEWAY_BASE_URL", "").strip()
    gw_key = os.environ.get("AI_GATEWAY_KEY", "").strip()
    if mode == "realtime" and bool(gw_url) != bool(gw_key):
        sys.exit("Set BOTH AI_GATEWAY_BASE_URL and AI_GATEWAY_KEY in .env (or neither).")
    if mode == "realtime" and gw_url and gw_key:
        model = os.environ.get("AI_GATEWAY_REALTIME_MODEL", "gpt-realtime-2.1")
        print(f"AI gateway: {gw_url} (realtime model {model})")
        instructions = os.environ.get("REALTIME_INSTRUCTIONS", DEFAULT_INSTRUCTIONS)
        instructions += f" Today is {datetime.now().strftime('%A, %d %B %Y')}."
        return BasicVoiceAssistant(
            endpoint=gw_url,
            credential=AzureKeyCredential(gw_key),
            voice=voice_name,
            model=model,
            instructions=instructions,
            gateway=True,
        )

    if not endpoint:
        sys.exit("Set VOICELIVE_ENDPOINT in your .env file.")
    if mode == "realtime":
        if not api_key:
            sys.exit("VOICE_MODE=realtime needs VOICELIVE_API_KEY in .env "
                     "(Foundry resource -> Keys and Endpoint).")
        # The realtime model has no clock; give it today's date.
        instructions = os.environ.get("REALTIME_INSTRUCTIONS", DEFAULT_INSTRUCTIONS)
        instructions += f" Today is {datetime.now().strftime('%A, %d %B %Y')}."
        return BasicVoiceAssistant(
            endpoint=endpoint,
            credential=AzureKeyCredential(api_key),
            voice=voice_name,
            model=model,
            instructions=instructions,
        )

    if not agent_name or not project_name:
        sys.exit("Agent mode needs AGENT_NAME and PROJECT_NAME in .env.")
    return BasicVoiceAssistant(
        endpoint=endpoint,
        credential=AzureCliCredential(),
        voice=voice_name,
        agent_name=agent_name,
        agent_version=os.environ.get("AGENT_VERSION"),
        project_name=project_name,
        conversation_id=os.environ.get("CONVERSATION_ID"),
        foundry_resource_override=os.environ.get("FOUNDRY_RESOURCE_OVERRIDE"),
        agent_authentication_identity_client_id=os.environ.get(
            "AGENT_AUTHENTICATION_IDENTITY_CLIENT_ID"),
    )


def _raise_keyboard_interrupt(*_: Any) -> None:
    raise KeyboardInterrupt()


def main() -> None:
    setup_logging()
    assistant = build_assistant()

    # SIGINT already raises KeyboardInterrupt; make SIGTERM do the same.
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    try:
        asyncio.run(assistant.run())
    except KeyboardInterrupt:
        print("\n👋 Voice assistant shut down. Goodbye!")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fatal error")
        sys.exit(f"Fatal Error: {exc}")


def _check_audio_devices() -> None:
    """Verify audio input/output devices are available."""
    p = pyaudio.PyAudio()
    try:
        def _has_channels(key: str) -> bool:
            return any(
                cast(Union[int, float], p.get_device_info_by_index(i).get(key, 0) or 0) > 0
                for i in range(p.get_device_count())
            )
        if not _has_channels("maxInputChannels"):
            sys.exit("❌ No audio input devices found. Please check your microphone.")
        if not _has_channels("maxOutputChannels"):
            sys.exit("❌ No audio output devices found. Please check your speakers.")
    finally:
        p.terminate()


if __name__ == "__main__":
    try:
        _check_audio_devices()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        sys.exit(f"❌ Audio system check failed: {e}")

    mode_label = os.environ.get("VOICE_MODE", "realtime").strip().lower()
    print(f"🎙️ Foundry Voice Agent with Azure Voice Live SDK ({mode_label} mode)")
    print("=" * 65)
    main()
