from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi.staticfiles import StaticFiles

from .api import build_api
from .action_model import action_num_gpu
from .services import build_services
from .system_browser import open_url as open_system_url
from .trace import log_event

services = build_services()


@asynccontextmanager
async def lifespan(app):
    pid_file = services.settings.root_dir / "logs" / "server.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    services.tasks.start()
    services.chat_jobs.start()
    if services.settings.companion_enabled:
        try:
            if bool(services.identity.get().desktop_avatar_enabled):
                services.companion.start()
        except Exception:
            pass
    if services.ambient is not None:
        try:
            services.ambient.start()
        except Exception:
            pass
    if services.voice_daemon is not None:
        try:
            services.voice_daemon.start()
        except Exception:
            pass

        voice_watchdog_stop = threading.Event()
        app.state.voice_watchdog_stop = voice_watchdog_stop

        def voice_watchdog() -> None:
            # Voice is a service, not a browser feature. If PortAudio/device startup
            # hiccups after login, recover it without waiting for the UI to be opened.
            while not voice_watchdog_stop.wait(3.0):
                try:
                    status = services.voice_daemon.status()
                    if not status.get("running"):
                        try:
                            services.voice_daemon.stop()
                        except Exception:
                            pass
                        services.voice_daemon.start()
                except Exception:
                    pass
        threading.Thread(target=voice_watchdog, daemon=True, name="voice-watchdog").start()
    if services.proactive is not None:
        try:
            services.proactive.start()
        except Exception:
            pass
    # r21: no automatic self-test during normal product use. Diagnostics stay internal.\n
    # r22 cold-start governor: GigaAM loads first, then the selected local voice weights,
    # and only then the small action model.  Deterministic commands stay available while
    # Qwen warms, but first speech no longer competes with a simultaneous model cold load.
    def delayed_action_model_prewarm() -> None:
        started = time.monotonic()
        try:
            while time.monotonic() - started < 210.0:
                if bool(getattr(services.voice, "stt_ready", lambda: True)()):
                    break
                time.sleep(0.20)
            # Let the lightweight local voice load before Qwen.  This is bounded and does
            # not gate deterministic turns; if TTS preload fails the flag is still released.
            tts_wait = time.monotonic()
            while time.monotonic() - tts_wait < 25.0:
                if bool(getattr(services.voice, "tts_warm_ready", lambda: True)()):
                    break
                time.sleep(0.20)
            model = str(services.universal_workflow._planner_model())
            num_gpu = action_num_gpu(services.settings, model=model)
            log_event(services.settings.root_dir, "MODEL_PREWARM_BEGIN", model=model, num_gpu=num_gpu)
            t0 = time.monotonic()
            services.gateway.warm(model, keep_alive=services.settings.keep_alive, num_gpu=num_gpu)
            log_event(services.settings.root_dir, "MODEL_PREWARM_END", model=model, ms=round((time.monotonic()-t0)*1000), ok=True)
        except Exception as exc:
            try:
                log_event(services.settings.root_dir, "MODEL_PREWARM_ERROR", error=str(exc)[:500])
            except Exception:
                pass
    threading.Thread(target=delayed_action_model_prewarm, daemon=True, name="eirven-action-model-prewarm").start()
    try:
        log_event(services.settings.root_dir, "RESOURCE_POLICY", action_model_prewarm=True, tts_preload=True, after="stt_then_tts", keep_alive=services.settings.keep_alive)
    except Exception:
        pass

    if services.db.get_setting("telegram_autostart", False):
        def start_telegram() -> None:
            try:
                services.telegram.start()
            except Exception:
                pass
        threading.Thread(target=start_telegram, daemon=True, name="telegram-autostart").start()

    yield
    watchdog_stop = getattr(app.state, "voice_watchdog_stop", None)
    if watchdog_stop is not None:
        watchdog_stop.set()
    services.chat_jobs.stop()
    services.tasks.stop()
    services.companion.stop()
    if services.proactive is not None:
        try:
            services.proactive.stop()
        except Exception:
            pass
    if services.camera is not None:
        try:
            services.camera.stop()
        except Exception:
            pass
    if services.voice_daemon is not None:
        try:
            services.voice_daemon.stop()
        except Exception:
            pass
    if services.ambient is not None:
        try:
            services.ambient.stop()
        except Exception:
            pass
    try:
        services.voice.close()
    except Exception:
        pass
    services.telegram.stop(persist=False)
    services.telegram.close_auth()
    services.browser.close()
    pid_file.unlink(missing_ok=True)


app = build_api(services)
app.router.lifespan_context = lifespan
web_dir = Path(__file__).resolve().parent / "web"
app.mount("/ui", StaticFiles(directory=web_dir, html=True), name="ui")


def main() -> None:
    url = f"http://{services.settings.host}:{services.settings.port}/ui/"
    if os.getenv("EIRVEN_OPEN_BROWSER", "false").strip().lower() not in {"0", "false", "no"}:
        threading.Timer(1.2, lambda: open_system_url(url)).start()
    uvicorn.run(
        app,
        host=services.settings.host,
        port=services.settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
