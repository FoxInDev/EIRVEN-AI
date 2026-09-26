from __future__ import annotations

import io
import os
import queue
import collections
import re
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .affect import analyze_speech_affect
from .identity import CANONICAL_ASSISTANT_NAME
from .trace import log_event
from . import voice_cues


class NativeVoiceDaemon:
    """Always-on local voice loop for Windows/Linux desktops.

    The capture device is opened at its native sample rate and resampled to 16 kHz for
    ASR. Adaptive energy + optional WebRTC VAD avoids chopping fast Russian speech.
    Speech playback uses a dedicated OutputStream so barge-in does not tear down the
    microphone stream.
    """

    SAMPLE_RATE = 16_000
    BLOCK_MS = 30
    BLOCK_SIZE = SAMPLE_RATE * BLOCK_MS // 1000
    WAKE_WINDOW_SECONDS = 5.0

    def __init__(self, services: Any):
        self.services = services
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._command_thread: threading.Thread | None = None
        self._notification_thread: threading.Thread | None = None
        self._audio_q: queue.Queue[tuple[Any, float, bool]] = queue.Queue(maxsize=280)
        # The final flag records whether Windows render output was active at *any*
        # point during the utterance.  Sampling only after endpointing loses speech
        # from a video when it happens to pause just before ASR starts.
        self._utterance_q: queue.Queue[tuple[bytes, float, float, float, bool]] = queue.Queue(maxsize=10)
        self._speaking = threading.Event()
        self._speaking_since = 0.0
        self._barge_in = threading.Event()
        self._generation_active = threading.Event()
        self._speak_lock = threading.Lock()
        self._bridged_turns: set[int] = set()
        # Ходы, где уже прозвучало отложенное «Берусь», и замок на проверку-и-отметку:
        # мост и отложенная фраза не должны оба решить, что говорят первыми.
        self._cued_turns: set[int] = set()
        # Маршрут каждого хода и появились ли уже слова ответа: отложенная фраза
        # «Берусь» нужна только действию, у которого ответа ещё нет.
        self._turn_actions: dict[int, str] = {}
        self._turn_tokens: set[int] = set()
        self._cue_lock = threading.Lock()
        self._turn_lock = threading.Lock()
        self._turn_serial = 0
        self._announced_task_state: dict[str, str] = {}
        self._active_until = 0.0
        # A wake opens a short conversational window; it must never survive for the
        # lifetime of the process.  This prevents YouTube/TV audio from becoming owner
        # commands after one historical wake word.
        self._session_activated = False
        self._last_activity_at = time.monotonic()
        self._conversation_id = str(services.db.get_setting("native_voice_conversation", "") or "")
        self._last_error = ""
        self._last_text = ""
        self._last_accepted_command = ""
        self._last_accepted_at = 0.0
        self._last_spoken_text = ""
        self._last_spoken_at = 0.0
        # Промежутки, когда звучал собственный динамик Эрви, и её недавние фразы.
        # По ним распознаётся её собственный голос в микрофоне — по времени ЗАПИСИ,
        # а не по моменту распознавания и не по длине фразы.
        self._self_speech_spans: collections.deque[tuple[float, float]] = collections.deque(maxlen=24)
        self._self_speech_open = 0.0
        self._recent_spoken: collections.deque[tuple[float, str]] = collections.deque(maxlen=16)
        # Идёт ли сейчас запись фрагмента речи, и приглушение звука других программ,
        # пока человек говорит при громком медиа (иначе его голос тонул в музыке).
        self._capture_active = False
        # Фон экрана: насколько громко колонки звучат в микрофоне, пока никто не говорит.
        # По нему открытый микрофон отличает человека у микрофона от звука с экрана.
        self._bleed_rms: collections.deque[float] = collections.deque(maxlen=240)
        self._bleed_seen_at = 0.0
        from .audio_duck import AudioDucker
        self._ducker = AudioDucker(
            can_release=self._duck_can_release,
            log=lambda event, **fields: log_event(self.services.settings.root_dir, event, **fields),
        )
        self._last_emotion = "natural"
        self._last_affect: dict[str, Any] = {}
        self._last_response_emotion = "natural"
        self._speaking_emotion = ""
        self._emotion_changed_at = 0.0
        self._noise_floor = 0.0045
        self._ambient_rms: deque[float] = deque(maxlen=160)
        self._input_level = 0.0
        self._speech_level = 0.0
        self._input_device = ""
        self._input_rate = 0
        self._input_backend = ""
        self._input_loopback_rejected = False
        self._media_probe_at = 0.0
        self._media_playing_guard = False
        self._media_session_count = 0
        self._media_positions: dict[str, tuple[float, float]] = {}
        self._render_output_peak = 0.0
        self._render_output_guard = False
        self._render_active_until = 0.0
        self._state = "stopped"
        self._repair_attempted = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._barge_in.clear()
        self._last_error = ""
        self._thread = threading.Thread(target=self._run, daemon=True, name="eirven-native-voice")
        self._command_thread = threading.Thread(target=self._command_loop, daemon=True, name="eirven-native-voice-commands")
        self._notification_thread = threading.Thread(target=self._notification_loop, daemon=True, name="eirven-native-voice-notifications")
        try:
            self._announced_task_state = {
                str(t.get("id")): str(t.get("status")) for t in self.services.tasks.list(limit=80)
            }
        except Exception:
            self._announced_task_state = {}
        self._command_thread.start()
        self._notification_thread.start()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._barge_in.set()
        for thread in (self._thread, self._command_thread, self._notification_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._thread = self._command_thread = self._notification_thread = None
        self._state = "stopped"

    def say(self, text: str, emotion: str = "natural") -> None:
        """Speak from another local service without blocking it."""
        if not text.strip() or self._stop.is_set():
            return
        threading.Thread(
            target=self._speak,
            args=(text.strip(), emotion, None),
            daemon=True,
            name="eirven-native-voice-say",
        ).start()

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        try:
            onboarding_complete = bool(self.services.identity.get().onboarding_completed)
        except Exception:
            onboarding_complete = True
        if self._active_until > 0 and now >= self._active_until:
            self._active_until = 0.0
            self._session_activated = False
            if self._state == "armed":
                self._state = "listening"
        visible_state = "onboarding" if not onboarding_complete else self._state
        mood = {}
        try:
            cognition = getattr(self.services, "cognition", None)
            mood = cognition.mood() if cognition is not None else {}
        except Exception:
            mood = {}
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "state": visible_state,
            "interactive_ready": bool(getattr(self.services.voice, "interactive_ready", lambda: True)()),
            "speaking": self._speaking.is_set(),
            "session_active": bool(onboarding_complete and now < self._active_until),
            "session_seconds_remaining": round(max(0.0, self._active_until - now), 1),
            "wake_phrase": self._wake_phrase(),
            "onboarding_complete": onboarding_complete,
            "last_text": self._last_text,
            "last_emotion": self._last_emotion,
            "last_affect": dict(self._last_affect),
            "response_emotion": self._last_response_emotion,
            "mood": mood,
            "speaking_emotion": self._speaking_emotion,
            "emotion_age_seconds": round(max(0.0, time.monotonic() - self._emotion_changed_at), 2) if self._emotion_changed_at else None,
            "noise_floor": round(self._noise_floor, 5),
            "input_level": round(self._input_level, 4),
            "speech_level": round(self._speech_level, 4),
            "input_device": self._input_device,
            "input_rate": self._input_rate,
            "input_backend": self._input_backend,
            "input_loopback_rejected": self._input_loopback_rejected,
            "media_playing_guard": self._media_playing_guard,
            "media_session_count": self._media_session_count,
            "render_output_peak": round(self._render_output_peak, 4),
            "render_output_guard": self._render_output_guard,
            "idle_seconds": round(self.idle_seconds(), 2),
            "error": self._last_error,
        }

    def idle_seconds(self) -> float:
        return max(0.0, time.monotonic() - float(self._last_activity_at or 0.0))


    def _session_seconds(self) -> float:
        # r21 interaction contract: "Эрви" arms exactly one following thought for five
        # seconds. If speech starts inside the window, it may continue for the full utterance.
        return self.WAKE_WINDOW_SECONDS

    @staticmethod
    def _session_reports_playback(row: dict[str, Any]) -> bool:
        """Interpret live media-session state without knowing the player or service.

        EIRVEN's WinRT adapter emits ``playing``, but third-party adapters and older
        builds may expose an enum, its numeric value, or only the enabled controls.
        ``pause`` enabled while ``play`` is disabled is the generic GSMTC signal that
        the session can currently be paused (and is therefore playing).
        """
        state_value = row.get("state", row.get("playback_status", ""))
        if isinstance(state_value, bool):
            return state_value
        if isinstance(state_value, (int, float)) and not isinstance(state_value, bool):
            return int(state_value) == 4
        state = re.sub(r"[^a-z0-9]+", "", str(state_value or "").strip().lower())
        if state in {"4", "play", "playing", "active", "running", "started", "resumed"}:
            return True
        if state.endswith("playing") and not state.endswith("notplaying"):
            return True
        for key in ("is_playing", "playing", "playback_active"):
            value = row.get(key)
            if isinstance(value, bool) and value:
                return True
        controls = row.get("controls")
        if isinstance(controls, dict):
            can_pause = controls.get("pause") is True
            can_play = controls.get("play") is True
            if can_pause and not can_play:
                return True
        return False

    def _session_timeline_advanced(self, row: dict[str, Any], now: float) -> bool:
        """Detect a progressing session when a provider reports an unknown/stale state."""
        session_id = str(row.get("session_id") or "").strip()
        try:
            position = float(row.get("position_seconds"))
        except (TypeError, ValueError):
            return False
        if not session_id or position < 0.0:
            return False
        positions = getattr(self, "_media_positions", None)
        if not isinstance(positions, dict):
            positions = {}
            self._media_positions = positions
        previous = positions.get(session_id)
        positions[session_id] = (position, now)
        if previous is None:
            return False
        previous_position, previous_at = previous
        elapsed = max(0.0, now - previous_at)
        # Timeline granularity differs between players; 80 ms is above ordinary
        # rounding jitter while still detecting progress between the 800 ms probes.
        return 0.15 <= elapsed <= 8.0 and position - previous_position >= 0.08

    @staticmethod
    def _default_output_peaks(samples: int = 3, interval: float = 0.016) -> tuple[float, ...]:
        """Sample the Windows default render endpoint, returning no data on any failure.

        The endpoint meter is app/service agnostic and catches loud browser playback
        even when a site has no usable GSMTC session.  pycaw is an optional desktop
        dependency; a missing/broken Core Audio runtime must never break open-mic mode.

        Keep every COM reference inside the balanced ``CoInitialize`` scope.  A raw
        ``ctypes.cast`` aliases the pointer returned by ``Activate`` without taking a
        second COM reference; comtypes can then release the same pointer twice after
        ``CoUninitialize``.  On Windows that produced repeated native access violations
        in ``_compointer_base.__del__`` and could eventually take down the host process.
        ``QueryInterface`` owns its reference, and clearing all wrappers before
        ``CoUninitialize`` makes the sampling loop safe across voice restarts.
        """
        if os.name != "nt":
            return ()
        com_initialized = False
        device = None
        native_device = None
        interface = None
        meter = None
        try:
            import comtypes  # type: ignore
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation  # type: ignore

            comtypes.CoInitialize()
            com_initialized = True
            device = AudioUtilities.GetSpeakers()
            native_device = getattr(device, "_dev", device)
            interface = native_device.Activate(
                IAudioMeterInformation._iid_, comtypes.CLSCTX_ALL, None,
            )
            meter = interface.QueryInterface(IAudioMeterInformation)
            peaks: list[float] = []
            for index in range(max(1, int(samples))):
                peaks.append(max(0.0, min(1.0, float(meter.GetPeakValue()))))
                if index + 1 < samples and interval > 0:
                    time.sleep(interval)
            return tuple(peaks)
        except Exception:
            return ()
        finally:
            if com_initialized:
                # CPython releases these synchronously in assignment order.  The COM
                # apartment must still be alive while their destructors call Release.
                meter = None
                interface = None
                native_device = None
                device = None
                try:
                    comtypes.CoUninitialize()  # type: ignore[possibly-undefined]
                except Exception:
                    pass

    @staticmethod
    def _output_peaks_active(peaks: tuple[float, ...]) -> tuple[float, bool]:
        clean = tuple(max(0.0, min(1.0, float(value))) for value in peaks)
        peak = max(clean, default=0.0)
        sustained = sum(value >= 0.006 for value in clean) >= 2
        # One clearly audible transient is enough to suppress its ASR echo; quieter
        # audio needs two consecutive samples so an idle endpoint stays open-mic.
        return peak, bool(sustained or peak >= 0.025)

    def _foreground_media_kind(self) -> str:
        """Return live playback from GSMTC or the default Windows output meter.

        No app names, page titles, or fixed service catalogue are involved.  The short
        cache keeps the capture loop cheap while still reacting within one second.
        """
        now = time.monotonic()
        if now - float(getattr(self, "_media_probe_at", 0.0) or 0.0) < 0.8:
            return "media" if bool(getattr(self, "_media_playing_guard", False)) else ""
        self._media_probe_at = now
        session_active = False
        sessions: list[dict[str, Any]] = []
        try:
            tools = getattr(self.services, "tools", None)
            if tools is not None and hasattr(tools, "tool_media_control"):
                status = tools.tool_media_control(action="list")
                if isinstance(status, dict):
                    sessions = [
                        row for row in list(status.get("sessions") or [])
                        if isinstance(row, dict)
                    ]
        except Exception as exc:
            log_event(
                self.services.settings.root_dir,
                "VOICE_MEDIA_PROBE_ERROR",
                error=str(exc)[:240],
            )
        self._media_session_count = len(sessions)
        live_ids: set[str] = set()
        for row in sessions:
            session_id = str(row.get("session_id") or "").strip()
            if session_id:
                live_ids.add(session_id)
            if self._session_reports_playback(row) or self._session_timeline_advanced(row, now):
                session_active = True
        positions = getattr(self, "_media_positions", {})
        if isinstance(positions, dict):
            self._media_positions = {
                key: value for key, value in positions.items()
                if key in live_ids and now - float(value[1]) <= 8.0
            }

        speaking = getattr(self, "_speaking", None)
        assistant_speaking = bool(speaking is not None and speaking.is_set())
        peaks = () if assistant_speaking else self._default_output_peaks()
        output_peak, output_now = self._output_peaks_active(peaks)
        self._render_output_peak = output_peak
        active_until = float(getattr(self, "_render_active_until", 0.0) or 0.0)
        if output_now:
            active_until = now + 1.2
        elif assistant_speaking:
            # Never let EIRVEN's own reply create a post-speech false playback hold.
            active_until = 0.0
        self._render_active_until = active_until
        self._render_output_guard = bool(output_now or now < active_until)
        self._media_playing_guard = bool(session_active or self._render_output_guard)
        return "media" if self._media_playing_guard else ""

    def _import_sounddevice(self):
        try:
            import sounddevice as sd  # type: ignore
            return sd
        except ImportError as first:
            if self._repair_attempted:
                raise
            self._repair_attempted = True
            # Old EIRVEN archives used a release marker shared across updates, so an
            # upgraded source tree could run on a venv created before sounddevice was
            # added. Repair only that exact missing dependency once.
            try:
                flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "sounddevice>=0.5,<1.0", "soundfile>=0.13,<1.0"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=240,
                    check=True,
                    creationflags=flags,
                )
                import sounddevice as sd  # type: ignore
                return sd
            except Exception as exc:
                raise ImportError(f"{first}; автоматическое восстановление не удалось: {exc}") from exc

    @staticmethod
    def _wav_bytes(chunks: list[Any]) -> bytes:
        import numpy as np  # type: ignore
        if not chunks:
            return b""
        audio = np.concatenate(chunks).reshape(-1)
        audio = np.clip(audio, -1.0, 1.0)
        pcm = (audio * 32767.0).astype("<i2").tobytes()
        target = io.BytesIO()
        with wave.open(target, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(NativeVoiceDaemon.SAMPLE_RATE)
            wav.writeframes(pcm)
        return target.getvalue()

    @staticmethod
    def _resample(block: Any, source_rate: int):
        import numpy as np  # type: ignore
        mono = np.asarray(block, dtype=np.float32).reshape(-1)
        if source_rate == NativeVoiceDaemon.SAMPLE_RATE and mono.size == NativeVoiceDaemon.BLOCK_SIZE:
            return mono
        target_n = NativeVoiceDaemon.BLOCK_SIZE
        if mono.size <= 1:
            return np.zeros(target_n, dtype=np.float32)
        x_old = np.linspace(0.0, 1.0, num=mono.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=target_n, endpoint=False)
        return np.interp(x_new, x_old, mono).astype(np.float32)

    @staticmethod
    def _webrtc_speech(vad: Any, block: Any) -> bool:
        if vad is None:
            return False
        try:
            import numpy as np  # type: ignore
            pcm = (np.clip(block, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
            return bool(vad.is_speech(pcm, NativeVoiceDaemon.SAMPLE_RATE))
        except Exception:
            return False

    @staticmethod
    def _start_frame_likely(rms: float, vad_speech: bool, noise_floor: float) -> bool:
        """Reject low-energy WebRTC false positives before opening an utterance.

        The r28 trace contained several 35-second recordings that began around RMS
        0.006.  WebRTC marked the laptop/room noise as speech, and the old ``vad OR
        energy`` rule then held the recorder open forever.  A start now needs both a
        calibrated energy rise and VAD, unless the energy rise is unambiguously strong.
        """
        threshold = max(0.0068, float(noise_floor) * 1.42)
        return bool(
            float(rms) >= threshold
            and (bool(vad_speech) or float(rms) >= threshold * 1.45)
        )

    @staticmethod
    def _recording_frame_voiced(
        rms: float,
        vad_speech: bool,
        noise_floor: float,
        peak_rms: float,
    ) -> bool:
        """Combine VAD with adaptive energy so steady noise can end a phrase."""
        threshold = max(
            0.0058,
            float(noise_floor) * 1.35,
            float(peak_rms) * 0.34,
        )
        return bool(
            float(rms) >= threshold
            and (bool(vad_speech) or float(rms) >= threshold * 1.35)
        )

    @staticmethod
    def _media_start_frame_likely(
        rms: float,
        vad_speech: bool,
        noise_floor: float,
        output_peak: float,
    ) -> bool:
        """Open an utterance over media only for a near-field speech burst.

        A video can make WebRTC VAD report speech for every block.  Requiring a stronger
        local energy rise, scaled by the render endpoint peak, prevents the recognizer
        from repeatedly processing the music itself.  An explicit wake word is still
        required later by ``_activation_route`` while playback is active.
        """
        threshold = max(
            0.0105,
            float(noise_floor) * 1.85,
            max(0.0, float(output_peak)) * 0.18,
        )
        return bool(float(rms) >= threshold and bool(vad_speech))

    @staticmethod
    def _owner_speech_gate(
        duration: float,
        average_energy: float,
        noise_floor: float,
        playback_guard: bool,
        peak_energy: float = 0.0,
    ) -> bool:
        """Reject short low-energy ASR hallucinations before they reach the LLM.

        GigaAM can produce a plausible-looking Russian sentence from fan noise or a
        distant video.  A real owner utterance stands out against the calibrated
        floor; a named wake or an already armed conversation bypasses this
        convenience gate and remains usable when spoken quietly.

        The test is deliberately relative. A fixed absolute minimum encodes one
        microphone's gain: on a quiet input every genuine phrase averages below it
        and is discarded, which is indistinguishable from a broken microphone. The
        average is also diluted by the silence padding at both ends of a recording,
        so the peak carries the real signal -- steady noise has no peak, speech does.
        """
        floor = max(0.001, float(noise_floor or 0.0))
        minimum = max(0.0035, floor * 1.8)
        peak_minimum = max(0.009, floor * 3.5)
        if playback_guard:
            minimum = max(minimum, 0.011)
            peak_minimum = max(peak_minimum, 0.02)
        # Keep long dictation usable; the gate is for the short fragments most often
        # generated by environmental speech/music.
        if float(duration or 0.0) > 2.8:
            minimum *= 0.88
            peak_minimum *= 0.88
        if float(average_energy or 0.0) >= minimum:
            return True
        return float(peak_energy or 0.0) >= peak_minimum

    def _observe_ambient(self, rms: float) -> None:
        """Track the quiet quartile, so speech bursts cannot poison calibration."""
        value = max(0.0004, min(0.03, float(rms)))
        self._ambient_rms.append(value)
        if len(self._ambient_rms) < 8:
            return
        ordered = sorted(self._ambient_rms)
        target = ordered[max(0, int(len(ordered) * 0.25) - 1)]
        self._noise_floor = 0.90 * self._noise_floor + 0.10 * target

    @staticmethod
    def _loopback_like_device(name: str) -> bool:
        """Detect capture endpoints that are render-loopback rather than microphones."""
        clean = re.sub(r"\s+", " ", str(name or "").casefold()).strip()
        return any(mark in clean for mark in (
            "stereo mix", "stereomix", "what u hear", "wave out mix", "loopback",
            "monitor of", "output capture", "speaker capture", "стерео микшер",
            "микшер стерео", "запись выхода", "монитор выхода",
        ))

    @classmethod
    def _preferred_wasapi_device(cls, sd: Any, kind: str, requested: int | None = None) -> tuple[int | None, Any | None]:
        """Prefer a physical shared-mode WASAPI microphone and reject loopback input."""
        if os.name != "nt":
            return requested, None
        try:
            hostapis = list(sd.query_hostapis())
            devices = list(sd.query_devices())
            host_index = next(
                index for index, row in enumerate(hostapis)
                if "windows wasapi" in str(row.get("name") or "").casefold()
            )
            extra = sd.WasapiSettings(exclusive=False, auto_convert=True)
            channels_key = "max_input_channels" if kind == "input" else "max_output_channels"

            if requested is not None and 0 <= int(requested) < len(devices):
                requested_row = devices[int(requested)]
                requested_name = str(requested_row.get("name") or "")
                rejected = kind == "input" and cls._loopback_like_device(requested_name)
                if not rejected and int(requested_row.get(channels_key, 0)) > 0:
                    if int(requested_row.get("hostapi", -1)) == host_index:
                        return int(requested), extra
                    # MME/DirectSound often exposes the same physical microphone with a
                    # second WASAPI id. Prefer that low-latency shared endpoint.
                    requested_key = re.sub(r"[^a-zа-яё0-9]+", "", requested_name.casefold())
                    same_endpoint = next((
                        index for index, row in enumerate(devices)
                        if int(row.get("hostapi", -1)) == host_index
                        and int(row.get(channels_key, 0)) > 0
                        and re.sub(r"[^a-zа-яё0-9]+", "", str(row.get("name") or "").casefold()) == requested_key
                    ), None)
                    if same_endpoint is not None:
                        return int(same_endpoint), extra
                    return int(requested), None

            key = "default_input_device" if kind == "input" else "default_output_device"
            device = int(hostapis[host_index].get(key, -1))
            default_safe = bool(
                0 <= device < len(devices)
                and int(devices[device].get(channels_key, 0)) > 0
                and not (kind == "input" and cls._loopback_like_device(str(devices[device].get("name") or "")))
            )
            if not default_safe:
                candidates = [
                    (index, row) for index, row in enumerate(devices)
                    if int(row.get("hostapi", -1)) == host_index
                    and int(row.get(channels_key, 0)) > 0
                    and not (kind == "input" and cls._loopback_like_device(str(row.get("name") or "")))
                ]
                if not candidates:
                    raise RuntimeError("Не найден физический WASAPI-вход; loopback использовать небезопасно")
                if kind == "input":
                    def input_rank(item: tuple[int, Any]) -> tuple[int, int]:
                        index, row = item
                        name = str(row.get("name") or "").casefold()
                        microphone = any(mark in name for mark in ("microphone", "mic ", "микрофон", "headset", "гарнитур"))
                        return (0 if microphone else 1, index)
                    candidates.sort(key=input_rank)
                device = int(candidates[0][0])
            return device, extra
        except Exception:
            return requested, None

    def _run(self) -> None:
        # Iterative supervisor around one capture session. Recovery must not
        # recurse: a long-lived daemon can hit many isolated device hiccups, and
        # each recursive retry would leave a stack frame behind permanently.
        while not self._stop.is_set():
            if self._run_capture_once() is not True:
                return

    def _run_capture_once(self) -> bool:
        """Run one capture session. Returns True if it should be retried."""
        try:
            sd = self._import_sounddevice()
            import numpy as np  # type: ignore
        except Exception as exc:
            self._last_error = f"sounddevice: {exc}"
            self._state = "unavailable"
            return False

        vad = None
        try:
            import webrtcvad  # type: ignore
            # Mode 3 is the documented most aggressive noise filter.  Energy gates below
            # remain necessary because laptop microphones can still produce VAD false
            # positives on fans, keyboard noise and speaker leakage.
            vad = webrtcvad.Vad(3)
        except Exception:
            vad = None

        selected = self.services.db.get_setting("microphone_device", None)
        requested_device = int(selected) if str(selected or "").isdigit() else None
        device, wasapi_settings = self._preferred_wasapi_device(sd, "input", requested_device)
        try:
            try:
                info = sd.query_devices(device, "input")
            except Exception:
                # A previously selected USB/Bluetooth device may disappear. Fall back to
                # the current Windows default instead of leaving the 24/7 daemon dead.
                device = None
                info = sd.query_devices(None, "input")
                self.services.db.set_setting("microphone_device", None)
            native_rate = int(round(float(info.get("default_samplerate") or 48_000)))
            native_rate = native_rate if native_rate >= 8_000 else 48_000
            native_block = max(64, int(native_rate * self.BLOCK_MS / 1000))
            self._input_device = str(info.get("name") or "default")
            self._input_rate = native_rate
            self._input_loopback_rejected = self._loopback_like_device(self._input_device)
            try:
                hostapi_index = int(info.get("hostapi", -1))
                hostapi = sd.query_hostapis(hostapi_index) if hostapi_index >= 0 else {}
                self._input_backend = str(hostapi.get("name") or "")
            except Exception:
                self._input_backend = ""
            if self._input_loopback_rejected:
                raise RuntimeError(f"выбран системный loopback вместо микрофона: {self._input_device}")
        except Exception as exc:
            self._last_error = f"Микрофон: {exc}"
            self._state = "unavailable"
            # Device selection failing is usually temporary: another app holds the
            # microphone, or Windows is re-enumerating it. Ending the daemon here is
            # what produced the "недоступен -> Запуск.. -> обрыв" cycle, because the
            # watchdog's full restart lands on the same condition moments later.
            if not self._stop.is_set():
                self._capture_failures = getattr(self, "_capture_failures", 0) + 1
                # Не сдаёмся (см. выше): как только микрофон освободится, голос оживёт сам.
                self._state = "warming" if self._capture_failures <= 5 else "unavailable"
                if self._capture_failures <= 5 or self._capture_failures % 10 == 0:
                    log_event(self.services.settings.root_dir, "VOICE_DEVICE_RETRY",
                              attempt=self._capture_failures, error=str(self._last_error)[:200])
                self._stop.wait(min(2.0 + self._capture_failures * 1.5, 15.0))
                return True
            return False

        def callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
            if self._stop.is_set():
                return
            try:
                raw = indata[:, 0].copy()
                block = self._resample(raw, native_rate)
                rms = float(np.sqrt(np.mean(np.square(block), dtype=np.float64)))
                is_speech = self._webrtc_speech(vad, block)
                self._input_level = min(1.0, rms * 14.0)
                try:
                    self._audio_q.put_nowait((block, rms, is_speech))
                except queue.Full:
                    try:
                        self._audio_q.get_nowait()
                        self._audio_q.put_nowait((block, rms, is_speech))
                    except queue.Empty:
                        pass
            except Exception as exc:
                self._last_error = str(exc)[:300]

        pre_roll: deque[Any] = deque(maxlen=max(12, 510 // self.BLOCK_MS))
        recording: list[Any] = []
        recording_started_at = 0.0
        recording_playback_seen = False
        speech_blocks = silence_blocks = candidate_blocks = 0
        energy_sum = 0.0
        recording_peak_rms = 0.0
        capture_ready_announced = False
        self._state = "warming"
        try:
            try:
                stream = sd.InputStream(
                    device=device, samplerate=native_rate, channels=1, dtype="float32",
                    blocksize=native_block, latency="low", callback=callback,
                    extra_settings=wasapi_settings,
                )
            except Exception:
                # Some Windows/WASAPI devices reject explicit low latency/block sizes.
                # Let PortAudio choose a safe native buffer before declaring the mic bad.
                try:
                    stream = sd.InputStream(
                        device=device, samplerate=native_rate, channels=1, dtype="float32",
                        callback=callback, extra_settings=wasapi_settings,
                    )
                except Exception as wasapi_exc:
                    # «Insufficient memory» (PaErrorCode -9992) — известная беда режима
                    # WASAPI на части систем: микрофон не открывается, хотя свободен.
                    # MME — старый, но самый терпимый режим Windows; открываем через него
                    # микрофон по умолчанию. Частоту берём от MME-устройства: обработчик
                    # звука пересчитывает запись по ней, иначе речь пришла бы искажённой.
                    mme_device = self._mme_default_input(sd)
                    if mme_device is None:
                        raise
                    mme_info = sd.query_devices(mme_device, "input")
                    native_rate = int(float(mme_info.get("default_samplerate") or native_rate))
                    self._input_rate = native_rate
                    self._input_backend = "MME"
                    log_event(self.services.settings.root_dir, "VOICE_CAPTURE_FALLBACK_MME",
                              error=str(wasapi_exc)[:200], device=str(mme_info.get("name") or mme_device)[:80],
                              rate=native_rate)
                    stream = sd.InputStream(
                        device=mme_device, samplerate=native_rate, channels=1, dtype="float32",
                        callback=callback,
                    )
            with stream:
                self._last_error = ""
                self._capture_failures = 0
                self._state = "warming"
                while not self._stop.is_set():
                    try:
                        block, rms, vad_speech = self._audio_q.get(timeout=0.35)
                    except queue.Empty:
                        if self._state == "armed" and time.monotonic() >= self._active_until:
                            self._state = "listening"
                            self._session_activated = False
                        continue

                    if self._state == "armed" and time.monotonic() >= self._active_until:
                        self._state = "listening"
                        self._session_activated = False
                        log_event(self.services.settings.root_dir, "VOICE_SESSION_EXPIRED")

                    # Never carry a noise-triggered recording across ASR startup.  The UI
                    # may say "слушаю" only after the real recognizer has loaded and
                    # completed its inference probe, and the first accepted block starts
                    # from a clean buffer calibrated during warm-up.
                    ready_fn = getattr(self.services.voice, "stt_ready", None)
                    capture_ready = bool(ready_fn()) if callable(ready_fn) else True
                    if not capture_ready:
                        if not self._speaking.is_set():
                            self._observe_ambient(rms)
                        recording = []
                        self._capture_active = False
                        recording_started_at = 0.0
                        recording_playback_seen = False
                        recording_peak_rms = 0.0
                        pre_roll.clear()
                        speech_blocks = silence_blocks = candidate_blocks = 0
                        energy_sum = 0.0
                        self._state = "warming"
                        continue
                    if not capture_ready_announced:
                        capture_ready_announced = True
                        recording = []
                        pre_roll.clear()
                        speech_blocks = silence_blocks = candidate_blocks = 0
                        energy_sum = 0.0
                        recording_peak_rms = 0.0
                        self._state = "listening"
                        log_event(
                            self.services.settings.root_dir,
                            "VOICE_CAPTURE_READY",
                            noise_floor=round(self._noise_floor, 6),
                        )
                        continue

                    if not recording:
                        speaking_now = self._speaking.is_set()
                        media_now = False if speaking_now else bool(self._foreground_media_kind())
                        if not speaking_now:
                            self._observe_ambient(rms)
                            if float(getattr(self, "_render_output_peak", 0.0) or 0.0) > 0.02:
                                self._bleed_rms.append(float(rms))
                                self._bleed_seen_at = time.monotonic()
                        if speaking_now:
                            # Barge-in r38: the owner must be able to say the wake name while
                            # EIRVEN is speaking and cut TTS quickly. Keep an echo margin, but
                            # no longer require an unrealistically loud 180 ms interruption.
                            threshold = max(0.032, self._noise_floor * 4.6)
                            speaker_age = time.monotonic() - self._speaking_since
                            likely = bool(vad_speech and rms >= threshold and speaker_age >= 0.22)
                            required_blocks = 3 if vad is not None else 4  # 90-120 ms
                        else:
                            likely = (
                                self._media_start_frame_likely(
                                    rms,
                                    vad_speech,
                                    self._noise_floor,
                                    self._render_output_peak,
                                )
                                if media_now
                                else self._start_frame_likely(rms, vad_speech, self._noise_floor)
                            )
                            required_blocks = 3 if vad is not None else 4
                        pre_roll.append(block)
                        candidate_blocks = candidate_blocks + 1 if likely else max(0, candidate_blocks - 1)
                        # Outside playback 60–90 ms is enough and avoids eating fast first words.
                        if candidate_blocks >= required_blocks:
                            # Do not stop audible speech on VAD alone. Speaker echo, a paused
                            # video's residual audio, keyboard noise and room speech were cutting
                            # EIRVEN mid-sentence. We keep recording/ASR running and only set
                            # barge_in after the utterance passes wake/session policy below.
                            self._last_activity_at = time.monotonic()
                            recording_started_at = self._last_activity_at
                            recording_playback_seen = bool(media_now)
                            self._capture_active = True
                            # Человек заговорил при громком медиа — приглушить другие
                            # программы, чтобы его голос стал в записи главным. Пока
                            # говорит сама Эрви, не приглушаем: это её же голос.
                            # Громкость на выходе ловит звук откуда угодно — и из
                            # свёрнутого плеера, а не только из активного окна.
                            loud_output = float(getattr(self, "_render_output_peak", 0.0) or 0.0) > 0.08
                            if (media_now or loud_output) and not speaking_now:
                                self._ducker.request()
                            # Without acoustic echo cancellation, raw VAD during TTS is
                            # not proof of near-field speech. Keep recording for ASR/wake
                            # policy, but never let speaker echo cut the assistant first.
                            log_event(self.services.settings.root_dir, "VOICE_START", rms=round(rms, 5), speaking_over_tts=speaking_now)
                            try:
                                runtime = getattr(self.services, "runtime", None)
                                if runtime is not None:
                                    runtime.voice_activity_started(rms=rms)
                            except Exception:
                                pass
                            recording = list(pre_roll)
                            speech_blocks = candidate_blocks
                            silence_blocks = 0
                            energy_sum = rms * candidate_blocks
                            recording_peak_rms = rms
                            candidate_blocks = 0
                            self._state = "hearing"
                        continue

                    recording.append(block)
                    # Keep observing the render endpoint for the whole phrase. Speech
                    # and music contain natural silences, so a single probe at the start
                    # or after endpointing can miss the very source being transcribed.
                    # ``_foreground_media_kind`` has its own 800 ms cache; calling it for
                    # each block is cheap while ensuring every long utterance contains
                    # multiple real Core Audio observations.
                    if not self._speaking.is_set():
                        recording_playback_seen = bool(
                            recording_playback_seen or self._foreground_media_kind()
                        )
                    energy_sum += rms
                    recording_peak_rms = max(recording_peak_rms, rms)
                    voiced = self._recording_frame_voiced(
                        rms, vad_speech, self._noise_floor, recording_peak_rms,
                    )
                    if voiced:
                        speech_blocks += 1
                        silence_blocks = 0
                        self._speech_level = min(1.0, rms * 18.0)
                    else:
                        silence_blocks += 1

                    # Endpoint quickly, but give long/fast utterances a little extra clause pause.
                    # The old hard 900–1050 ms floor added a full second before ASR even began.
                    voiced_ms = speech_blocks * self.BLOCK_MS
                    base_hangover = max(480, min(720, int(self.services.settings.voice_silence_ms)))
                    hangover_ms = base_hangover
                    if voiced_ms >= 3500:
                        hangover_ms += 140
                    if voiced_ms >= 8000:
                        hangover_ms += 100
                    # onnx-asr documents a 20–30 second practical input ceiling.  A
                    # desktop command should never be held for r28's 35 seconds even if a
                    # noisy device defeats both gates.
                    max_blocks = int(20_000 / self.BLOCK_MS)
                    if silence_blocks * self.BLOCK_MS >= hangover_ms or len(recording) >= max_blocks:
                        duration = len(recording) * self.BLOCK_MS / 1000.0
                        speech_ms = speech_blocks * self.BLOCK_MS
                        # 150 ms of voiced audio is 5 blocks at 30 ms; a brisk
                        # "Привет" that the VAD only partly marks as voiced lands
                        # right on that edge and was thrown away. 90 ms still
                        # rejects clicks and single-block noise.
                        if speech_ms >= 90 and duration >= 0.22:
                            wav = self._wav_bytes(recording)
                            avg_energy = energy_sum / max(1, len(recording))
                            log_event(
                                self.services.settings.root_dir, "VOICE_UTTERANCE_QUEUED",
                                duration=round(duration, 3), speech_ms=int(speech_ms),
                                peak=round(recording_peak_rms, 5),
                                queued=self._utterance_q.qsize(),
                            )
                            payload = (
                                wav, duration, avg_energy, recording_started_at,
                                recording_playback_seen, recording_peak_rms,
                            )
                            try:
                                self._utterance_q.put_nowait(payload)
                            except queue.Full:
                                try:
                                    self._utterance_q.get_nowait()
                                    self._utterance_q.put_nowait(payload)
                                except queue.Empty:
                                    pass
                        else:
                            # Previously dropped in silence. A short word ("Привет")
                            # that lands just under either threshold produced no answer
                            # and no trace, which is indistinguishable from a broken mic.
                            log_event(
                                self.services.settings.root_dir, "VOICE_UTTERANCE_TOO_SHORT",
                                duration=round(duration, 3), speech_ms=int(speech_ms),
                                need_speech_ms=90, need_duration=0.22,
                            )
                        recording = []
                        self._capture_active = False
                        recording_started_at = 0.0
                        recording_playback_seen = False
                        pre_roll.clear()
                        speech_blocks = silence_blocks = 0
                        energy_sum = 0.0
                        recording_peak_rms = 0.0
                        self._speech_level = 0.0
                        self._state = "listening"
        except Exception as exc:
            self._last_error = str(exc)[:500]
            # A transient capture failure must not kill the daemon. Letting this
            # thread end leaves the service dead until the watchdog restarts the
            # whole thing, which re-loads the recognizer and usually lands on the
            # same condition -- the observed "мик недоступен -> Запуск.. -> обрыв"
            # cycle. Reopen the stream in place instead, keeping the warm ASR model.
            if not self._stop.is_set():
                self._capture_failures = getattr(self, "_capture_failures", 0) + 1
                # Не сдаёмся. Раньше после 20 неудач голос останавливался навсегда, и
                # помогал только перезапуск. Микрофон мог занять другой процесс — как
                # только он освободится, голос оживёт сам. Первые попытки — «подожди»,
                # дальше честно «недоступен», но повторы продолжаются.
                if True:
                    self._state = "warming" if self._capture_failures <= 5 else "unavailable"
                    self._speaking.clear()
                    self._generation_active.clear()
                    for q in (self._utterance_q, self._audio_q):
                        try:
                            while True:
                                q.get_nowait()
                        except queue.Empty:
                            pass
                    log_event(
                        self.services.settings.root_dir, "VOICE_CAPTURE_RETRY",
                        attempt=self._capture_failures, error=self._last_error[:200],
                    )
                    self._stop.wait(min(2.0 + self._capture_failures * 1.5, 15.0))
                    return True
            self._state = "error"
        return False

    @staticmethod
    def _mme_default_input(sd: Any) -> int | None:
        """Микрофон по умолчанию через MME — запасной путь, если WASAPI отказал."""
        try:
            for row in sd.query_hostapis():
                if str(row.get("name") or "").casefold() == "mme":
                    index = int(row.get("default_input_device", -1))
                    return index if index >= 0 else None
        except Exception:
            return None
        return None

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[^a-zа-яё0-9]+", " ", text.casefold()).strip()

    # Запас после конца её речи: гулкость комнаты, задержка звуковой карты и то,
    # что начало записи фиксируется детектором голоса чуть позже реального звука.
    SELF_SPEECH_TAIL = 0.7

    def _duck_can_release(self) -> bool:
        """Вернуть громкость можно, когда разговор закончен: человек не говорит,
        Эрви не слушает, не думает и не отвечает, и в очереди ничего нет."""
        return (not self._capture_active and not self._speaking.is_set()
                and not self._generation_active.is_set() and self._utterance_q.empty())

    def _screen_level(self) -> float:
        """Насколько громко колонки звучат в микрофоне сейчас (0 — экран молчит).

        Верхняя пятая часть недавних замеров, пока никто не говорил: голос из видео
        и вокал в песне поднимаются как раз до этого уровня.
        """
        # Фон живёт 3 секунды после последнего замера — столько длятся фраза и её
        # распознавание. Раньше было 8: видео уже стояло на паузе, а Эрви всё ещё
        # отбрасывала человека из-за звука, которого не было.
        if not self._bleed_rms or time.monotonic() - self._bleed_seen_at > 3.0:
            return 0.0
        ordered = sorted(self._bleed_rms)
        return float(ordered[min(len(ordered) - 1, int(len(ordered) * 0.8))])

    # Во сколько раз ПИК фразы должен быть выше фона экрана, чтобы считаться голосом
    # человека у микрофона. Настроено по реальным замерам (VOICE_SCREEN_CHECK): живое
    # «Привет» у микрофона давало пик в 2,9–8,3 раза выше фона, а голос из видео по
    # своей природе держится около фона — его пики редко выше него вдвое.
    NEAR_FIELD_PEAK = 2.3

    def _near_field(self, energy: float, peak: float) -> bool:
        """Это человек у микрофона, а не звук с экрана?

        Голос из видео приходит в микрофон через колонки — на уровне самого фона
        экрана. Человек у микрофона звучит заметно громче. Пока экран молчит, фон
        нулевой — и любая речь проходит, как в тихой комнате.
        """
        level = self._screen_level()
        if level <= 0.0:
            return True
        # Только пик. Среднюю громкость фразы размывает тишина до и после слова: в
        # замерах живое «Привет» по среднему было вровень с фоном экрана, и Эрви
        # отбрасывала каждую фразу. Пик несёт настоящий сигнал голоса.
        return float(peak or 0.0) >= level * self.NEAR_FIELD_PEAK

    @staticmethod
    def _split_glued_wake(text: str) -> str:
        """«Эрвистоп» → «Эрви, стоп»: распознаватель иногда склеивает имя со словом."""
        return re.sub(r"^(\s*)(эрви|эрби|ерви)(?=[а-яё]{2,})", r"\1\2, ", str(text or ""), flags=re.IGNORECASE)

    def _self_speech_at(self, started_at: float) -> bool:
        """Звучал ли динамик Эрви, когда началась эта запись.

        Раньше защита смотрела, говорит ли Эрви в момент РАСПОЗНАВАНИЯ. Но
        распознавание отстаёт от записи на секунду-две: Эрви успевала договорить,
        и её же «Сейчас» принималось как слова человека — начинался новый ход,
        новая фраза, и так по кругу. Теперь решает момент записи.
        """
        try:
            t = float(started_at or 0.0)
        except (TypeError, ValueError):
            return False
        if t <= 0:
            return False
        opened = self._self_speech_open
        if opened and self._speaking.is_set() and t >= opened - 0.15:
            return True
        for start, end in list(self._self_speech_spans):
            if start - 0.15 <= t <= end + self.SELF_SPEECH_TAIL:
                return True
        return False

    def _looks_like_recent_echo(self, text: str) -> bool:
        # Сравниваем со ВСЕМИ её фразами за последние 10 секунд, а не с последней.
        # Короткие тоже: прежние пороги в 8–10 символов пропускали «Сейчас»,
        # «Делаю», «Берусь» — а это как раз её отложенные фразы.
        heard = self._normalize(text)
        if not heard:
            return False
        now = time.monotonic()
        for said_at, spoken in list(self._recent_spoken):
            if not spoken or now - said_at > 10.0:
                continue
            if heard == spoken:
                return True
            if len(heard) >= 10 and (heard in spoken or spoken in heard):
                return True
            if len(heard) >= 5 and SequenceMatcher(None, heard, spoken).ratio() >= 0.84:
                return True
        return False

    def _duplicate_command(self, text: str, now: float) -> bool:
        command = self._normalize(text)
        if not command or command != self._last_accepted_command:
            return False
        age = now - self._last_accepted_at
        busy = self._generation_active.is_set() or self._speaking.is_set()
        try:
            busy = busy or bool(self.services.voice.status().get("synthesis_active"))
        except Exception:
            pass
        return age <= (8.0 if busy else 2.0)

    def _wake_variants(self) -> set[str]:
        # Voice activation follows the canonical product name. ASR tolerance
        # is handled below by phonetic/fuzzy matching against this configured name; no old
        # The canonical product wake name is permanent across upgrades.
        wake=self._normalize(self._wake_phrase())
        return {wake} if wake else {"эрви"}

    def _wake_phrase(self) -> str:
        return CANONICAL_ASSISTANT_NAME

    def _strip_leading_wakes(self, text: str) -> str:
        value = self._normalize(text)
        variants = sorted(self._wake_variants(), key=len, reverse=True)
        changed = True
        while changed and value:
            changed = False
            for wake in variants:
                if value == wake:
                    return ""
                if value.startswith(wake + " "):
                    value = value[len(wake):].strip()
                    changed = True
                    break
        return value

    def _extract_after_wake(self, text: str) -> tuple[bool, str]:
        normalized = self._normalize(text)
        for wake in sorted(self._wake_variants(), key=len, reverse=True):
            pattern = r"(?:^|\s)" + re.escape(wake) + r"(?:$|\s)"
            match = re.search(pattern, normalized)
            if match:
                before = normalized[:match.start()].strip()
                tail = normalized[match.end():].strip()
                tail = re.sub(r"^(?:привет|здравствуй|слушай|пожалуйста|эй)\s+", "", tail).strip()
                before = re.sub(r"^(?:привет|здравствуй|слушай|пожалуйста|эй)\s+", "", before).strip()
                # Name may naturally appear at the beginning, middle or end. Prefer the
                # text after the name; if the user put the configured name at the end, use
                # the meaningful part before it rather than treating this as a greeting.
                command = self._strip_leading_wakes(tail or before)
                return True, command

        # ASR can turn a custom name into a phonetically close word. Compare tokens near
        # a greeting and at the start of a command, but keep the threshold high enough to
        # avoid waking on normal background speech.
        words = normalized.split()
        variants = [v.replace(" ", "") for v in self._wake_variants()]
        for index, word in enumerate(words[:5]):
            # Greeting/service words are context, never candidates for the wake name.
            # Otherwise adding ASR-tolerant forms such as «эйрви» can make «привет»
            # accidentally pass the fuzzy similarity threshold.
            if word in {"привет", "здравствуй", "здравствуйте", "эй", "слушай", "пожалуйста"}:
                continue
            compact = word.replace(" ", "")
            score = max((SequenceMatcher(None, compact, v).ratio() for v in variants), default=0.0)
            greeting_near = any(w in {"привет", "здравствуй", "эй", "слушай"} for w in words[max(0, index-2):index+1])
            threshold = 0.56 if greeting_near else (0.72 if index == 0 and len(compact) >= 4 else 0.78)
            if score >= threshold:
                tail_words = words[index + 1:]
                return True, " ".join(tail_words).strip()
        return False, normalized

    def _extract_explicit_wake(self, text: str) -> tuple[bool, str]:
        """Strip the assistant name only when it is explicitly present.

        Once the session is activated we must never use fuzzy wake matching on normal
        command words. In r15 the word "привет" inside "Напиши привет человеку..."
        was fuzzily mistaken for the assistant name and the verb/message were deleted.
        """
        normalized = self._normalize(text)
        for wake in sorted(self._wake_variants(), key=len, reverse=True):
            # Match whole normalized words/phrases, not an arbitrary substring.
            pattern = r"(?:^|\s)" + re.escape(wake) + r"(?:$|\s)"
            match = re.search(pattern, normalized)
            if not match:
                continue
            before = normalized[:match.start()].strip()
            tail = normalized[match.end():].strip()
            before = re.sub(r"^(?:привет|здравствуй|слушай|пожалуйста|эй)\s+", "", before).strip()
            tail = re.sub(r"^(?:привет|здравствуй|слушай|пожалуйста|эй)\s+", "", tail).strip()
            return True, self._strip_leading_wakes(tail or before)
        return False, normalized

    def _activation_route(
        self,
        text: str,
        speech_started_at: float,
        now: float | None = None,
        *,
        playback_seen: bool = False,
        near_field: bool = True,
    ) -> dict[str, Any]:
        """Открытый микрофон: включённый переключатель — и есть согласие слушать.

        Обращение «Эрви» не требуется никогда. Отсеивается только то, что пришло из
        колонок: её собственный голос (по времени записи) и звук с экрана — фраза,
        которая в микрофоне не громче фона колонок (near_field=False). Имя, если его
        назвать, работает по-прежнему: «Эрви, стоп» перебивает её в любой момент.
        """
        now = time.monotonic() if now is None else float(now)
        # Открытый микрофон: включённый переключатель микрофона и есть согласие
        # слушать — обращение «Эрви» не нужно, как и обещает подсказка в интерфейсе.
        # Раньше «играет медиа» определялось в том числе по заголовку активного окна:
        # открытая вкладка YouTube, даже на паузе, — и Эрви требовала «Эрви». Теперь
        # экран считается звучащим только по реальному звуку из колонок.
        screen_audible = self._screen_level() > 0.0 or bool(
            playback_seen and float(getattr(self, "_render_output_peak", 0.0) or 0.0) > 0.02
        )
        eirven_speaking = self._speaking.is_set() or self._self_speech_at(speech_started_at)
        has_wake, tail = self._extract_explicit_wake(text)
        if not has_wake and (screen_audible or eirven_speaking):
            # На шумном фоне распознаватель искажает имя («эйрви») — ищем мягче.
            has_wake, tail = self._extract_after_wake(text)
        if has_wake and not tail:
            return {"action": "arm", "has_wake": True, "command": ""}
        if has_wake:
            return {"action": "accept", "has_wake": True, "command": tail}
        if eirven_speaking:
            # Её собственный голос из колонок. Перебить её можно по имени: «Эрви, стоп».
            return {"action": "ignore", "has_wake": False, "command": "", "reason": "own_voice"}
        if screen_audible and not near_field:
            # Фраза звучит на уровне колонок — это экран: видео, песня, чужой голос.
            return {"action": "ignore", "has_wake": False, "command": "", "reason": "screen_audio"}
        return {
            "action": "accept",
            "has_wake": False,
            "command": self._normalize(text),
        }

    def _is_emergency_cancel(self, text: str) -> bool:
        """Emergency cancellation deliberately bypasses the wake-name contract."""
        clean = self._strip_leading_wakes(text)
        clean = re.sub(r"^(?:первая|прямая)\s+(?=отмена$)", "", clean).strip()
        return bool(re.fullmatch(
            r"(?:стоп|отмена|отменить|отмени|остановись|останови|остановить|прекрати|хватит|"
            r"отмена\s+(?:задачи|всего)|отмени\s+(?:задачу|все|всё)|"
            r"останови\s+(?:задачу|все|всё))",
            clean,
        ))

    def emergency_cancel(self, heard_text: str = "") -> dict[str, int]:
        """Stop speech, generation, GUI work and persistent missions as one operation."""
        self._barge_in.set()
        self._next_turn()
        self._active_until = 0.0
        self._session_activated = False
        try:
            if self._conversation_id:
                self.services.chat.stop(self._conversation_id)
        except Exception:
            pass
        cancelled = 0
        try:
            runtime = getattr(self.services, "runtime", None)
            result = runtime.stop_all() if runtime is not None else {"cancelled": 0}
            cancelled = int(result.get("cancelled") or 0)
        except Exception:
            pass
        for workflow_name in ("universal_workflow", "autonomous_workflow"):
            workflow = getattr(self.services, workflow_name, None)
            try:
                if workflow is not None and self._conversation_id:
                    workflow._clear_pending(self._conversation_id)
            except Exception:
                pass
        for target in (self._utterance_q, self._audio_q):
            try:
                while True:
                    target.get_nowait()
            except queue.Empty:
                pass
        try:
            runtime = getattr(self.services, "runtime", None)
            if runtime is not None:
                runtime.voice_activity_resolved(accepted=True)
        except Exception:
            pass
        self._generation_active.clear()
        self._state = "listening"
        log_event(
            self.services.settings.root_dir, "VOICE_EMERGENCY_CANCEL",
            text=str(heard_text or "")[:180], cancelled=cancelled,
        )
        # This phrase is synthesized into the startup cache, so acknowledgement is not
        # held behind the task which has just been cancelled.
        self.say("Остановила.", emotion="calm")
        return {"cancelled": cancelled}

    def _is_activation_phrase(self, text: str) -> bool:
        """Require a greeting only for the first wake of a conversation window."""
        normalized = self._normalize(text)
        words = set(normalized.split())
        greetings = {"привет", "здравствуй", "здравствуйте", "доброе", "добрый", "добрыйдень", "хай", "hello"}
        if words.intersection(greetings):
            return True
        # Common ASR punctuation/spacing variants of a greeting plus the configured name.
        return bool(re.search(r"\bпривет\w{0,4}\b", normalized))

    def _emotion(self, text: str, duration: float, energy: float, wav_bytes: bytes = b"") -> str:
        textual = self.services.identity.infer_emotion(text)
        affect = analyze_speech_affect(
            text,
            duration=duration,
            energy=energy,
            noise_floor=self._noise_floor,
            wav_bytes=wav_bytes,
            textual_emotion=textual,
        )
        self._last_affect = affect.to_dict()
        try:
            cognition = getattr(self.services, "cognition", None)
            if cognition is not None:
                cognition.update_mood(affect.emotion, float(affect.confidence))
        except Exception:
            pass
        return affect.emotion

    def _response_emotion(self, text: str, user_emotion: str) -> str:
        try:
            identity = self.services.identity.get()
            if identity.emotion_mode != "auto":
                return identity.emotion_mode
            if identity.voice_mode != "natural":
                return identity.voice_mode
            commentary = str(getattr(identity, "action_commentary", "adaptive"))
            inferred = self.services.identity.infer_emotion(text)
            if inferred != "natural":
                return inferred
            if user_emotion == "sad":
                return "empathetic"
            if user_emotion == "tired":
                return "warm"
            if user_emotion == "concerned":
                return "calm"
            if user_emotion == "amused":
                return "amused"
            if commentary == "playful":
                return "amused"
            # Carry a strong, recent user affect into an otherwise neutral answer.
            # This keeps a supportive reply supportive even when the language model
            # writes a factual sentence without an explicit emotional keyword.
            try:
                cognition = getattr(self.services, "cognition", None)
                mood = cognition.mood() if cognition is not None else {}
                mood_emotion = str(mood.get("emotion") or "natural")
                mood_strength = float(mood.get("strength") or 0.0)
                if mood_strength >= 0.42:
                    continuity = {
                        "sad": "empathetic",
                        "tired": "warm",
                        "concerned": "calm",
                        "amused": "amused",
                        "energetic": "warm",
                    }.get(mood_emotion)
                    if continuity:
                        return continuity
            except Exception:
                pass
            style = self.services.style.get()
            style_humor = str(getattr(style, "humor", "") or "").casefold()
            if commentary == "adaptive" and any(mark in style_humor for mark in ("игрив", "живой", "юмор")) and "без юмора" not in style_humor:
                return "energetic"
            if bool(getattr(style, "emotional_support", True)) and any(w in text.casefold() for w in ("держись", "понимаю", "рядом", "спокой", "не переж", "рада", "слышать")):
                return "warm"
        except Exception:
            pass
        return user_emotion if user_emotion in {
            "energetic", "quiet", "warm", "calm", "strict", "amused", "sad",
            "empathetic", "curious", "concerned", "proud", "tired",
        } else "natural"

    def reset_pipeline(self) -> None:
        """Drop stale speech/model work while keeping the microphone service alive."""
        self._barge_in.set()
        self._next_turn()
        try:
            if self._conversation_id:
                self.services.chat.stop(self._conversation_id)
        except Exception:
            pass
        for q in (self._utterance_q, self._audio_q):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        self._generation_active.clear()
        self._speaking.clear()
        self._state = "listening"
        log_event(self.services.settings.root_dir, "VOICE_PIPELINE_RESET")

    def _next_turn(self) -> int:
        with self._turn_lock:
            self._turn_serial += 1
            return self._turn_serial

    def _is_current_turn(self, turn_id: int | None) -> bool:
        if turn_id is None:
            return True
        with self._turn_lock:
            return turn_id == self._turn_serial

    def _command_loop(self) -> None:
        while not self._stop.is_set():
            try:
                wav, duration, energy, speech_started_at, playback_seen, peak_energy = self._utterance_q.get(timeout=0.3)
            except queue.Empty:
                continue
            # Latest speech wins. If several utterances accumulated while ASR/model work
            # was busy, discard stale audio instead of answering old requests seconds later.
            dropped = 0
            while True:
                try:
                    wav, duration, energy, speech_started_at, playback_seen, peak_energy = self._utterance_q.get_nowait()
                    dropped += 1
                except queue.Empty:
                    break
            if dropped:
                log_event(self.services.settings.root_dir, "VOICE_DROP_STALE", count=dropped)
            try:
                try:
                    onboarding_complete = bool(self.services.identity.get().onboarding_completed)
                except Exception:
                    onboarding_complete = True
                if not onboarding_complete:
                    self._state = "onboarding"
                    try:
                        runtime = getattr(self.services, "runtime", None)
                        if runtime is not None:
                            runtime.voice_activity_resolved(accepted=False)
                    except Exception:
                        pass
                    continue

                ready = getattr(self.services.voice, "interactive_ready", None)
                if callable(ready) and not ready():
                    self._state = "warming"
                    warm_started = time.monotonic()
                    log_event(self.services.settings.root_dir, "VOICE_QUEUE_WARMING", duration=round(duration, 3), energy=round(energy, 6))
                    waiter = getattr(self.services.voice, "wait_until_ready", None)
                    if not callable(waiter) or not waiter(180.0):
                        try:
                            runtime = getattr(self.services, "runtime", None)
                            if runtime is not None: runtime.voice_activity_resolved(accepted=False)
                        except Exception:
                            pass
                        self._state = "listening"
                        continue
                    log_event(self.services.settings.root_dir, "VOICE_WARM_READY", wait_ms=round((time.monotonic()-warm_started)*1000))
                    # The owner may repeat a phrase while startup is warming. Re-sample
                    # the queue *after* readiness too, so only the latest recording is
                    # transcribed and the earlier copy cannot answer several seconds later.
                    postwarm_dropped = 0
                    while True:
                        try:
                            wav, duration, energy, speech_started_at, playback_seen, peak_energy = self._utterance_q.get_nowait()
                            postwarm_dropped += 1
                        except queue.Empty:
                            break
                    if postwarm_dropped:
                        log_event(self.services.settings.root_dir, "VOICE_DROP_POSTWARM_STALE", count=postwarm_dropped)
                self._state = "recognizing"
                asr_started = time.monotonic()
                playback_guard = bool(playback_seen or self._foreground_media_kind())
                try:
                    text = self.services.voice.transcribe_bytes(
                        wav, ".wav", allow_fallback=not playback_guard,
                    ).strip()
                except TypeError:
                    # Lightweight test/custom voice adapters from older releases may not
                    # expose the policy keyword yet.
                    text = self.services.voice.transcribe_bytes(wav, ".wav").strip()
                self._last_activity_at = time.monotonic()
                asr_ms=(time.monotonic()-asr_started)*1000
                log_event(self.services.settings.root_dir, "VOICE_HEARD", text=text, duration=round(duration, 3), energy=round(energy, 6), asr_ms=round(asr_ms), playback_guard=playback_guard)
                try:
                    if getattr(self.services,"runtime",None) is not None:
                        self.services.runtime.record_perf("asr",asr_ms,engine=self.services.settings.asr_engine)
                except Exception:
                    pass
                if not text or text.casefold().startswith("речь не распознана"):
                    try:
                        runtime = getattr(self.services, "runtime", None)
                        if runtime is not None: runtime.voice_activity_resolved(accepted=False)
                    except Exception: pass
                    self._state = "listening"
                    continue
                self._last_text = text
                if self._is_emergency_cancel(text):
                    self.emergency_cancel(text)
                    continue
                now = time.monotonic()
                text = self._split_glued_wake(text)
                near_field = self._near_field(energy, peak_energy)
                screen_level = self._screen_level()
                if screen_level > 0.0 or playback_guard:
                    # Замеры для подстройки порогов под конкретный микрофон.
                    log_event(self.services.settings.root_dir, "VOICE_SCREEN_CHECK", text=text[:80],
                              energy=round(float(energy or 0.0), 5), peak=round(float(peak_energy or 0.0), 5),
                              screen_level=round(screen_level, 5), near_field=near_field)
                route = self._activation_route(
                    text, speech_started_at, now, playback_seen=playback_guard, near_field=near_field,
                )
                has_wake = bool(route.get("has_wake"))
                if (
                    not has_wake
                    and not self._session_activated
                    and route.get("action") == "accept"
                    and not self._owner_speech_gate(
                        duration, energy, self._noise_floor, playback_guard,
                        peak_energy,
                    )
                ):
                    try:
                        runtime = getattr(self.services, "runtime", None)
                        if runtime is not None: runtime.voice_activity_resolved(accepted=False)
                    except Exception:
                        pass
                    self._state = "listening"
                    log_event(
                        self.services.settings.root_dir,
                        "VOICE_IGNORED_LOW_ENERGY",
                        text=text[:120],
                        duration=round(duration, 3),
                        energy=round(energy, 6),
                        noise_floor=round(self._noise_floor, 6),
                        playback_guard=playback_guard,
                    )
                    continue
                # Запись началась, пока звучал её собственный динамик, и обращения
                # «Эрви» нет — это её же голос. Обращение по имени по-прежнему
                # проходит всегда: «Эрви, стоп» перебивает её в любой момент.
                if not has_wake and self._self_speech_at(speech_started_at):
                    try:
                        runtime = getattr(self.services, "runtime", None)
                        if runtime is not None: runtime.voice_activity_resolved(accepted=False)
                    except Exception: pass
                    self._state = "listening"
                    log_event(self.services.settings.root_dir, "VOICE_IGNORED_SELF_SPEECH", text=text[:120])
                    continue
                # Echo rejection is a heuristic, never an authority boundary. A named
                # command and emergency stop must survive even when it overlaps words
                # that EIRVEN spoke moments ago.
                if self._looks_like_recent_echo(text) and not has_wake:
                    try:
                        runtime = getattr(self.services, "runtime", None)
                        if runtime is not None: runtime.voice_activity_resolved(accepted=False)
                    except Exception: pass
                    self._state = "listening"
                    log_event(self.services.settings.root_dir, "VOICE_IGNORED_ECHO", text=text)
                    continue

                # The visible microphone switch is the activation boundary.  When it is
                # on, a complete high-confidence utterance is a command; when off, this
                # daemon is stopped and no audio is processed.
                if route.get("action") == "ignore":
                    try:
                        self.services.runtime.voice_activity_resolved(accepted=False)
                    except Exception:
                        pass
                    self._state = "listening"
                    self._active_until = 0.0
                    self._session_activated = False
                    log_event(
                        self.services.settings.root_dir,
                        "VOICE_IGNORED_NOT_ACTIVATED",
                        text=text,
                        wake_detected=False,
                        reason=str(route.get("reason") or ""),
                    )
                    continue

                # Saying only "Эрви" arms the next utterance. There is deliberately no
                # spoken acknowledgement; the living sphere is the acknowledgement.
                if route.get("action") == "arm":
                    self._session_activated = True
                    self._active_until = now + self._session_seconds()
                    self._state = "armed"
                    try:
                        self.services.runtime.voice_activity_resolved(accepted=True)
                    except Exception:
                        pass
                    log_event(
                        self.services.settings.root_dir,
                        "VOICE_ARMED",
                        seconds=self._session_seconds(),
                    )
                    continue

                command_text = str(route.get("command") or text).strip()
                self._active_until = 0.0
                self._session_activated = False
                if self._duplicate_command(command_text, now):
                    try:
                        self.services.runtime.voice_activity_resolved(accepted=False)
                    except Exception:
                        pass
                    self._state = "thinking" if self._generation_active.is_set() else ("speaking" if self._speaking.is_set() else "listening")
                    log_event(self.services.settings.root_dir, "VOICE_IGNORED_DUPLICATE", text=text, tail=command_text, age_ms=round((now-self._last_accepted_at)*1000))
                    continue
                self._last_accepted_command = self._normalize(command_text)
                self._last_accepted_at = now
                turn_id = self._next_turn()
                log_event(
                    self.services.settings.root_dir,
                    "VOICE_ACCEPT",
                    text=text,
                    wake=has_wake,
                    tail=command_text,
                    turn_id=turn_id,
                )
                # A new human turn owns the output channel immediately. Stop previous text
                # generation and any currently playing/queued stale TTS.
                self._barge_in.set()
                try:
                    if self.services.voice.cancel_synthesis():
                        log_event(self.services.settings.root_dir, "TTS_PREEMPTED", turn_id=turn_id)
                except Exception as exc:
                    log_event(self.services.settings.root_dir, "TTS_PREEMPT_ERROR", turn_id=turn_id, error=str(exc)[:300])
                tail = command_text
                emotion = self._emotion(text, duration, energy, wav)
                self._last_emotion = emotion
                self.services.db.set_setting("last_voice_emotion", emotion)
                self.services.db.set_setting("last_voice_affect", dict(self._last_affect))
                if self._conversation_id:
                    try:
                        self.services.chat.stop(self._conversation_id)
                    except Exception:
                        pass
                # The old turn has now received its stop token.  Re-open the commit gate;
                # any old side-effect waiter will observe that stop token, while the newly
                # accepted turn starts with a clean gate.
                try:
                    runtime = getattr(self.services, "runtime", None)
                    if runtime is not None: runtime.voice_activity_resolved(accepted=True)
                except Exception: pass
                threading.Thread(
                    target=self._handle_turn,
                    args=(text, has_wake, tail, emotion, turn_id),
                    daemon=True,
                    name="eirven-native-voice-turn",
                ).start()
            except Exception as exc:
                try:
                    runtime = getattr(self.services, "runtime", None)
                    if runtime is not None: runtime.voice_activity_resolved(accepted=False)
                except Exception: pass
                self._last_error = str(exc)[:500]
                self._state = "listening"

    def _notification_loop(self) -> None:
        terminal = {"done", "partial", "failed", "waiting_user", "cancelled"}
        while not self._stop.wait(1.0):
            conversation_id = self._conversation_id
            if not bool(self.services.db.get_setting("notifications_enabled", True)):
                continue
            if not conversation_id or self._speaking.is_set() or self._generation_active.is_set():
                continue
            try:
                tasks = self.services.tasks.list(limit=60)
            except Exception:
                continue
            for task in reversed(tasks):
                task_id = str(task.get("id") or "")
                status = str(task.get("status") or "")
                if not task_id:
                    continue
                previous = self._announced_task_state.get(task_id)
                self._announced_task_state[task_id] = status
                if task.get("conversation_id") != conversation_id or status not in terminal or previous == status:
                    continue
                if task.get("kind") in getattr(self.services.tasks, "FAST_KINDS", set()) and status == "done":
                    continue
                title = str(task.get("title") or "задача").strip()
                if status == "done":
                    text = f"Задача «{title}» завершена; проверка результата пройдена."
                elif status == "partial":
                    detail = str(task.get("error") or "постусловие не подтвердилось").strip()[:260]
                    text = f"Задача «{title}» выполнена частично. {detail}"
                elif status == "waiting_user":
                    detail = str(task.get("current_step") or "нужно твоё действие").strip()
                    text = f"По задаче «{title}» нужно твоё действие. {detail}"
                elif status == "failed":
                    detail = str(task.get("error") or "не удалось завершить задачу").strip()[:260]
                    text = f"По задаче «{title}» ошибка. {detail}"
                else:
                    text = f"Задача «{title}» остановлена."
                self.say(text)
                break

    def _owner_address(self, identity) -> str:
        explicit = str(getattr(identity, "user_address", "") or "").strip()
        if explicit:
            return explicit
        try:
            with self.services.db.connect() as conn:
                rows = conn.execute(
                    "SELECT content FROM messages WHERE role='user' ORDER BY id DESC LIMIT 240"
                ).fetchall()
            pattern = re.compile(
                r"(?:меня зовут|зови меня|называй меня|обращайся ко мне как)\s+([а-яёa-z][а-яёa-z0-9_-]{1,31}(?:\s+[а-яёa-z][а-яёa-z0-9_-]{1,31})?)",
                re.IGNORECASE,
            )
            for row in rows:
                match = pattern.search(str(row["content"] or ""))
                if match:
                    return match.group(1).strip()[:48]
        except Exception:
            pass
        return "бро"

    def _wake_greeting(self, identity) -> str:
        address = self._owner_address(identity)
        suffix = "Я на связи. Что делаем?"
        try:
            active = next(
                (
                    t for t in self.services.tasks.list(limit=30)
                    if t.get("status") in {"running", "queued", "waiting_user"}
                    and (not self._conversation_id or t.get("conversation_id") == self._conversation_id)
                ),
                None,
            )
            if active:
                title = str(active.get("title") or "текущая задача").strip()
                suffix = (
                    f"По «{title}» жду твоё действие. Можешь сказать, когда продолжать."
                    if active.get("status") == "waiting_user"
                    else f"«{title}» уже в работе. Докидывай правки или параллельную команду."
                )
        except Exception:
            pass
        return f"Привет, {address}. {suffix}"

    @staticmethod
    def _pop_speakable(buffer: str, *, force: bool = False) -> tuple[list[str], str]:
        """Return complete short speech segments and the unfinished tail."""
        clean=buffer
        chunks=[]
        while True:
            match=re.search(r"^(.{12,180}?[.!?…])(?:\s+|$)",clean,re.S)
            if not match:
                if len(clean)>180:
                    cut=max(clean.rfind(',',0,170),clean.rfind(';',0,170),clean.rfind(':',0,170),clean.rfind(' ',0,170))
                    if cut>=45:
                        chunks.append(clean[:cut+1].strip()); clean=clean[cut+1:].lstrip(); continue
                break
            chunks.append(match.group(1).strip()); clean=clean[match.end():]
        if force and clean.strip():
            chunks.append(clean.strip()); clean=''
        return chunks,clean

    def _stream_chat_to_voice(self, query: str, emotion: str, turn_id: int) -> tuple[str,str]:
        """Generate and synthesize concurrently; newest owner turn still preempts both."""
        # Do not drop the end of a long answer merely because generation is faster than
        # Baya. A new owner turn still invalidates the turn id and stops the speaker.
        speech_q: queue.Queue[str|None]=queue.Queue()
        answer=''; cid=self._conversation_id or ''
        def speaker() -> None:
            while self._is_current_turn(turn_id) and not self._stop.is_set():
                try: part=speech_q.get(timeout=.25)
                except queue.Empty: continue
                if part is None: break
                
                if part.strip():
                    spoken=part.strip()
                    try: spoken=self.services.chat.enforce_gender(spoken)
                    except Exception: pass
                    self._speak(spoken,emotion,turn_id)
        thread=threading.Thread(target=speaker,daemon=True,name=f'eirven-voice-stream-{turn_id}')
        thread.start()
        pending=''
        generated=''
        self._generation_active.set()
        try:
            for event in self.services.chat.stream_events(query,cid or None,mode='Друг'):
                if not self._is_current_turn(turn_id): break
                if event.get('type')=='start':
                    cid=str(event.get('conversation_id') or cid)
                    self._turn_actions[turn_id]=str((event.get('route') or {}).get('action') or 'chat')
                elif event.get('type')=='bridge':
                    # Реплика моста встаёт в ту же очередь, что и ответ, и первой.
                    # Раньше она шла через say() в отдельном потоке — и это давало
                    # две ошибки. Порядок не гарантировался: на быстрой музыке
                    # ответ «Включила» мог прозвучать раньше «Включаю». А say()
                    # передавал turn_id=None, который считается всегда текущим,
                    # поэтому при перебивании «Как настроение?» всё равно
                    # договаривалось невпопад. Очередь speech_q читает поток с
                    # настоящим turn_id — он и отменит реплику при новом ходе.
                    line=str(event.get('text') or '').strip()
                    question=str(event.get('question') or '').strip()
                    with self._cue_lock:
                        already_cued = turn_id in self._cued_turns
                        self._bridged_turns.add(turn_id)
                    # Если «Берусь» уже прозвучало, подтверждение от моста было бы
                    # вторым подряд — говорим только встречный вопрос.
                    if already_cued:
                        line = question
                    if line:
                        speech_q.put(line)
                        log_event(self.services.settings.root_dir, "VOICE_BRIDGE_SPOKEN", turn_id=turn_id,
                                  question_only=already_cued)
                elif event.get('type')=='token':
                    self._turn_tokens.add(turn_id)
                    full=str(event.get('full') or '')
                    delta=str(event.get('content') or '')
                    if full:
                        if full.startswith(generated):
                            speech_delta=full[len(generated):]
                        elif not generated:
                            speech_delta=full
                        else:
                            speech_delta=delta
                        generated=full
                    else:
                        speech_delta=delta
                        generated += delta
                    answer=generated
                    pending += speech_delta
                    ready,pending=self._pop_speakable(pending)
                    for part in ready:
                        speech_q.put(part)
                elif event.get('type')=='done':
                    final_answer=str(event.get('answer') or answer)
                    # Some streaming providers place the final suffix only in ``done``.
                    # Queue it before the forced flush so visible and spoken text match.
                    if final_answer.startswith(generated):
                        pending += final_answer[len(generated):]
                    elif final_answer and not generated:
                        pending += final_answer
                    generated=final_answer or generated
                    answer=generated
                elif event.get('type')=='error' and not answer:
                    answer=str(event.get('message') or '')
            ready,pending=self._pop_speakable(pending,force=True)
            for part in ready:
                if self._is_current_turn(turn_id):
                    speech_q.put(part)
        finally:
            self._generation_active.clear()
            speech_q.put(None)
            thread.join(timeout=35.0)
        return answer,cid

    def _handle_turn(self, text: str, has_wake: bool, tail: str, emotion: str, turn_id: int) -> None:
        started = time.monotonic()
        try:
            if not self._is_current_turn(turn_id):
                return
            identity = self.services.identity.get()
            query = tail if has_wake and tail else text
            streamed = False
            if has_wake and not tail:
                answer = self._wake_greeting(identity)
            else:
                # Camera/developer-mode commands are deterministic and must never wait
                # for a local language model that is busy building a project.
                handled = False
                if self.services.modes is not None:
                    try:
                        handled, answer, _meta = self.services.modes.handle(query)
                    except Exception:
                        handled = False
                streamed = False
                if not handled:
                    # r21.1: no spoken filler before actions. The living sphere is the
                    # acknowledgement; saying «Да» used to add ~1 s of TTS latency before
                    # the actual deterministic route even started.
                    if not self._is_current_turn(turn_id):
                        return
                    self._state = "thinking"
                    # Реплика о ходе работы — только если задача действительно
                    # затянулась. Раньше её говорили сразу перед действием, и это
                    # добавляло секунду задержки на каждую команду; здесь таймер
                    # срабатывает лишь когда ответа всё ещё нет, то есть ровно
                    # тогда, когда тишина начинает читаться как зависание.
                    cue_done = threading.Event()
                    cue_started = time.monotonic()

                    def _late_cue() -> None:
                        # Фраза «Берусь» нужна только ДЕЙСТВИЮ, у которого ответа ещё нет.
                        # Раньше она звучала через 2,2 с в любом ходе — и на «Привет»
                        # Эрви отвечала «Берусь», ещё не поняв, что это просто разговор.
                        if cue_done.is_set() or not self._is_current_turn(turn_id) or self._stop.is_set():
                            return
                        action = self._turn_actions.get(turn_id)
                        if action is None:
                            # Ещё не ясно, задача это или разговор: ждём маршрут, но
                            # не дольше 15 секунд.
                            if time.monotonic() - cue_started < 15.0:
                                retry = threading.Timer(0.5, _late_cue)
                                retry.daemon = True
                                retry.start()
                            return
                        if action in {"chat", "conversation", ""}:
                            return
                        if turn_id in self._turn_tokens or self._speaking.is_set():
                            return
                        # Мост уже сказал «Включаю» — второе подтверждение было бы повтором.
                        with self._cue_lock:
                            if turn_id in self._bridged_turns:
                                return
                            self._cued_turns.add(turn_id)
                        try:
                            self.say(voice_cues.start_cue(), "calm")
                            log_event(self.services.settings.root_dir, "VOICE_PROGRESS_CUE", turn_id=turn_id, action=action)
                        except Exception:
                            pass
                    cue_timer = threading.Timer(2.2, _late_cue)
                    cue_timer.daemon = True
                    cue_timer.start()
                    try:
                        answer, cid = self._stream_chat_to_voice(query, emotion, turn_id)
                    finally:
                        # Ход закончен — никакая отложенная фраза после ответа не прозвучит.
                        cue_done.set()
                        cue_timer.cancel()
                        self._turn_actions.pop(turn_id, None)
                        self._turn_tokens.discard(turn_id)
                    streamed = True
                    self._conversation_id = str(cid or self._conversation_id)
                    if self._conversation_id:
                        self.services.db.set_setting("native_voice_conversation", self._conversation_id)
                    answer = str(answer or "").strip()
            try:
                answer = self.services.chat.enforce_gender(answer)
            except Exception:
                pass
            if not self._is_current_turn(turn_id):
                # The reply was fully generated and then dropped without ever being
                # spoken. Record it: the trace previously ended at RUNTIME_END with a
                # good answer and simply no VOICE_ANSWER, which gave nothing to work from.
                log_event(
                    self.services.settings.root_dir, "VOICE_TURN_SUPERSEDED",
                    turn_id=turn_id, current_turn=self._turn_serial,
                    answer_chars=len(str(answer or "")),
                    think_ms=round((time.monotonic() - started) * 1000),
                )
                return
            if not answer:
                # Never fail silently: diagnostics are much easier when the owner hears a
                # short explicit failure instead of watching thinking -> listening.
                answer = "Я на связи, но этот ответ получился пустым. Скажи ещё раз — я не буду молчать."
            if answer and not self._stop.is_set():
                # Короткое подтверждение после долгой работы: голосом человек не
                # видит экрана, и без «Готово» непонятно, закончилось ли действие.
                elapsed = time.monotonic() - started
                if elapsed >= 4.0 and not streamed:
                    try:
                        low = answer.casefold()
                        failed = any(w in low for w in ("не получилось", "не вышло",
                                                        "не подтвердил", "не справилась"))
                        cue = (voice_cues.failed_cue(has_links="http" in low)
                               if failed else voice_cues.done_cue())
                        answer = f"{cue} {answer}" if not failed else answer
                    except Exception:
                        pass
                log_event(self.services.settings.root_dir, "VOICE_ANSWER", turn_id=turn_id, answer=answer[:1200], think_ms=round((time.monotonic()-started)*1000), streamed=streamed)
                if not streamed:
                    self._speak(answer, emotion, turn_id)
            elif answer:
                # The daemon is shutting down while this turn was finishing. Previously
                # the reply vanished here without any record, which is indistinguishable
                # from the assistant deciding not to answer.
                log_event(
                    self.services.settings.root_dir, "VOICE_ANSWER_DROPPED_STOPPING",
                    turn_id=turn_id, answer_chars=len(answer),
                    think_ms=round((time.monotonic() - started) * 1000),
                )
        except Exception as exc:
            self._last_error = str(exc)[:500]
            log_event(self.services.settings.root_dir, "VOICE_ERROR", turn_id=turn_id, error=self._last_error)
        finally:
            if not self._stop.is_set() and not self._speaking.is_set():
                self._state = "listening"

    @staticmethod
    def _resample_output(data: Any, source_rate: int, target_rate: int):
        import numpy as np  # type: ignore
        array = np.asarray(data, dtype=np.float32)
        if source_rate == target_rate or len(array) <= 1:
            return array
        target_n = max(1, int(round(len(array) * float(target_rate) / float(source_rate))))
        x_old = np.linspace(0.0, 1.0, num=len(array), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=target_n, endpoint=False)
        channels = [np.interp(x_new, x_old, array[:, idx]) for idx in range(array.shape[1])]
        return np.stack(channels, axis=1).astype(np.float32)

    @staticmethod
    def _synthesis_chunk(text: str) -> str:
        """Give the speech engine a closing prosody mark without changing visible text."""
        value = str(text or "").strip()
        return value if not value or re.search(r"[.!?…,:;]$", value) else f"{value}."

    @staticmethod
    def _append_playback_tail(data: Any, sample_rate: int, channels: int, seconds: float = .14):
        """Append a silent device-flush tail so Windows does not clip the final phoneme."""
        import numpy as np  # type: ignore
        array = np.asarray(data, dtype=np.float32)
        tail = np.zeros(
            (max(1, int(int(sample_rate) * float(seconds))), max(1, int(channels))),
            dtype=np.float32,
        )
        return np.concatenate((array, tail), axis=0)

    @staticmethod
    def _speech_chunks(text: str, limit: int = 220) -> list[str]:
        """Split long replies so the first audible sentence starts immediately."""
        clean = re.sub(r"\s+", " ", text).strip()
        if not clean:
            return []
        raw_parts = re.split(r"(?<=[.!?…])\s+|(?<=[,;:])\s+(?=.{70,})", clean)
        parts: list[str] = []
        for raw in raw_parts:
            tail = raw.strip()
            while len(tail) > limit:
                window = tail[: limit + 1]
                cut = max(
                    window.rfind(". "), window.rfind("! "), window.rfind("? "),
                    window.rfind(", "), window.rfind("; "), window.rfind(": "),
                    window.rfind(" "),
                )
                if cut < max(36, limit // 2):
                    cut = limit
                part = tail[:cut].strip()
                if part:
                    parts.append(part)
                tail = tail[cut:].strip()
            if tail:
                parts.append(tail)
        chunks: list[str] = []
        current = ""
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if current and len(current) + 1 + len(part) > limit:
                chunks.append(current)
                current = part
            else:
                current = f"{current} {part}".strip()
        if current:
            chunks.append(current)
        return chunks or [clean]

    def _speak(self, text: str, user_emotion: str, turn_id: int | None = None) -> None:
        tts_started=time.monotonic()
        with self._speak_lock:
            if not self._is_current_turn(turn_id):
                return
            self._barge_in.clear()
            log_event(self.services.settings.root_dir, "TTS_BEGIN", turn_id=turn_id, chars=len(text), emotion=user_emotion)
            try:
                sd = self._import_sounddevice()
                import soundfile as sf  # type: ignore
                import numpy as np  # type: ignore
                mode = self._response_emotion(text, user_emotion)
                self._last_response_emotion = mode
                self._speaking_emotion = mode
                self._emotion_changed_at = time.monotonic()
                chunks = self._speech_chunks(text)
                if not chunks:
                    return

                for chunk in chunks:
                    if self._stop.is_set() or self._barge_in.is_set() or not self._is_current_turn(turn_id):
                        break
                    # Do not send emoji/punctuation-only fragments to Silero. r15.6 had a
                    # one-character chunk that failed TTS after the spoken sentence.
                    if not re.search(r"[A-Za-zА-Яа-яЁё0-9]", chunk):
                        continue
                    # Synthesize only the next semantic chunk. This cuts time-to-first-audio
                    # substantially for long answers and keeps interruption responsive.
                    synth_started = time.monotonic()
                    synthesis_text = self._synthesis_chunk(chunk)
                    path = Path(self.services.voice.synthesize(synthesis_text, mode=mode))
                    synth_ms = (time.monotonic() - synth_started) * 1000
                    try:
                        engine = str(self.services.voice.status().get("last_tts_engine") or "")
                    except Exception:
                        engine = ""
                    log_event(self.services.settings.root_dir, "TTS_SYNTH", turn_id=turn_id, chars=len(chunk), engine=engine, synth_ms=round(synth_ms))
                    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
                    if data.size == 0:
                        continue

                    target_rate = int(rate)
                    output_channels = int(data.shape[1])
                    output_device, output_settings = self._preferred_wasapi_device(sd, "output", None)
                    try:
                        output_info = sd.query_devices(output_device, "output")
                        native = int(round(float(output_info.get("default_samplerate") or rate)))
                        if native >= 8_000:
                            target_rate = native
                        max_channels = int(output_info.get("max_output_channels") or output_channels)
                        output_channels = 2 if max_channels >= 2 else 1
                    except Exception:
                        output_channels = max(1, min(2, output_channels))

                    data = self._resample_output(data, int(rate), target_rate)
                    if output_channels == 1 and data.shape[1] > 1:
                        data = np.mean(data, axis=1, keepdims=True, dtype=np.float32)
                    elif output_channels == 2 and data.shape[1] == 1:
                        data = np.repeat(data, 2, axis=1)
                    elif data.shape[1] > output_channels:
                        data = data[:, :output_channels]

                    # Keep the Windows output device alive after the waveform. Without
                    # this flush tail, some drivers clip the final phoneme on close.
                    data = self._append_playback_tail(data, target_rate, output_channels)

                    self._speaking_since = time.monotonic()
                    self._last_spoken_text = self._normalize(chunk)
                    self._last_spoken_at = self._speaking_since
                    if not self._self_speech_open:
                        self._self_speech_open = self._speaking_since
                    self._recent_spoken.append((self._speaking_since, self._last_spoken_text))
                    self._speaking.set()
                    self._state = "speaking"
                    block = max(256, int(target_rate * 0.04))
                    try:
                        with sd.OutputStream(
                            device=output_device, samplerate=target_rate, channels=output_channels,
                            dtype="float32", blocksize=0, extra_settings=output_settings,
                        ) as out:
                            for pos in range(0, len(data), block):
                                if self._stop.is_set() or not self._is_current_turn(turn_id):
                                    break
                                if self._barge_in.is_set():
                                    # Barge-in should feel like a person yielding the floor,
                                    # not an audio cable being pulled. Fade the next ~85 ms.
                                    fade_len = min(max(1, int(target_rate * 0.085)), len(data) - pos)
                                    if fade_len > 0:
                                        fade = np.linspace(1.0, 0.0, fade_len, dtype=np.float32)[:, None]
                                        tail_audio = np.asarray(data[pos:pos + fade_len], dtype=np.float32) * fade
                                        out.write(tail_audio)
                                    break
                                frame = np.asarray(data[pos:pos + block], dtype=np.float32)
                                if frame.size:
                                    rms = float(np.sqrt(np.mean(np.square(frame), dtype=np.float64)))
                                    peak = float(np.max(np.abs(frame)))
                                    syllable = max(0.0, min(1.0, rms * 7.5 + peak * 0.9))
                                    self._speech_level = self._speech_level * 0.32 + syllable * 0.68
                                out.write(frame)
                    except Exception as first:
                        try:
                            sd.play(data, target_rate, blocking=True)
                        except Exception as second:
                            raise RuntimeError(f"Аудиовыход: {first}; fallback: {second}") from second
            except Exception as exc:
                self._last_error = str(exc)[:500]
                log_event(self.services.settings.root_dir, "TTS_ERROR", turn_id=turn_id, error=self._last_error)
            finally:
                tts_ms=(time.monotonic()-tts_started)*1000
                log_event(self.services.settings.root_dir, "TTS_END", turn_id=turn_id, interrupted=self._barge_in.is_set() or not self._is_current_turn(turn_id), tts_ms=round(tts_ms))
                try:
                    if getattr(self.services,"runtime",None) is not None:
                        self.services.runtime.record_perf("tts",tts_ms,chars=len(text),emotion=self._response_emotion(text,user_emotion))
                except Exception:
                    pass
                if self._self_speech_open:
                    self._self_speech_spans.append((self._self_speech_open, time.monotonic()))
                    self._self_speech_open = 0.0
                self._speaking.clear()
                self._speech_level = 0.0
                self._speaking_since = 0.0
                self._speaking_emotion = ""
                self._emotion_changed_at = time.monotonic()
                self._barge_in.clear()
                if not self._stop.is_set():
                    self._state = "thinking" if self._generation_active.is_set() and self._is_current_turn(turn_id) else "listening"
