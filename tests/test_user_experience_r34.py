from __future__ import annotations

import json
import base64
import hashlib
import socket
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import eirven_ai.api as api_module
import launcher


def test_qr_prefers_real_wifi_over_default_route_vpn_and_virtual_adapters(
    monkeypatch,
) -> None:
    addresses = {
        "Wi-Fi": [SimpleNamespace(family=socket.AF_INET, address="192.168.1.34")],
        "vEthernet (WSL)": [SimpleNamespace(family=socket.AF_INET, address="172.28.48.1")],
        "My VPN": [SimpleNamespace(family=socket.AF_INET, address="10.8.0.2")],
    }
    fake_psutil = SimpleNamespace(
        net_if_addrs=lambda: addresses,
        net_if_stats=lambda: {
            name: SimpleNamespace(isup=True) for name in addresses
        },
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    monkeypatch.setattr(api_module, "_default_route_ipv4", lambda: "10.8.0.2")
    monkeypatch.setattr(api_module.socket, "getaddrinfo", lambda *_args, **_kwargs: [])

    candidates = api_module._lan_candidates(7860)

    assert candidates[0]["url"] == "http://192.168.1.34:7860"
    assert candidates[0]["kind"] == "wifi"
    assert candidates[0]["recommended"] is True
    assert [item["kind"] for item in candidates[1:]] == ["virtual", "virtual"]
    assert all(item["warning"] for item in candidates[1:])


def test_inactive_wifi_is_ignored_and_active_ethernet_is_recommended(monkeypatch) -> None:
    addresses = {
        "Wi-Fi": [SimpleNamespace(family=socket.AF_INET, address="192.168.0.21")],
        "Ethernet": [SimpleNamespace(family=socket.AF_INET, address="192.168.0.22")],
    }
    fake_psutil = SimpleNamespace(
        net_if_addrs=lambda: addresses,
        net_if_stats=lambda: {
            "Wi-Fi": SimpleNamespace(isup=False),
            "Ethernet": SimpleNamespace(isup=True),
        },
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    monkeypatch.setattr(api_module, "_default_route_ipv4", lambda: "192.168.0.22")
    monkeypatch.setattr(api_module.socket, "getaddrinfo", lambda *_args, **_kwargs: [])

    candidates = api_module._lan_candidates(7862)

    assert [item["ip"] for item in candidates] == ["192.168.0.22"]
    assert candidates[0]["kind"] == "ethernet"


def test_launcher_persists_firewall_result_for_phone_panel(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(launcher, "APP_ROOT", tmp_path)

    assert launcher._ensure_mobile_firewall(Path(sys.executable), 7860) is True
    value = json.loads((tmp_path / "logs" / "mobile_network.json").read_text("utf-8"))

    assert value["port"] == 7860
    assert value["firewall_ready"] is True
    assert value["detail"]


def test_windows_firewall_rule_is_exact_port_and_local_subnet() -> None:
    source = Path(launcher.__file__).read_text(encoding="utf-8")

    for contract in (
        "-Profile Any",
        "-Protocol TCP",
        "-LocalPort $port",
        "-RemoteAddress LocalSubnet",
    ):
        assert contract in source
    assert "-Program $program" not in source


def test_phone_panels_explain_wrong_adapter_and_firewall_recovery() -> None:
    root = Path(__file__).resolve().parents[1]
    desktop_html = (root / "src/eirven_ai/web/index.html").read_text("utf-8")
    desktop_js = (root / "src/eirven_ai/web/app.js").read_text("utf-8")
    mobile_js = (root / "mobile_client/assets/mobile.js").read_text("utf-8")

    assert 'id="mobile-network-detail"' in desktop_html
    assert 'id="mobile-address-options"' in desktop_html
    assert 'id="refresh-mobile-network"' in desktop_html
    assert "гостевой Wi‑Fi" in desktop_html
    assert "VPN/виртуальная" in desktop_js
    assert "Windows Firewall" in desktop_js
    assert "статус сети должен быть зелёным" in mobile_js


def test_r37_metadata_is_consistent() -> None:
    root = Path(__file__).resolve().parents[1]
    assert api_module.APP_BUILD == "r37-mobile-clean"
    assert launcher.CURRENT_BUILD == api_module.APP_BUILD
    assert "r37-mobile-clean" in (root / "src/eirven_ai/web/index.html").read_text("utf-8")
    assert "verified install r37" in (root / "mobile_client/assets/index.html").read_text("utf-8")


def test_bundled_mobile_r37_has_valid_manifest_v1_contract_and_v2_block() -> None:
    root = Path(__file__).resolve().parents[1]
    apk = root / "mobile_client/EIRVEN-Mobile.apk"
    with zipfile.ZipFile(apk) as archive:
        manifest = archive.read("AndroidManifest.xml")
        mobile_js = archive.read("assets/mobile.js").decode("utf-8")
        index = archive.read("assets/index.html").decode("utf-8")
        names = set(archive.namelist())
        jar_manifest = archive.read("META-INF/MANIFEST.MF").decode("ascii")
        signature_file = archive.read("META-INF/EIRVEN37.SF").decode("ascii")

    assert "1.9.6".encode("utf-16le") in manifest
    assert (10906).to_bytes(4, "little") in manifest
    assert "ai.eirven.client".encode("utf-16le") in manifest
    assert "статус сети должен быть зелёным" in mobile_js
    assert "verified install r37" in index
    assert any(name.startswith("META-INF/") and name.endswith(".RSA") for name in names)
    assert "X-Android-APK-Signed: 2" in signature_file
    expected = base64.b64encode(hashlib.sha256(manifest).digest()).decode("ascii")
    assert f"Name: AndroidManifest.xml\r\nSHA-256-Digest: {expected}" in jar_manifest
    assert b"APK Sig Block 42" in apk.read_bytes()


def test_desktop_shortcut_is_required_and_verified() -> None:
    root = Path(__file__).resolve().parents[1]
    shortcut = (root / "scripts/create_shortcut.ps1").read_text("utf-8")
    shortcut_bytes = (root / "scripts/create_shortcut.ps1").read_bytes()
    installer = (root / "scripts/bootstrap_r27.py").read_text("utf-8")
    assert 'SpecialFolders.Item("Desktop")' in shortcut
    assert "Desktop shortcut created and verified" in shortcut
    assert all(byte < 128 for byte in shortcut_bytes)
    assert "Создание и проверка ярлыка" in installer
    assert "Ярлык не создан автоматически" not in installer


def test_windows_build_synchronizes_legacy_and_versioned_launchers() -> None:
    root = Path(__file__).resolve().parents[1]
    build = (root / "scripts/build_windows.ps1").read_text("utf-8")
    start = (root / "scripts/start_windows.bat").read_text("utf-8")
    assert '$LegacyTarget = Join-Path $Root "EIRVEN-AI.exe"' in build
    assert '$VersionedTarget = Join-Path $Root "EIRVEN-AI-r37.exe"' in build
    assert "replace_launcher_when_free.ps1" in build
    assert 'EIRVEN-AI-r37.exe' in start


def test_photo_studio_ui_is_removed_from_settings() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/eirven_ai/web/index.html").read_text("utf-8")
    assert "Фото 18+" not in html


def test_photo_workflow_upscales_generated_pixels_to_4k() -> None:
    from eirven_ai.creative import CreativeService

    workflow = CreativeService._workflow(
        "studio portrait", "bad hands", "model.safetensors", 832, 1216, 32, 7,
        output_width=2160, output_height=3840,
    )
    assert workflow["8"]["class_type"] == "ImageScale"
    assert workflow["8"]["inputs"]["width"] == 2160
    assert workflow["8"]["inputs"]["height"] == 3840
    assert workflow["7"]["inputs"]["images"] == ["8", 0]


def test_capability_question_does_not_wait_for_local_model() -> None:
    from eirven_ai.chat import ChatService

    chat = ChatService.__new__(ChatService)
    chat.identity = SimpleNamespace(get=lambda: SimpleNamespace(
        assistant_name="Эрви", user_address="Даниил", gender="female", action_commentary="adaptive",
    ))
    chat._agent_mood = lambda _identity: "natural"
    answer = chat._instant_reply("Эрви, ты умеешь гуглить?")
    assert answer and "искать в интернете" in answer
    assert "локальн" not in answer.casefold()



def test_r37_photo_result_is_decoded_validated_and_cpu_upscaled(tmp_path: Path) -> None:
    from io import BytesIO

    from PIL import Image

    from eirven_ai.creative import CreativeService

    image = Image.new("RGB", (320, 448))
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            pixels[x, y] = ((x * 3) % 256, (y * 5) % 256, (x + y) % 256)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    target = tmp_path / "generated-test.png"

    info = CreativeService._finalize_image(
        buffer.getvalue(), target, output_width=640, output_height=896
    )

    assert info["validated"] is True
    assert info["upscale"] == "pillow-lanczos"
    assert (info["output_width"], info["output_height"]) == (640, 896)
    with Image.open(target) as result:
        assert result.size == (640, 896)


def test_r37_photo_result_rejects_flat_or_corrupt_files(tmp_path: Path) -> None:
    from io import BytesIO

    import pytest
    from PIL import Image

    from eirven_ai.creative import CreativeError, CreativeService

    flat = Image.new("RGB", (320, 320), "white")
    buffer = BytesIO()
    flat.save(buffer, format="PNG")
    with pytest.raises(CreativeError, match="однотонным"):
        CreativeService._finalize_image(buffer.getvalue(), tmp_path / "flat.png")
    with pytest.raises(CreativeError, match="повреждённый"):
        CreativeService._finalize_image(b"not-an-image", tmp_path / "broken.png")




def test_r37_windows_version_resource_matches_release_identity() -> None:
    root = Path(__file__).resolve().parents[1]
    build = json.loads((root / "BUILD_INFO.json").read_text("utf-8"))
    version_resource = (root / "assets/eirven-version.txt").read_text("utf-8")

    assert build["build"] == "r37-mobile-clean"
    assert build["release_date"] == "2026-08-12"
    assert "filevers=(1, 7, 3, 37)" in version_resource
    assert "1.7.3.37" in version_resource
    assert "EIRVEN-AI.exe" in version_resource
