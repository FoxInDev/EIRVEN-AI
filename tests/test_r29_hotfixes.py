from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import launcher

try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    stub = types.ModuleType("httpx")
    stub.Response = object
    stub.Timeout = object
    sys.modules["httpx"] = stub

from eirven_ai.chat import ChatService
from eirven_ai.desktop_operator import DesktopOperator
from eirven_ai.reliability_router import ReliabilityRouter


class _Tools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name: str, arguments: dict):
        self.calls.append((name, dict(arguments)))
        return {"ok": True}


class R29HotfixTests(unittest.TestCase):
    def test_mini_sphere_has_no_separate_blurred_background_layer(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src" / "eirven_ai" / "companion.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("make_glow", source)
        self.assertNotIn("glow_idle", source)

    def test_autostart_uses_quiet_launcher_without_uac(self) -> None:
        self.assertTrue(launcher._autostart_requested(["--autostart"]))
        self.assertFalse(launcher._autostart_requested([]))
        with mock.patch.object(launcher.os, "name", "nt"), mock.patch.object(
            launcher, "_env_wants_full_access"
        ) as wants_access:
            self.assertFalse(launcher._request_elevation(autostart=True))
            wants_access.assert_not_called()

        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "install_autostart.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("$Shortcut.TargetPath = $PythonW", script)
        self.assertIn("--autostart", script)
        self.assertNotIn("EIRVEN-AI-r29.exe", script)

    def test_existing_autostart_shortcut_is_migrated_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            appdata = root / "AppData" / "Roaming"
            shortcut = (
                appdata / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / "Startup" / "EIRVEN AI.lnk"
            )
            shortcut.parent.mkdir(parents=True)
            shortcut.write_bytes(b"old shortcut")
            script = root / "scripts" / "install_autostart.ps1"
            script.parent.mkdir(parents=True)
            script.write_text("# test", encoding="utf-8")
            with mock.patch.object(launcher, "_windows_platform", return_value=True), mock.patch.dict(
                launcher.os.environ, {"APPDATA": str(appdata)}
            ), mock.patch.object(launcher, "APP_ROOT", root), mock.patch.object(
                launcher.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ) as run:
                self.assertTrue(launcher._repair_existing_autostart())
                self.assertTrue((root / "data" / ".autostart-quiet-launcher").is_file())
                self.assertTrue(launcher._repair_existing_autostart())
                run.assert_called_once()

    def test_noncommitting_telegram_search_never_retries_a_valid_paste(self) -> None:
        operator = DesktopOperator.__new__(DesktopOperator)
        operator.tools = _Tools()
        operator._click_input_rect = lambda _field: True
        operator._elements = lambda *_args, **_kwargs: []
        operator._trace = lambda *_args, **_kwargs: None

        clipboard = types.ModuleType("pyperclip")
        clipboard.value = ""
        clipboard.copy = lambda value: setattr(clipboard, "value", value)
        clipboard.paste = lambda: clipboard.value
        acquired = {
            "ok": True,
            "title": "Telegram Web",
            "handle": 10,
            "purpose": "search",
            "field": {"control_type": "Group", "rectangle": [80, 109, 360, 153]},
            "rows": [],
        }
        with mock.patch.dict(sys.modules, {"pyperclip": clipboard}):
            result = operator.type_verified(
                acquired, "тима", submit=False, require_verified=False,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["typed"])
        hotkeys = [args["keys"] for name, args in operator.tools.calls if name == "hotkey"]
        self.assertEqual(hotkeys, [["ctrl", "a"], ["ctrl", "v"]])
        self.assertNotIn(["shift", "insert"], hotkeys)

    def test_telegram_k_topbar_is_active_chat_evidence(self) -> None:
        operator = DesktopOperator.__new__(DesktopOperator)
        rows = [{
            "visible": True,
            "name": "Тима",
            "class_name": "",
            "automation_id": "",
            "rectangle": [1204, 108, 1243, 128],
        }]
        confirmed, evidence = operator._telegram_chat_evidence(rows, "тима")
        self.assertTrue(confirmed)
        self.assertTrue(evidence["header"])

        rows[0]["rectangle"] = [1204, 508, 1243, 528]
        confirmed, evidence = operator._telegram_chat_evidence(rows, "тима")
        self.assertFalse(confirmed)
        self.assertFalse(evidence["header"])

    def test_telegram_voice_order_and_common_asr_variant_route_deterministically(self) -> None:
        router = ReliabilityRouter()
        decision = router.classify("Открой и напиши Тиме привет в Telegram")
        self.assertEqual((decision.kind, decision.app), ("app_compound", "telegram"))
        self.assertEqual(
            ChatService._parse_telegram_command(decision.remainder),
            ("тиме", "привет"),
        )

        asr = router.classify("Открой елеграм и напиши Тиме привет")
        self.assertEqual((asr.kind, asr.app), ("app_compound", "telegram"))


if __name__ == "__main__":
    unittest.main()
