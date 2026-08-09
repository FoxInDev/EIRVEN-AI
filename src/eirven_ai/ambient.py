from __future__ import annotations

import math
import threading
import time
from typing import Any


class AmbientMusic:
    """Very light local generative ambient bed with smooth ducking.

    No files, services or API keys are required. The layer is intentionally subtle and
    automatically ducks while the owner or EIRVEN speaks.
    """

    def __init__(self, services: Any):
        self.services = services
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._target = 0.0
        self._level = 0.0
        self._lock = threading.RLock()
        self._phase = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="eirven-ambient")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.2)
        self._thread = None
        self._level = 0.0

    def enabled(self) -> bool:
        try:
            identity = self.services.identity.get()
            suspended = bool(self.services.db.get_setting("neuro_music_suspended", False))
            return bool(getattr(identity, "ambient_music_enabled", True)) and not suspended
        except Exception:
            return False

    def volume(self) -> float:
        try:
            identity = self.services.identity.get()
            return max(0.0, min(1.0, float(getattr(identity, "ambient_music_volume", 0.42))))
        except Exception:
            return 0.42

    def wake(self) -> None:
        with self._lock:
            self._target = self.volume() if self.enabled() else 0.0

    def duck(self) -> None:
        with self._lock:
            self._target = 0.0

    def resume(self) -> None:
        self.wake()

    def suspend(self, value: bool = True) -> None:
        try:
            self.services.db.set_setting("neuro_music_suspended", bool(value))
        except Exception:
            pass
        with self._lock:
            self._target = 0.0 if value else self.volume()

    def _run(self) -> None:
        try:
            import numpy as np  # type: ignore
            import sounddevice as sd  # type: ignore
            info = sd.query_devices(None, "output")
            rate = int(round(float(info.get("default_samplerate") or 48_000)))
            rate = rate if rate >= 8_000 else 48_000
            channels = 2 if int(info.get("max_output_channels") or 1) >= 2 else 1
            block = max(256, int(rate * 0.04))
            notes = (110.0, 164.81, 220.0, 261.63)
            with sd.OutputStream(samplerate=rate, channels=channels, dtype="float32", blocksize=block) as out:
                while not self._stop.is_set():
                    with self._lock:
                        target = self._target if self.enabled() else 0.0
                    # ~350 ms fade-out, ~900 ms fade-in.
                    speed = 0.22 if target < self._level else 0.07
                    self._level += (target - self._level) * speed
                    t = (np.arange(block, dtype=np.float32) + self._phase) / float(rate)
                    self._phase += block
                    slow = 0.5 + 0.5 * np.sin(2 * math.pi * 0.035 * t)
                    sig = np.zeros(block, dtype=np.float32)
                    for idx, hz in enumerate(notes):
                        amp = (0.34 / (idx + 1)) * (0.72 + 0.28 * np.sin(2 * math.pi * (0.018 + idx * 0.006) * t + idx))
                        sig += amp.astype(np.float32) * np.sin(2 * math.pi * hz * t + idx * 0.7).astype(np.float32)
                    sig += 0.10 * np.sin(2 * math.pi * 55.0 * t).astype(np.float32)
                    sig *= slow.astype(np.float32) * float(min(1.0, self._level * 2.65))
                    # r11: the previous pad was barely audible on laptop speakers even
                    # at 100%. Raise perceived loudness while keeping soft clipping.
                    sig = (np.tanh(sig * 3.1) * 0.98).astype(np.float32)
                    data = sig[:, None]
                    if channels == 2:
                        data = np.repeat(data, 2, axis=1)
                    out.write(data)
        except Exception:
            # Ambient sound is optional; voice must remain available if an audio device
            # refuses a second shared output stream.
            while not self._stop.wait(1.0):
                pass
