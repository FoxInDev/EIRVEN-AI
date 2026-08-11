from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit

_ONES = {0:"ноль",1:"один",2:"два",3:"три",4:"четыре",5:"пять",6:"шесть",7:"семь",8:"восемь",9:"девять",10:"десять",11:"одиннадцать",12:"двенадцать",13:"тринадцать",14:"четырнадцать",15:"пятнадцать",16:"шестнадцать",17:"семнадцать",18:"восемнадцать",19:"девятнадцать"}
_TENS = {20:"двадцать",30:"тридцать",40:"сорок",50:"пятьдесят",60:"шестьдесят",70:"семьдесят",80:"восемьдесят",90:"девяносто"}
_HUNDREDS = {100:"сто",200:"двести",300:"триста",400:"четыреста",500:"пятьсот",600:"шестьсот",700:"семьсот",800:"восемьсот",900:"девятьсот"}
_MONTHS = {1:"января",2:"февраля",3:"марта",4:"апреля",5:"мая",6:"июня",7:"июля",8:"августа",9:"сентября",10:"октября",11:"ноября",12:"декабря"}
_ORDINAL_DAY = {1:"первое",2:"второе",3:"третье",4:"четвёртое",5:"пятое",6:"шестое",7:"седьмое",8:"восьмое",9:"девятое",10:"десятое",11:"одиннадцатое",12:"двенадцатое",13:"тринадцатое",14:"четырнадцатое",15:"пятнадцатое",16:"шестнадцатое",17:"семнадцатое",18:"восемнадцатое",19:"девятнадцатое",20:"двадцатое",21:"двадцать первое",22:"двадцать второе",23:"двадцать третье",24:"двадцать четвёртое",25:"двадцать пятое",26:"двадцать шестое",27:"двадцать седьмое",28:"двадцать восьмое",29:"двадцать девятое",30:"тридцатое",31:"тридцать первое"}
_YEAR_ORDINAL_GENITIVE = {
    1:"первого",2:"второго",3:"третьего",4:"четвёртого",5:"пятого",6:"шестого",7:"седьмого",8:"восьмого",9:"девятого",10:"десятого",
    11:"одиннадцатого",12:"двенадцатого",13:"тринадцатого",14:"четырнадцатого",15:"пятнадцатого",16:"шестнадцатого",17:"семнадцатого",18:"восемнадцатого",19:"девятнадцатого",20:"двадцатого",
    21:"двадцать первого",22:"двадцать второго",23:"двадцать третьего",24:"двадцать четвёртого",25:"двадцать пятого",26:"двадцать шестого",27:"двадцать седьмого",28:"двадцать восьмого",29:"двадцать девятого",30:"тридцатого",31:"тридцать первого",
}



def _below_thousand(n: int, *, feminine: bool = False) -> str:
    n = int(n)
    parts: list[str] = []
    if n >= 100:
        parts.append(_HUNDREDS[(n // 100) * 100])
        n %= 100
    if n in _ONES:
        if n:
            if feminine and n == 1:
                parts.append("одна")
            elif feminine and n == 2:
                parts.append("две")
            else:
                parts.append(_ONES[n])
    elif n in _TENS:
        parts.append(_TENS[n])
    elif n:
        parts.extend((_TENS[(n // 10) * 10], _ONES[n % 10]))
    return " ".join(parts)


def cardinal(n: int) -> str:
    """Spell an integer for the Russian TTS instead of leaving silent digits."""
    n = int(n)
    if n < 0:
        return "минус " + cardinal(-n)
    if n == 0:
        return _ONES[0]
    if n < 1000:
        return _below_thousand(n)
    if n >= 1_000_000_000_000:
        # Huge identifiers are clearer digit by digit than as a malformed number.
        return " ".join(_ONES[int(digit)] for digit in str(n))

    parts: list[str] = []
    rest = n
    scales = (
        (1_000_000_000, "миллиард", "миллиарда", "миллиардов", False),
        (1_000_000, "миллион", "миллиона", "миллионов", False),
        (1_000, "тысяча", "тысячи", "тысяч", True),
    )
    for value, one, few, many, feminine in scales:
        amount, rest = divmod(rest, value)
        if amount:
            parts.append(f"{_below_thousand(amount, feminine=feminine)} {_form(amount, one, few, many)}")
    if rest:
        parts.append(_below_thousand(rest))
    return " ".join(parts)


def decimal_phrase(value: float, digits: int = 2) -> str:
    """TTS-safe Russian decimal, e.g. 81.25 -> 'восемьдесят один рубль двадцать пять копеек' pieces."""
    rounded=round(float(value), max(0,int(digits)))
    whole=int(abs(rounded))
    factor=10**max(0,int(digits))
    frac=int(round((abs(rounded)-whole)*factor))
    prefix="минус " if rounded < 0 else ""
    if not digits or frac == 0:
        return prefix+cardinal(whole)
    denominator = {1: "десятых", 2: "сотых", 3: "тысячных"}.get(int(digits), "долей")
    return f"{prefix}{cardinal(whole)} целых {cardinal(frac)} {denominator}"


def _form(n: int, one: str, few: str, many: str) -> str:
    n=abs(int(n))%100
    if 11 <= n <= 14: return many
    last=n%10
    if last==1: return one
    if 2 <= last <= 4: return few
    return many


def time_phrase(dt: datetime | None = None) -> str:
    dt=dt or datetime.now()
    h,m=dt.hour,dt.minute
    return f"Сейчас {cardinal(h)} {_form(h,'час','часа','часов')} {cardinal(m)} {_form(m,'минута','минуты','минут')}."


def date_phrase(dt: datetime | None = None) -> str:
    dt=dt or datetime.now()
    if 2001 <= dt.year <= 2031:
        year = "две тысячи " + _YEAR_ORDINAL_GENITIVE[dt.year - 2000]
    elif dt.year == 2000:
        year = "двухтысячного"
    else:
        year = cardinal(dt.year)
    return f"Сегодня {_ORDINAL_DAY.get(dt.day, str(dt.day))} {_MONTHS.get(dt.month, str(dt.month))} {year} года."


def russian_weather_condition(text: str) -> str:
    raw=(text or '').strip()
    low=raw.casefold()
    mapping=(
        (("partly cloudy","partly cloudy"),"переменная облачность"),
        (("cloudy","overcast"),"облачно"),
        (("clear","sunny"),"ясно"),
        (("light rain","drizzle"),"небольшой дождь"),
        (("rain","rainy"),"дождь"),
        (("snow","snowy"),"снег"),
        (("fog","mist"),"туман"),
        (("thunder","storm"),"гроза"),
    )
    for keys, value in mapping:
        if any(k in low for k in keys): return value
    return raw


_LETTER_NAMES = {
    "a": "эй", "b": "би", "c": "си", "d": "ди", "e": "и", "f": "эф",
    "g": "джи", "h": "эйч", "i": "ай", "j": "джей", "k": "кей", "l": "эл",
    "m": "эм", "n": "эн", "o": "оу", "p": "пи", "q": "кью", "r": "ар",
    "s": "эс", "t": "ти", "u": "ю", "v": "ви", "w": "дабл ю", "x": "икс",
    "y": "уай", "z": "зи",
}

_KNOWN_LATIN = {
    "android": "андроид", "apple": "эппл", "bluetooth": "блютус", "browser": "браузер",
    "client": "клиент", "code": "код", "desktop": "десктоп", "discord": "дискорд",
    "download": "даунлоуд", "edge": "эдж", "error": "эррор", "file": "файл",
    "folder": "фолдер", "github": "гитхаб", "google": "гугл", "hello": "хеллоу",
    "linux": "линукс", "mobile": "мобайл", "offline": "офлайн", "online": "онлайн",
    "open": "оупен", "pause": "пауза", "play": "плэй", "playwright": "плэйрайт",
    "python": "пайтон", "server": "сервер", "silero": "силеро", "spotify": "спотифай",
    "success": "саксэс", "telegram": "телеграм", "test": "тест", "update": "апдэйт",
    "upload": "аплоуд", "windows": "виндоус", "world": "ворлд", "yandex": "яндекс",
    "youtube": "ютуб", "music": "мьюзик", "search": "сёч", "message": "мэссидж",
    "saved": "сэйвд", "settings": "сэттингс", "phone": "фоун", "chrome": "хроум",
    "microsoft": "майкрософт", "steam": "стим", "zoom": "зум", "whatsapp": "вотсап",
    "install": "инстол", "setup": "сэтап", "connect": "коннэкт", "network": "нэтворк",
    "scan": "скэн", "ready": "рэди", "start": "старт", "stop": "стоп", "send": "сэнд",
    "go": "гоу", "ok": "окей",
}

_KNOWN_ACRONYMS = {
    "mp": "эм пи", "pc": "пи си", "tv": "ти ви", "vr": "ви ар", "ar": "эй ар",
    "ui": "ю ай", "ux": "ю икс", "qr": "кью ар", "apk": "эй пи кей",
    "html": "эйч ти эм эл", "css": "си эс эс", "js": "джей эс", "sql": "эс кью эл",
    "vpn": "ви пи эн",
}

_TECH_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bwi[\s-]*fi\b", re.I), "вай фай"),
    (re.compile(r"\bvs\s*code\b", re.I), "ви эс код"),
    (re.compile(r"\bopen\s*ai\b", re.I), "оупен эй ай"),
    (re.compile(r"\bchat\s*gpt\b", re.I), "чат джи пи ти"),
    (re.compile(r"\beirven\b", re.I), "Эрви"),
    (re.compile(r"\bhttps\b", re.I), "эйч ти ти пи эс"),
    (re.compile(r"\bhttp\b", re.I), "эйч ти ти пи"),
    (re.compile(r"\bapi\b", re.I), "эй пи ай"),
    (re.compile(r"\bgpu\b", re.I), "джи пи ю"),
    (re.compile(r"\bcpu\b", re.I), "си пи ю"),
    (re.compile(r"\bram\b", re.I), "рэм"),
    (re.compile(r"\bssd\b", re.I), "эс эс ди"),
    (re.compile(r"\busb\b", re.I), "ю эс би"),
    (re.compile(r"\bpdf\b", re.I), "пи ди эф"),
)

_TRANSLIT_GROUPS = (
    ("shch", "щ"), ("sch", "щ"), ("tch", "ч"), ("zh", "ж"), ("kh", "х"),
    ("ch", "ч"), ("sh", "ш"), ("ph", "ф"), ("th", "т"), ("ya", "я"),
    ("yu", "ю"), ("yo", "ё"), ("ye", "е"), ("qu", "кв"), ("ck", "к"),
    ("ee", "и"), ("oo", "у"), ("ai", "ай"), ("ay", "эй"), ("ey", "эй"),
    ("oy", "ой"),
)
_TRANSLIT_CHARS = str.maketrans({
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
    "v": "в", "w": "в", "x": "кс", "y": "й", "z": "з",
})


def _latin_word_for_speech(match: re.Match[str]) -> str:
    raw = match.group(0)
    low = raw.casefold()
    if low in _KNOWN_LATIN:
        return _KNOWN_LATIN[low]
    if low in _KNOWN_ACRONYMS:
        return _KNOWN_ACRONYMS[low]
    value = low
    for source, target in _TRANSLIT_GROUPS:
        value = value.replace(source, target)
    return value.translate(_TRANSLIT_CHARS)


def _number_value_for_speech(raw: str) -> str:
    value = str(raw or "").strip().replace(" ", "").replace(",", ".")
    if "." not in value:
        return cardinal(int(value))
    whole, fraction = value.split(".", 1)
    digits = min(len(fraction), 3)
    fraction = fraction[:digits]
    sign = "минус " if whole.startswith("-") else ""
    whole_value = abs(int(whole or "0"))
    denominator = {1: "десятых", 2: "сотых", 3: "тысячных"}[digits]
    return f"{sign}{cardinal(whole_value)} целых {cardinal(int(fraction or '0'))} {denominator}"


def _url_for_speech(match: re.Match[str]) -> str:
    raw = match.group(0).rstrip(".,!?;:")
    try:
        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
        value = parsed.netloc or parsed.path
        if parsed.netloc and parsed.path not in {"", "/"}:
            value += parsed.path
    except Exception:
        value = raw
    return (
        value.replace("www.", "").replace(".", " точка ").replace("/", " слэш ")
        .replace("-", " дефис ").replace("_", " подчёркивание ")
    )


def speech_ready_text(text: str) -> str:
    """Prepare arbitrary assistant text for Russian-only speech engines.

    Silero Baya may silently skip Arabic digits and Latin tokens.  This normalizer keeps
    the visible answer unchanged and only creates a pronounceable TTS copy.
    """
    value = str(text or "").strip()
    if not value:
        return ""

    # Preserve link captions and useful code words, but remove Markdown control marks.
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = value.replace("```", " ").replace("`", "")
    value = re.sub(r"(^|\s)[*•]\s+", r"\1", value)

    value = re.sub(r"https?://[^\s<>()]+", _url_for_speech, value, flags=re.I)
    for pattern, replacement in _TECH_PATTERNS:
        value = pattern.sub(replacement, value)

    # Context-specific numbers must run before the generic integer replacement.
    def time_repl(match: re.Match[str]) -> str:
        hours, minutes, seconds = (int(match.group(1)), int(match.group(2)), match.group(3))
        out = f"{cardinal(hours)} {_form(hours, 'час', 'часа', 'часов')} {cardinal(minutes)} {_form(minutes, 'минута', 'минуты', 'минут')}"
        if seconds is not None:
            sec = int(seconds)
            out += f" {cardinal(sec)} {_form(sec, 'секунда', 'секунды', 'секунд')}"
        return out

    value = re.sub(r"(?<!\d)([0-2]?\d):([0-5]\d)(?::([0-5]\d))?(?!\d)", time_repl, value)
    value = re.sub(
        r"(?<!\d)(-?\d+(?:[.,]\d+)?)\s*°\s*[cCСс]\b",
        lambda m: f"{_number_value_for_speech(m.group(1))} градусов Цельсия",
        value,
    )
    value = re.sub(
        r"(?<!\d)(-?\d+(?:[.,]\d+)?)\s*%",
        lambda m: (
            f"{_number_value_for_speech(m.group(1))} "
            f"{'процента' if re.search(r'[.,]', m.group(1)) else _form(int(m.group(1)), 'процент', 'процента', 'процентов')}"
        ),
        value,
    )
    value = re.sub(r"(?<!\w)(\d{3,4})\s*[pP]\b", lambda m: f"{cardinal(int(m.group(1)))} пи", value)
    value = re.sub(r"(?<!\w)(\d+)\s*[kK]\b", lambda m: f"{cardinal(int(m.group(1)))} ка", value)
    value = re.sub(
        r"(?<!\d)(\d+)(?:\.(\d+)){2,}(?!\d)",
        lambda m: " точка ".join(cardinal(int(part)) for part in m.group(0).split(".")),
        value,
    )
    value = re.sub(
        r"(?<![\w])(-?\d+[.,]\d+)(?![\w])",
        lambda m: _number_value_for_speech(m.group(1)),
        value,
    )
    value = re.sub(
        r"(?<![\w])(-?\d+)(?![\w])",
        lambda m: cardinal(int(m.group(1))),
        value,
    )
    # Digits inside identifiers/extensions (MP4, GPT-4) are still spoken.
    value = re.sub(r"\d", lambda m: f" {_ONES[int(m.group(0))]} ", value)

    value = value.replace("@", " собака ").replace("&", " и ").replace("#", " решётка ")
    value = re.sub(r"[A-Za-z]+", _latin_word_for_speech, value)
    value = re.sub(r"[_/\\|]+", " ", value)
    value = re.sub(r"[^А-Яа-яЁё\s.,!?;:…—–\-+%°№()\"'«»]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value
