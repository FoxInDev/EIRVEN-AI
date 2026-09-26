"""Темп машины: во сколько раз эта машина медленнее той, под которую писались сроки.

Сроки ожидания модели в Эрви подбирались на компьютере с видеокартой: решение за
3–8 секунд, проверка за 5. На ноутбуке, где модель думает на процессоре, те же
шаги занимают в пять-десять раз дольше, и почти всё обрывалось по сроку: Эрви не
отвечала и не выполняла действия, хотя модель работала и ответ бы пришёл.

Этот модуль хранит один множитель на весь процесс. Он складывается из двух частей:
— тип машины: без видеокарты множитель сразу не меньше шести;
— реальные замеры: сколько на самом деле заняло управляющее решение.
Все сроки умножаются на него. С видеокартой множитель 1 — ничего не меняется.
"""

from __future__ import annotations

import threading

# Сколько обычно занимает управляющее решение на машине с видеокартой, секунд.
# От этой точки считаем, во сколько раз текущая машина медленнее.
_REFERENCE_DECISION_SECONDS = 3.0

_lock = threading.Lock()
_hardware_floor = 1.0
_measured = 1.0


def set_hardware(runtime_mode: str) -> None:
    """Задать нижнюю границу по типу машины. Вызывается один раз при запуске."""
    global _hardware_floor
    with _lock:
        _hardware_floor = 1.0 if str(runtime_mode or "") == "gpu_resident" else 6.0


def note_decision(seconds: float) -> None:
    """Учесть реальное время решения. Плавно — один быстрый ответ не обнуляет запас."""
    global _measured
    try:
        ratio = max(1.0, float(seconds) / _REFERENCE_DECISION_SECONDS)
    except Exception:
        return
    with _lock:
        _measured = max(ratio, _measured * 0.85)


def factor() -> float:
    with _lock:
        return max(1.0, _hardware_floor, _measured)


def scale(seconds: float, cap: float = 150.0) -> float:
    """Срок с поправкой на эту машину. С видеокартой возвращает исходное значение."""
    try:
        value = float(seconds)
    except Exception:
        return float(cap)
    return min(float(cap), value * factor()) if factor() > 1.0 else value
