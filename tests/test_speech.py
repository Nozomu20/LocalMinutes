import unittest

import numpy as np

from localminutes.diarization import TurnIndex, label_segment
from localminutes.transcription import RATE, has_audio


class SpeechTests(unittest.TestCase):
    def test_speaker_changes_inside_a_single_asr_segment(self):
        index = TurnIndex([
            {"start": 0.0, "end": 1.0, "speaker": "S1"},
            {"start": 1.0, "end": 2.0, "speaker": "S2"},
            {"start": 2.0, "end": 3.0, "speaker": "S1"},
        ])
        words = [
            {"start": 0.1, "end": 0.8, "word": "始めます。"},
            {"start": 1.1, "end": 1.8, "word": "はい。"},
            {"start": 2.1, "end": 2.8, "word": "では。"},
        ]
        row = {"start": 0.0, "end": 3.0, "text": "始めます。はい。では。", "words": words}
        parts = label_segment(row, index)
        self.assertEqual([part["speaker"] for part in parts], ["S1", "S2", "S1"])
        self.assertEqual("".join(part["text"] for part in parts), row["text"])

    def test_overlapping_speakers_and_gaps_remain_unassigned(self):
        index = TurnIndex([
            {"start": 0.0, "end": 4.0, "speaker": "S1"},
            {"start": 1.0, "end": 2.0, "speaker": "S2"},
        ])
        self.assertEqual(index.speaker(1.1, 1.4), "")
        self.assertEqual(index.speaker(4.1, 5.0), "")

    def test_quiet_stationary_noise_and_quiet_varying_signal(self):
        rng = np.random.default_rng(742)
        noise = rng.normal(0, 8 / 32768, RATE * 2).astype(np.float32)
        self.assertFalse(has_audio(np.zeros(RATE), np))
        self.assertFalse(has_audio(noise, np))
        noise[RATE // 2:RATE] *= 2.6
        self.assertTrue(has_audio(noise, np))


if __name__ == "__main__":
    unittest.main()
