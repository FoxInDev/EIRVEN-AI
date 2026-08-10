from __future__ import annotations

import re
import threading
import time
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx

from .config import Settings
from .system_browser import open_url as open_system_url, foreground_window, paste_text, press


class BrowserError(RuntimeError):
    pass


class BrowserAutomation:
    """Persistent hidden Chromium rendered into EIRVEN spatial widgets.

    The profile is isolated under data/browser-profile. The browser itself stays hidden;
    its live viewport is streamed into the camera scene so websites can be manipulated
    by voice/tools without opening a separate desktop window.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.profile_dir = settings.data_dir / "browser-profile"
        self.download_dir = settings.workspace_dir / "downloads"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._telegram_active_recipient = ""
        self._pending_url = ""
        self._pending_title = ""

    def available(self) -> bool:
        if not self.settings.enable_browser:
            return False
        try:
            import playwright.sync_api  # noqa: F401

            return True
        except ImportError:
            return False

    def _ensure(self) -> Any:
        if not self.settings.enable_browser:
            raise BrowserError("Управление браузером отключено в настройках")
        with self._lock:
            if self._page is not None:
                return self._page
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise BrowserError(
                    "Playwright не установлен. Запустите scripts/repair_windows.ps1"
                ) from exc
            try:
                self._playwright = sync_playwright().start()
                self._context = self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=True,
                    accept_downloads=True,
                    downloads_path=str(self.download_dir),
                    viewport={"width": 1280, "height": 720},
                    locale="ru-RU",
                )
                self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
                return self._page
            except Exception as exc:
                self.close()
                raise BrowserError(
                    "Не удалось запустить Chromium. Выполните: .venv\\Scripts\\python -m playwright install chromium"
                ) from exc

    @staticmethod
    def _safe_url(url: str) -> str:
        url = url.strip()
        if not url:
            raise BrowserError("Пустой адрес")
        parsed = urlparse(url if "://" in url else f"https://{url}")
        if parsed.scheme not in {"http", "https"}:
            raise BrowserError("Разрешены только http/https адреса")
        if parsed.hostname in {"127.0.0.1", "localhost"}:
            return parsed.geturl()
        return parsed.geturl()

    def open(self, url: str) -> dict[str, Any]:
        target = self._safe_url(url)
        self._pending_url = target
        self._pending_title = (urlparse(target).hostname or target)
        try:
            with self._lock:
                page = self._ensure()
                page.goto(target, wait_until="commit", timeout=20_000)
                return {"url": page.url, "title": page.title()}
        finally:
            self._pending_url = ""
            self._pending_title = ""

    def search(self, query: str) -> dict[str, Any]:
        return self.open(f"https://duckduckgo.com/?q={quote_plus(query)}")

    def snapshot(self, max_chars: int = 30_000) -> dict[str, Any]:
        with self._lock:
            page = self._ensure()
            try:
                text = page.locator("body").inner_text(timeout=10_000)
            except Exception:
                text = ""
            links = []
            try:
                for item in page.locator("a").all()[:80]:
                    label = (item.inner_text(timeout=1000) or "").strip()
                    href = item.get_attribute("href") or ""
                    if label or href:
                        links.append({"text": label[:200], "href": href[:500]})
            except Exception:
                pass
            return {
                "url": page.url,
                "title": page.title(),
                "text": text[:max_chars],
                "truncated": len(text) > max_chars,
                "links": links,
            }

    def click_text(self, text: str, exact: bool = False) -> dict[str, Any]:
        with self._lock:
            page = self._ensure()
            locator = page.get_by_text(text, exact=exact).first
            locator.click(timeout=20_000)
            page.wait_for_timeout(500)
            return {"url": page.url, "title": page.title(), "clicked": text}

    def fill(self, selector_or_label: str, value: str) -> dict[str, Any]:
        with self._lock:
            page = self._ensure()
            target = None
            try:
                target = page.get_by_label(selector_or_label).first
                if target.count() == 0:
                    target = None
            except Exception:
                target = None
            if target is None:
                target = page.locator(selector_or_label).first
            target.fill(value, timeout=20_000)
            return {"filled": selector_or_label, "chars": len(value), "url": page.url}

    def press(self, key: str) -> dict[str, Any]:
        with self._lock:
            page = self._ensure()
            page.keyboard.press(key)
            return {"pressed": key, "url": page.url}

    def upload_file(self, path: str, selector: str = "input[type=file]") -> dict[str, Any]:
        target_path = Path(path).expanduser().resolve()
        if not target_path.is_file():
            raise BrowserError(f"Файл для загрузки не найден: {target_path}")
        if target_path.stat().st_size > 100_000_000:
            raise BrowserError("Файл больше 100 МБ; загрузка через браузерный инструмент остановлена")
        with self._lock:
            page = self._ensure()
            locator = page.locator(selector).first
            if locator.count() == 0:
                raise BrowserError(f"Поле загрузки не найдено: {selector}")
            locator.set_input_files(str(target_path), timeout=20_000)
            page.wait_for_timeout(350)
            return {"uploaded": str(target_path), "selector": selector, "url": page.url, "title": page.title()}

    def screenshot(self) -> dict[str, Any]:
        with self._lock:
            page = self._ensure()
            folder = self.settings.data_dir / "screenshots"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"browser-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
            page.screenshot(path=str(path), full_page=False)
            return {"path": str(path), "url": page.url, "title": page.title()}


    def spatial_state(self) -> dict[str, Any]:
        """Return non-invasive state for the fullscreen spatial UI."""
        # Navigation can hold the Playwright lock for several seconds. Expose a pending
        # state immediately so the camera UI shows a real loading tile instead of silence.
        if self._pending_url:
            return {"active": True, "url": self._pending_url, "title": self._pending_title or "Загрузка…", "loading": True}
        with self._lock:
            if self._page is None:
                return {"active": False, "url": "", "title": ""}
            try:
                return {
                    "active": True,
                    "url": str(self._page.url or ""),
                    "title": str(self._page.title() or ""),
                }
            except Exception:
                return {"active": False, "url": "", "title": ""}

    def frame(self, *, quality: int = 74) -> bytes:
        """Capture the current managed-browser viewport for a spatial widget."""
        with self._lock:
            if self._page is None:
                raise BrowserError("Пространственный браузер ещё не открыт")
            try:
                data = self._page.screenshot(type="jpeg", quality=max(35, min(90, int(quality))), full_page=False)
                if not data:
                    raise BrowserError("Браузер не вернул кадр")
                return bytes(data)
            except BrowserError:
                raise
            except Exception as exc:
                raise BrowserError(f"Не удалось получить кадр пространственного браузера: {exc}") from exc


    @staticmethod
    def _official_score(target: str, title: str, href: str) -> float:
        needle = re.sub(r"[^a-zа-я0-9]+", " ", target.casefold()).strip()
        hay = f"{title} {href}".casefold()
        tokens = [t for t in needle.split() if len(t) >= 3 and t not in {"приложение", "app", "web", "официальный", "сайт"}]
        score = sum(3.0 for t in tokens if t in hay)
        host = (urlparse(href).hostname or "").casefold().removeprefix("www.")
        host_root = host.split(".", 1)[0]
        if any(t in host for t in tokens):
            score += 5.0
        # Brand ASR often differs from the real domain by one transliteration letter
        # (e.g. a spoken ...story vs a registered ...stori). Prefer a near-matching host
        # over an article/social post whose title merely mentions the brand.
        if tokens and host_root:
            similarity = max(SequenceMatcher(None, t, host_root).ratio() for t in tokens)
            if similarity >= .90:
                score += 8.0
            elif similarity >= .76:
                score += 5.0
        # Social networks, marketplaces and download/wiki mirrors are rarely the official
        # destination for a generic "открой сайт BRAND" request.
        if any(x in host for x in (
            "pinterest.", "instagram.", "facebook.", "vk.com", "tiktok.", "youtube.",
            "wikipedia.org", "softonic", "uptodown", "4pda", "github.com",
            "market.yandex", "ozon.", "wildberries.",
        )):
            score -= 7.0
        return score

    def search_first_site(self, query: str, *, open_visible: bool = True) -> dict[str, Any]:
        """Search without an API key and open the best actual result, not the SERP."""
        try:
            from ddgs import DDGS
            rows = list(DDGS(timeout=5).text(query, region="ru-ru", safesearch="moderate", max_results=6))
        except Exception as exc:
            raise BrowserError(f"Поиск не сработал: {exc}") from exc
        candidates=[]
        for row in rows:
            href=str(row.get("href") or row.get("url") or "").strip()
            title=str(row.get("title") or "").strip()
            if not href.startswith(("http://","https://")): continue
            candidates.append((self._official_score(query,title,href), title, href))
        if not candidates:
            raise BrowserError("Поиск не вернул подходящий сайт")
        candidates.sort(key=lambda x:x[0], reverse=True)
        _, title, href=candidates[0]
        if open_visible:
            open_system_url(href)
        else:
            self.open(href)
        return {"url":href,"title":title,"query":query,"results":len(candidates)}

    def weather(self, location: str = "") -> dict[str, Any]:
        """Current weather without API keys; wttr.in auto-locates when city is omitted."""
        place = quote_plus(location.strip()) if location.strip() else ""
        url = f"https://wttr.in/{place}?format=j1&lang=ru"
        try:
            response=httpx.get(url, timeout=6, headers={"User-Agent":"EIRVEN-AI/1.2"}, follow_redirects=True)
            response.raise_for_status()
            payload=response.json()
            current=(payload.get("current_condition") or [{}])[0]
            nearest=(payload.get("nearest_area") or [{}])[0]
            area=((nearest.get("areaName") or [{}])[0].get("value") if isinstance(nearest,dict) else "") or location
            desc=((current.get("lang_ru") or current.get("weatherDesc") or [{}])[0].get("value") if isinstance(current,dict) else "") or ""
            return {"location":area,"temp_c":current.get("temp_C"),"feels_c":current.get("FeelsLikeC"),"condition":desc,"humidity":current.get("humidity"),"wind_kmph":current.get("windspeedKmph"),"source":"wttr.in"}
        except Exception as exc:
            raise BrowserError(f"Не удалось получить погоду: {exc}") from exc

    def currency_rate(self, code: str = "USD") -> dict[str, Any]:
        """Official Central Bank of Russia daily rate, no API key."""
        import xml.etree.ElementTree as ET
        code=(code or "USD").upper()
        try:
            response=httpx.get("https://www.cbr.ru/scripts/XML_daily.asp", timeout=6, headers={"User-Agent":"EIRVEN-AI/1.2"}, follow_redirects=True)
            response.raise_for_status()
            root=ET.fromstring(response.content)
            for valute in root.findall("Valute"):
                if (valute.findtext("CharCode") or "").upper()==code:
                    nominal=int(valute.findtext("Nominal") or "1")
                    value=float((valute.findtext("Value") or "0").replace(",","."))
                    return {"code":code,"rub":value/nominal,"date":root.attrib.get("Date","") or "","source":"Банк России"}
            raise BrowserError(f"Валюта {code} не найдена")
        except Exception as exc:
            raise BrowserError(f"Не удалось получить курс {code}: {exc}") from exc

    @staticmethod
    def _uia_elements(window: Any) -> list[Any]:
        try:
            return list(window.descendants())
        except Exception:
            return []

    @staticmethod
    def _uia_name(element: Any) -> str:
        try:
            return str(element.element_info.name or element.window_text() or '').strip()
        except Exception:
            return ''

    def _uia_click_named(self, window: Any, names: tuple[str, ...], *, threshold: float = .70) -> str:
        best=None
        for element in self._uia_elements(window):
            label=self._uia_name(element)
            if not label: continue
            folded=label.casefold()
            for name in names:
                n=name.casefold()
                score=1.0 if folded==n else (0.93 if n in folded else SequenceMatcher(None,n,folded).ratio())
                if best is None or score>best[0]: best=(score,element,label)
        if not best or best[0] < threshold:
            raise BrowserError(f"Элемент «{names[0]}» не найден в браузере по умолчанию")
        try:
            best[1].click_input()
        except Exception:
            best[1].invoke()
        return best[2]

    def yandex_music_wave(self) -> dict[str, Any]:
        """Use the owner's default browser/profile, never Playwright Testing Chrome."""
        if not open_system_url('https://music.yandex.ru/'):
            raise BrowserError('Не удалось открыть браузер по умолчанию')
        time.sleep(2.0)
        last_error=''
        for attempt in range(5):
            try:
                window=foreground_window(4.0)
                for dismiss in (('Закрыть','Close'),('Не сейчас','Not now'),('Понятно','Got it'),('Позже','Later')):
                    try: self._uia_click_named(window,dismiss,threshold=.82)
                    except Exception: pass
                self._uia_click_named(window,('Моя волна','My Wave'),threshold=.62)
                time.sleep(.7)
                window=foreground_window(2.0)
                self._uia_click_named(window,('Воспроизведение','Воспроизвести','Play'),threshold=.64)
                return {'ok':True,'attempt':attempt+1,'browser':'system_default','url':'https://music.yandex.ru/'}
            except Exception as exc:
                last_error=str(exc)
                try:
                    press('ctrl','r'); time.sleep(1.25)
                except Exception: pass
        raise BrowserError(f'Не удалось запустить Мою волну в браузере по умолчанию: {last_error}')

    def _telegram_window(self, recipient: str) -> Any:
        if not open_system_url('https://web.telegram.org/a/'):
            raise BrowserError('Не удалось открыть браузер по умолчанию')
        time.sleep(1.8)
        window=foreground_window(5.0)
        elements=self._uia_elements(window)
        search=None
        for el in elements:
            label=self._uia_name(el).casefold()
            ctype=str(getattr(el.element_info,'control_type','') or '').casefold()
            if ctype in {'edit','document'} and ('search' in label or 'поиск' in label or not label):
                search=el; break
        if search is None:
            raise BrowserError('Telegram Web открыт, но поле поиска чатов не доступно через Windows UI Automation. Проверь вход в аккаунт и повтори.')
        try: search.click_input()
        except Exception: pass
        try:
            search.set_edit_text(recipient)
        except Exception:
            try: press('ctrl','a'); paste_text(recipient)
            except Exception as exc: raise BrowserError(f'Не удалось ввести имя чата: {exc}') from exc
        time.sleep(.8)
        aliases=[recipient.strip()]
        if recipient.casefold() in {'мама','маме','маму','мам','мамочка'}: aliases += ['мама','мамочка','мать']
        best=None
        for el in self._uia_elements(window):
            label=self._uia_name(el)
            if not label: continue
            for term in aliases:
                score=SequenceMatcher(None,term.casefold(),label.casefold()).ratio()
                if term.casefold() in label.casefold(): score += .30
                if best is None or score>best[0]: best=(score,el,label)
        if not best or best[0] < .60:
            raise BrowserError(f'Не нашла чат «{recipient}». Назови точнее, как он записан в Telegram.')
        try: best[1].click_input()
        except Exception: best[1].invoke()
        time.sleep(.6)
        self._telegram_active_recipient = recipient.strip().casefold()
        return foreground_window(2.0)

    def telegram_active_send(self, recipient: str, text: str) -> dict[str, Any]:
        """Send through an already focused Telegram Desktop window.

        Prefer Windows accessibility; fall back to Telegram's quick-switch keyboard
        flow. This keeps the owner's logged-in desktop session and avoids any test
        browser/profile.
        """
        recipient = str(recipient or "").strip()
        clean = str(text or "").strip()
        if not recipient or not clean:
            raise BrowserError("Не указаны получатель или текст сообщения")
        window = foreground_window(2.0)
        elements = self._uia_elements(window)
        search = None
        for el in elements:
            label = self._uia_name(el).casefold()
            ctype = str(getattr(el.element_info, 'control_type', '') or '').casefold()
            try:
                rect = el.rectangle()
                topish = rect.top < window.rectangle().top + max(260, int(window.rectangle().height() * .38))
            except Exception:
                topish = True
            if ctype in {'edit', 'document'} and topish and ('search' in label or 'поиск' in label or not label):
                search = el
                break
        if search is not None:
            try:
                search.click_input()
            except Exception:
                pass
            press('ctrl', 'a'); paste_text(recipient); time.sleep(.65)
            best = None
            for el in self._uia_elements(window):
                label = self._uia_name(el)
                if not label:
                    continue
                score = SequenceMatcher(None, recipient.casefold(), label.casefold()).ratio()
                if recipient.casefold() in label.casefold():
                    score += .35
                if best is None or score > best[0]:
                    best = (score, el, label)
            if best and best[0] >= .67:
                try:
                    best[1].click_input()
                except Exception:
                    try:
                        best[1].invoke()
                    except Exception:
                        press('enter')
            else:
                press('enter')
        else:
            # Telegram Desktop quick switch. The app is already foreground because the
            # launcher was invoked immediately before this helper.
            press('ctrl', 'k'); time.sleep(.15); paste_text(recipient); time.sleep(.7); press('enter')
        time.sleep(.55)
        window = foreground_window(1.0)
        composer = None
        lowest = -1
        for el in self._uia_elements(window):
            label = self._uia_name(el).casefold()
            ctype = str(getattr(el.element_info, 'control_type', '') or '').casefold()
            if ctype not in {'edit', 'document'}:
                continue
            if any(x in label for x in ('message', 'сообщ', 'write', 'написать')) or not label:
                try:
                    y = int(el.rectangle().top)
                except Exception:
                    y = 0
                if y >= lowest:
                    lowest, composer = y, el
        if composer is not None:
            try:
                composer.click_input()
            except Exception:
                pass
        else:
            try:
                r = window.rectangle()
                import pyautogui
                pyautogui.click(int(r.left + r.width() * .62), int(r.bottom - max(55, r.height() * .075)))
            except Exception as exc:
                raise BrowserError(f"Не удалось сфокусировать поле сообщения: {exc}") from exc
        paste_text(clean); time.sleep(.10); press('enter'); time.sleep(.25)
        return {'sent': True, 'recipient': recipient, 'chars': len(clean), 'client': 'telegram_desktop'}

    def telegram_recent_outgoing(self, recipient: str) -> dict[str, Any]:
        # Reading exact outgoing DOM is browser-specific in the system browser. Return
        # accessible visible text when available; sending itself never depends on it.
        window=self._telegram_window(recipient)
        values=[]
        for el in self._uia_elements(window):
            label=self._uia_name(el)
            if label and 2 <= len(label) <= 500:
                values.append(label)
        return {'recipient':recipient,'messages':values[-12:],'browser':'system_default'}

    def telegram_send(self, recipient: str, text: str) -> dict[str, Any]:
        clean=text.strip()
        if not clean: raise BrowserError('Пустое сообщение')
        if self._telegram_active_recipient == recipient.strip().casefold():
            window = foreground_window(1.5)
        else:
            window = self._telegram_window(recipient)
        edits=[]
        for el in self._uia_elements(window):
            ctype=str(getattr(el.element_info,'control_type','') or '').casefold()
            label=self._uia_name(el).casefold()
            if ctype in {'edit','document'} and ('message' in label or 'сообщ' in label or 'write' in label or not label):
                edits.append(el)
        if edits:
            composer=edits[-1]
            try: composer.click_input()
            except Exception: pass
        else:
            # Chat was verified above. Telegram Web puts the composer in the center panel;
            # clicking the lower-center point is only the final accessibility fallback.
            try:
                r=window.rectangle(); import pyautogui
                pyautogui.click(int(r.left+r.width()*.64), int(r.top+r.height()*.92))
            except Exception as exc: raise BrowserError(f'Поле сообщения не найдено: {exc}') from exc
        paste_text(clean); time.sleep(.12); press('enter'); time.sleep(.35)
        return {'sent':True,'recipient':recipient,'chars':len(clean),'browser':'system_default'}

    def crypto_price(self, symbol: str = "bitcoin", currency: str = "usd") -> dict[str, Any]:
        coin = symbol.strip().lower()
        aliases = {"btc": "bitcoin", "биток": "bitcoin", "eth": "ethereum", "эфир": "ethereum"}
        coin = aliases.get(coin, coin)
        currency = re.sub(r"[^a-z]", "", currency.lower()) or "usd"
        try:
            response = httpx.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin, "vs_currencies": currency, "include_last_updated_at": "true"},
                timeout=15,
                headers={"User-Agent": "EIRVEN-AI/0.2"},
            )
            response.raise_for_status()
            data = response.json().get(coin)
            if not data or currency not in data:
                raise BrowserError(f"Цена {coin} не найдена")
            return {
                "asset": coin,
                "currency": currency,
                "price": data[currency],
                "last_updated_at": data.get("last_updated_at"),
                "source": "CoinGecko",
            }
        except Exception as exc:
            raise BrowserError(f"Не удалось получить цену: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            try:
                if self._context is not None:
                    self._context.close()
            except Exception:
                pass
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            except Exception:
                pass
            self._page = None
            self._context = None
            self._playwright = None
            self._telegram_active_recipient = ""
        self._pending_url = ""
        self._pending_title = ""
