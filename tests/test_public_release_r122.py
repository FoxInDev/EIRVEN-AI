from pathlib import Path
from types import SimpleNamespace

from eirven_ai.companion import DesktopCompanion
from eirven_ai.database import Database
from eirven_ai.identity import IdentityService, VOICE_CATALOG

ROOT = Path(__file__).resolve().parents[1]


def test_public_identity_is_baya_only(tmp_path: Path):
    service = IdentityService(Database(tmp_path / "identity.db"))
    current = service.update({"gender": "male", "voice_key": "denis"})
    assert current.gender == "female"
    assert current.voice_key == "irina_soft"
    assert list(VOICE_CATALOG) == ["irina_soft"]


def test_official_supplied_logo_is_used_across_product():
    html = (ROOT / "src/eirven_ai/web/index.html").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts/bootstrap.py").read_text(encoding="utf-8")
    companion = (ROOT / "src/eirven_ai/companion.py").read_text(encoding="utf-8")
    assert (ROOT / "assets/eirven.png").stat().st_size > 100_000
    assert (ROOT / "assets/eirven.ico").stat().st_size > 20_000
    assert '/ui/eirven-orb.png' in html
    assert 'assets" / "eirven.png' in bootstrap
    assert 'web" / "eirven-orb.png' in companion
    assert "Мужской" not in html


def test_installer_is_self_recovering_and_living():
    bootstrap = (ROOT / "scripts/bootstrap.py").read_text(encoding="utf-8")
    ensure = (ROOT / "scripts/ensure_runtime.ps1").read_text(encoding="utf-8")
    assert "for attempt in range(1, 4)" in bootstrap
    assert 'self.gui.post("retry"' in bootstrap
    assert "installer-recovery.json" in bootstrap
    assert "def _run_once" in bootstrap and "attempts: int = 3" in bootstrap
    assert "Invoke-EirvenRetry" in ensure
    assert "self._orb_texture" in bootstrap
    assert "Image.LANCZOS" in bootstrap


def test_desktop_companion_has_eyes_and_polished_comments(tmp_path: Path):
    db = Database(tmp_path / "c.db")
    service = IdentityService(db)
    companion = DesktopCompanion(db, service, "http://127.0.0.1:7860/ui/")
    assert companion._eye_mode({"state": "hearing"}) == "listening"
    assert companion._eye_mode({"state": "thinking"}) == "thinking"
    assert companion._human_comment({"state": "hearing"}) == "Слушаю тебя. Не торопись."
    result = companion._human_comment({"runtime": {"cancellable": True, "step": "search product", "goal": "найти товар"}})
    assert "ищу" in result.casefold()
    source = (ROOT / "src/eirven_ai/companion.py").read_text(encoding="utf-8")
    assert "desktop_eyes_enabled" in source
    assert "Liquid-glass style card" in source


def test_settings_expose_eyes_and_safe_shutdown():
    html = (ROOT / "src/eirven_ai/web/index.html").read_text(encoding="utf-8")
    js = (ROOT / "src/eirven_ai/web/app.js").read_text(encoding="utf-8")
    api = (ROOT / "src/eirven_ai/api.py").read_text(encoding="utf-8")
    assert 'id="desktop-eyes-enabled"' in html
    assert 'id="shutdown-eirven"' in html
    assert "/api/system/shutdown" in api and "stop.request" in api
    assert "desktop_eyes_enabled" in api
    assert "/api/system/shutdown" in js


def test_version_and_github_release_pipeline_are_122():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    package = (ROOT / "scripts/package_release.py").read_text(encoding="utf-8")
    assert 'version = "1.2.2"' in pyproject
    assert '"v1.2.2"' in workflow
    assert 'default="v1.2.2"' in package
    assert 'authors = [{name = "Даниил"}]' in pyproject
