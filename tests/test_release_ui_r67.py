# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eirven_ai.database import Database
from eirven_ai.version import APP_BUILD, APP_VERSION


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "eirven_ai" / "web"


def test_release_identity_and_web_cache_busting_are_r67() -> None:
    build = json.loads((ROOT / "BUILD_INFO.json").read_text(encoding="utf-8"))
    index = (WEB / "index.html").read_text(encoding="utf-8")
    app = (WEB / "app.js").read_text(encoding="utf-8")

    assert (APP_VERSION, APP_BUILD) == ("2.0.0", "r67-universal-engine")
    assert (build["version"], build["build"]) == (APP_VERSION, APP_BUILD)
    assert index.count("?v=r67") == 6
    assert "EIRVEN 2.0.0 · r67-universal-engine" in index
    assert "p.version||'2.0.0'" in app
    assert "p.build||'r67-universal-engine'" in app


def test_fresh_onboarding_is_resumable_and_committed_once() -> None:
    app = (WEB / "app.js").read_text(encoding="utf-8")
    api = (ROOT / "src" / "eirven_ai" / "api.py").read_text(encoding="utf-8")

    assert "eirven_onboarding_draft_r67" in app
    assert "persistOnboardingDraft()" in app
    assert "if(!state.identity?.onboarding_completed)openOnboarding()" in app
    assert "new URLSearchParams(location.search).get('welcome')==='1'" not in app
    assert "'/api/onboarding/complete'" in app
    assert "await saveIdentity({user_address:onboardingDraft" not in app
    assert "$('#app-shell').inert=true" in app
    assert 'role="dialog" aria-modal="true"' in (WEB / "index.html").read_text(encoding="utf-8")
    assert '@app.post("/api/onboarding/complete")' in api
    assert "services.db.set_settings({" in api


def test_setting_group_is_all_or_nothing_before_sql(tmp_path: Path) -> None:
    db = Database(tmp_path / "eirven.db")
    db.set_settings({"identity_v1": {"user_address": "Дима"}, "style_dna": {"directness": 4}})
    assert db.get_setting("identity_v1")["user_address"] == "Дима"
    assert db.get_setting("style_dna")["directness"] == 4

    with pytest.raises(TypeError):
        db.set_settings({"identity_v1": {"user_address": "Другое"}, "bad": object()})
    assert db.get_setting("identity_v1")["user_address"] == "Дима"
    assert db.get_setting("bad") is None


def test_liquid_glass_has_motion_and_accessibility_fallbacks() -> None:
    index = (WEB / "index.html").read_text(encoding="utf-8")
    css = (WEB / "eirven-ui.css").read_text(encoding="utf-8")
    assert "eirven-ui.css?v=r67&rev=aurora-redesign-1" in index
    assert "styles.css" not in index
    assert "canonical-ervi.css" not in index
    assert "--canvas:#03050d" in css
    assert ".living-orb" in css
    assert "backdrop-filter:blur(30px) saturate(165%)" in css
    assert "@media(prefers-reduced-motion:reduce)" in css
    assert "@media(forced-colors:active)" in css
    assert "@supports not ((backdrop-filter:blur(1px))" in css
    assert "button:focus-visible" in css


def test_native_installer_launcher_and_companion_share_glass_language() -> None:
    bootstrap = (ROOT / "scripts" / "bootstrap.py").read_text(encoding="utf-8")
    launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
    companion = (ROOT / "src" / "eirven_ai" / "companion.py").read_text(encoding="utf-8")

    assert "def _enable_windows_glass" in bootstrap
    assert "def _enable_windows_glass" in launcher
    assert "Layered glass: soft depth" in companion


def test_native_install_and_launch_windows_remain_opaque_and_actionable() -> None:
    bootstrap = (ROOT / "scripts" / "bootstrap.py").read_text(encoding="utf-8")
    launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")

    assert 'attributes("-alpha", 1.0)' in bootstrap
    assert 'attributes("-alpha", 1.0)' in launcher
    assert "SetWindowCompositionAttribute(hwnd" not in bootstrap
    assert "SetWindowCompositionAttribute(hwnd" not in launcher
    assert "DwmSetWindowAttribute" in bootstrap
    assert "DwmSetWindowAttribute" in launcher
    assert 'preview="--ui-preview" in sys.argv[1:]' in bootstrap
    assert 'preview = "--ui-preview" in sys.argv[1:]' in launcher
    assert 'text="Открыть журнал"' in launcher
    assert 'text="Повторить"' in launcher
