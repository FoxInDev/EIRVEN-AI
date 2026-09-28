"""Проверка подписи Ed25519 без внешних библиотек.

Эрви проверяет лицензию, подписанную сервером: по адресу сервера лежит открытый
ключ, закрытый есть только у сервера. Здесь только проверка — подписывать этот
модуль не умеет, и это правильно: в приложении подписывать нечего.

Реализация — прямо по RFC 8032 (Ed25519, вариант с учётом кофактора). Без
зависимостей: pynacl или cryptography может не оказаться в окружении, а
проверка нужна при каждом запуске.
"""

from __future__ import annotations

import hashlib

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _xrecover(y: int) -> int | None:
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if (x * x - xx) % _P != 0:
        return None
    if x % 2 != 0:
        x = _P - x
    return x


_BY = 4 * pow(5, _P - 2, _P) % _P
_BX = _xrecover(_BY) or 0
_BASE = (_BX % _P, _BY % _P, 1, _BX * _BY % _P)


def _add(point: tuple[int, int, int, int], other: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, z1, t1 = point
    x2, y2, z2, t2 = other
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = t1 * 2 * _D * t2 % _P
    d = z1 * 2 * z2 % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _scalarmult(point: tuple[int, int, int, int], scalar: int) -> tuple[int, int, int, int]:
    result = (0, 1, 1, 0)
    current = point
    while scalar > 0:
        if scalar & 1:
            result = _add(result, current)
        current = _add(current, current)
        scalar >>= 1
    return result


def _on_curve(point: tuple[int, int, int, int]) -> bool:
    x, y, z, t = point
    return (
        z % _P != 0
        and x * y % _P == z * t % _P
        and (y * y - x * x - z * z - _D * t * t) % _P == 0
    )


def _decode_point(data: bytes) -> tuple[int, int, int, int] | None:
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little") & ((1 << 255) - 1)
    if y >= _P:
        return None
    sign = data[31] >> 7
    x = _xrecover(y)
    if x is None:
        return None
    if x & 1 != sign:
        x = _P - x
    point = (x % _P, y, 1, x * y % _P)
    return point if _on_curve(point) else None


def _equal(point: tuple[int, int, int, int], other: tuple[int, int, int, int]) -> bool:
    x1, y1, z1, _ = point
    x2, y2, z2, _ = other
    return (x1 * z2 - x2 * z1) % _P == 0 and (y1 * z2 - y2 * z1) % _P == 0


def verify(message: bytes, signature: bytes, public_key: bytes) -> bool:
    """True, если подпись действительно сделана владельцем этого открытого ключа."""
    if len(signature) != 64 or len(public_key) != 32:
        return False
    try:
        pub = _decode_point(public_key)
        r_point = _decode_point(signature[:32])
        if pub is None or r_point is None:
            return False
        s_value = int.from_bytes(signature[32:], "little")
        if s_value >= _L:
            return False
        digest = hashlib.sha512(signature[:32] + public_key + message).digest()
        factor = int.from_bytes(digest, "little") % _L
        return _equal(_scalarmult(_BASE, s_value), _add(r_point, _scalarmult(pub, factor)))
    except Exception:
        # Любая неожиданная арифметика — это неверная подпись, а не сбой Эрви.
        return False
