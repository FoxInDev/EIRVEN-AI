from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

from eirven_ai.chat import ChatService
from eirven_ai.desktop_operator import DesktopOperator


ROOT = Path(__file__).resolve().parents[1]


class _ClickTools:
    def __init__(self):
        self.calls = []

    def execute(self, name, args):
        self.calls.append((name, args))
        return {"ok": True, "result": args}


class _TelegramButtonTools(_ClickTools):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def execute(self, name, args):
        self.calls.append((name, args))
        if name == "window_elements":
            return {"ok": True, "result": self.rows}
        return {"ok": True, "result": args}


def test_composer_click_uses_left_caret_zone():
    operator = object.__new__(DesktopOperator)
    operator.tools = _ClickTools()
    field = {
        "control_type": "Group",
        "class_name": "input-message-input is-empty contenteditable",
        "rectangle": [100, 500, 1100, 580],
    }
    assert operator._click_input_rect(field) is True
    click = next(args for name, args in operator.tools.calls if name == "click")
    assert 130 <= click["x"] <= 240
    assert click["y"] == 536


def test_gender_guard_covers_unlisted_common_self_forms():
    chat = object.__new__(ChatService)
    chat.identity = SimpleNamespace(get=lambda: SimpleNamespace(gender="female"))
    result = chat.enforce_gender("Я был уверен. Закончил настройку. Рад, что всё готово.")
    assert result == "Я была уверена. Закончила настройку. Рада, что всё готово."


def test_ui_uses_runtime_wake_phrase_and_dynamic_name():
    js = (ROOT / "src" / "eirven_ai" / "web" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "src" / "eirven_ai" / "web" / "index.html").read_text(encoding="utf-8")
    assert "r?.wake_phrase||configuredName()" in js
    assert "applyAssistantName" in js
    assert "onboard-wake-name" in html
    assert "скажи «Эрви»" not in html


def test_all_windows_shortcuts_use_bundled_icon():
    desktop = (ROOT / "scripts" / "create_shortcut.ps1").read_text(encoding="utf-8")
    startup = (ROOT / "scripts" / "install_autostart.ps1").read_text(encoding="utf-8")
    build = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    assert "eirven.ico" in desktop
    assert "eirven.ico" in startup
    assert "eirven.ico" in build
    assert "--version-file $VersionPath" in build
    assert "ExtractAssociatedIcon" in build


def test_telegram_ready_button_is_clicked_once_without_enter():
    rows = [
        {
            "name": "glyphs", "control_type": "Button", "enabled": True, "visible": True,
            "class_name": "btn-icon rp btn-circle btn-send animated-button-icon send",
            "rectangle": [2528, 1588, 2624, 1668],
        },
        {
            "name": "Отправить", "control_type": "Button", "enabled": True, "visible": True,
            "class_name": "send-button", "rectangle": [2918, 1621, 3078, 1684],
        },
    ]
    operator = object.__new__(DesktopOperator)
    operator.tools = _TelegramButtonTools(rows)
    operator.learning = SimpleNamespace(remember=lambda *args, **kwargs: None)
    result = operator.commit_composer({"ok": True, "title": "Telegram Web - Samsung Browser", "handle": 42})
    assert result == {"ok": True, "committed": True, "method": "send_button", "error": ""}
    clicks = [args for name, args in operator.tools.calls if name == "click"]
    assert clicks == [{"x": 2576, "y": 1628}]
    assert not any(name == "press_key" for name, _ in operator.tools.calls)


def test_telegram_record_state_is_not_treated_as_send():
    rows = [{
        "name": "glyphs", "control_type": "Button", "enabled": True, "visible": True,
        "class_name": "btn-icon rp btn-circle btn-send animated-button-icon record",
        "rectangle": [2528, 1588, 2624, 1668],
    }]
    assert DesktopOperator._telegram_send_button(rows, ready_only=True) is None
    assert DesktopOperator._telegram_send_button(rows, ready_only=False) is rows[0]


def test_telegram_send_state_transition_verifies_hidden_contenteditable(monkeypatch):
    record = [{
        "name": "glyphs", "control_type": "Button", "enabled": True, "visible": True,
        "class_name": "btn-icon rp btn-circle btn-send animated-button-icon record",
        "rectangle": [2528, 1588, 2624, 1668],
    }]
    ready = [{
        "name": "glyphs", "control_type": "Button", "enabled": True, "visible": True,
        "class_name": "btn-icon rp btn-circle btn-send animated-button-icon send",
        "rectangle": [2528, 1588, 2624, 1668],
    }]
    clipboard = {"value": ""}
    monkeypatch.setitem(sys.modules, "pyperclip", SimpleNamespace(
        copy=lambda value: clipboard.__setitem__("value", value),
        paste=lambda: clipboard["value"],
    ))
    operator = object.__new__(DesktopOperator)
    operator.tools = _ClickTools()
    operator._click_input_rect = lambda _field: True
    operator._elements = lambda _title, limit=360, handle=None: ready
    operator._trace = lambda *args, **kwargs: None
    result = operator.type_verified({
        "ok": True, "title": "Telegram Web - Samsung Browser", "handle": 42,
        "field": {"control_type": "Group", "class_name": "input-message-input", "rectangle": [1336, 1591, 2432, 1665]},
        "rows": record, "purpose": "composer",
    }, "привет", submit=False, require_verified=True)
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["evidence"] == "telegram_send_ready"


def test_favicon_is_a_separate_transparent_logo_asset():
    html = (ROOT / "src" / "eirven_ai" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'href="/ui/favicon.png"' in html
    assert (ROOT / "src" / "eirven_ai" / "web" / "favicon.png").exists()
