import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from audio import AudioProcessor  # noqa: E402


def make(prebuffer_ms=100):
    ap = AudioProcessor.__new__(AudioProcessor)  # skip opening real audio devices
    import threading
    ap.rate = 24000
    ap._lock = threading.Lock()
    ap._buf = bytearray()
    ap._primed = False
    ap._flush = False
    ap._prebuffer_bytes = int(24000 * 2 * prebuffer_ms / 1000)
    return ap


class PlaybackBufferTests(unittest.TestCase):
    def test_silence_until_prebuffer_filled(self):
        ap = make()
        ap.queue_audio(b"\x01" * 1000)  # < 4800 bytes
        self.assertEqual(ap.read_playback(2400), bytes(2400))
        ap.queue_audio(b"\x01" * 4000)
        self.assertEqual(ap.read_playback(2400), b"\x01" * 2400)

    def test_short_reply_plays_after_finish(self):
        ap = make()
        ap.queue_audio(b"\x02" * 1000)
        ap.finish_response()
        self.assertEqual(ap.read_playback(1000), b"\x02" * 1000)

    def test_rebuffers_after_underrun(self):
        ap = make()
        ap.queue_audio(b"\x03" * 4800)
        ap.read_playback(4800)           # drains buffer
        ap.queue_audio(b"\x03" * 100)    # tiny late chunk
        self.assertEqual(ap.read_playback(100), bytes(100))

    def test_partial_chunk_is_padded(self):
        ap = make()
        ap.queue_audio(b"\x04" * 4800)
        out = ap.read_playback(6000)
        self.assertEqual(len(out), 6000)
        self.assertEqual(out[:4800], b"\x04" * 4800)
        self.assertEqual(out[4800:], bytes(1200))

    def test_barge_in_clears_everything(self):
        ap = make()
        ap.queue_audio(b"\x05" * 10000)
        ap.read_playback(2400)
        ap.skip_pending_audio()
        self.assertEqual(ap.read_playback(2400), bytes(2400))
        self.assertEqual(ap.buffered_ms, 0)

    def test_stale_flush_does_not_leak_into_next_reply(self):
        ap = make()
        ap.finish_response()             # nothing buffered
        ap.read_playback(100)
        ap.queue_audio(b"\x06" * 500)    # new reply, below prebuffer
        self.assertEqual(ap.read_playback(500), bytes(500))


if __name__ == "__main__":
    unittest.main()
