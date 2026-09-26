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
from .system_browser import open_url as open_system_url, open_app_window
from .trace import log_event
from .training_guard import owner_training_active, write_runtime_marker

services = build_services()


@asynccontextmanager
async def lifespan(app):
    pid_file = services.settings.root_dir / "logs" / "server.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    training_coexist = owner_training_active()
    os.environ["EIRVEN_TRAINING_COEXIST"] = "1" if training_coexist else "0"
    write_runtime_marker(services.settings.root_dir, training_coexist)
    try:
        log_event(services.settings.root_dir, "TRAINING_COEXIST", active=training_coexist, runtime_num_gpu=(0 if training_coexist else "normal"))
    except Exception:
        pass
    services.tasks.start()

    # Own stop event, declared before use: voice_watchdog_stop is created further
    # down in this function, so referencing it here raised NameError on startup.
    broadcast_stop = threading.Event()
    app.state.broadcast_stop = broadcast_stop

    def scheduled_broadcast_worker() -> None:
        """Send queued Telegram broadcasts when their time arrives."""
        while not broadcast_stop.wait(30.0):
            try:
                due = services.telegram.due_broadcasts()
                for entry in due:
                    try:
                        services.telegram.broadcast(entry.get("chats") or [], str(entry.get("message") or ""))
                    except Exception:
                        # A failed send must not drop the remaining queue.
                        continue
            except Exception:
                pass

    threading.Thread(target=scheduled_broadcast_worker, daemon=True, name="eirven-tg-schedule").start()
    services.chat_jobs.start()
    if services.settings.companion_enabled:
        try:
            if bool(services.identity.get().desktop_avatar_enabled):
                services.companion.start()
        except Exception as exc:
            log_event(services.settings.root_dir, "COMPANION_START_FAILED", error=str(exc)[:300])

    # Иконка в трее: вернуться в Эрви после закрытия окна, показать или скрыть
    # сферу, выйти. Её сбой не должен мешать остальному — без неё Эрви
    # открывается ярлыком и сферой.
    try:
        from .tray import TrayIcon
        from . import app_key as _tray_key
        from .system_browser import open_app_window as _tray_open

        def _tray_open_ui() -> None:
            _tray_open(_tray_key.entry_url(services.settings.root_dir, services.settings.port))

        def _tray_show_orb() -> None:
            services.companion.start()
            services.companion.show()

        def _tray_shutdown() -> None:
            # Та же схема, что у штатного выключения: сигнал наблюдателю, а если его
            # нет (прямой запуск) — выход самого процесса через секунду.
            stop_file = services.settings.root_dir / "logs" / "stop.request"
            stop_file.parent.mkdir(parents=True, exist_ok=True)
            stop_file.touch(exist_ok=True)
            try:
                services.companion.hide()
            except Exception:
                pass

            def _fallback() -> None:
                time.sleep(1.0)
                os._exit(0)

            threading.Thread(target=_fallback, daemon=True, name="eirven-tray-exit").start()

        services.tray = TrayIcon(services.settings, _tray_open_ui, _tray_show_orb,
                                 services.companion.hide, _tray_shutdown)
        services.tray.start()
    except Exception as exc:
        log_event(services.settings.root_dir, "TRAY_WIRE_FAILED", error=str(exc)[:300])
    if services.voice_daemon is not None:
        voice_allowed = bool(services.db.get_setting("voice_wake_enabled", True))
        if voice_allowed:
            try:
                services.voice_daemon.start()
            except Exception:
                pass

        voice_watchdog_stop = threading.Event()
        app.state.voice_watchdog_stop = voice_watchdog_stop

        def voice_watchdog() -> None:
            # Voice is a service, not a browser feature. If PortAudio/device startup
            # hiccups after login, recover it without waiting for the UI to be opened.
            #
            # Restarts are deliberately conservative. Cycling the audio device every
            # few seconds does not fix a genuinely unavailable microphone -- it keeps
            # the device churning, and a restart landing mid-reply tears down playback
            # so the answer never reaches the speakers, which then looks like another
            # failure and triggers another restart. Back off instead, and never
            # interrupt speech or an in-flight generation to do it.
            failures = 0
            last_attempt = 0.0
            while not voice_watchdog_stop.wait(3.0):
                try:
                    if not bool(services.db.get_setting("voice_wake_enabled", True)):
                        status = services.voice_daemon.status()
                        if status.get("running"):
                            # Let an in-flight turn finish. Stopping here throws away a
                            # reply that has already been generated, with no trace, and
                            # looks identical to the assistant simply going silent.
                            if status.get("speaking") or status.get("state") in {"thinking", "recognizing"}:
                                continue
                            services.voice_daemon.stop()
                        failures = 0
                        continue
                    status = services.voice_daemon.status()
                    if status.get("running"):
                        failures = 0
                        continue
                    if status.get("speaking") or status.get("state") in {"thinking", "recognizing"}:
                        # Busy finishing a turn; a restart here would cut it off.
                        continue
                    # 0s, 6s, 15s, 30s, 60s, then once a minute.
                    delay = (0.0, 6.0, 15.0, 30.0, 60.0)[min(failures, 4)]
                    if failures and (time.monotonic() - last_attempt) < delay:
                        continue
                    last_attempt = time.monotonic()
                    failures += 1
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
    try:
        services.phone_sync.start()
    except Exception:
        pass
    if services.updater is not None:
        try:
            services.updater.maybe_auto_update()
        except Exception:
            pass
    # r21: no automatic self-test during normal product use. Diagnostics stay internal.\n
    # Cold-start governor: warm the quality action/semantic checkpoint once, in parallel
    # with voice.  It is the model that decides whether a request needs live computer
    # control; the smaller checkpoint remains available for fast conversational prose.
    def delayed_action_model_prewarm() -> None:
        started = time.monotonic()
        try:
            if training_coexist:
                log_event(services.settings.root_dir, "MODEL_PREWARM_SKIPPED", reason="owner_training_active", num_gpu=0)
                return
            hardware = services.hardware
            cpu_only = not bool(hardware.cuda_available) and float(hardware.vram_gb or 0.0) < 4.0
            if cpu_only:
                while time.monotonic() - started < 60.0:
                    if bool(getattr(services.voice, "stt_ready", lambda: True)()):
                        break
                    time.sleep(0.20)
                tts_wait = time.monotonic()
                while time.monotonic() - tts_wait < 15.0:
                    if bool(getattr(services.voice, "tts_warm_ready", lambda: True)()):
                        break
                    time.sleep(0.20)
            # Route admission is intentionally warmed on the fast resident model.  The
            # quality planner is loaded only after the envelope authorizes a real
            # computer action, so ordinary speech is not queued behind a 9B cold start.
            model = str(services.universal_workflow._admission_model())
            num_gpu = action_num_gpu(services.settings, model=model)
            log_event(services.settings.root_dir, "MODEL_PREWARM_BEGIN", model=model, num_gpu=num_gpu)
            t0 = time.monotonic()
            # Warm through the same arbiter as normal generations.  A raw Ollama
            # ``warm`` request cannot be pre-empted, so a command spoken during the
            # first ten seconds used to time out behind startup and fall through as a
            # text-only answer.  Background chat is cancellable: the owner's turn wins
            # immediately, then this one-token warm-up resumes after it.
            with services.gateway.background():
                services.gateway.chat(
                    [{"role": "user", "content": " "}],
                    model=model,
                    temperature=0.0,
                    think=False,
                    num_ctx=min(int(getattr(services.settings, "chat_num_ctx", 3072) or 3072), 3072),
                    num_predict=1,
                    keep_alive=services.settings.keep_alive,
                    timeout_seconds=300,
                    num_gpu=num_gpu,
                )
            log_event(services.settings.root_dir, "MODEL_PREWARM_END", model=model, ms=round((time.monotonic()-t0)*1000), ok=True)
        except Exception as exc:
            try:
                log_event(services.settings.root_dir, "MODEL_PREWARM_ERROR", error=str(exc)[:500])
            except Exception:
                pass
    threading.Thread(target=delayed_action_model_prewarm, daemon=True, name="eirven-action-model-prewarm").start()
    try:
        log_event(
            services.settings.root_dir,
            "RESOURCE_POLICY",
            action_model_prewarm=(not training_coexist),
            training_coexist=training_coexist,
            tts_preload=True,
            prewarm_policy="immediate_gpu_short_voice_headstart_cpu",
            keep_alive=services.settings.keep_alive,
        )
    except Exception:
        pass

    # One autostart path, one setting. A second inline copy used to live earlier in
    # this function reading a different key: it ran on the startup thread, so a slow
    # Telegram connect delayed the whole application, and the UI toggle wrote a key
    # the original code never read.
    autostart_telegram = bool(
        services.db.get_setting("telegram_autostart_monitor", False)
        or services.db.get_setting("telegram_autostart", False)
    )
    if autostart_telegram:
        def start_telegram() -> None:
            try:
                # Without an authorized session the monitor would start, find no
                # account and stop again; skip it rather than churn.
                if not bool((services.telegram.config() or {}).get("authorized")):
                    return
                services.telegram.start()
            except Exception:
                pass
        threading.Thread(target=start_telegram, daemon=True, name="telegram-autostart").start()

    yield
    watchdog_stop = getattr(app.state, "voice_watchdog_stop", None)
    if watchdog_stop is not None:
        watchdog_stop.set()
    broadcast_stop_event = getattr(app.state, "broadcast_stop", None)
    if broadcast_stop_event is not None:
        broadcast_stop_event.set()
    services.chat_jobs.stop()
    services.tasks.stop()
    try:
        services.video.stop()
    except Exception:
        pass
    services.companion.stop()
    try:
        services.phone_sync.stop()
    except Exception:
        pass
    if services.updater is not None:
        try:
            services.updater.stop()
        except Exception:
            pass
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
    browser_host = (
        "127.0.0.1"
        if services.settings.host in {"0.0.0.0", "::"}
        else services.settings.host
    )
    url = f"http://{browser_host}:{services.settings.port}/ui/"
    if os.getenv("EIRVEN_OPEN_BROWSER", "false").strip().lower() not in {"0", "false", "no"}:
        # Окно входит по ключу сеанса: без него сервер покажет страницу «только в приложении».
        from . import app_key as _app_key
        threading.Timer(1.2, lambda: open_app_window(
            _app_key.entry_url(services.settings.root_dir, services.settings.port))).start()
    uvicorn.run(
        app,
        host=services.settings.host,
        port=services.settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
