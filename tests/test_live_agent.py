from __future__ import annotations

import unittest

from eirven_ai.autonomous_workflow import AutonomousWorkflowEngine
from eirven_ai.companion import DesktopCompanion
from eirven_ai.proactive import ProactiveObserver


class LiveAgentTests(unittest.TestCase):
    def test_missing_product_is_clarified(self) -> None:
        engine = AutonomousWorkflowEngine.__new__(AutonomousWorkflowEngine)
        self.assertIn("Какой", engine._clarification_prompt("Добавь товар в корзину"))
        self.assertEqual(engine._clarification_prompt("Добавь любой товар в корзину"), "")

    def test_delete_scope_is_clarified(self) -> None:
        engine = AutonomousWorkflowEngine.__new__(AutonomousWorkflowEngine)
        self.assertIn("Сколько", engine._clarification_prompt("Удали последние сообщения в чате с Анной"))
        self.assertEqual(engine._clarification_prompt("Удали последнее сообщение в чате с Анной у всех"), "")

    def test_eyes_follow_spoken_emotion(self) -> None:
        self.assertEqual(DesktopCompanion._eye_mode({"speaking": True, "speaking_emotion": "amused"}), "amused")
        self.assertEqual(DesktopCompanion._eye_mode({"speaking": True, "speaking_emotion": "sad"}), "sad")

    def test_sensitive_surfaces_are_recognized(self) -> None:
        self.assertIsNotNone(ProactiveObserver.SENSITIVE_CONTEXT.search("Введите пароль и код 2FA"))
        self.assertIsNone(ProactiveObserver.SENSITIVE_CONTEXT.search("VS Code: tests failed"))


if __name__ == "__main__":
    unittest.main()
