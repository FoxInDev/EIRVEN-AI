from __future__ import annotations

import unittest

from eirven_ai.affect import analyze_speech_affect
from eirven_ai.identity import IdentityService, VOICE_MODES


class AffectTests(unittest.TestCase):
    def test_text_emotions_are_specific(self) -> None:
        self.assertEqual(IdentityService.infer_emotion("Мне сегодня очень грустно"), "sad")
        self.assertEqual(IdentityService.infer_emotion("Ахаха, вот это смешно"), "amused")
        self.assertEqual(IdentityService.infer_emotion("Я совсем не выспался и устал"), "tired")

    def test_textual_affect_wins_with_confidence(self) -> None:
        result = analyze_speech_affect(
            "мне грустно", duration=2.0, energy=.02, noise_floor=.004,
            textual_emotion="sad",
        )
        self.assertEqual(result.emotion, "sad")
        self.assertGreaterEqual(result.confidence, .8)

    def test_all_live_modes_have_voice_profiles(self) -> None:
        required = {"amused", "sad", "empathetic", "curious", "concerned", "proud", "tired"}
        self.assertTrue(required.issubset(VOICE_MODES))


if __name__ == "__main__":
    unittest.main()
