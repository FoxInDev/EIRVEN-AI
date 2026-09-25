# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "EIRVEN.exe"
MODEL = "qwen3.5:9b"


class Handler(BaseHTTPRequestHandler):
    show_requested = threading.Event()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _reply(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path == "/api/ping":
            self._reply(200, {"ok": True, "service": "eirven"})
            return
        if self.path == "/api/preferences":
            self._reply(
                200,
                {
                    "build": "r67-universal-engine",
                    "model": MODEL,
                },
            )
            return
        self._reply(404, {"ok": False})

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path == "/api/companion/show":
            type(self).show_requested.set()
            self._reply(200, {"ok": True})
            return
        self._reply(404, {"ok": False})


def main() -> int:
    if not EXE.is_file():
        raise SystemExit(f"Missing executable: {EXE}")

    Handler.show_requested.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = int(server.server_address[1])
    if port != 7860:
        server.server_close()
        server = ThreadingHTTPServer(("127.0.0.1", 7860), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        with tempfile.TemporaryDirectory(prefix="eirven-exe-smoke-") as raw:
            target = Path(raw) / "EIRVEN.exe"
            shutil.copy2(EXE, target)
            started = time.monotonic()
            process = subprocess.Popen([str(target)], cwd=target.parent)
            try:
                code = process.wait(timeout=90)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                raise RuntimeError("Launcher did not exit after detecting the live service")
            elapsed = time.monotonic() - started
            if code != 0:
                raise RuntimeError(f"Launcher exited with code {code}")
            if not Handler.show_requested.wait(timeout=2):
                raise RuntimeError("Launcher did not request the companion UI")
            print(
                json.dumps(
                    {
                        "ok": True,
                        "exe": str(EXE),
                        "exit_code": code,
                        "companion_show_requested": True,
                        "elapsed_seconds": round(elapsed, 3),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
