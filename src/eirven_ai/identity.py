from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .database import Database


# Kept for API compatibility, but the desktop companion is now always a sphere.
AVATARS: dict[str, dict[str, str]] = {
    "fox": {"title": "Сфера", "symbol": ""},
    "engineer": {"title": "Сфера", "symbol": ""},
    "anime": {"title": "Сфера", "symbol": ""},
    "orb": {"title": "Сфера", "symbol": ""},
}

VOICE_MODES: dict[str, dict[str, Any]] = {
    "natural": {"title": "Естественно", "length_scale": 0.90, "noise_scale": 0.67, "noise_w": 0.8, "volume": 1.0, "breath": 0.0},
    "warm": {"title": "Тепло", "length_scale": 0.93, "noise_scale": 0.70, "noise_w": 0.84, "volume": 0.98, "breath": 0.05},
    "calm": {"title": "Спокойно", "length_scale": 0.97, "noise_scale": 0.60, "noise_w": 0.74, "volume": 0.94, "breath": 0.04},
    "energetic": {"title": "Энергично", "length_scale": 0.82, "noise_scale": 0.74, "noise_w": 0.88, "volume": 1.03, "breath": 0.0},
    "strict": {"title": "Серьёзно", "length_scale": 0.88, "noise_scale": 0.53, "noise_w": 0.68, "volume": 1.0, "breath": 0.0},
    "quiet": {"title": "Тихо", "length_scale": 0.95, "noise_scale": 0.62, "noise_w": 0.76, "volume": 0.72, "breath": 0.025},
    "amused": {"title": "С улыбкой", "length_scale": 0.84, "noise_scale": 0.76, "noise_w": 0.90, "volume": 1.01, "breath": 0.0},
    "sad": {"title": "Грустно", "length_scale": 1.03, "noise_scale": 0.58, "noise_w": 0.72, "volume": 0.86, "breath": 0.05},
    "empathetic": {"title": "С поддержкой", "length_scale": 0.98, "noise_scale": 0.66, "noise_w": 0.78, "volume": 0.94, "breath": 0.04},
    "curious": {"title": "С любопытством", "length_scale": 0.89, "noise_scale": 0.72, "noise_w": 0.85, "volume": 0.98, "breath": 0.0},
    "concerned": {"title": "Обеспокоенно", "length_scale": 0.94, "noise_scale": 0.60, "noise_w": 0.72, "volume": 0.97, "breath": 0.02},
    "proud": {"title": "Гордо", "length_scale": 0.87, "noise_scale": 0.69, "noise_w": 0.83, "volume": 1.01, "breath": 0.0},
    "tired": {"title": "Устало", "length_scale": 1.04, "noise_scale": 0.56, "noise_w": 0.70, "volume": 0.82, "breath": 0.06},
}

# The public product has one real local speaker.  The label and engine must stay aligned.
VOICE_CATALOG: dict[str, dict[str, Any]] = {
    "irina_soft": {
        "title": "Бая · фирменный голос Эйрвен",
        # Baya is the real named speaker in Silero v5.5 RU.  Do not map this label to
        # Piper Irina or another fallback voice: a missing Baya stays a visible error.
        "gender": "female", "preferred_engine": "silero",
        "silero_speaker": "baya", "edge_voice": "ru-RU-SvetlanaNeural",
        "reference": "models/voice_refs/baya.wav", "model": "ru_RU-irina-medium.onnx",
    },
}

DEFAULT_VOICE_BY_GENDER = {"female": "irina_soft"}


@dataclass(slots=True)
class Identity:
    assistant_name: str = "Эйрвен"
    user_address: str = ""
    gender: str = "female"
    voice_key: str = "irina"
    avatar: str = "fox"
    custom_avatar_path: str = ""
    accent_color: str = "#78e8ff"
    voice_mode: str = "natural"
    emotion_mode: str = "auto"
    background_enabled: bool = True
    desktop_avatar_enabled: bool = True
    desktop_avatar_size: int = 92
    desktop_avatar_opacity: float = 0.96
    game_control_enabled: bool = False
    creative_backend: str = "none"
    action_commentary: str = "adaptive"
    ambient_music_enabled: bool = False
    ambient_music_volume: float = 0.42
    speech_speed: float = 1.0
    strict_wake_name: bool = True
    onboarding_completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["avatars"] = AVATARS
        value["voice_modes"] = VOICE_MODES
        # Public EIRVEN is intentionally a single feminine identity with the Baya voice.
        # The public product exposes one canonical voice: Baya.
        value["voice_catalog"] = VOICE_CATALOG
        return value


class IdentityService:
    KEY = "identity_v1"

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _normalize(identity: Identity) -> Identity:
        identity.assistant_name = (str(identity.assistant_name).strip() or "Эйрвен")[:32]
        identity.user_address = str(identity.user_address or "").strip()[:48]
        # Public release: EIRVEN has one canonical feminine identity. Legacy values are
        # accepted from old databases but normalized immediately and are no longer exposed.
        identity.gender = "female"
        if identity.avatar not in AVATARS:
            identity.avatar = "fox"
        # r21: delivery is automatic. The UI no longer exposes emotion/speed/commentary
        # tuning; one high-quality natural voice is selected from the assistant gender.
        identity.voice_mode = "natural"
        identity.emotion_mode = "auto"
        identity.action_commentary = "adaptive"
        identity.voice_key = "irina_soft"
        identity.desktop_avatar_size = max(48, min(int(identity.desktop_avatar_size), 180))
        identity.desktop_avatar_opacity = max(0.35, min(float(identity.desktop_avatar_opacity), 1.0))
        identity.ambient_music_enabled = False
        identity.ambient_music_volume = 0.0
        identity.speech_speed = 1.0
        identity.strict_wake_name = True
        identity.onboarding_completed = bool(identity.onboarding_completed)
        return identity

    def get(self) -> Identity:
        raw = self.db.get_setting(self.KEY, {})
        if not isinstance(raw, dict):
            raw = {}
        allowed = {field.name for field in Identity.__dataclass_fields__.values()}
        values = {key: value for key, value in raw.items() if key in allowed}
        try:
            identity = Identity(**values)
        except (TypeError, ValueError):
            identity = Identity()
        return self._normalize(identity)

    def update(self, values: dict[str, Any]) -> Identity:
        current = self.get()
        for key, value in values.items():
            # gender/voice_key are intentionally fixed in the public product. Ignore stale
            # clients rather than allowing an unsupported identity to leak back in.
            if key in {"gender", "voice_key"}:
                continue
            if hasattr(current, key):
                setattr(current, key, value)
        current = self._normalize(current)
        self.db.set_setting(self.KEY, asdict(current))
        return current

    @staticmethod
    def infer_emotion(text: str) -> str:
        clean = str(text or "").casefold().replace("ё", "е")
        if any(word in clean for word in ("ахаха", "ахах", "хаха", "ха-ха", "смешно", "шутк", "ору с", "лол")):
            return "amused"
        if any(word in clean for word in ("ура", "круто", "отлично", "супер", "победа", "готово!", "кайф", "получилось")):
            return "energetic"
        if any(word in clean for word in ("груст", "печаль", "плохо на душе", "хочется плак", "расстро", "одиноко", "больно")):
            return "sad"
        if any(word in clean for word in ("устал", "нет сил", "выжат", "сонн", "не выспал")):
            return "tired"
        if any(word in clean for word in ("держись", "я рядом", "понимаю тебя", "поддерж", "не переживай")):
            return "empathetic"
        if any(word in clean for word in ("боюсь", "тревог", "пережива", "опас", "осторож", "срочно", "что-то не так")):
            return "concerned"
        if any(word in clean for word in ("внимание", "ошибка", "важно", "останов", "критическ")):
            return "strict"
        if any(word in clean for word in ("спокойно", "не спеши", "отдохни", "тише")):
            return "calm"
        if any(word in clean for word in ("интересно", "любопытно", "а что если", "давай проверим", "хм,")):
            return "curious"
        if any(word in clean for word in ("горжусь", "молодец", "классно справил", "вот это результат")):
            return "proud"
        return "natural"
