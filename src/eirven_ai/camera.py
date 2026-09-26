from __future__ import annotations

import base64
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .human_errors import humanize


class CameraService:
    """Opt-in local camera sensor.

    Capture never starts during service construction.  OpenCV is optional; when it is
    missing or no device is available the UI receives an honest unavailable status.
    MediaPipe, when installed, adds a best-effort fist/hand position signal without
    making camera mode depend on a second model or network service.
    """

    def __init__(self, settings: Any, gateway: Any | None = None):
        self.settings = settings
        self.gateway = gateway
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._cap: Any = None
        self._jpeg: bytes | None = None
        self._frame_at = 0.0
        self._error = ""
        self._gesture: dict[str, Any] = {"present": False, "fist": False, "x": None, "y": None}
        self._hands: Any = None
        self._mp_draw: Any = None
        try:
            import cv2  # type: ignore
            self._cv2 = cv2
        except Exception as exc:  # pragma: no cover - platform dependency
            self._cv2 = None
            self._error = f"OpenCV недоступен: {humanize(exc)}"
        try:  # optional gesture enhancement
            import mediapipe as mp  # type: ignore
            self._hands = mp.solutions.hands.Hands(
                static_image_mode=False, max_num_hands=1, min_detection_confidence=0.55,
                min_tracking_confidence=0.5,
            )
            self._mp_draw = mp
        except Exception:
            self._hands = None

    @property
    def available(self) -> bool:
        return self._cv2 is not None

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            age = round(max(0.0, time.time() - self._frame_at), 2) if self._frame_at else None
            gesture = dict(self._gesture)
            return {
                "available": self.available,
                "running": running,
                "frame_age_seconds": age,
                "gesture": gesture,
                "gesture_available": self._hands is not None,
                "error": self._error,
            }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if not self.available:
                return self.status()
            if self._thread and self._thread.is_alive():
                return self.status()
            self._stop.clear()
            self._ready.clear()
            self._error = ""
            self._thread = threading.Thread(target=self._capture_loop, daemon=True, name="eirven-camera")
            self._thread.start()
        # Do not report a healthy camera before the worker has either opened the
        # device or recorded a concrete failure.  The bounded wait keeps the API
        # responsive when a driver is slow or a camera is physically absent.
        self._ready.wait(timeout=0.75)
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._ready.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            cap, self._cap = self._cap, None
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            self._jpeg = None
            self._gesture = {"present": False, "fist": False, "x": None, "y": None}
        return self.status()

    def latest_jpeg(self) -> bytes | None:
        with self._lock:
            return self._jpeg

    def _capture_loop(self) -> None:
        cv2 = self._cv2
        if cv2 is None:
            return
        index = int(os.environ.get("EIRVEN_CAMERA_INDEX", "0") or 0)
        cap = None
        try:
            cap = cv2.VideoCapture(index, getattr(cv2, "CAP_DSHOW", 700))
            if not cap or not cap.isOpened():
                raise RuntimeError("Камера не найдена или занята другим приложением")
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 15)
            with self._lock:
                self._cap = cap
            self._ready.set()
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.08)
                    continue
                self._update_gesture(frame)
                ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
                if ok:
                    with self._lock:
                        self._jpeg = bytes(encoded)
                        self._frame_at = time.time()
                time.sleep(0.03)
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
            self._ready.set()
        finally:
            self._ready.set()
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            with self._lock:
                if self._cap is cap:
                    self._cap = None

    def _update_gesture(self, frame: Any) -> None:
        hands = self._hands
        if hands is None:
            return
        try:
            rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
            result = hands.process(rgb)
            landmarks = result.multi_hand_landmarks[0] if result.multi_hand_landmarks else None
            if landmarks is None:
                gesture = {"present": False, "fist": False, "x": None, "y": None}
            else:
                points = landmarks.landmark
                wrist = points[0]
                # A fist has most fingertips closer to the wrist than their PIP joints.
                pairs = ((8, 6), (12, 10), (16, 14), (20, 18))
                fist = sum(1 for tip, pip in pairs if points[tip].y > points[pip].y) >= 3
                gesture = {"present": True, "fist": bool(fist), "x": round(float(wrist.x), 4), "y": round(float(wrist.y), 4)}
            with self._lock:
                self._gesture = gesture
        except Exception:
            return

    def mjpeg(self) -> Iterator[bytes]:
        started = time.monotonic()
        while not self._stop.is_set():
            frame = self.latest_jpeg()
            if frame:
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
            elif self._thread is not None and not self._thread.is_alive() and time.monotonic() - started > 0.2:
                # A capture worker can fail after the HTTP stream was accepted;
                # terminate instead of holding a client forever on an empty stream.
                break
            time.sleep(0.06)

    def describe(self, prompt: str) -> str:
        frame = self.latest_jpeg()
        if not frame or self.gateway is None:
            return "Камера включена, но свежий кадр пока недоступен."
        try:
            raw = self.gateway.chat(
                [
                    {"role": "system", "content": "Опиши локальный кадр кратко, без догадок и персональных выводов."},
                    {"role": "user", "content": str(prompt or "Что перед камерой?"), "images": [base64.b64encode(frame).decode("ascii")]},
                ],
                model=getattr(self.settings, "vision_model", None),
                temperature=0.0,
                num_ctx=2048,
                num_predict=120,
                timeout_seconds=10,
            )
            return str(raw.get("message", {}).get("content") or raw.get("response") or "").strip()
        except Exception as exc:
            return f"Не удалось описать кадр: {humanize(exc)}"
