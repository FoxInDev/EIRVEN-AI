# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
"""Глаза Эрви поверх картинки сферы — такие же, как в окне приложения.

В окне Эрви глаза рисуют стили поверх картинки сферы, а в самой картинке их нет.
Поэтому сфера на рабочем столе и сфера в окне запуска оставались без глаз.
Этот модуль рисует глаза прямо на картинке — теми же слоями и в тех же местах,
что стили окна, чтобы сфера выглядела одинаково везде.

Глаз — стеклянная бусина по макету: насыщенная синяя основа, светлее к низу;
крупный белый блик справа вверху; аквамариновый отсвет слева внизу; маленькая
искра слева; мягкий сине-фиолетовый край. Без светлой обводки — она делала
прежние глаза плоскими кнопками.

Модуль намеренно не зависит от остального пакета: его подключает и лончер,
который пакет Эрви не импортирует. Нужны только Pillow и NumPy.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

# Положение и размер глаз — доли картинки, ровно как в стилях окна:
# .ervi-eye{top:44%;width:6.8%;height:7.8%} левый left:36%, правый right:36%.
_TOP, _W, _H = 0.44, 0.068, 0.078
_EYES = ((0.36, -3.0), (1.0 - 0.36 - _W, 3.0))   # (левый край, наклон в градусах)


def _mix(base: np.ndarray, color: tuple[int, int, int], alpha: np.ndarray) -> np.ndarray:
    """Наложить цвет с прозрачностью alpha поверх base (как слой градиента в CSS)."""
    c = np.array(color, dtype=np.float32)
    a = np.clip(alpha, 0.0, 1.0)[..., None]
    return base * (1.0 - a) + c * a


def _ramp(t: np.ndarray, stops: list[tuple[float, float]]) -> np.ndarray:
    """Прозрачность вдоль градиента по опорным точкам (позиция, прозрачность)."""
    pos = np.array([p for p, _ in stops], dtype=np.float32)
    val = np.array([v for _, v in stops], dtype=np.float32)
    return np.interp(np.clip(t, 0.0, 1.0), pos, val)


def _render_eye(w: int, h: int) -> Image.Image:
    """Один глаз в размере w×h — те же слои, что background в стилях окна."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    u, v = (xx + 0.5) / w, (yy + 0.5) / h

    # Основа: radial-gradient(circle at 50% 60%, #3d58ff 0, #2a3ee0 24%, #1c29a8 50%,
    # #121872 74%, #0b0e4c 100%) — радиус до дальнего угла, как в CSS.
    cx, cy = 0.50 * w, 0.60 * h
    far = max(np.hypot(cx - x, cy - y) for x in (0, w) for y in (0, h))
    t = np.hypot(xx - cx, yy - cy) / far
    stops_c = [(0.00, (0x3D, 0x58, 0xFF)), (0.24, (0x2A, 0x3E, 0xE0)), (0.50, (0x1C, 0x29, 0xA8)),
               (0.74, (0x12, 0x18, 0x72)), (1.00, (0x0B, 0x0E, 0x4C))]
    rgb = np.zeros((h, w, 3), np.float32)
    for ch in range(3):
        rgb[..., ch] = np.interp(np.clip(t, 0, 1), [p for p, _ in stops_c], [c[ch] for _, c in stops_c])

    # Искра слева: circle at 23% 51%, яркая до 6%, гаснет к 11% дальнего радиуса.
    sx, sy = 0.23 * w, 0.51 * h
    sfar = max(np.hypot(sx - x, sy - y) for x in (0, w) for y in (0, h))
    ts = np.hypot(xx - sx, yy - sy) / sfar
    rgb = _mix(rgb, (150, 245, 255), _ramp(ts, [(0.0, 1.0), (0.06, 1.0), (0.08, 0.5), (0.11, 0.0)]))

    # Голубой отсвет слева внизу: ellipse 36%×28% at 33% 72%.
    te = np.hypot((u - 0.33) / 0.36, (v - 0.72) / 0.28)
    rgb = _mix(rgb, (130, 240, 255), _ramp(te, [(0.0, 1.0), (0.35, 0.72), (0.70, 0.24), (1.0, 0.0)]))

    # Главный блик справа вверху: ellipse 27%×23% at 66% 29%, почти белый.
    th = np.hypot((u - 0.66) / 0.27, (v - 0.29) / 0.23)
    rgb = _mix(rgb, (255, 255, 255), _ramp(th, [(0.0, 1.0), (0.40, 0.95), (0.70, 0.35), (1.0, 0.0)]))

    # Форма глаза — чуть шире сверху (border-radius 52% 52% 48% 48%) — эллипс.
    r = np.hypot((u - 0.5) / 0.5, (v - 0.5) / 0.5)
    alpha = np.clip((1.0 - r) * min(w, h) / 1.6, 0.0, 1.0)

    # Край: мягкое затемнение по кромке и фиолетовый отлив справа снизу
    # (в стилях — inset-тени), чтобы глаз читался как объёмная бусина.
    rim = np.clip((r - 0.80) / 0.20, 0.0, 1.0)
    rgb = _mix(rgb, (10, 12, 70), rim * 0.55)
    lower_right = np.clip((u - 0.45) * 1.6, 0, 1) * np.clip((v - 0.45) * 1.6, 0, 1)
    rgb = _mix(rgb, (200, 110, 255), rim * lower_right * 0.9)

    rgba = np.dstack([np.clip(rgb, 0, 255), alpha * 255.0]).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def add_eyes(sphere: Image.Image) -> Image.Image:
    """Вернуть картинку сферы с глазами. Исходную картинку не меняет."""
    base = sphere.convert("RGBA")
    width, height = base.size
    eye_w = max(4, int(round(width * _W)))
    eye_h = max(4, int(round(height * _H)))
    # Рисуем крупнее и уменьшаем — иначе на маленькой сфере блики рассыпаются.
    k = max(4, int(np.ceil(96 / max(eye_w, 1))))
    for left, tilt in _EYES:
        big = _render_eye(eye_w * k, eye_h * k)
        eye = big.resize((eye_w, eye_h), Image.LANCZOS).rotate(-tilt, resample=Image.BICUBIC, expand=True)
        # Мягкое свечение вокруг глаза: 0 0 7px синее и 0 0 16px фиолетовое в стилях.
        pad = max(3, int(round(eye_w * 0.6)))
        glow = Image.new("RGBA", (eye.width + pad * 2, eye.height + pad * 2), (0, 0, 0, 0))
        tint = Image.new("RGBA", eye.size, (95, 130, 255, 0))
        tint.putalpha(eye.getchannel("A").point(lambda a: int(a * 0.55)))
        glow.alpha_composite(tint, (pad, pad))
        glow = glow.filter(ImageFilter.GaussianBlur(max(1.5, pad * 0.45)))
        x = int(round(width * left + (eye_w - eye.width) / 2))
        y = int(round(height * _TOP + (eye_h - eye.height) / 2))
        base.alpha_composite(glow, (max(0, x - pad), max(0, y - pad)))
        base.alpha_composite(eye, (max(0, x), max(0, y)))
    return base
