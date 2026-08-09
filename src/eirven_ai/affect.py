from __future__ import annotations

import io
import wave
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class SpeechAffect:
    emotion: str = "natural"
    confidence: float = 0.35
    pace_wps: float = 0.0
    energy: float = 0.0
    pitch_hz: float = 0.0
    pitch_variation: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _audio_features(wav_bytes: bytes) -> tuple[float, float, float]:
    """Return a conservative pitch median, variation and RMS from mono PCM.

    The detector intentionally avoids an additional ML model.  Autocorrelation runs only
    over a few voiced 40 ms frames, so it is cheap enough for the always-on voice daemon.
    """

    if not wav_bytes:
        return 0.0, 0.0, 0.0
    try:
        import numpy as np  # type: ignore

        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            rate = int(wav.getframerate())
            channels = max(1, int(wav.getnchannels()))
            width = int(wav.getsampwidth())
            raw = wav.readframes(wav.getnframes())
        if width != 2 or rate < 8_000 or not raw:
            return 0.0, 0.0, 0.0
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        rms = float(np.sqrt(np.mean(samples * samples, dtype=np.float64))) if samples.size else 0.0
        frame_len = max(320, int(rate * 0.04))
        hop = max(160, int(rate * 0.08))
        low_lag = max(1, int(rate / 360.0))
        high_lag = max(low_lag + 1, int(rate / 75.0))
        pitches: list[float] = []
        for start in range(0, max(0, len(samples) - frame_len), hop):
            frame = samples[start : start + frame_len]
            frame_rms = float(np.sqrt(np.mean(frame * frame, dtype=np.float64)))
            if frame_rms < max(0.008, rms * 0.45):
                continue
            frame = frame - float(frame.mean())
            corr = np.correlate(frame, frame, mode="full")[frame_len - 1 :]
            upper = min(high_lag, len(corr) - 1)
            if upper <= low_lag or float(corr[0]) <= 1e-8:
                continue
            segment = corr[low_lag : upper + 1]
            lag = low_lag + int(np.argmax(segment))
            strength = float(corr[lag] / corr[0])
            if strength >= 0.28:
                pitches.append(float(rate / lag))
            if len(pitches) >= 18:
                break
        if not pitches:
            return 0.0, 0.0, rms
        values = np.asarray(pitches, dtype=np.float32)
        median = float(np.median(values))
        variation = float(np.std(values) / max(1.0, median))
        return median, variation, rms
    except Exception:
        return 0.0, 0.0, 0.0


def analyze_speech_affect(
    text: str,
    *,
    duration: float,
    energy: float,
    noise_floor: float,
    wav_bytes: bytes = b"",
    textual_emotion: str = "natural",
) -> SpeechAffect:
    words = max(1, len(str(text or "").split()))
    pace = words / max(0.35, float(duration or 0.0))
    pitch, pitch_variation, measured_rms = _audio_features(wav_bytes)
    effective_energy = max(float(energy or 0.0), measured_rms)
    floor = max(0.001, float(noise_floor or 0.0))

    if textual_emotion != "natural":
        return SpeechAffect(
            textual_emotion, 0.88, pace, effective_energy, pitch, pitch_variation
        )
    if pace >= 3.7 and effective_energy >= max(0.028, floor * 4.2):
        return SpeechAffect("energetic", 0.72, pace, effective_energy, pitch, pitch_variation)
    if pace <= 1.75 and effective_energy <= max(0.018, floor * 2.8):
        return SpeechAffect("sad", 0.58, pace, effective_energy, pitch, pitch_variation)
    if pace <= 1.45:
        return SpeechAffect("tired", 0.52, pace, effective_energy, pitch, pitch_variation)
    if effective_energy < max(0.010, floor * 2.0):
        return SpeechAffect("quiet", 0.56, pace, effective_energy, pitch, pitch_variation)
    if pitch_variation >= 0.24 and pace >= 2.6:
        return SpeechAffect("amused", 0.50, pace, effective_energy, pitch, pitch_variation)
    return SpeechAffect("natural", 0.38, pace, effective_energy, pitch, pitch_variation)
