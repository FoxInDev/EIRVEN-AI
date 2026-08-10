from __future__ import annotations

import unittest

from eirven_ai.mission_engine import MissionEngine


class _Operator:
    @staticmethod
    def _norm(value) -> str:
        return " ".join(str(value or "").casefold().replace("ё", "е").split())


class MissionEngineTests(unittest.TestCase):
    def test_telegram_candidates_use_relative_left_pane(self) -> None:
        rows = [
            {"visible": True, "enabled": True, "control_type": "Button", "class_name": "ListItem Button", "name": "Привет \ue952 Анна \ue900 Aug 9", "rectangle": [20, 190, 510, 270]},
            {"visible": True, "enabled": True, "control_type": "Button", "class_name": "ListItem Button", "name": "Прочитанный чат", "rectangle": [20, 280, 510, 360]},
            {"visible": True, "enabled": True, "control_type": "Button", "class_name": "message", "name": "Send", "rectangle": [1200, 760, 1300, 820]},
        ]
        candidates, fingerprint = MissionEngine._telegram_chat_candidates(
            rows, _Operator(), (0, 0, 1600, 900), set()
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][2], "Анна")
        self.assertIn("прочитанный чат", fingerprint)

    def test_telegram_date_is_not_an_unread_badge(self) -> None:
        self.assertEqual(MissionEngine._telegram_row_unread_count("Спасибо Папа Aug 9"), 0)
        self.assertEqual(MissionEngine._telegram_row_unread_count("Новость 6 Mash \uea0e Aug 9"), 6)

    def test_header_kind_is_resolution_independent(self) -> None:
        rows = [{"name": "online", "rectangle": [800, 80, 1050, 120]}]
        self.assertEqual(MissionEngine._telegram_header_kind(rows, (0, 0, 1366, 768)), "personal")
        channels = [{"name": "1 240 subscribers", "rectangle": [800, 80, 1100, 120]}]
        self.assertEqual(MissionEngine._telegram_header_kind(channels, (0, 0, 1366, 768)), "non_personal")

    def test_mesh_homework_becomes_typed_three_node_graph(self) -> None:
        engine = MissionEngine.__new__(MissionEngine)
        nodes = engine._mesh_homework_plan(
            "Посмотри домашнее задание на завтра в МЭШ и отправь его в Избранное в Telegram"
        )
        self.assertIsNotNone(nodes)
        assert nodes is not None
        self.assertEqual([node.kind for node in nodes], ["open_target", "extract_text", "telegram_message"])
        self.assertEqual(nodes[-1].metadata["artifact_from"], "n2")
        self.assertTrue(nodes[-1].commit)


if __name__ == "__main__":
    unittest.main()
