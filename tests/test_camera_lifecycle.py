from __future__ import annotations

import threading
from types import SimpleNamespace

from eirven_ai.camera import CameraService


def test_camera_start_reports_open_failure_instead_of_false_running() -> None:
    camera = CameraService(SimpleNamespace())

    class Capture:
        def isOpened(self):
            return False

    class FakeCV2:
        CAP_DSHOW = 700

        @staticmethod
        def VideoCapture(*_args):
            return Capture()

    camera._cv2 = FakeCV2
    status = camera.start()
    assert status["running"] is False
    assert "Камера не найдена" in status["error"]


def test_camera_mjpeg_ends_after_capture_worker_dies_without_frame() -> None:
    camera = CameraService(SimpleNamespace())
    camera._thread = threading.Thread(target=lambda: None)
    assert list(camera.mjpeg()) == []
