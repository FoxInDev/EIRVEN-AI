from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from eirven_ai.database import Database
from eirven_ai.identity import IdentityService
from eirven_ai.voice import VoiceService
from eirven_ai.voice_daemon import NativeVoiceDaemon


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "eirven_ai" / "web"


def _daemon(tmp_path: Path) -> NativeVoiceDaemon:
    db = Database(tmp_path / "eirven.db")
    identity = IdentityService(db)
    services = SimpleNamespace(db=db, identity=identity)
    return NativeVoiceDaemon(services)


def test_identity_is_onboarding_first_and_automatic(tmp_path: Path):
    db = Database(tmp_path / "identity.db")
    ident = IdentityService(db).get()
    assert ident.assistant_name == "Эйрвен"
    assert ident.onboarding_completed is False
    assert ident.strict_wake_name is True
    assert ident.ambient_music_enabled is False
    assert ident.emotion_mode == "auto"
    assert ident.voice_mode == "natural"
    assert ident.action_commentary == "adaptive"
    assert ident.speech_speed == 1.0


def test_wake_is_fixed_one_shot_five_seconds(tmp_path: Path):
    daemon = _daemon(tmp_path)
    assert "эрви" in daemon._wake_variants()
    assert daemon._session_seconds() == 5.0

    ignored = daemon._activation_route("открой телеграм", speech_started_at=10.0, now=10.2)
    assert ignored["action"] == "ignore"

    armed = daemon._activation_route("эрви", speech_started_at=11.0, now=11.2)
    assert armed["action"] == "arm"

    direct = daemon._activation_route("эрви открой телеграм", speech_started_at=12.0, now=12.2)
    assert direct["action"] == "accept"
    assert direct["command"] == "открой телеграм"

    daemon._active_until = 20.0
    # Speech began before the 5-second window expired, but the user kept talking.
    long_thought = daemon._activation_route("найди товар и потом открой корзину", speech_started_at=19.8, now=27.0)
    assert long_thought["action"] == "accept"


def test_voice_tempo_is_automatic_not_slider_driven():
    natural = VoiceService._automatic_speech_speed("Хорошо, сейчас посмотрю.", "natural")
    calm = VoiceService._automatic_speech_speed("Не спеши. Я рядом…", "calm")
    energetic = VoiceService._automatic_speech_speed("Готово! Всё получилось!", "energetic")
    assert 0.88 <= calm < natural < energetic <= 1.08


def test_ui_is_living_glass_and_settings_are_reduced():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")

    for removed in (
        "Нейромузыка", "Self-test", "Остановить всё", "strict-wake-name",
        "developer-toggle", "run-diagnostics", "emotion-mode", "action-commentary",
        "speech-speed",
    ):
        assert removed not in html

    assert "living-orb" in html and "eirven-orb.png" in html
    assert "onboarding-step" in html
    assert "5 секунд" in html
    assert "скажи «Эрви»" in html
    assert "backdrop-filter" in css
    assert "orbWander" in css and "liquidLight" in css
    assert "onboarding_completed:true" in js
    assert "STYLE_PRESETS" in js


def test_installer_is_single_percent_without_eta_or_visible_log():
    source = (ROOT / "scripts" / "bootstrap.py").read_text(encoding="utf-8")
    gui = source[source.index("class InstallerGUI:"):]
    assert 'text="0%"' in gui
    assert "self.percent" in gui
    assert "self.log = tk.Text" not in gui
    assert "Осталось примерно" not in gui
    assert "Оценка времени" not in gui
    assert 'elif kind == "log":\n                    pass' in gui


def test_first_install_opens_onboarding_and_companion_has_human_comments():
    launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
    companion = (ROOT / "src" / "eirven_ai" / "companion.py").read_text(encoding="utf-8")
    assert "/ui/?welcome=1" in launcher
    assert "Давай сначала познакомимся." in companion
    assert "Я здесь. Говори." in companion
    assert "Уже ищу. Проверяю, чтобы выбрать верно." in companion
    assert "math.sin(phase * .16)" in companion


def test_camera_remains_removed():
    assert not (ROOT / "src" / "eirven_ai" / "camera.py").exists()
