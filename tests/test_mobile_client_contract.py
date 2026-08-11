from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_mobile_hidden_attribute_cannot_be_overridden_by_layout_css() -> None:
    css = (ROOT / "mobile_client" / "assets" / "styles.css").read_text("utf-8")
    assert "[hidden]{display:none!important}" in css.replace(" ", "")


def test_successful_pairing_is_not_rolled_back_by_chat_bootstrap_failure() -> None:
    js = (ROOT / "mobile_client" / "assets" / "mobile.js").read_text("utf-8")
    assert "let paired = false" in js
    assert "paired = true" in js
    assert "if (!paired)" in js
    assert '$("#pairing-screen").hidden = true' in js
    assert '$("#app-shell").hidden = false' in js
    assert 'showScreen("chat")' in js


def test_mobile_error_response_body_is_read_once() -> None:
    js = (ROOT / "mobile_client" / "assets" / "mobile.js").read_text("utf-8")
    error_block = js[js.index("if (!response.ok) {"):js.index("setConnectionOnline(true);")]
    assert error_block.count("response.text()") == 1
    assert "response.json()" not in error_block


def test_mobile_navigation_contains_all_user_pages() -> None:
    html = (ROOT / "mobile_client" / "assets" / "index.html").read_text("utf-8")
    for screen in ("chat", "files", "tasks", "settings"):
        assert f'data-screen="{screen}"' in html
        assert f'data-nav="{screen}"' in html


def test_mobile_api_allowlist_covers_every_client_operation() -> None:
    api = (ROOT / "src" / "eirven_ai" / "api.py").read_text("utf-8")
    required = (
        '("/api/mobile/status", "GET")',
        '("/api/mobile/video", "POST")',
        '("/api/conversations", "POST")',
        '("/api/chat/stream", "POST")',
        '("/api/voice/speak", "POST")',
        '("/api/voice/transcribe", "POST")',
        '("/api/uploads", "POST")',
        '("/api/tasks", "GET")',
        '(r"/api/conversations/[A-Za-z0-9_-]+", "GET")',
        '(r"/api/chat/[A-Za-z0-9_-]+/stop", "POST")',
        '(r"/api/tasks/[A-Za-z0-9_-]+/cancel", "POST")',
    )
    for item in required:
        assert item in api
