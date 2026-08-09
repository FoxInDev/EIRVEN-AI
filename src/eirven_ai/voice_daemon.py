from __future__ import annotations

import io
import os
import queue
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

from .trace import log_event


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
        self._utterance_q: queue.Queue[tuple[bytes, float, float, float]] = queue.Queue(maxsize=10)
        self._speaking = threading.Event()
        self._speaking_since = 0.0
        self._barge_in = threading.Event()
        self._generation_active = threading.Event()
        self._speak_lock = threading.Lock()
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
        self._last_emotion = "natural"
        self._noise_floor = 0.0045
        self._input_level = 0.0
        self._input_device = ""
        self._input_rate = 0
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
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "state": visible_state,
            "interactive_ready": bool(getattr(self.services.voice, "interactive_ready", lambda: True)()),
            "speaking": self._speaking.is_set(),
            "session_active": bool(onboarding_complete and now < self._active_until),
            "session_seconds_remaining": round(max(0.0, self._active_until - now), 1),
            "wake_phrase": "эрви",
            "onboarding_complete": onboarding_complete,
            "last_text": self._last_text,
            "last_emotion": self._last_emotion,
            "noise_floor": round(self._noise_floor, 5),
            "input_level": round(self._input_level, 4),
            "input_device": self._input_device,
            "input_rate": self._input_rate,
            "idle_seconds": round(self.idle_seconds(), 2),
            "error": self._last_error,
        }

    def idle_seconds(self) -> float:
        return max(0.0, time.monotonic() - float(self._last_activity_at or 0.0))


    def _session_seconds(self) -> float:
        # r21 interaction contract: "Эрви" arms exactly one following thought for five
        # seconds. If speech starts inside the window, it may continue for the full utterance.
        return self.WAKE_WINDOW_SECONDS

    def _foreground_media_kind(self) -> str:
        """Return foreground media from the same native Win32 source used by tools.

        Do not depend on ProactiveObserver's cached/secondary title lookup: r15.5 accepted
        YouTube speech with wake=False even though the real foreground was YouTube.
        """
        title = ""
        try:
            tools = getattr(self.services, "tools", None)
            if tools is not None:
                row = tools.execute("foreground_window", {})
                if row.get("ok"):
                    title = str((row.get("result") or {}).get("title") or "")
        except Exception:
            title = ""
        try:
            proactive = getattr(self.services, "proactive", None)
            if proactive is not None:
                if not title:
                    title = str(proactive._foreground_title() or "")
                return str(proactive._media_kind(title) or "")
        except Exception:
            pass
        low = title.casefold().replace("ё", "е")
        if any(x in low for x in ("youtube", "ютуб", "twitch", "яндекс музыка", "yandex music", "spotify", "vlc", "kinopoisk", "кинопоиск", "netflix", "okko", "ivi")):
            return "media"
        return ""

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

    def _run(self) -> None:
        try:
            sd = self._import_sounddevice()
            import numpy as np  # type: ignore
        except Exception as exc:
            self._last_error = f"sounddevice: {exc}"
            self._state = "unavailable"
            return

        vad = None
        try:
            import webrtcvad  # type: ignore
            vad = webrtcvad.Vad(2)
        except Exception:
            vad = None

        selected = self.services.db.get_setting("microphone_device", None)
        requested_device = int(selected) if str(selected or "").isdigit() else None
        device = requested_device
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
        except Exception as exc:
            self._last_error = f"Микрофон: {exc}"
            self._state = "unavailable"
            return

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
        speech_blocks = silence_blocks = candidate_blocks = 0
        energy_sum = 0.0
        self._state = "listening"
        try:
            try:
                stream = sd.InputStream(
                    device=device, samplerate=native_rate, channels=1, dtype="float32",
                    blocksize=native_block, latency="low", callback=callback,
                )
            except Exception:
                # Some Windows/WASAPI devices reject explicit low latency/block sizes.
                # Let PortAudio choose a safe native buffer before declaring the mic bad.
                stream = sd.InputStream(
                    device=device, samplerate=native_rate, channels=1, dtype="float32",
                    callback=callback,
                )
            with stream:
                self._last_error = ""
                self._state = "listening"
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

                    if not recording:
                        if not vad_speech and rms < max(0.025, self._noise_floor * 4.2):
                            self._noise_floor = 0.992 * self._noise_floor + 0.008 * max(0.0004, rms)
                        threshold = max(0.0065, self._noise_floor * 2.15)
                        speaking_now = self._speaking.is_set()
                        if speaking_now:
                            # Loudspeaker echo must not make EIRVEN interrupt herself. During
                            # playback, accept barge-in only for a sustained, clearly louder
                            # human utterance and ignore the first 450 ms of speaker onset.
                            threshold = max(0.055, self._noise_floor * 7.0)
                            speaker_age = time.monotonic() - self._speaking_since
                            likely = bool(vad_speech and rms >= threshold and speaker_age >= 0.45)
                            required_blocks = 6  # 180 ms sustained speech
                        else:
                            likely = bool(vad_speech or rms >= threshold)
                            required_blocks = 2 if vad is not None else 3
                        pre_roll.append(block)
                        candidate_blocks = candidate_blocks + 1 if likely else max(0, candidate_blocks - 1)
                        # Outside playback 60–90 ms is enough and avoids eating fast first words.
                        if candidate_blocks >= required_blocks:
                            # Do not stop audible speech on VAD alone. Speaker echo, a paused
                            # video's residual audio, keyboard noise and room speech were cutting
                            # EIRVEN mid-sentence. We keep recording/ASR running and only set
                            # barge_in after the utterance passes wake/session policy below.
                            ambient = getattr(self.services, "ambient", None)
                            if ambient is not None:
                                try:
                                    ambient.duck()
                                except Exception:
                                    pass
                            self._last_activity_at = time.monotonic()
                            recording_started_at = self._last_activity_at
                            if speaking_now:
                                # Sustained near-field human speech is a barge-in candidate.
                                # Fade speech immediately; wake policy is still enforced after ASR.
                                self._barge_in.set()
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
                            candidate_blocks = 0
                            self._state = "hearing"
                        continue

                    recording.append(block)
                    energy_sum += rms
                    speech_threshold = max(0.0055, self._noise_floor * 1.8)
                    if vad_speech or rms >= speech_threshold:
                        speech_blocks += 1
                        silence_blocks = 0
                    else:
                        silence_blocks += 1

                    # Endpoint quickly, but give long/fast utterances a little extra clause pause.
                    # The old hard 900–1050 ms floor added a full second before ASR even began.
                    voiced_ms = speech_blocks * self.BLOCK_MS
                    base_hangover = max(680, min(920, int(self.services.settings.voice_silence_ms) + 100))
                    hangover_ms = base_hangover
                    if voiced_ms >= 3500:
                        hangover_ms += 180
                    if voiced_ms >= 8000:
                        hangover_ms += 120
                    max_blocks = int(35_000 / self.BLOCK_MS)
                    if silence_blocks * self.BLOCK_MS >= hangover_ms or len(recording) >= max_blocks:
                        duration = len(recording) * self.BLOCK_MS / 1000.0
                        speech_ms = speech_blocks * self.BLOCK_MS
                        if speech_ms >= 150 and duration >= 0.28:
                            wav = self._wav_bytes(recording)
                            avg_energy = energy_sum / max(1, len(recording))
                            try:
                                self._utterance_q.put_nowait((wav, duration, avg_energy, recording_started_at))
                            except queue.Full:
                                try:
                                    self._utterance_q.get_nowait()
                                    self._utterance_q.put_nowait((wav, duration, avg_energy, recording_started_at))
                                except queue.Empty:
                                    pass
                        recording = []
                        recording_started_at = 0.0
                        pre_roll.clear()
                        speech_blocks = silence_blocks = 0
                        energy_sum = 0.0
                        self._state = "listening"
        except Exception as exc:
            self._last_error = str(exc)[:500]
            self._state = "error"

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[^a-zа-яё0-9]+", " ", text.casefold()).strip()

    def _wake_variants(self) -> set[str]:
        # The wake phrase is intentionally independent from the display name. The user may
        # rename the assistant during onboarding, but voice activation stays predictable.
        return {"эрви", "эрве", "эрвей", "эрби", "эйрви", "эйрве"}

    def _extract_after_wake(self, text: str) -> tuple[bool, str]:
        normalized = self._normalize(text)
        for wake in sorted(self._wake_variants(), key=len, reverse=True):
            pos = normalized.find(wake)
            if pos >= 0:
                before = normalized[:pos].strip()
                tail = normalized[pos + len(wake):].strip()
                tail = re.sub(r"^(?:привет|здравствуй|слушай|пожалуйста|эй)\s+", "", tail).strip()
                before = re.sub(r"^(?:привет|здравствуй|слушай|пожалуйста|эй)\s+", "", before).strip()
                # Name may naturally appear at the beginning, middle or end. Prefer the
                # text after the name; if the user said "открой Telegram, Эйрвен", use
                # the meaningful part before it rather than treating this as a greeting.
                command = tail or before
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
            if score >= (0.56 if greeting_near else 0.78):
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
            return True, tail or before
        return False, normalized

    def _activation_route(self, text: str, speech_started_at: float, now: float | None = None) -> dict[str, Any]:
        """Classify one recognized utterance against the one-shot wake contract."""
        current = float(now if now is not None else time.monotonic())
        armed_at_start = bool(
            self._active_until > 0
            and float(speech_started_at or current) <= self._active_until
        )
        if armed_at_start:
            has_wake, tail = self._extract_explicit_wake(text)
        else:
            has_wake, tail = self._extract_after_wake(text)
        if not has_wake and not armed_at_start:
            return {"action": "ignore", "has_wake": False, "command": ""}
        if has_wake and not tail:
            return {"action": "arm", "has_wake": True, "command": ""}
        return {
            "action": "accept",
            "has_wake": bool(has_wake),
            "command": tail if has_wake and tail else self._normalize(text),
        }

    def _is_activation_phrase(self, text: str) -> bool:
        """Require a greeting only for the first wake of a conversation window."""
        normalized = self._normalize(text)
        words = set(normalized.split())
        greetings = {"привет", "здравствуй", "здравствуйте", "доброе", "добрый", "добрыйдень", "хай", "hello"}
        if words.intersection(greetings):
            return True
        # Common ASR punctuation/spacing variants of "приветик" / "привет эйрвен".
        return bool(re.search(r"\bпривет\w{0,4}\b", normalized))

    def _emotion(self, text: str, duration: float, energy: float) -> str:
        words = max(1, len(text.split()))
        pace = words / max(0.35, duration)
        textual = self.services.identity.infer_emotion(text)
        if textual != "natural":
            return textual
        if pace >= 3.8 or energy >= max(0.04, self._noise_floor * 5.0):
            return "energetic"
        if energy < max(0.011, self._noise_floor * 2.1):
            return "quiet"
        return "natural"

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
            if commentary == "playful":
                return "energetic"
            style = self.services.style.get()
            style_humor = str(getattr(style, "humor", "") or "").casefold()
            if commentary == "adaptive" and any(mark in style_humor for mark in ("игрив", "живой", "юмор")) and "без юмора" not in style_humor:
                return "energetic"
            if bool(getattr(style, "emotional_support", True)) and any(w in text.casefold() for w in ("держись", "понимаю", "рядом", "спокой", "не переж", "рада", "слышать")):
                return "warm"
        except Exception:
            pass
        return user_emotion if user_emotion in {"energetic", "quiet", "warm", "calm", "strict"} else "natural"

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
                wav, duration, energy, speech_started_at = self._utterance_q.get(timeout=0.3)
            except queue.Empty:
                continue
            # Latest speech wins. If several utterances accumulated while ASR/model work
            # was busy, discard stale audio instead of answering old requests seconds later.
            dropped = 0
            while True:
                try:
                    wav, duration, energy, speech_started_at = self._utterance_q.get_nowait()
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
                    log_event(self.services.settings.root_dir, "VOICE_DROP_WARMING", duration=round(duration, 3), energy=round(energy, 6))
                    try:
                        runtime = getattr(self.services, "runtime", None)
                        if runtime is not None: runtime.voice_activity_resolved(accepted=False)
                    except Exception:
                        pass
                    continue
                self._state = "recognizing"
                asr_started = time.monotonic()
                text = self.services.voice.transcribe_bytes(wav, ".wav").strip()
                self._last_activity_at = time.monotonic()
                asr_ms=(time.monotonic()-asr_started)*1000
                log_event(self.services.settings.root_dir, "VOICE_HEARD", text=text, duration=round(duration, 3), energy=round(energy, 6), asr_ms=round(asr_ms))
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
                    ambient = getattr(self.services, "ambient", None)
                    if ambient is not None and time.monotonic() < self._active_until:
                        try: ambient.resume()
                        except Exception: pass
                    continue
                self._last_text = text
                now = time.monotonic()
                route = self._activation_route(text, speech_started_at, now)
                has_wake = bool(route.get("has_wake"))

                # Every actionable thought needs the wake phrase or a speech onset inside
                # the five-second armed window. Background speech never inherits a long
                # conversational session.
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
                tail = command_text
                emotion = self._emotion(text, duration, energy)
                self._last_emotion = emotion
                self.services.db.set_setting("last_voice_emotion", emotion)
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
        terminal = {"done", "failed", "waiting_user", "cancelled"}
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
                    text = f"Готово. Задача «{title}» завершена."
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
        speech_q: queue.Queue[str|None]=queue.Queue(maxsize=16)
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
        self._generation_active.set()
        try:
            for event in self.services.chat.stream_events(query,cid or None,mode='Друг'):
                if not self._is_current_turn(turn_id): break
                if event.get('type')=='start': cid=str(event.get('conversation_id') or cid)
                elif event.get('type')=='token':
                    full=str(event.get('full') or '')
                    delta=str(event.get('content') or '')
                    answer=full or (answer+delta)
                    pending += delta
                    ready,pending=self._pop_speakable(pending)
                    for part in ready:
                        try: speech_q.put(part,timeout=.2)
                        except queue.Full: break
                elif event.get('type')=='done':
                    answer=str(event.get('answer') or answer)
                elif event.get('type')=='error' and not answer:
                    answer=str(event.get('message') or '')
            ready,pending=self._pop_speakable(pending,force=True)
            for part in ready:
                if self._is_current_turn(turn_id):
                    try: speech_q.put(part,timeout=.2)
                    except queue.Full: break
        finally:
            self._generation_active.clear()
            try: speech_q.put(None,timeout=.2)
            except queue.Full: pass
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
                    answer, cid = self._stream_chat_to_voice(query, emotion, turn_id)
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
                return
            if not answer:
                # Never fail silently: diagnostics are much easier when the owner hears a
                # short explicit failure instead of watching thinking -> listening.
                answer = "Я на связи, но этот ответ получился пустым. Скажи ещё раз — я не буду молчать."
            if answer and not self._stop.is_set():
                log_event(self.services.settings.root_dir, "VOICE_ANSWER", turn_id=turn_id, answer=answer[:1200], think_ms=round((time.monotonic()-started)*1000), streamed=streamed)
                if not streamed:
                    self._speak(answer, emotion, turn_id)
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
    def _speech_chunks(text: str, limit: int = 220) -> list[str]:
        """Split long replies so the first audible sentence starts immediately."""
        clean = re.sub(r"\s+", " ", text).strip()
        if not clean:
            return []
        parts = re.split(r"(?<=[.!?…])\s+|(?<=[,;:])\s+(?=.{70,})", clean)
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
            ambient = getattr(self.services, "ambient", None)
            if ambient is not None:
                try: ambient.duck()
                except Exception: pass
            log_event(self.services.settings.root_dir, "TTS_BEGIN", turn_id=turn_id, chars=len(text), emotion=user_emotion)
            try:
                sd = self._import_sounddevice()
                import soundfile as sf  # type: ignore
                import numpy as np  # type: ignore
                mode = self._response_emotion(text, user_emotion)
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
                    path = Path(self.services.voice.synthesize(chunk, mode=mode))
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
                    try:
                        output_info = sd.query_devices(None, "output")
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

                    self._speaking_since = time.monotonic()
                    self._speaking.set()
                    self._state = "speaking"
                    block = max(256, int(target_rate * 0.04))
                    try:
                        with sd.OutputStream(
                            samplerate=target_rate, channels=output_channels, dtype="float32", blocksize=0
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
                                out.write(np.asarray(data[pos:pos + block], dtype=np.float32))
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
                self._speaking.clear()
                self._speaking_since = 0.0
                self._barge_in.clear()
                if ambient is not None and not self._stop.is_set() and time.monotonic() < self._active_until:
                    try: ambient.resume()
                    except Exception: pass
                if not self._stop.is_set():
                    self._state = "thinking" if self._generation_active.is_set() and self._is_current_turn(turn_id) else "listening"

