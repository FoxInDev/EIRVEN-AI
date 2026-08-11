from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "data" / "photo_engine"
SOURCE_DIR = ENGINE_ROOT / "ComfyUI"
VENV_DIR = ENGINE_ROOT / ".venv"
STATUS = ENGINE_ROOT / "install_status.json"
LOCK = ENGINE_ROOT / ".install.lock"
COMFY_VERSION = "v0.29.0"
COMFY_ZIP = f"https://github.com/Comfy-Org/ComfyUI/archive/refs/tags/{COMFY_VERSION}.zip"
TORCH_VERSION = "2.8.0"
TORCHVISION_VERSION = "0.23.0"
TORCHAUDIO_VERSION = "2.8.0"
MODELS = (
    (
        "sd_xl_base_1.0.safetensors",
        "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/f298da3c058bd8f1f1c62f3ecfa775244a243897/sd_xl_base_1.0.safetensors?download=true",
        6_938_078_334,
        "31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b",
        "CreativeML Open RAIL++-M",
    ),
    (
        "animagine-xl-4.0-opt.safetensors",
        "https://huggingface.co/cagliostrolab/animagine-xl-4.0/resolve/4688bebb86806957f6f83c39f2573161630e22db/animagine-xl-4.0-opt.safetensors?download=true",
        6_938_350_040,
        "6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac",
        "OpenRAIL++",
    ),
)


def update(phase: str, detail: str, progress: float, *, done: bool = False, error: str = "") -> None:
    ENGINE_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        "running": not done and not error,
        "done": done,
        "phase": phase,
        "detail": detail,
        "progress": max(0.0, min(float(progress), 1.0)),
        "error": error,
        "updated_at": int(time.time()),
        "pid": os.getpid(),
        "engine_version": COMFY_VERSION,
    }
    pending = STATUS.with_suffix(".tmp")
    pending.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    pending.replace(STATUS)


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _pid_alive(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return pid == os.getpid()
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def acquire_lock() -> None:
    ENGINE_ROOT.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            handle = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing_pid = int(LOCK.read_text("ascii").strip() or "0")
            except Exception:
                existing_pid = 0
            if _pid_alive(existing_pid):
                raise RuntimeError("Установка фото-движка уже выполняется.")
            LOCK.unlink(missing_ok=True)
            continue
        with os.fdopen(handle, "w", encoding="ascii") as output:
            output.write(str(os.getpid()))
        return
    raise RuntimeError("Не удалось получить блокировку установки фото-движка.")


def release_lock() -> None:
    try:
        if LOCK.is_file() and LOCK.read_text("ascii").strip() == str(os.getpid()):
            LOCK.unlink(missing_ok=True)
    except Exception:
        pass


def _valid_existing(target: Path, expected_size: int | None, expected_sha256: str) -> bool:
    if not target.is_file():
        return False
    if expected_size and target.stat().st_size != expected_size:
        return False
    if expected_sha256 and sha256(target).casefold() != expected_sha256.casefold():
        return False
    return True


def _content_range_start(value: str) -> int | None:
    match = re.match(r"bytes\s+(\d+)-\d+/(?:\d+|\*)", str(value or ""), re.I)
    return int(match.group(1)) if match else None


def download(
    url: str,
    target: Path,
    *,
    phase: str,
    start: float,
    span: float,
    expected_size: int | None = None,
    expected_sha256: str = "",
    retries: int = 4,
) -> None:
    """Resume a download safely and never promote a truncated .part file to final."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")

    if _valid_existing(target, expected_size, expected_sha256):
        return
    if target.exists():
        target.unlink(missing_ok=True)
    if expected_size and partial.is_file() and partial.stat().st_size > expected_size:
        partial.unlink(missing_ok=True)

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        received = partial.stat().st_size if partial.is_file() else 0
        request = urllib.request.Request(url, headers={"User-Agent": "EIRVEN-r37"})
        if received:
            request.add_header("Range", f"bytes={received}-")
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                status = int(getattr(response, "status", 200) or 200)
                if received and status == 206:
                    range_start = _content_range_start(response.headers.get("Content-Range", ""))
                    if range_start != received:
                        partial.unlink(missing_ok=True)
                        raise RuntimeError("Сервер вернул неверный диапазон; загрузка будет начата заново.")
                elif received and status != 206:
                    # Server ignored Range. Reuse this full 200 response from byte zero.
                    partial.unlink(missing_ok=True)
                    received = 0

                content_length = int(response.headers.get("Content-Length") or 0)
                total = received + content_length if content_length else (expected_size or 0)
                if expected_size and total and total != expected_size:
                    raise RuntimeError(
                        f"Сервер сообщил неожиданный размер {total} байт вместо {expected_size}."
                    )
                mode = "ab" if received else "wb"
                last_update = 0.0
                with partial.open(mode) as output:
                    while True:
                        chunk = response.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        received += len(chunk)
                        now = time.monotonic()
                        if now - last_update >= 0.4:
                            ratio = (received / (expected_size or total)) if (expected_size or total) else 0.0
                            total_gib = (expected_size or total) / 1024**3 if (expected_size or total) else 0.0
                            update(
                                phase,
                                f"{received / 1024**3:.2f} из {total_gib:.2f} ГБ",
                                start + span * max(0.0, min(ratio, 1.0)),
                            )
                            last_update = now

            if expected_size and received != expected_size:
                raise RuntimeError(
                    f"Загрузка оборвалась: получено {received} из {expected_size} байт."
                )
            partial.replace(target)
            if expected_sha256 and sha256(target).casefold() != expected_sha256.casefold():
                target.unlink(missing_ok=True)
                raise RuntimeError("Контрольная сумма не совпала; файл будет скачан заново.")
            return
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 416 and partial.exists():
                partial.unlink(missing_ok=True)
            if attempt >= retries:
                break
        except Exception as exc:
            last_error = exc
            # Keep a sane partial file so the next attempt/restart resumes it. A hash mismatch
            # deliberately deletes the promoted target and starts again from zero.
            if attempt >= retries:
                break
        time.sleep(min(2**(attempt - 1), 8))

    raise RuntimeError(f"Не удалось скачать {target.name}: {last_error}") from last_error


def run(command: list[str], phase: str, progress: float, *, timeout: int = 7200) -> None:
    update(phase, "Подготавливаю локальные компоненты…", progress)
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{phase}: операция превысила лимит времени") from exc
    if completed.returncode:
        tail = (completed.stderr or completed.stdout or "unknown error").strip()[-1600:]
        raise RuntimeError(f"{phase}: {tail}")


def extract_source() -> None:
    version_file = SOURCE_DIR / ".eirven-comfy-version"
    if (SOURCE_DIR / "main.py").is_file() and version_file.is_file():
        if version_file.read_text("utf-8").strip() == COMFY_VERSION:
            return

    archive = ENGINE_ROOT / f"ComfyUI-{COMFY_VERSION}.zip"
    for attempt in range(2):
        download(COMFY_ZIP, archive, phase="Скачиваю ComfyUI", start=0.02, span=0.04)
        if zipfile.is_zipfile(archive):
            break
        archive.unlink(missing_ok=True)
        if attempt:
            raise RuntimeError("Архив ComfyUI повреждён после повторной загрузки.")
    unpack = ENGINE_ROOT / "source-unpack"
    if unpack.exists():
        shutil.rmtree(unpack)
    unpack.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        root = unpack.resolve()
        for item in bundle.infolist():
            target = (unpack / item.filename).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError("Небезопасный путь в архиве ComfyUI")
        bundle.extractall(unpack)
    candidate = next((path for path in unpack.iterdir() if (path / "main.py").is_file()), None)
    if candidate is None:
        raise RuntimeError("В архиве ComfyUI не найден main.py")
    SOURCE_DIR.parent.mkdir(parents=True, exist_ok=True)
    if SOURCE_DIR.exists():
        shutil.rmtree(SOURCE_DIR)
    shutil.move(str(candidate), str(SOURCE_DIR))
    version_file.write_text(COMFY_VERSION + "\n", encoding="utf-8")
    shutil.rmtree(unpack)


def _has_nvidia() -> bool:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return False
    try:
        return subprocess.run(
            [executable, "-L"], capture_output=True, text=True, timeout=10
        ).returncode == 0
    except Exception:
        return False


def write_license_notice() -> None:
    lines = [
        "EIRVEN local photo engine - third-party model notice",
        "",
        f"ComfyUI source: {COMFY_VERSION} (see the LICENSE included in the ComfyUI folder).",
        "Downloaded checkpoints are not bundled with EIRVEN and remain subject to their model licenses:",
        "",
    ]
    for filename, url, size, digest, license_name in MODELS:
        lines.extend(
            [
                f"- {filename}",
                f"  License: {license_name}",
                f"  Expected size: {size} bytes",
                f"  SHA-256: {digest}",
                f"  Source: {url}",
                "",
            ]
        )
    (ENGINE_ROOT / "MODEL_LICENSES.txt").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    acquired = False
    try:
        acquire_lock()
        acquired = True
        update("Подготовка", "Проверяю место и компоненты…", 0.01)
        usage = shutil.disk_usage(ROOT)
        if usage.free < 22 * 1024**3:
            raise RuntimeError("Для двух качественных моделей нужно минимум 22 ГБ свободного места.")
        extract_source()
        venv_python = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not venv_python.is_file():
            run([sys.executable, "-m", "venv", str(VENV_DIR)], "Создаю окружение генератора", 0.07)
        run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "wheel"], "Обновляю установщик", 0.09)

        index = "https://download.pytorch.org/whl/cu128" if _has_nvidia() else "https://download.pytorch.org/whl/cpu"
        engine_label = "NVIDIA" if "cu128" in index else "CPU"
        run(
            [
                str(venv_python), "-m", "pip", "install",
                f"torch=={TORCH_VERSION}",
                f"torchvision=={TORCHVISION_VERSION}",
                f"torchaudio=={TORCHAUDIO_VERSION}",
                "--index-url", index,
            ],
            f"Устанавливаю {engine_label}-движок",
            0.11,
        )
        run(
            [str(venv_python), "-m", "pip", "install", "-r", str(SOURCE_DIR / "requirements.txt")],
            "Устанавливаю ComfyUI",
            0.15,
        )
        run(
            [str(venv_python), "-c", "import torch; print(torch.__version__); print(torch.cuda.is_available())"],
            "Проверяю PyTorch",
            0.18,
            timeout=120,
        )

        checkpoints = SOURCE_DIR / "models" / "checkpoints"
        checkpoints.mkdir(parents=True, exist_ok=True)
        positions = ((0.20, 0.36), (0.58, 0.36))
        for (filename, url, expected_size, expected, _license), (start, span) in zip(MODELS, positions, strict=True):
            target = checkpoints / filename
            download(
                url,
                target,
                phase=f"Скачиваю {filename}",
                start=start,
                span=span,
                expected_size=expected_size,
                expected_sha256=expected,
            )
            if not _valid_existing(target, expected_size, expected):
                target.unlink(missing_ok=True)
                raise RuntimeError(f"Проверка модели {filename} не пройдена; повтори установку")

        write_license_notice()
        detail = (
            "Realistic и Anime модели установлены. Локальный движок готов к запуску."
            if _has_nvidia()
            else "Realistic и Anime модели установлены. NVIDIA не обнаружена: будет использован медленный CPU-режим."
        )
        update("Готово", detail, 1.0, done=True)
        return 0
    except Exception as exc:
        update("Ошибка установки", str(exc), 0.0, error=str(exc))
        return 1
    finally:
        if acquired:
            release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
