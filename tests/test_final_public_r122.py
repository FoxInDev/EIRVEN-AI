from pathlib import Path
from types import SimpleNamespace

from eirven_ai.database import Database
from eirven_ai.identity import IdentityService
from eirven_ai.voice_daemon import NativeVoiceDaemon

ROOT = Path(__file__).resolve().parents[1]


def test_duplicate_wake_word_is_stripped(tmp_path: Path):
    db = Database(tmp_path / "eirven.db")
    identity = IdentityService(db)
    identity.update({"assistant_name": "Эрви"})
    daemon = NativeVoiceDaemon(SimpleNamespace(db=db, identity=identity))
    route = daemon._activation_route(
        "Эрви, Эрви, напиши Тиме в Telegram что я скоро приеду",
        speech_started_at=10.0,
        now=10.1,
    )
    assert route["action"] == "accept"
    assert route["has_wake"] is True
    assert route["command"] == "напиши тиме в telegram что я скоро приеду"


def test_desktop_eyes_are_line_only_and_soft():
    source = (ROOT / "src" / "eirven_ai" / "companion.py").read_text(encoding="utf-8")
    section = source[source.index("def _eye_curve"):source.index("def animate()")]
    assert "canvas.create_line" in section
    assert "canvas.create_oval" not in section
    assert "canvas.create_polygon" not in section


def test_shutdown_is_observed_by_supervisor_and_has_direct_fallback():
    supervisor = (ROOT / "src" / "eirven_ai" / "supervisor.py").read_text(encoding="utf-8")
    api = (ROOT / "src" / "eirven_ai" / "api.py").read_text(encoding="utf-8")
    assert "while child.poll() is None" in supervisor
    assert "stop_file.exists()" in supervisor
    assert "child.terminate()" in supervisor
    assert "os._exit(0)" in api


def test_simple_live_regressions_have_deterministic_ui_fixes():
    operator = (ROOT / "src" / "eirven_ai" / "desktop_operator.py").read_text(encoding="utf-8")
    assert "def exact_play_button" in operator
    assert 'name in {"воспроизведение","воспроизвести","play","playback"}' in operator
    assert "type_verified(search,recipient,submit=False,require_verified=False)" in operator
