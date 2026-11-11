"""Offline tests for the Voice Live session logic (no API key, network or audio).

A fake connection replays wire-format JSON server events, parsed by the SDK's own
deserializer exactly as a live socket would be, and records every client call.
"""

import asyncio
import base64
import json
import os
import sys
import unittest
from unittest import mock

import aiohttp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from azure.ai.voicelive.aio._patch import ConnectionError as VLConnectionError  # noqa: E402
from azure.ai.voicelive.models import ServerEvent  # noqa: E402
from azure.core.credentials import AzureKeyCredential  # noqa: E402

import audio  # noqa: E402
import voice_client  # noqa: E402


# ------------------------------------------------------------------ fakes
def ev(etype, **fields):
    return ServerEvent._deserialize({"type": etype, "event_id": "evt", **fields}, [])


def response(rid, status="in_progress"):
    return {"id": rid, "object": "realtime.response", "status": status, "output": []}


def delta(rid, pcm):
    return ev("response.audio.delta", response_id=rid, item_id="i", output_index=0,
              content_index=0, delta=base64.b64encode(pcm).decode())


SESSION_UPDATED = ev("session.updated", session={"id": "sess_1"})
SPEECH_STARTED = ev("input_audio_buffer.speech_started", audio_start_ms=0, item_id="u")
SPEECH_STOPPED = ev("input_audio_buffer.speech_stopped", audio_end_ms=0, item_id="u")


def created(rid):
    return ev("response.created", response=response(rid))


def audio_done(rid):
    return ev("response.audio.done", response_id=rid, item_id="i", output_index=0, content_index=0)


def done(rid, status="completed"):
    return ev("response.done", response=response(rid, status))


def tool_turn(rid, call_id):
    return [
        created(rid),
        ev("response.output_item.added", response_id=rid, output_index=0,
           item={"type": "function_call", "id": "it" + call_id, "call_id": call_id,
                 "name": "get_current_datetime", "arguments": "", "status": "in_progress"}),
        ev("response.function_call_arguments.done", response_id=rid, item_id="it" + call_id,
           output_index=0, call_id=call_id, name="get_current_datetime", arguments="{}"),
        done(rid),
    ]


class Pause:
    """Inside an event script: yield to the loop so background tasks can run."""

    def __init__(self, seconds=0.05):
        self.seconds = seconds


class _Recorder:
    def __init__(self, log, prefix):
        self._log, self._prefix = log, prefix

    def __getattr__(self, name):
        path = f"{self._prefix}.{name}"
        if name in ("create", "update", "cancel", "append"):
            async def call(**kwargs):
                self._log.append((path, kwargs))
            return call
        return _Recorder(self._log, path)


class FakeConnection:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.session = _Recorder(self.calls, "session")
        self.response = _Recorder(self.calls, "response")
        self.conversation = _Recorder(self.calls, "conversation")
        self.input_audio_buffer = _Recorder(self.calls, "input_audio_buffer")

    async def send(self, event):
        self.calls.append(("send", {"event": event}))

    def names(self):
        return [c[0] for c in self.calls]

    async def __aiter__(self):
        for item in self.script:
            if isinstance(item, Pause):
                await asyncio.sleep(item.seconds)
            else:
                yield item
        await asyncio.sleep(0)


class FakeAudio:
    def __init__(self, connection):
        self.played = bytearray()
        self.events = []

    def start_capture(self):
        self.events.append("start_capture")

    def start_playback(self):
        self.events.append("start_playback")

    def queue_audio(self, data):
        self.played.extend(data)

    def finish_response(self):
        self.events.append("finish")

    def skip_pending_audio(self):
        self.events.append("skip")
        self.played.clear()

    def shutdown(self):
        self.events.append("shutdown")


class _ConnectCM:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def run_session(script, env=None):
    """Run one session over `script`; return (connection, audio processor)."""
    conn = FakeConnection(script)
    holder = {}

    def fake_audio(c):
        holder["ap"] = FakeAudio(c)
        return holder["ap"]

    environ = {"REALTIME_TOOLS": "0", "RESPONSE_TIMEOUT_S": "0", **(env or {})}
    with mock.patch.dict(os.environ, environ), \
            mock.patch.object(voice_client, "connect", lambda **kw: _ConnectCM(conn)), \
            mock.patch.object(voice_client, "AudioProcessor", fake_audio), \
            mock.patch("builtins.print"):
        assistant = voice_client.BasicVoiceAssistant(
            endpoint="https://x", credential=AzureKeyCredential("k"), voice="alloy",
            model="gpt-realtime", instructions="hi")
        with assistant_raises_closed():
            asyncio.run(assistant.start())
    return conn, holder["ap"], assistant


class assistant_raises_closed:
    """start() ends with ConnectionError when the fake stream runs out of events."""

    def __enter__(self):
        return self

    def __exit__(self, etype, exc, tb):
        return etype is ConnectionError and "closed the connection" in str(exc)


# ------------------------------------------------------------------ tests
class SessionFlowTests(unittest.TestCase):
    def test_normal_turn(self):
        conn, ap, _ = run_session([
            SESSION_UPDATED, created("g"), delta("g", b"\x01" * 10), audio_done("g"), done("g"),
            SPEECH_STARTED, SPEECH_STOPPED,
            created("r1"), delta("r1", b"\x02" * 10), audio_done("r1"), done("r1"),
        ])
        self.assertEqual(conn.names()[:2], ["session.update", "response.create"])  # config, greeting
        self.assertEqual(ap.events[:2], ["start_playback", "start_capture"])
        self.assertEqual(bytes(ap.played), b"\x02" * 10)  # greeting cleared on barge-in
        self.assertIn("shutdown", ap.events)

    def test_realtime_session_config(self):
        conn, _, _ = run_session([], env={"VAD_SILENCE_MS": ""})
        session = conn.calls[0][1]["session"]
        self.assertEqual(session.turn_detection.silence_duration_ms, 400)
        self.assertTrue(session.turn_detection.interrupt_response)

    def test_barge_in_drops_late_audio_and_does_not_cancel_manually(self):
        conn, ap, _ = run_session([
            SESSION_UPDATED,
            created("r1"), delta("r1", b"\x01" * 10),
            SPEECH_STARTED,                       # user interrupts
            delta("r1", b"\x01" * 10),            # already in flight -> must be dropped
            audio_done("r1"),                     # must not set a stale flush
            done("r1", "cancelled"),
            SPEECH_STOPPED,
            created("r2"), delta("r2", b"\x02" * 10), audio_done("r2"), done("r2"),
        ])
        self.assertEqual(bytes(ap.played), b"\x02" * 10)
        self.assertNotIn("response.cancel", conn.names())
        self.assertEqual(ap.events.count("finish"), 1)  # only r2

    def test_tool_output_sent_after_response_done(self):
        conn, _, _ = run_session([
            SESSION_UPDATED,
            created("r1"),
            ev("response.output_item.added", response_id="r1", output_index=0,
               item={"type": "function_call", "id": "it", "call_id": "c1",
                     "name": "get_current_datetime", "arguments": "", "status": "in_progress"}),
            ev("response.function_call_arguments.done", response_id="r1", item_id="it",
               output_index=0, call_id="c1", name="get_current_datetime", arguments="{}"),
            Pause(),                                 # tool finishes before response.done
            done("r1"),
            Pause(),
        ], env={"REALTIME_TOOLS": "1"})
        names = conn.names()
        self.assertEqual(names[-2:], ["conversation.item.create", "response.create"])
        output = conn.calls[-2][1]["item"]
        self.assertEqual(output.call_id, "c1")
        self.assertIn("date", json.loads(output.output))

    def test_tool_after_cancelled_response_does_not_request_reply(self):
        conn, _, _ = run_session([
            SESSION_UPDATED,
            created("r1"),
            ev("response.output_item.added", response_id="r1", output_index=0,
               item={"type": "function_call", "id": "it", "call_id": "c1",
                     "name": "get_current_datetime", "arguments": "", "status": "in_progress"}),
            ev("response.function_call_arguments.done", response_id="r1", item_id="it",
               output_index=0, call_id="c1", name="get_current_datetime", arguments="{}"),
            done("r1", "cancelled"),
            Pause(),
        ], env={"REALTIME_TOOLS": "1"})
        self.assertEqual(conn.names()[-1], "conversation.item.create")

    def test_back_to_back_tool_turns_both_deliver(self):
        conn, _, _ = run_session(
            [SESSION_UPDATED] + tool_turn("r1", "c1") + tool_turn("r2", "c2") + [Pause(0.1)],
            env={"REALTIME_TOOLS": "1"})
        sent = [c[1]["item"].call_id for c in conn.calls if c[0] == "conversation.item.create"]
        self.assertEqual(sorted(sent), ["c1", "c2"])
        self.assertEqual(conn.names().count("response.create"), 3)  # greeting + 2 follow-ups

    def test_tools_ignored_when_disabled(self):
        conn, _, _ = run_session([
            SESSION_UPDATED, created("r1"),
            ev("response.output_item.added", response_id="r1", output_index=0,
               item={"type": "function_call", "id": "it", "call_id": "c1",
                     "name": "get_current_datetime", "arguments": "", "status": "in_progress"}),
            ev("response.function_call_arguments.done", response_id="r1", item_id="it",
               output_index=0, call_id="c1", name="get_current_datetime", arguments="{}"),
            done("r1"), Pause(),
        ])
        self.assertNotIn("conversation.item.create", conn.names())


def _handshake_error(status):
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL
    url = URL("wss://example.services.ai.azure.com/voice-live/realtime")
    info = aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)
    err = aiohttp.WSServerHandshakeError(info, (), status=status, message="Invalid response status")
    try:
        raise VLConnectionError(f"Failed to establish WebSocket connection: {err}") from err
    except VLConnectionError as exc:
        return exc


class ReconnectTests(unittest.TestCase):
    def _run(self, errors):
        assistant = voice_client.BasicVoiceAssistant(
            endpoint="https://x", credential=AzureKeyCredential("k"), voice="alloy", model="m")
        assistant.max_retries = 3
        calls = []

        async def fake_start():
            calls.append(1)
            if errors:
                raise errors.pop(0)

        assistant.start = fake_start
        with mock.patch.object(voice_client.asyncio, "sleep", mock.AsyncMock()), \
                mock.patch("builtins.print"), mock.patch.object(voice_client.logger, "exception"):
            try:
                asyncio.run(assistant.run())
            except Exception as exc:  # noqa: BLE001
                return calls, exc
        return calls, None

    def test_auth_failure_is_not_retried(self):
        calls, exc = self._run([_handshake_error(401)])
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(exc, VLConnectionError)

    def test_transient_failure_reconnects(self):
        calls, exc = self._run([_handshake_error(503), ConnectionError("drop")])
        self.assertEqual(len(calls), 3)
        self.assertIsNone(exc)

    def test_gives_up_after_max_retries(self):
        calls, exc = self._run([ConnectionError("drop")] * 10)
        self.assertEqual(len(calls), 4)  # first try + 3 retries
        self.assertIsInstance(exc, ConnectionError)

    def test_audio_device_failure_is_not_retried(self):
        calls, exc = self._run([audio.AudioDeviceError("cannot open microphone")])
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(exc, audio.AudioDeviceError)

    def test_is_auth_error(self):
        self.assertTrue(voice_client._is_auth_error(_handshake_error(403)))
        self.assertFalse(voice_client._is_auth_error(_handshake_error(500)))
        self.assertFalse(voice_client._is_auth_error(ValueError("x")))


class MicSenderTests(unittest.TestCase):
    def _processor(self, conn):
        ap = audio.AudioProcessor.__new__(audio.AudioProcessor)  # no audio devices
        ap.connection = conn
        ap.dropped_mic_chunks = 0
        return ap

    def test_backlog_is_coalesced_into_one_message(self):
        conn = FakeConnection([])

        async def scenario():
            ap = self._processor(conn)
            ap._mic_queue = asyncio.Queue(maxsize=10)
            for b in (b"a", b"b", b"c"):
                ap._enqueue_mic(b)
            task = asyncio.create_task(ap._mic_sender())
            await asyncio.sleep(0.01)
            task.cancel()

        asyncio.run(scenario())
        self.assertEqual(len(conn.calls), 1)
        self.assertEqual(base64.b64decode(conn.calls[0][1]["audio"]), b"abc")

    def test_half_duplex_drops_mic_while_speaker_plays(self):
        async def scenario():
            ap = self._processor(FakeConnection([]))
            ap._mic_queue = asyncio.Queue(maxsize=10)
            ap.half_duplex = True
            ap._speaking_until = audio.time.monotonic() + 5
            ap._enqueue_mic(b"echo")
            muted = ap._mic_queue.qsize()
            ap._speaking_until = 0
            ap._enqueue_mic(b"you")
            return muted, ap._mic_queue.qsize()

        self.assertEqual(asyncio.run(scenario()), (0, 1))

    def test_full_queue_drops_oldest(self):
        async def scenario():
            ap = self._processor(FakeConnection([]))
            ap._mic_queue = asyncio.Queue(maxsize=2)
            for b in (b"1", b"2", b"3"):
                ap._enqueue_mic(b)
            return ap, [ap._mic_queue.get_nowait() for _ in range(2)]

        ap, left = asyncio.run(scenario())
        self.assertEqual(left, [b"2", b"3"])
        self.assertEqual(ap.dropped_mic_chunks, 1)



class ShutdownTests(unittest.TestCase):
    def test_shutdown_continues_when_a_device_vanished(self):
        import threading
        ap = audio.AudioProcessor.__new__(audio.AudioProcessor)
        ap._lock, ap._buf, ap._primed, ap._flush = threading.Lock(), bytearray(b"x"), True, False
        ap._sender_task, ap.rate = None, audio.RATE
        ap.input_stream = mock.Mock(**{"stop_stream.side_effect": OSError("device gone")})
        ap.output_stream = mock.Mock()
        backend = ap.audio = mock.Mock()
        with mock.patch.object(audio.logger, "warning"):
            ap.shutdown()
        self.assertIsNone(ap.input_stream)
        self.assertIsNone(ap.output_stream)
        backend.terminate.assert_called_once()
        self.assertEqual(ap.buffered_ms, 0)


class GatewayTests(unittest.TestCase):
    def test_url_forms(self):
        for base in ("https://gw.example.com", "https://gw.example.com/",
                     "https://gw.example.com/v1", "https://gw.example.com/v1/"):
            self.assertEqual(voice_client.gateway_realtime_url(base, "gpt-realtime-2.1"),
                             "wss://gw.example.com/v1/realtime?model=gpt-realtime-2.1")
        self.assertEqual(voice_client.gateway_realtime_url("http://localhost:4000", "m"),
                         "ws://localhost:4000/v1/realtime?model=m")

    def test_manager_url_and_bearer_auth(self):
        mgr = voice_client.GatewayConnectionManager(
            base_url="https://gw.example.com/v1", api_key="sk-1", model="gpt-realtime-2.1")
        self.assertEqual(mgr._prepare_url(),
                         "wss://gw.example.com/v1/realtime?model=gpt-realtime-2.1")
        self.assertEqual(asyncio.run(mgr._get_auth_headers()), {"Authorization": "Bearer sk-1"})

    def _run_gateway(self, script):
        conn = FakeConnection(script)
        holder = {}

        def fake_audio(c):
            holder["ap"] = FakeAudio(c)
            return holder["ap"]

        with mock.patch.dict(os.environ, {"RESPONSE_TIMEOUT_S": "0", "REALTIME_TOOLS": "0"}), \
                mock.patch.object(voice_client, "GatewayConnectionManager",
                                  lambda **kw: _ConnectCM(conn)), \
                mock.patch.object(voice_client, "AudioProcessor", fake_audio), \
                mock.patch("builtins.print"):
            assistant = voice_client.BasicVoiceAssistant(
                endpoint="https://gw", credential=AzureKeyCredential("k"), voice="alloy",
                model="gpt-realtime-2.1", instructions="hi", gateway=True)
            with assistant_raises_closed():
                asyncio.run(assistant.start())
        return conn, holder["ap"]

    def test_session_is_ga_shaped(self):
        conn, _ = self._run_gateway([])
        session = conn.calls[0][1]["session"]
        self.assertEqual(session["type"], "realtime")
        self.assertEqual(session["model"], "gpt-realtime-2.1")
        self.assertNotIn("modalities", session)
        audio_in = session["audio"]["input"]
        self.assertEqual(audio_in["turn_detection"]["type"], "server_vad")
        self.assertEqual(audio_in["transcription"], {"model": "whisper-1", "language": "en"})
        self.assertEqual(audio_in["noise_reduction"], {"type": "far_field"})
        self.assertEqual(audio_in["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertEqual(session["audio"]["output"]["voice"], "alloy")
        self.assertNotIn("input_audio_echo_cancellation", session)

    def test_duplicate_response_error_is_not_shown(self):
        msg = "Conversation already has an active response in progress: resp_1. Wait."
        err = ev("error", error={"type": "invalid_request_error", "message": msg})
        with mock.patch.dict(os.environ, {"RESPONSE_TIMEOUT_S": "0"}), \
                mock.patch.object(voice_client, "GatewayConnectionManager",
                                  lambda **kw: _ConnectCM(FakeConnection([SESSION_UPDATED, err]))), \
                mock.patch.object(voice_client, "AudioProcessor", lambda c: FakeAudio(c)), \
                mock.patch("builtins.print") as printed:
            a = voice_client.BasicVoiceAssistant(
                endpoint="https://gw", credential=AzureKeyCredential("k"), voice="alloy",
                model="m", instructions="hi", gateway=True)
            with assistant_raises_closed():
                asyncio.run(a.start())
        self.assertFalse(any("Error:" in str(c) for c in printed.call_args_list))

    def test_ga_event_names_play_audio(self):
        def ga_delta(rid, pcm):
            return ev("response.output_audio.delta", response_id=rid, item_id="i",
                      output_index=0, content_index=0, delta=base64.b64encode(pcm).decode())
        conn, ap = self._run_gateway([
            SESSION_UPDATED, created("g"), ga_delta("g", b"\x03" * 10),
            ev("response.output_audio.done", response_id="g", item_id="i",
               output_index=0, content_index=0),
            done("g")])
        self.assertEqual(bytes(ap.played), b"\x03" * 10)
        self.assertEqual(conn.calls[1][0], "send")  # GA greeting via raw response.create


if __name__ == "__main__":
    unittest.main()
