from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from localminutes.store import Library


class RecordingStorageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.library = Library(Path(folder.name))
        self.addCleanup(self.library.lockfile.close)
        self.addCleanup(self.library.close)

    def test_delayed_or_duplicate_requests_do_not_change_audio(self):
        library = self.library
        key = library.start("遅延試験", 16000)
        first = b"\x01\x02" * 4000
        tail = b"\x03\x04" * 250
        with ThreadPoolExecutor(max_workers=2) as workers:
            writes = [workers.submit(library.chunk, key, first, 0) for _ in range(2)]
            self.assertEqual([w.result() for w in writes], [len(first)] * 2)
        for position, data in [(0, tail), (len(first) - 2, tail), (len(first) + 2, tail)]:
            with self.subTest(position=position), self.assertRaises(ValueError):
                library.chunk(key, data, position)
        self.assertEqual(library.chunk(key, tail, len(first)), len(first) + len(tail))
        library.stop(key, interrupted=True)
        self.assertEqual(library.rows[key]["status"], "ready")
        with wave.open(str(library.audio_file(key)), "rb") as audio:
            self.assertEqual(audio.readframes(audio.getnframes()), first + tail)

    def test_old_client_can_still_append_without_an_offset(self):
        library = self.library
        key = library.start("旧版の録音", 16000)
        library.chunk(key, b"\x01\x00")
        library.chunk(key, b"\x02\x00")
        library.stop(key, interrupted=True)
        with wave.open(str(library.audio_file(key)), "rb") as audio:
            self.assertEqual(audio.readframes(2), b"\x01\x00\x02\x00")

    @patch("localminutes.store.SystemAudio")
    def test_remote_capture_stops_before_the_last_mic_write(self, system_audio):
        capture = system_audio.return_value
        capture.wait_ready.return_value = capture.start_time = 1000.0
        capture.status.return_value = {"error": "", "heard": True}
        library = self.library
        key = library.start("オンライン録音", 16000, mode="online")
        library.last_chunk -= 20
        library.tick()
        self.assertEqual(library.active, key)
        library.stop_input(key)
        capture.stop.assert_called()
        self.assertEqual(library.chunk(key, b"\x03\x00", 0), 2)
        library.stop(key, interrupted=True)
        with wave.open(str(library.root / key / "mic.wav"), "rb") as audio:
            self.assertEqual(audio.readframes(1), b"\x03\x00")
