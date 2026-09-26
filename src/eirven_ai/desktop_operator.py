from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .trace import log_event


class DesktopOperator:
    """Human-like Windows operator using the visible desktop as the source of truth.

    Accessibility is preferred because it is faster and less error-prone. When accessibility
    does not expose the control, a tiny local multimodal model may inspect a real screenshot
    and choose one of a very small set of mouse/keyboard actions. Every action is bounded,
    cancellable and followed by a state check.
    """

    def __init__(self, services: Any, learning: Any):
        self.services = services
        self.tools = services.tools
        self.gateway = services.gateway
        self.learning = learning
        self._lock = getattr(services, "desktop_lock", None) or threading.RLock()

    @staticmethod
    def _norm(text: str) -> str:
        text = str(text or "").casefold().replace("ё", "е")
        text = re.sub(r"[^a-zа-я0-9]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _coerce_window_id(value: Any) -> int:
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _element_blob(element: dict[str, Any]) -> str:
        return " ".join(
            str(element.get(key) or "")
            for key in ("control_type", "name", "value", "automation_id", "class_name")
        )

    def _trace(self, event: str, **data: Any) -> None:
        try:
            log_event(self.services.settings.root_dir, event, **data)
        except Exception:
            pass

    def _windows(self) -> list[dict[str, Any]]:
        result = self.tools.execute("window_list", {"max_windows": 80})
        return list(result.get("result") or []) if result.get("ok") else []

    def wait_window(self, needles: list[str], timeout: float = 4.0) -> dict[str, Any] | None:
        wanted = [self._norm(x) for x in needles if self._norm(x)]
        end = time.monotonic() + max(.2, timeout)
        while time.monotonic() < end:
            for row in self._windows():
                title = self._norm(row.get("title"))
                if any(n in title or title in n for n in wanted):
                    return row
            time.sleep(.12)
        return None

    def _elements(self, title: str, limit: int = 320, *, handle: int | None = None) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"title_contains": title, "max_elements": limit}
        if handle:
            args["handle"] = int(handle)
        result = self.tools.execute("window_elements", args)
        return list(result.get("result") or []) if result.get("ok") else []

    @staticmethod
    def _score(label: str, terms: list[str]) -> float:
        label_n = DesktopOperator._norm(label)
        if not label_n:
            return 0.0
        best = 0.0
        for term in terms:
            term_n = DesktopOperator._norm(term)
            if not term_n:
                continue
            score = SequenceMatcher(None, term_n, label_n).ratio()
            if term_n == label_n:
                score += .75
            elif term_n in label_n or label_n in term_n:
                score += .35
            best = max(best, score)
        return best

    def find_element(self, title: str, terms: list[str], *, types: tuple[str, ...] = (), content_only: bool = False) -> dict[str, Any] | None:
        best: tuple[float, dict[str, Any]] | None = None
        wanted_types = {self._norm(t) for t in types}
        for el in self._elements(title):
            if not el.get("visible", True) or not el.get("enabled", True):
                continue
            ctype = self._norm(el.get("control_type"))
            if wanted_types and ctype not in wanted_types:
                continue
            rect = el.get("rectangle") or []
            # Browser chrome lives above ~140 px in the user's current browser. App skills
            # must never confuse a page popup's "Закрыть" with a tab/window close button.
            if content_only and len(rect) == 4 and int(rect[3]) <= 150:
                continue
            blob = f"{el.get('name','')} {el.get('automation_id','')} {el.get('class_name','')}"
            score = self._score(blob, terms)
            if best is None or score > best[0]:
                best = (score, el)
        return best[1] if best and best[0] >= .60 else None

    @staticmethod
    def _rect_area(rect: list[Any] | tuple[Any, ...]) -> int:
        if len(rect or []) != 4:
            return 0
        try:
            return max(0, int(rect[2]) - int(rect[0])) * max(0, int(rect[3]) - int(rect[1]))
        except Exception:
            return 0

    def _is_browser_chrome(self, element: dict[str, Any]) -> bool:
        rect = element.get("rectangle") or []
        blob = self._norm(f"{element.get('name','')} {element.get('automation_id','')} {element.get('class_name','')}")
        # Telegram Web can start at y=109 under Windows DPI scaling. Its stable DOM/UIA
        # semantics outrank the old y<=150 browser-toolbar heuristic.
        if any(marker in blob for marker in (
            "telegram search input", "telegram-search-input", "input search input",
            "input-search-input", "input message input", "input-message-input",
            "editable message text", "editable-message-text", "btn send", "btn-send",
            "chatlist", "chat list",
        )):
            return False
        if len(rect) == 4 and int(rect[3]) <= 150:
            return True
        return any(marker in blob for marker in (
            "omnibox", "address bar", "адресная строка", "tabs toolbar", "tabstrip",
            "browserappmenubutton", "mediatoolbarbuttonview", "windowcaptionbutton", "view 1012",
        ))

    @classmethod
    def _telegram_send_button(
        cls, rows: list[dict[str, Any]], *, ready_only: bool = True,
    ) -> dict[str, Any] | None:
        """Return Telegram's real composer button, never a same-window background tab.

        Telegram Web exposes the same ``btn-send`` control in two states: ``record``
        while the composer is empty and a separate ``send`` class token after text has
        landed.  The state transition is useful input evidence even when Chromium does
        not expose the contenteditable value through UIA.
        """
        candidates: list[tuple[float, dict[str, Any]]] = []
        for element in rows:
            if not element.get("visible", True) or not element.get("enabled", True):
                continue
            if cls._norm(element.get("control_type")) != "button":
                continue
            rect = element.get("rectangle") or []
            if len(rect) != 4 or int(rect[3]) <= 150:
                continue
            raw_class = str(element.get("class_name") or "").casefold()
            class_tokens = {token for token in re.split(r"\s+", raw_class) if token}
            is_telegram = "btn-send" in raw_class
            if not is_telegram:
                continue
            ready = "send" in class_tokens and "record" not in class_tokens
            if ready_only and not ready:
                continue
            score = 20.0 + (8.0 if ready else 0.0) + min(4.0, int(rect[1]) / 400.0)
            candidates.append((score, element))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def commit_composer(self, acquired: dict[str, Any]) -> dict[str, Any]:
        """Commit one verified composer payload exactly once.

        Telegram gets a semantic click on its ready ``btn-send`` control. Other apps
        use an exposed Send button when available and retain Enter as a bounded fallback.
        A successful click is never followed by Enter, preventing duplicate messages.
        """
        if not acquired.get("ok"):
            return {"ok": False, "committed": False, "error": "Поле ввода не готово"}
        title = str(acquired.get("title") or "")
        handle = int(acquired.get("handle") or 0) or None
        rows = self._elements(title, limit=420, handle=handle)
        # Telegram Web changes the browser tab/window title to the active contact on
        # some Chromium builds.  The stable evidence is its unique ``btn-send`` class,
        # not the word Telegram in the title.
        send = self._telegram_send_button(rows, ready_only=True)
        empty_button = self._telegram_send_button(rows, ready_only=False)
        telegram_surface = bool(
            "telegram" in self._norm(title)
            or "телеграм" in self._norm(title)
            or send is not None
            or empty_button is not None
        )
        if telegram_surface:
            if send is not None:
                ok = self.click_element(title, send, goal="telegram_send_message_commit")
                self._trace("OPERATOR_COMMIT", app="telegram", method="send_button", ok=ok)
                return {
                    "ok": ok, "committed": ok, "method": "send_button",
                    "error": "Кнопка отправки Telegram найдена, но клик не выполнился" if not ok else "",
                }
            # Native Telegram clients do not always publish a Send button through UIA.
            # Enter remains a single-shot fallback only when no web btn-send exists.
            if empty_button is not None:
                return {
                    "ok": False, "committed": False, "method": "send_button",
                    "error": "Telegram показывает кнопку записи: текст не готов к отправке",
                }
        send = self.resolve_element(
            title, ["Отправить", "Send"], handle=handle, roles=("Button",),
            purpose="activate", content_only=True, rows=rows,
        )
        if send is not None:
            ok = self.click_element(title, send, goal="composer_send_message_commit")
            self._trace("OPERATOR_COMMIT", method="send_button", ok=ok, title=title)
            return {
                "ok": ok, "committed": ok, "method": "send_button",
                "error": "Кнопка отправки найдена, но клик не выполнился" if not ok else "",
            }
        ok = bool(self.tools.execute("press_key", {"key": "enter"}).get("ok", False))
        self._trace("OPERATOR_COMMIT", method="enter", ok=ok, title=title)
        return {
            "ok": ok, "committed": ok, "method": "enter",
            "error": "Не удалось подтвердить отправку" if not ok else "",
        }

    def _ui_fingerprint(self, rows: list[dict[str, Any]]) -> str:
        parts=[]
        for e in rows:
            if not e.get("visible", True):
                continue
            rect=e.get("rectangle") or []
            if len(rect)!=4:
                continue
            blob=self._norm(f"{e.get('control_type','')}|{e.get('name','')}|{e.get('automation_id','')}|{e.get('class_name','')}|{e.get('value','')}")
            if blob:
                parts.append(f"{blob}@{int(rect[0])//8},{int(rect[1])//8},{int(rect[2])//8},{int(rect[3])//8}")
        return hashlib.sha1("\n".join(parts[:360]).encode("utf-8",errors="ignore")).hexdigest()

    def resolve_element(
        self, title: str, targets: list[str] | tuple[str, ...] | str, *,
        handle: int | None = None, roles: tuple[str, ...] = (), purpose: str = "activate",
        content_only: bool = True, rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Resolve a semantic UI target without app-specific coordinates.

        Role and geometry are first-class evidence. A plain Text label can describe a target,
        but it must not outrank an actual Button/Hyperlink/Edit just because the words match.
        """
        target_list=[targets] if isinstance(targets,str) else list(targets or [])
        wanted=[self._norm(x) for x in target_list if self._norm(x)]
        wanted_roles={self._norm(x) for x in roles}
        rows=list(rows) if rows is not None else self._elements(title,limit=360,handle=handle)
        best: tuple[float,dict[str,Any]]|None=None
        for e in rows:
            if not e.get("visible",True) or not e.get("enabled",True):
                continue
            rect=e.get("rectangle") or []
            if len(rect)!=4 or self._rect_area(rect)<80:
                continue
            if content_only and self._is_browser_chrome(e):
                continue
            typ=self._norm(e.get("control_type")); name=self._norm(e.get("name")); aid=self._norm(e.get("automation_id")); cls=self._norm(e.get("class_name")); value=self._norm(e.get("value"))
            if wanted_roles and typ not in wanted_roles:
                continue
            blob=" ".join(x for x in (name,aid,cls,value) if x)
            if not blob and purpose not in {"input","composer"}:
                continue
            score=0.0
            for target in wanted:
                if not target: continue
                target_tokens=[x for x in target.split() if len(x)>=2]
                if name==target: score=max(score,8.0)
                elif target==aid or target==cls: score=max(score,7.5)
                elif target in name: score=max(score,6.0)
                elif target in blob: score=max(score,5.0)
                elif target_tokens and all(tok in blob for tok in target_tokens): score=max(score,4.6)
                else:
                    score=max(score,SequenceMatcher(None,target,name or blob).ratio()*3.2)
            if purpose in {"input","search","composer"}:
                if typ=="edit": score+=6.0
                elif typ=="combobox": score+=5.5
                elif typ in {"group","document"}: score+=1.0
                else: score-=2.0
                markers=("search","поиск","query","find") if purpose=="search" else ("message","сообщение","composer","contenteditable","textbox","input message","write a message","reply")
                if any(x in blob for x in markers): score+=4.0
                if purpose=="composer":
                    score+=min(3.0,max(0.0,(int(rect[1])-220)/420.0))
                if self._rect_area(rect)>2_500_000: score-=5.0
            else:
                if typ in {"button","hyperlink","listitem","menuitem","tabitem","treeitem","checkbox"}: score+=3.2
                elif typ in {"group"}: score+=0.3
                elif typ=="text": score-=2.0
                if "active" in cls or "selected" in cls or "current" in cls: score+=0.6
            if e.get("focused"):
                score+=1.5 if purpose in {"input","search","composer"} else .2
            # Prefer page content over low-confidence zero-size/offscreen artefacts.
            if int(rect[3]) > 150: score+=.4
            if best is None or score>best[0]: best=(score,e)
        threshold=5.6 if purpose in {"input","search","composer"} else 4.2
        return best[1] if best and best[0]>=threshold else None

    def wait_for_state(
        self, *, handle: int | None, title: str, before_rows: list[dict[str, Any]] | None = None,
        timeout: float = 6.0, stable_for: float = .35, expected: list[str] | None = None,
    ) -> dict[str, Any]:
        """Wait for a SPA/page transition and then for the new UI to settle."""
        before_sig=self._ui_fingerprint(before_rows or []) if before_rows is not None else ""
        end=time.monotonic()+max(.4,min(float(timeout),10.0))
        last_sig=""; stable_since=0.0; changed=False; latest_rows=[]; latest_title=title
        expected_n=[self._norm(x) for x in (expected or []) if self._norm(x)]
        while time.monotonic()<end:
            try:
                fg=self.tools.execute("foreground_window",{})
                if fg.get("ok"):
                    row=dict(fg.get("result") or {})
                    if not handle or int(row.get("handle") or 0)==int(handle):
                        latest_title=str(row.get("title") or latest_title)
            except Exception:
                pass
            latest_rows=self._elements(latest_title,limit=320,handle=handle)
            sig=self._ui_fingerprint(latest_rows)
            if before_sig and sig and sig!=before_sig:
                changed=True
            expected_seen=False
            if expected_n:
                content=" ".join(self._norm(f"{e.get('name','')} {e.get('automation_id','')} {e.get('class_name','')} {e.get('value','')}") for e in latest_rows if e.get("visible",True) and not self._is_browser_chrome(e))
                expected_seen=any(x in content for x in expected_n)
            now=time.monotonic()
            if sig and sig==last_sig:
                if not stable_since: stable_since=now
            else:
                stable_since=0.0; last_sig=sig
            if (changed or expected_seen) and stable_since and now-stable_since>=stable_for:
                return {"changed":changed,"expected_seen":expected_seen,"settled":True,"title":latest_title,"rows":latest_rows,"fingerprint":sig}
            time.sleep(.16)
        return {"changed":changed,"expected_seen":False,"settled":False,"title":latest_title,"rows":latest_rows,"fingerprint":last_sig}

    def _click_rect(self, element: dict[str, Any]) -> bool:
        rect=element.get("rectangle") or []
        if len(rect)!=4: return False
        x=int((int(rect[0])+int(rect[2]))/2); y=int((int(rect[1])+int(rect[3]))/2)
        self.tools.execute("mouse_move",{"x":x,"y":y,"duration":.10})
        return bool(self.tools.execute("click",{"x":x,"y":y}).get("ok"))

    def _click_input_rect(self, element: dict[str, Any]) -> bool:
        """Click the safely visible upper-middle of a potentially clipped web input."""
        rect=element.get("rectangle") or []
        if len(rect)!=4: return self._click_rect(element)
        left,top,right,bottom=[int(v) for v in rect]
        width=max(1,right-left)
        blob=self._norm(f"{element.get('name','')} {element.get('automation_id','')} {element.get('class_name','')}")
        # Telegram Web (notably in Samsung Browser) exposes its contenteditable composer
        # as two overlapping Groups.  Clicking the geometric centre can hit the fake
        # overlay and never place a caret.  The real placeholder/caret zone is close to
        # the left padding, so use it for message/contenteditable fields while retaining
        # the centre for ordinary native Edit controls and search boxes.
        composer=any(marker in blob for marker in (
            "input message", "input-message", "editable message", "editable-message",
            "write a message", "composer", "contenteditable", "textbox", "reply",
        ))
        x=int(left+min(max(36,width*.12),132)) if composer else int((left+right)/2)
        height=max(1,bottom-top)
        y=int(top+min(height/2.0,36.0))
        self.tools.execute("mouse_move",{"x":x,"y":y,"duration":.10})
        return bool(self.tools.execute("click",{"x":x,"y":y}).get("ok"))

    @staticmethod
    def _rect_contains(outer: list[Any] | tuple[Any, ...], inner: list[Any] | tuple[Any, ...]) -> bool:
        if len(outer or []) != 4 or len(inner or []) != 4:
            return False
        try:
            return (
                int(outer[0]) <= int(inner[0]) <= int(inner[2]) <= int(outer[2])
                and int(outer[1]) <= int(inner[1]) <= int(inner[3]) <= int(outer[3])
            )
        except Exception:
            return False

    def acquire_input(
        self, *, purpose: str, aliases: list[str], trigger_aliases: list[str] | None = None,
        max_scrolls: int = 0, visual_fallback: bool = False,
        title_hint: str = "", handle_hint: int | None = None,
    ) -> dict[str, Any]:
        """Find, reveal and focus an input. Never type blindly after only a visual click."""
        handle=int(handle_hint or 0) or None
        title=str(title_hint or "").strip()
        if handle and title:
            # The caller already resolved this exact window. Keep that HWND as source
            # of truth even when focus telemetry still reports the previous app.
            self.tools.execute("window_focus", {"handle": handle})
        else:
            fg=self.tools.execute("foreground_window",{})
            if not fg.get("ok"): return {"ok":False,"error":"Не вижу активное окно"}
            win=dict(fg.get("result") or {})
            title=title or str(win.get("title") or "")
            handle=handle or (int(win.get("handle") or 0) or None)
        def rows(): return self._elements(title,limit=360,handle=handle)
        def search_safe(element):
            blob=self._norm(f"{element.get('name','')} {element.get('value','')} {element.get('automation_id','')} {element.get('class_name','')}")
            if any(mark in blob for mark in ("сортир","sort","фильтр","filter","диапазон цен","price range","quantity","количество")):
                return False
            return any(self._norm(alias) in blob for alias in aliases if self._norm(alias))
        def field(current):
            roles=("Edit","ComboBox","Group","Document")
            candidates=current
            if purpose == "search":
                candidates=[element for element in current if search_safe(element)]
            return self.resolve_element(title,aliases,handle=handle,roles=roles,purpose=purpose,content_only=True,rows=candidates)
        moved=0; current=rows(); target=field(current)
        # Web apps often expose a placeholder as Text while the actual contenteditable
        # is an unnamed focusable Group/Document. Use the visible descriptor only to
        # acquire focus, then require a real focused input-like element before typing.
        if target is None:
            descriptor=self.resolve_element(title,aliases,handle=handle,roles=("Text",),purpose="activate",content_only=True,rows=current)
            if descriptor is not None and self._click_input_rect(descriptor):
                time.sleep(.14); after=rows()
                focused=next((e for e in after if e.get("focused") and self._norm(e.get("control_type")) in {"edit","combobox","group","document"} and not self._is_browser_chrome(e)),None)
                if focused is not None:
                    current=after; target=focused
        # Search/menu buttons often reveal the actual Edit only after navigation.
        if target is None and trigger_aliases:
            trigger=self.resolve_element(title,trigger_aliases,handle=handle,roles=("Button","Hyperlink","ListItem","MenuItem","TabItem","Group"),purpose="activate",content_only=True,rows=current)
            if trigger is not None:
                before=list(current)
                clicked=self.click_element(title,trigger,goal=f"reveal_{purpose}")
                if clicked:
                    state=self.wait_for_state(handle=handle,title=title,before_rows=before,timeout=6.0,expected=aliases)
                    title=str(state.get("title") or title); current=list(state.get("rows") or rows()); target=field(current)
                    if target is None and purpose == "search":
                        target=next((
                            element for element in current
                            if element.get("focused")
                            and self._norm(element.get("control_type")) in {"edit","combobox"}
                            and not self._is_browser_chrome(element)
                            and not any(mark in self._norm(self._element_blob(element)) for mark in ("сортир","sort","фильтр","filter"))
                        ),None)
        while target is None and moved<max(0,int(max_scrolls)):
            self.tools.execute("scroll",{"amount":-6}); moved+=1; time.sleep(.22)
            current=rows(); target=field(current)
        if target is None and visual_fallback:
            # Vision may reveal a hidden search icon, but it never grants permission to type
            # until a real accessible input appears afterwards.
            if self.visual_click("Найди и открой поле поиска внутри текущей страницы",trigger_aliases or aliases,timeout=2.6):
                state=self.wait_for_state(handle=handle,title=title,before_rows=current,timeout=4.0,expected=aliases)
                title=str(state.get("title") or title); current=list(state.get("rows") or rows()); target=field(current)
        if target is None:
            return {"ok":False,"error":"Интерактивное поле не найдено","title":title,"scrolls":moved}
        clicked=self._click_input_rect(target)
        if not clicked:
            return {"ok":False,"error":"Поле найдено, но сфокусировать его не получилось","title":title,"field":target}
        time.sleep(.16)
        after=rows()
        focused=next((e for e in after if e.get("focused") and self._norm(e.get("control_type")) in {"edit","combobox","group","document"} and not self._is_browser_chrome(e)),None)
        if focused is None and purpose in {"composer","input"}:
            # Chromium may report both contenteditable Groups as Focusable=false even
            # though the visible placeholder Text is the reliable hit target.  Clicking
            # that descriptor is still grounded inside the already-resolved field and
            # avoids blind typing elsewhere on the page.
            target_rect=(target.get("rectangle") or [])
            descriptors=[]
            for e in after:
                if self._norm(e.get("control_type"))!="text" or not e.get("visible",True):
                    continue
                if not self._rect_contains(target_rect,e.get("rectangle") or []):
                    continue
                blob=self._norm(f"{e.get('name','')} {e.get('class_name','')}")
                score=max((self._score(blob,[alias]) for alias in aliases),default=0.0)
                if score>=.55:
                    descriptors.append((score,e))
            if descriptors and self._click_input_rect(max(descriptors,key=lambda item:item[0])[1]):
                time.sleep(.12)
                after=rows()
                focused=next((e for e in after if e.get("focused") and self._norm(e.get("control_type")) in {"edit","combobox","group","document"} and not self._is_browser_chrome(e)),None)
        target_after=field(after)
        focus_verified=bool(focused)
        self._trace("OPERATOR_INPUT_ACQUIRED",purpose=purpose,title=title,field=str((target_after or target).get("name") or ""),focused=focus_verified,scrolls=moved)
        return {"ok":True,"title":title,"handle":handle,"field":target_after or target,"focused":focus_verified,"scrolls":moved,"rows":after,"purpose":purpose}

    def type_verified(self, acquired: dict[str, Any], text: str, *, submit: bool = False, require_verified: bool = True) -> dict[str, Any]:
        text=str(text or "").strip()
        if not acquired.get("ok") or not text:
            return {"ok":False,"completed":False,"verified":False,"error":"Нет готового поля или текста"}
        title=str(acquired.get("title") or ""); handle=int(acquired.get("handle") or 0) or None; field=dict(acquired.get("field") or {})
        if str(acquired.get("purpose") or "") == "search":
            field_blob=self._norm(self._element_blob(field))
            if any(mark in field_blob for mark in ("сортир","sort","фильтр","filter","диапазон цен","price range","quantity","количество")):
                return {"ok":False,"completed":False,"verified":False,"error":"Поле сортировки/фильтра отклонено: это не поиск страницы"}
        # Re-focus immediately before typing; state may have changed after a SPA transition.
        if not self._click_input_rect(field):
            return {"ok":False,"completed":False,"verified":False,"error":"Не удалось вернуть фокус в поле"}
        typ=self._norm(field.get("control_type")); typed=False
        if typ in {"edit","combobox"} and (field.get("automation_id") or field.get("name")):
            args={"title_contains":title,"handle":handle,"text":text,"replace":True,"control_type":str(field.get("control_type") or "Edit")}
            if field.get("automation_id"): args["automation_id"]=str(field.get("automation_id"))
            elif field.get("name"): args["element_text"]=str(field.get("name"))
            try: typed=bool(self.tools.execute("window_type",args).get("ok"))
            except Exception: typed=False
        clipboard_used=False
        if not typed:
            try:
                import pyperclip
                # A coordinate-focused HTML input may not be addressable by pywinauto at
                # all (Tilda/Yandex custom inputs are common examples).  Replace through
                # the keyboard instead of merely pasting at an unknown caret position.
                pyperclip.copy(text)
                self.tools.execute("hotkey",{"keys":["ctrl","a"]})
                typed=bool(self.tools.execute("hotkey",{"keys":["ctrl","v"]}).get("ok"))
                clipboard_used=typed
            except Exception:
                typed=bool(self.tools.execute("type_text",{"text":text,"interval":.01}).get("ok"))
        if not typed:
            return {"ok":False,"completed":False,"verified":False,"error":"Ввод не выполнился"}
        payload=self._norm(text)
        purpose=str(acquired.get("purpose") or "")
        acquired_rows = list(acquired.get("rows") or [])
        telegram_composer = purpose == "composer" and bool(
            "telegram" in self._norm(title)
            or "телеграм" in self._norm(title)
            or self._telegram_send_button(acquired_rows, ready_only=False) is not None
        )
        ready_before=bool(self._telegram_send_button(acquired_rows,ready_only=True)) if telegram_composer else False
        def inspect_evidence() -> tuple[bool,bool,bool,bool]:
            field_evidence=False; visible_evidence=False; focused_now=False
            rows=self._elements(title,limit=360,handle=handle)
            for e in rows:
                if self._is_browser_chrome(e): continue
                eblob=self._norm(f"{e.get('value','')} {e.get('name','')}")
                if e.get("focused") and self._norm(e.get("control_type")) in {"edit","combobox","group","document"}:
                    focused_now=True
                if payload and payload in eblob and self._norm(e.get("control_type")) in {"edit","combobox","group","document"}:
                    field_evidence=True
                rect=e.get("rectangle") or []
                if payload and payload in eblob and len(rect)==4 and int(rect[3])>150:
                    visible_evidence=True
            send_ready=bool(self._telegram_send_button(rows,ready_only=True)) if telegram_composer else False
            return field_evidence,visible_evidence,focused_now,bool(send_ready and not ready_before)
        # Chromium/Telegram updates UI Automation a little after the DOM.  Poll the
        # already-grounded field before considering a paste retry; otherwise a valid
        # first paste can be duplicated while the accessibility tree is still stale.
        evidence_deadline=time.monotonic()+(2.40 if require_verified else .30)
        while True:
            time.sleep(.10)
            field_evidence,visible_evidence,focused,send_ready=inspect_evidence()
            verified=bool(field_evidence or (focused and visible_evidence) or send_ready)
            if verified or time.monotonic()>=evidence_deadline:
                break
        paste_retry=False
        clipboard_roundtrip=False
        # Before retrying a paste, inspect the *actual focused control*. Chromium and
        # Telegram Desktop can lag UIA by well over a second while the text is already
        # present. A clipboard round-trip proves that state without risking a duplicate.
        if require_verified and not verified and typed:
            # Chromium often exposes a perfectly usable HTML input with an empty UIA
            # ValuePattern (and sometimes Focusable=false).  Verify the *actual focused
            # control* by selecting/copying its contents.  A sentinel prevents a failed
            # Ctrl+C from being mistaken for the text we put on the clipboard to paste.
            try:
                import pyperclip
                old_clip=str(pyperclip.paste() or "")
                sentinel=f"__EIRVEN_VERIFY_{time.monotonic_ns()}__"
                pyperclip.copy(sentinel)
                a_ok=bool(self.tools.execute("hotkey",{"keys":["ctrl","a"]}).get("ok"))
                c_ok=bool(self.tools.execute("hotkey",{"keys":["ctrl","c"]}).get("ok")) if a_ok else False
                time.sleep(.06)
                copied=str(pyperclip.paste() or "")
                expected=self._norm(text); got=self._norm(copied)
                clipboard_roundtrip=bool(c_ok and copied!=sentinel and got==expected)
                if clipboard_roundtrip:
                    verified=True
                    # Collapse the selection back to a caret without changing content.
                    self.tools.execute("press_key",{"key":"end"})
                try: pyperclip.copy(old_clip)
                except Exception: pass
            except Exception:
                clipboard_roundtrip=False
        if require_verified and not verified and clipboard_used:
            # One idempotent replacement retry is allowed only after the round-trip
            # failed to prove that text is already in the composer. Re-ground first;
            # never keep retrying in a loop on a stale accessibility tree.
            self._click_input_rect(field)
            self.tools.execute("hotkey",{"keys":["ctrl","a"]})
            paste_retry=bool(self.tools.execute("hotkey",{"keys":["shift","insert"]}).get("ok"))
            if paste_retry:
                retry_deadline=time.monotonic()+1.8
                while time.monotonic()<retry_deadline:
                    time.sleep(.12)
                    field_evidence,visible_evidence,focused,send_ready=inspect_evidence()
                    verified=bool(field_evidence or (focused and visible_evidence) or send_ready)
                    if verified:
                        break
        if submit and (verified or not require_verified):
            self.tools.execute("press_key",{"key":"enter"})
        completed=bool(typed and (verified or not require_verified))
        self._trace("OPERATOR_TYPE_VERIFY",title=title,chars=len(text),verified=verified,focused=focused,submit=submit,completed=completed,clipboard_roundtrip=clipboard_roundtrip,telegram_send_ready=send_ready,clipboard_used=clipboard_used,paste_retry=paste_retry,purpose=purpose)
        if require_verified and not verified:
            return {"ok":False,"completed":False,"verified":False,"typed":True,"submitted":False,"error":"Текст отправлен в поле, но интерфейс не подтвердил, что он действительно появился"}
        return {"ok":True,"completed":completed,"verified":verified,"typed":True,"submitted":bool(submit and completed),"title":title,"evidence":"uia_value" if field_evidence else ("telegram_send_ready" if send_ready else ("clipboard_roundtrip" if clipboard_roundtrip else "focused_visible"))}

    def click_element(self, title: str, element: dict[str, Any], *, goal: str = "") -> bool:
        rect = element.get("rectangle") or []
        name = str(element.get("name") or "")
        if len(rect) == 4:
            x = int((int(rect[0]) + int(rect[2])) / 2)
            y = int((int(rect[1]) + int(rect[3])) / 2)
            self.tools.execute("mouse_move", {"x": x, "y": y, "duration": .14})
            result = self.tools.execute("click", {"x": x, "y": y})
        else:
            result = self.tools.execute("window_click", {
                "title_contains": title, "element_text": name,
                "control_type": str(element.get("control_type") or ""),
                "automation_id": str(element.get("automation_id") or ""),
            })
        ok = bool(result.get("ok"))
        self._trace("OPERATOR_CLICK", title=title, goal=goal, element=name, ok=ok)
        if ok and goal:
            learned = dict(element)
            if len(rect) == 4:
                learned["rect"] = {"left": rect[0], "top": rect[1], "right": rect[2], "bottom": rect[3]}
            self.learning.remember(title, goal, learned)
        return ok

    def click_keywords(self, title: str, terms: list[str], *, goal: str, types: tuple[str, ...] = ("Button", "Hyperlink", "ListItem", "Text"), content_only: bool = False) -> bool:
        # Previously successful controls and bundled demonstrations get first chance.
        for remembered in self.learning.candidates(title, goal, limit=6):
            remembered_terms = [remembered.get("name", ""), remembered.get("automation_id", ""), remembered.get("class_name", "")]
            element = self.find_element(title, remembered_terms, types=types, content_only=content_only)
            if element and self.click_element(title, element, goal=goal):
                return True
        element = self.find_element(title, terms, types=types, content_only=content_only)
        return bool(element and self.click_element(title, element, goal=goal))

    def type_into(self, title: str, terms: list[str], text: str, *, goal: str, submit: bool = False) -> bool:
        element = self.find_element(title, terms, types=("Edit", "Document", "Group"), content_only=True)
        if element:
            rect = element.get("rectangle") or []
            if len(rect) == 4:
                self.tools.execute("mouse_move", {"x": int((rect[0]+rect[2])/2), "y": int((rect[1]+rect[3])/2), "duration": .12})
                self.tools.execute("click", {"x": int((rect[0]+rect[2])/2), "y": int((rect[1]+rect[3])/2)})
            self.tools.execute("hotkey", {"keys": ["ctrl", "a"]})
            # pyautogui.write is poor for Cyrillic. window_type uses UIA clipboard-safe path.
            typed = self.tools.execute("window_type", {
                "title_contains": title, "element_text": str(element.get("name") or ""),
                "automation_id": str(element.get("automation_id") or ""), "text": text,
            })
            if not typed.get("ok"):
                # Clipboard paste is the robust fallback for Unicode.
                try:
                    import pyperclip
                    pyperclip.copy(text)
                    self.tools.execute("hotkey", {"keys": ["ctrl", "v"]})
                except Exception:
                    return False
            if submit:
                self.tools.execute("press_key", {"key": "enter"})
            self.learning.remember(title, goal, element)
            return True
        return False

    def _click_relative_window(self, window: dict[str, Any], fx: float, fy: float, *, goal: str = "") -> bool:
        rect = window.get("rectangle") or []
        if len(rect) != 4:
            return False
        left, top, right, bottom = [int(x) for x in rect]
        x = int(left + max(0.0, min(1.0, fx)) * max(1, right-left))
        y = int(top + max(0.0, min(1.0, fy)) * max(1, bottom-top))
        self.tools.execute("mouse_move", {"x": x, "y": y, "duration": .08})
        result = self.tools.execute("click", {"x": x, "y": y})
        ok = bool(result.get("ok"))
        self._trace("OPERATOR_DEMO_CLICK", goal=goal, fx=round(fx,4), fy=round(fy,4), x=x, y=y, ok=ok)
        return ok

    def click_current(self, labels: list[str], *, goal: str = "current_screen_click") -> bool:
        """Click a visible control in the current foreground window without opening anything."""
        windows = self._windows()
        if not windows:
            return False
        # window_list is ordered with the active user window near the front; skip EIRVEN shell/taskbar.
        window = next((w for w in windows if self._norm(w.get("title")) not in {"eirven", "панель задач", "program manager"}), None)
        if not window:
            return False
        title = str(window.get("title") or "")
        if not title:
            return False
        element = self.find_element(title, labels, types=("Button","Hyperlink","ListItem","Text","Group"), content_only=True)
        return bool(element and self.click_element(title, element, goal=goal))

    def _screenshot_digest(self) -> tuple[str, str]:
        result = self.tools.execute("screenshot", {})
        if not result.get("ok"):
            return "", ""
        path = str((result.get("result") or {}).get("path") or "")
        try:
            digest = hashlib.sha1(Path(path).read_bytes()).hexdigest()
        except Exception:
            digest = ""
        return path, digest

    @staticmethod
    def _vision_image_b64(path: str, max_side: int = 960) -> str:
        try:
            import io
            from PIL import Image
            with Image.open(path) as image:
                image=image.convert("RGB")
                image.thumbnail((max_side,max_side))
                out=io.BytesIO(); image.save(out,"JPEG",quality=80,optimize=True)
            return base64.b64encode(out.getvalue()).decode("ascii")
        except Exception:
            return base64.b64encode(Path(path).read_bytes()).decode("ascii")

    def visual_click(self, goal: str, labels: list[str], *, timeout: float = 6.0) -> bool:
        """Look at the owner's current screen and click a requested control like a person.

        This is the recovery lane when Windows accessibility exposes too little of a web
        app. It uses the already resident adaptive multimodal model and normalized screen
        coordinates, so recovery does not make the next conversation cold.
        """
        path,_ = self._screenshot_digest()
        if not path:
            return False
        try:
            image=self._vision_image_b64(path)
            installed={str(x).casefold():str(x) for x in self.gateway.installed_models()}
            configured=str(self.services.settings.vision_model)
            model=(installed.get(configured.casefold()) or
                   installed.get("qwen3.5:2b") or installed.get("qwen3-vl:2b") or configured)
            schema={"type":"object","properties":{"found":{"type":"boolean"},"x":{"type":"number"},"y":{"type":"number"},"label":{"type":"string"}},"required":["found","x","y","label"]}
            prompt=(
                "<|grounding|>Ты управляешь текущим экраном как пользователь. Найди ОДИН видимый интерактивный элемент для цели: " + goal +
                ". Подходящие подписи: " + ", ".join(labels) +
                ". Верни found=true и координаты центра x,y НОРМАЛИЗОВАННЫЕ от 0 до 1 относительно всего изображения. "
                "Не придумывай невидимый элемент; если его нет — found=false."
            )
            choice={}
            try:
                candidate=self.gateway.json(
                    [{"role":"user","content":prompt,"images":[image]}],
                    model=model, temperature=0.0, schema=schema, num_ctx=512,
                    num_predict=64, keep_alive=self.services.settings.keep_alive,
                    timeout_seconds=min(4.0,max(1.5,timeout)),
                )
                if isinstance(candidate,dict): choice=candidate
            except Exception as exc:
                self._trace("OPERATOR_VISUAL_ERROR",goal=goal,execution="oneshot",error=str(exc)[:900])
            if not isinstance(choice,dict) or not choice.get("found"):
                self._trace("OPERATOR_VISUAL",goal=goal,found=False)
                return False
            x=max(0.0,min(1.0,float(choice.get("x") or .5))); y=max(0.0,min(1.0,float(choice.get("y") or .5)))
            try:
                import pyautogui
                width,height=pyautogui.size()
            except Exception:
                return False
            px=int(x*max(1,width-1)); py=int(y*max(1,height-1))
            self.tools.execute("mouse_move",{"x":px,"y":py,"duration":.22})
            result=self.tools.execute("click",{"x":px,"y":py})
            ok=bool(result.get("ok"))
            self._trace("OPERATOR_VISUAL",goal=goal,found=True,label=choice.get("label"),x=x,y=y,ok=ok)
            return ok
        except Exception as exc:
            self._trace("OPERATOR_VISUAL_ERROR",goal=goal,error=str(exc)[:900])
            return False

    def _tiny_visual_json(self, prompt: str, schema: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
        path, _ = self._screenshot_digest()
        if not path:
            return {}
        try:
            image = self._vision_image_b64(path)
            installed = {str(x).casefold(): str(x) for x in self.gateway.installed_models()}
            configured = str(self.services.settings.vision_model)
            model = (
                installed.get(configured.casefold())
                or installed.get("qwen3.5:2b")
                or installed.get("qwen3-vl:2b")
                or configured
            )
            try:
                choice = self.gateway.json(
                    [{"role": "user", "content": prompt, "images": [image]}],
                    model=model, temperature=0.0, schema=schema,
                    num_ctx=512, num_predict=72,
                    keep_alive=self.services.settings.keep_alive,
                    timeout_seconds=min(4.2,max(1.5,timeout)),
                )
                if isinstance(choice,dict):
                    self._trace("OPERATOR_VISUAL_RESULT",execution="oneshot",goal=prompt[:160])
                    return dict(choice)
            except Exception as exc:
                self._trace("OPERATOR_VISUAL_ERROR",execution="oneshot",goal=prompt[:180],error=str(exc)[:900])
            return {}
        except Exception as exc:
            self._trace("OPERATOR_VISUAL_ERROR", goal=prompt[:180], error=str(exc)[:900])
            return {}

    def verify_visible_goal(self, goal: str, *, timeout: float = 4.5) -> bool:
        schema={"type":"object","properties":{"done":{"type":"boolean"},"evidence":{"type":"string"}},"required":["done","evidence"]}
        result=self._tiny_visual_json(
            "Посмотри на текущий экран Windows. Проверена ли уже цель пользователя: " + goal +
            "? done=true только если на экране есть явное визуальное подтверждение результата. Не угадывай.",
            schema, timeout=timeout,
        )
        ok=bool(result.get("done"))
        self._trace("OPERATOR_VERIFY_VISUAL", goal=goal, ok=ok, evidence=str(result.get("evidence") or "")[:400])
        return ok

    def perform_goal(self, goal: str, *, text_to_type: str = "", max_steps: int = 4) -> dict[str, Any]:
        """Bounded visible-screen operator.

        It behaves like a careful person on the current desktop: inspect -> one atomic
        action -> inspect again. The resident multimodal model chooses only coordinates/action
        type; it cannot spawn projects, open a hidden browser profile or loop forever.
        """
        try:
            import pyautogui
            width, height = pyautogui.size()
        except Exception as exc:
            return {"ok": False, "verified": False, "error": str(exc)}
        schema={
            "type":"object",
            "properties":{
                "action":{"type":"string","enum":["click","double_click","type","press_enter","press_escape","wait","done","fail"]},
                "x":{"type":"number"},"y":{"type":"number"},"reason":{"type":"string"}
            },
            "required":["action","x","y","reason"]
        }
        history=[]
        for step in range(max(1,min(int(max_steps),6))):
            prompt=(
                "Ты локальный экранный оператор EIRVEN. Текущая цель владельца: " + goal + ". "
                "Выбери РОВНО ОДИН следующий безопасный шаг по тому, что реально видно на экране. "
                "Координаты x,y от 0 до 1. Нельзя покупать, подтверждать платежи, вводить пароли/коды, "
                "изменять защиту Windows или придумывать невидимые элементы. Если цель уже достигнута — done. "
                "Если нужен ввод текста, выбери type; будет вставлен только заранее разрешённый текст."
            )
            decision=self._tiny_visual_json(prompt,schema,timeout=3.8)
            action=str(decision.get("action") or "fail")
            history.append({"step":step+1,**decision})
            self._trace("OPERATOR_STEP",goal=goal,step=step+1,action=action,reason=str(decision.get("reason") or "")[:300])
            if action=="done":
                return {"ok":True,"verified":True,"steps":history}
            if action=="fail":
                break
            if action=="wait":
                time.sleep(.45); continue
            x=max(0.0,min(1.0,float(decision.get("x") or .5))); y=max(0.0,min(1.0,float(decision.get("y") or .5)))
            px=int(x*max(1,width-1)); py=int(y*max(1,height-1))
            if action in {"click","double_click","type"}:
                self.tools.execute("mouse_move",{"x":px,"y":py,"duration":.16})
                self.tools.execute("click",{"x":px,"y":py})
                if action=="double_click": self.tools.execute("click",{"x":px,"y":py})
            if action=="type":
                if not text_to_type:
                    break
                try:
                    import pyperclip
                    pyperclip.copy(text_to_type)
                    self.tools.execute("hotkey",{"keys":["ctrl","v"]})
                except Exception:
                    self.tools.execute("type_text",{"text":text_to_type,"interval":.01})
            elif action=="press_enter": self.tools.execute("press_key",{"key":"enter"})
            elif action=="press_escape": self.tools.execute("press_key",{"key":"esc"})
            time.sleep(.28)
        verified=self.verify_visible_goal(goal,timeout=3.2)
        return {"ok":verified,"verified":verified,"steps":history,"error":"Цель не подтверждена на экране" if not verified else ""}

    def observe(self, question: str, *, timeout: float = 5.0) -> str:
        """Inspect the current visible desktop with the resident multimodal model."""
        path, _ = self._screenshot_digest()
        if not path:
            return "Не смогла получить снимок текущего экрана."
        chat = getattr(self.services, "chat", None)
        if chat is not None:
            return chat._vision_for_path(path, question)
        return "Снимок сделан, но vision-контур недоступен."

    def _telegram_result_score(self, element: dict[str, Any], recipient: str) -> float | None:
        """Score a real Telegram search row across Web and native Desktop clients."""
        aliases=self._telegram_recipient_aliases(recipient)
        typ=self._norm(element.get("control_type")); cls=self._norm(element.get("class_name")); name=self._norm(element.get("name")); rect=element.get("rectangle") or []
        if self._is_browser_chrome(element) or not element.get("visible",True) or not element.get("enabled",True) or len(rect)!=4:
            return None
        matched=next((alias for alias in aliases if alias and (name==alias or name.startswith(alias+" ") or (alias.startswith("@") and alias in name))),"")
        if not matched:
            return None
        width=max(0,int(rect[2])-int(rect[0])); height=max(0,int(rect[3])-int(rect[1]))
        modern_web=typ=="button" and "listitem button" in cls
        legacy_web=typ in {"hyperlink","listitem"} and "chatlist" in cls
        # Native Telegram Desktop exposes search rows differently between Qt/WinUI
        # builds. Semantic row role + useful geometry + exact name evidence is enough;
        # plain Text labels are deliberately excluded.
        native_row=typ in {"button","listitem","treeitem"} and width>=170 and height>=26
        if not (modern_web or legacy_web or native_row):
            return None
        score=8.0 if modern_web else (6.0 if legacy_web else 5.2)
        if name==matched: score+=5.0
        if name.startswith(matched+" "): score+=2.5
        if any(marker in name or marker in cls for marker in ("subscribers","subscriber","channel","members","member","участник","подписчик","группа","group")):
            score-=6.0
        # Telegram's chat list/search lives in the left portion on normal layouts.
        if int(rect[0])<900: score+=1.0
        score += max(0.0,1.2-max(0,int(rect[1])-450)/1000.0)
        return score if score>=4.5 else None

    @classmethod
    def _telegram_recipient_aliases(cls, recipient: str) -> list[str]:
        rec = cls._norm(recipient)
        if rec in {"избранное", "избранном", "избранку", "saved messages", "saved message", "сохраненные сообщения", "сохраненные", "сохранённые сообщения", "себе", "мне"}:
            return ["избранное", "saved messages", "сохраненные сообщения", "сохранённые сообщения"]
        return [rec] if rec else []

    def _telegram_ready(self, rows: list[dict[str, Any]]) -> bool:
        """Telegram is interactive as soon as search/chat affordances are exposed.

        Do not wait for the whole SPA tree to become stable: timestamps, presence and
        animation can keep changing even though the owner can already interact.
        """
        for e in rows:
            if not e.get("visible", True) or self._is_browser_chrome(e):
                continue
            name=self._norm(e.get("name")); aid=self._norm(e.get("automation_id")); cls=self._norm(e.get("class_name"))
            typ=self._norm(e.get("control_type"))
            if aid=="telegram-search-input" or name in {"search","поиск","chats","чаты"}:
                return True
            if typ in {"edit","combobox"} and any(mark in f"{name} {aid} {cls}" for mark in ("search","поиск","chat")):
                return True
            if "chatlist" in cls or "listitem button" in cls or (typ in {"listitem","treeitem"} and name):
                return True
        return False

    def _clear_telegram_search(self, acquired: dict[str, Any]) -> dict[str, Any]:
        """Clear a stale Telegram Web search through its real clear affordance."""
        title=str(acquired.get("title") or "Telegram")
        rows=list(acquired.get("rows") or [])
        clear=next((
            element for element in rows
            if element.get("visible",True) and element.get("enabled",True)
            and "input-search-clear" in str(element.get("class_name") or "").casefold()
        ),None)
        if clear is not None and self.click_element(title,clear,goal="telegram_clear_search"):
            time.sleep(.14)
            acquired=dict(acquired)
            acquired["rows"]=self._elements(
                title,limit=360,handle=int(acquired.get("handle") or 0) or None,
            )
            field=dict(acquired.get("field") or {})
            field["value"]=""
            acquired["field"]=field
        return acquired

    def _telegram_chat_evidence(self, rows: list[dict[str, Any]], recipient: str, selected_username: str = "") -> tuple[bool, dict[str, bool]]:
        aliases=self._telegram_recipient_aliases(recipient); address_match=False; header_match=False; active_match=False; composer=False
        topbars=[
            element.get("rectangle") or [] for element in rows
            if element.get("visible",True)
            and "sidebar header" in self._norm(element.get("class_name"))
            and "topbar" in self._norm(element.get("class_name"))
        ]
        for e in rows:
            if not e.get("visible", True):
                continue
            name=self._norm(e.get("name")); cls=self._norm(e.get("class_name")); aid=self._norm(e.get("automation_id")); rect=e.get("rectangle") or []
            raw=str(e.get("name") or "")
            if selected_username and ("omnibox" in cls or "адресная строка" in name) and selected_username.casefold() in raw.casefold():
                address_match=True
            if "chatlist" in cls and "active" in cls and any(alias in name for alias in aliases):
                active_match=True
            # Telegram K renders the active chat title in the web topbar around y=108,
            # immediately below Chromium chrome.  The former y>=150 bound rejected the
            # correct visible header even after a successful chat click.
            in_topbar=any(self._rect_contains(container,rect) for container in topbars)
            fallback_topbar=not topbars and len(rect)==4 and int(rect[0])>=420 and 90<=int(rect[1])<=190
            if len(rect)==4 and (in_topbar or fallback_topbar) and any(
                name==alias or name.startswith(alias+" ") for alias in aliases
            ):
                header_match=True
            if aid in {"editable-message-text","input-message-input"} or any(x in name for x in ("write a message","сообщение")):
                composer=True
        return bool(address_match or active_match or header_match), {
            "address":address_match,"header":header_match,"active":active_match,"composer":composer,
        }

    def telegram_send(
        self, recipient: str, text: str, *, expected_surface: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Grounded Telegram send with state-driven waits and safe recovery.

        Transient loading never becomes a completed task. A committed message is never
        sent twice unless the composer itself proves the first commit did not consume
        the text.
        """
        recipient=str(recipient or "").strip(); text=str(text or "").strip()
        if not recipient or not text:
            raise RuntimeError("Нужны получатель и текст сообщения")
        aliases=["Telegram","Телеграм","web.telegram"]
        expected = dict(expected_surface or {})
        expected_handle = self._coerce_window_id(expected.get("handle"))
        expected_pid = self._coerce_window_id(expected.get("pid"))
        window = None
        client = "existing"
        if expected_handle:
            listing = self.tools.execute("window_list", {"max_windows": 120})
            candidates = list(listing.get("result") or []) if listing.get("ok") else []
            window = next(
                (
                    dict(row) for row in candidates if isinstance(row, dict)
                    and self._coerce_window_id(row.get("handle")) == expected_handle
                    and (not expected_pid or self._coerce_window_id(row.get("pid")) == expected_pid)
                ),
                None,
            )
            if not window:
                raise RuntimeError("CONFIRMATION_SURFACE_DRIFT: окно Telegram изменилось")
            client = "confirmed_surface"
        else:
            window=self.wait_window(aliases,.5)
        if not window:
            launched=self.tools.execute("launch_application",{"application":"Telegram"}); client="desktop"
            if not launched.get("ok"):
                from .system_browser import open_url
                open_url("https://web.telegram.org/a/"); client="web_default"
            window=self.wait_window(aliases,15.0)
        if not window:
            raise RuntimeError("Не появилось окно Telegram Desktop/Web")
        handle=self._coerce_window_id(window.get("handle")) or None; title=str(window.get("title") or "Telegram")
        if handle: self.tools.execute("window_focus",{"handle":handle})

        # Do not confuse a slow network/VPN start with a failed action. Wait for actual
        # interactive affordances, not for a fixed five-second sleep.
        initial=[]; ready_deadline=time.monotonic()+60.0
        while time.monotonic()<ready_deadline:
            initial=self._elements(title,limit=420,handle=handle)
            if self._telegram_ready(initial):
                break
            time.sleep(.25)
        if not self._telegram_ready(initial):
            raise RuntimeError("Telegram остаётся в состоянии загрузки и пока не показал интерактивный интерфейс")

        search=None; acquire_deadline=time.monotonic()+15.0
        while time.monotonic()<acquire_deadline:
            search=self.acquire_input(
                purpose="search",aliases=["telegram-search-input","input-search-input","search","поиск"],
                trigger_aliases=["Search","Поиск"],max_scrolls=0,visual_fallback=True,
                title_hint=title,handle_hint=handle,
            )
            if search.get("ok"):
                break
            time.sleep(.3)
        if not search or not search.get("ok"):
            raise RuntimeError("Telegram открыт, но поле поиска пока не стало доступно")

        saved = "избранное" in self._telegram_recipient_aliases(recipient)
        queries=[recipient]
        if saved:
            queries=["Избранное","Saved Messages","Сохраненные сообщения"]
        selected=None; rows=[]; selected_query=""
        for search_text in queries:
            search=self._clear_telegram_search(search)
            typed=self.type_verified(search,search_text,submit=False,require_verified=False)
            if not typed.get("typed"):
                continue
            selected_query=search_text
            end=time.monotonic()+8.0
            while time.monotonic()<end and selected is None:
                rows=self._elements(title,limit=440,handle=handle)
                candidates=[]
                for el in rows:
                    score=self._telegram_result_score(el,recipient)
                    if score is not None:
                        candidates.append((score,el))
                if candidates:
                    selected=max(candidates,key=lambda x:x[0])[1]
                    break
                time.sleep(.2)
            if selected is not None:
                break
        if selected is None:
            suffix=f" (пробовала: {', '.join(queries)})" if len(queries)>1 else ""
            raise RuntimeError(f"Поле поиска работает, но не нашла однозначный чат «{recipient}»{suffix}")

        selected_name=str(selected.get("name") or "")
        username_match=re.search(r"@[A-Za-z0-9_]{3,}",selected_name)
        selected_username=username_match.group(0) if username_match else ""
        if not self.click_element(title,selected,goal="telegram_open_chat"):
            raise RuntimeError(f"Чат «{recipient}» найден, но открыть его не получилось")

        opened_rows=[]; evidence={}; confirmed=False; deadline=time.monotonic()+18.0
        while time.monotonic()<deadline:
            opened_rows=self._elements(title,limit=440,handle=handle)
            confirmed,evidence=self._telegram_chat_evidence(opened_rows,recipient,selected_username)
            if confirmed:
                break
            time.sleep(.2)
        if not confirmed:
            raise RuntimeError(f"Чат «{recipient}» найден, но Telegram не подтвердил переход в него")
        self._trace("OPERATOR_VERIFY",app="telegram",action="open_chat",recipient=recipient,verified=True,query=selected_query,**evidence)

        composer=None; compose_deadline=time.monotonic()+18.0
        while time.monotonic()<compose_deadline:
            composer=self.acquire_input(
                purpose="composer",aliases=["input-message-input","editable-message-text","write a message","сообщение","message","composer","contenteditable"],
                trigger_aliases=None,max_scrolls=0,visual_fallback=False,
                title_hint=title,handle_hint=handle,
            )
            if composer.get("ok"):
                break
            time.sleep(.25)
        if not composer or not composer.get("ok"):
            raise RuntimeError(f"Чат «{recipient}» открыт, но поле сообщения не появилось")

        typed=self.type_verified(composer,text,submit=False,require_verified=True)
        if not typed.get("ok"):
            # Re-enumerate after a SPA/native transition and attempt an idempotent
            # replace once. type_verified replaces the focused field, not appending.
            time.sleep(.35)
            retry=self.acquire_input(
                purpose="composer",aliases=["input-message-input","editable-message-text","write a message","сообщение","message","composer","contenteditable"],
                trigger_aliases=None,max_scrolls=0,visual_fallback=False,title_hint=title,handle_hint=handle,
            )
            if retry.get("ok"):
                typed=self.type_verified(retry,text,submit=False,require_verified=True)
                if typed.get("ok"):
                    composer=retry
        if not typed.get("ok"):
            raise RuntimeError("Поле сообщения найдено, но Telegram не подтвердил появление текста; отправка не выполнялась")

        before_send=self._elements(title,limit=440,handle=handle); before_sig=self._ui_fingerprint(before_send)
        commit=self.commit_composer(composer)
        if not commit.get("ok"):
            raise RuntimeError(str(commit.get("error") or "Не удалось нажать кнопку отправки Telegram"))

        def inspect_send_state() -> tuple[bool,bool,bool,bool,list[dict[str,Any]]]:
            current=self._elements(title,limit=440,handle=handle)
            payload=self._norm(text); text_visible=False; composer_empty=False; composer_contains=False; failure=False
            for e in current:
                if self._is_browser_chrome(e):
                    continue
                rect=e.get("rectangle") or []
                typ=self._norm(e.get("control_type"))
                blob=self._norm(f"{e.get('name','')} {e.get('value','')} {e.get('class_name','')}")
                if any(mark in blob for mark in ("failed to send","не отправлено","ошибка отправки","retry message","повторить отправку")):
                    failure=True
                composer_like=typ in {"edit","combobox","group","document"} and any(x in blob for x in ("input message","input-message","write a message","сообщение","composer","editable-message"))
                if composer_like:
                    if payload and payload in blob:
                        composer_contains=True
                    elif payload not in blob:
                        composer_empty=True
                # Exact outgoing text anywhere in the chat content is strong evidence.
                if payload and payload in blob and len(rect)==4 and int(rect[3])>180 and not composer_like:
                    text_visible=True
            button=self._telegram_send_button(current,ready_only=False)
            send_reset=False
            if button is not None:
                tokens={token for token in re.split(r"\s+",str(button.get("class_name") or "").casefold()) if token}
                send_reset="record" in tokens and "send" not in tokens
            return text_visible,composer_empty,composer_contains,bool(send_reset and not failure),current

        verified=False; text_visible=False; composer_empty=False; composer_contains=False; send_reset=False
        safe_commit_retried=False
        end=time.monotonic()+45.0
        while time.monotonic()<end:
            text_visible,composer_empty,composer_contains,send_reset,current=inspect_send_state()
            changed=self._ui_fingerprint(current)!=before_sig
            if text_visible or (composer_empty and changed) or (send_reset and changed):
                verified=True
                break
            if composer_contains and not safe_commit_retried:
                # The text still being present proves the first commit was not consumed.
                # Allow exactly one repeat commit; never hammer Send while Telegram is
                # reconnecting or its accessibility tree is stale.
                retry_commit=self.commit_composer({**composer,"rows":current})
                safe_commit_retried=True
                if retry_commit.get("ok"):
                    commit={**retry_commit,"method":f"retry_{retry_commit.get('method','commit')}"}
                    before_sig=self._ui_fingerprint(current)
                composer_contains=False
            time.sleep(.25)
        self._trace(
            "OPERATOR_VERIFY",app="telegram",action="send",recipient=recipient,verified=verified,
            text_visible=text_visible,composer_empty=composer_empty,send_reset=send_reset,
            composer_contains=composer_contains,commit_method=commit.get("method"),active_chat=True,
        )
        if not verified:
            return {
                "sent":True,"verified":False,"completed":False,"recipient":recipient,
                "client":client,"method":commit.get("method"),
                "error":"Команда отправки была выполнена, но Telegram не подтвердил доставку; задача не помечена завершённой",
            }
        return {"sent":True,"verified":True,"completed":True,"recipient":recipient,"client":client,"method":commit.get("method")}

    def telegram_send_file(self, recipient: str, path: str) -> dict[str, Any]:
        """Send one existing local file through the owner's visible Telegram session.

        r19 keeps the file path as a typed artifact. This method never converts the path
        into chat/search text and never presses Send twice when verification is uncertain.
        """
        from pathlib import Path

        recipient = str(recipient or "").strip() or "Избранное"
        file_path = Path(path).expanduser().resolve()
        if not file_path.is_file():
            raise RuntimeError(f"Файл не найден: {file_path}")
        aliases = ["Telegram", "Телеграм", "web.telegram"]
        window = self.wait_window(aliases, .35); client = "existing"
        if not window:
            launched = self.tools.execute("launch_application", {"application": "Telegram"}); client = "desktop"
            if not launched.get("ok"):
                from .system_browser import open_url
                open_url("https://web.telegram.org/a/"); client = "web_default"
            window = self.wait_window(aliases, 7.0)
        if not window:
            raise RuntimeError("Не появилось окно Telegram Desktop/Web")
        handle = int(window.get("handle") or 0) or None
        title = str(window.get("title") or "Telegram")
        if handle:
            self.tools.execute("window_focus", {"handle": handle})

        search = self.acquire_input(
            purpose="search", aliases=["telegram-search-input", "search", "поиск"],
            trigger_aliases=["Search", "Поиск"], max_scrolls=0, visual_fallback=True,
            title_hint=title, handle_hint=handle,
        )
        if not search.get("ok"):
            raise RuntimeError("Telegram открыт, но поле поиска не найдено")
        search = self._clear_telegram_search(search)
        search_text = "Saved Messages" if "saved messages" in self._telegram_recipient_aliases(recipient) else recipient
        typed = self.type_verified(search, search_text, submit=False, require_verified=False)
        if not typed.get("typed"):
            raise RuntimeError("Не удалось ввести имя получателя в поиск Telegram")
        time.sleep(.18)
        selected = None; rows = []; end = time.monotonic() + 6.0
        while time.monotonic() < end and selected is None:
            rows = self._elements(title, limit=400, handle=handle)
            candidates = []
            for el in rows:
                score = self._telegram_result_score(el, recipient)
                if score is not None:
                    candidates.append((score, el))
            if candidates:
                selected = max(candidates, key=lambda x: x[0])[1]
            else:
                time.sleep(.18)
        if selected is None:
            raise RuntimeError(f"Не нашла чат «{recipient}»")
        before = list(rows)
        if not self.click_element(title, selected, goal="telegram_open_chat_for_file"):
            raise RuntimeError(f"Не удалось открыть чат «{recipient}»")
        state = self.wait_for_state(handle=handle, title=title, before_rows=before, timeout=6.0, stable_for=.25, expected=[recipient])
        title = str(state.get("title") or title)
        rows = list(state.get("rows") or self._elements(title, limit=420, handle=handle))

        def find_button(markers: tuple[str, ...]) -> dict[str, Any] | None:
            best = None
            for el in self._elements(title, limit=460, handle=handle):
                if not el.get("visible", True) or not el.get("enabled", True):
                    continue
                if self._is_browser_chrome(el):
                    continue
                if self._norm(el.get("control_type")) not in {"button", "hyperlink", "listitem", "menuitem", "group"}:
                    continue
                blob = self._norm(f"{el.get('name','')} {el.get('automation_id','')} {el.get('class_name','')}")
                score = sum(1 for marker in markers if marker in blob)
                if score and (best is None or score > best[0]):
                    best = (score, el)
            return best[1] if best else None

        attach = find_button(("attach", "прикреп", "paperclip", "attachment"))
        if not attach:
            raise RuntimeError("В открытом чате не нашла кнопку прикрепления файла")
        if not self.click_element(title, attach, goal="telegram_attach_file"):
            raise RuntimeError("Кнопку прикрепления нашла, но нажать не удалось")
        time.sleep(.2)
        choose = find_button(("file", "файл", "document", "документ"))
        if choose:
            self.click_element(title, choose, goal="telegram_attach_file_menu")

        # Native Windows file chooser. Match by class as well as localized title.
        dialog = None; end = time.monotonic() + 5.0
        while time.monotonic() < end and dialog is None:
            listed = self.tools.execute("window_list", {"max_windows": 80})
            for row in list(listed.get("result") or []) if listed.get("ok") else []:
                rtitle = str(row.get("title") or "")
                rclass = str(row.get("class_name") or "")
                if rclass == "#32770" or re.search(r"(?:^| )(?:open|opening|открыт|выбор|select)(?: |$)", self._norm(rtitle)):
                    dialog = dict(row); break
            if dialog is None:
                time.sleep(.12)
        if dialog is None:
            raise RuntimeError("После кнопки прикрепления не появилось окно выбора файла")
        dtitle = str(dialog.get("title") or "Open")
        dhandle = int(dialog.get("handle") or 0) or None
        edits = self.tools.execute("window_elements", {"title_contains": dtitle, "handle": dhandle, "max_elements": 120})
        edit = None
        for el in list(edits.get("result") or []) if edits.get("ok") else []:
            if self._norm(el.get("control_type")) != "edit":
                continue
            blob = self._norm(f"{el.get('name','')} {el.get('automation_id','')}")
            if any(x in blob for x in ("file name", "имя файла", "filename", "1148")):
                edit = el; break
            if edit is None:
                edit = el
        if edit is None:
            raise RuntimeError("Окно выбора файла открыто, но поле имени файла не найдено")
        args = {"title_contains": dtitle, "handle": dhandle, "text": str(file_path), "replace": True, "control_type": "Edit"}
        if str(edit.get("name") or ""):
            args["element_text"] = str(edit.get("name") or "")
        elif str(edit.get("automation_id") or ""):
            args["automation_id"] = str(edit.get("automation_id") or "")
        typed_path = self.tools.execute("window_type", args)
        if not typed_path.get("ok"):
            raise RuntimeError("Не удалось ввести путь файла в системный диалог")
        if not self.tools.execute("press_key", {"key": "enter"}).get("ok"):
            raise RuntimeError("Не удалось подтвердить выбор файла")

        # Wait for Telegram preview/send affordance. If the client auto-sends, verification
        # below sees the filename and does not issue another Send.
        filename = file_path.name
        before_sig = self._ui_fingerprint(rows)
        send = None; auto_visible = False; end = time.monotonic() + 5.0
        while time.monotonic() < end:
            current = self._elements(title, limit=460, handle=handle)
            filename_visible = any(self._norm(filename) in self._norm(f"{e.get('name','')} {e.get('value','')}") for e in current)
            send = find_button(("send", "отправ"))
            if filename_visible and not send and self._ui_fingerprint(current) != before_sig:
                auto_visible = True; break
            if filename_visible and send:
                break
            time.sleep(.15)
        if send:
            if not self.click_element(title, send, goal="telegram_send_file_commit"):
                raise RuntimeError("Файл выбран, но кнопку отправки нажать не удалось")
        elif not auto_visible:
            raise RuntimeError("Telegram не показал подтверждённый preview выбранного файла")

        # One post-commit verification only. Never click Send again on uncertainty.
        end = time.monotonic() + 5.0; verified = False
        while time.monotonic() < end:
            current = self._elements(title, limit=460, handle=handle)
            filename_visible = any(self._norm(filename) in self._norm(f"{e.get('name','')} {e.get('value','')}") for e in current)
            changed = self._ui_fingerprint(current) != before_sig
            if filename_visible and changed:
                verified = True; break
            time.sleep(.18)
        self._trace("OPERATOR_VERIFY", app="telegram", action="send_file", recipient=recipient, file=filename, verified=verified)
        return {"ok": True, "sent": True, "completed": True, "verified": verified, "recipient": recipient, "file": str(file_path), "client": client}

    def yandex_surface(self, timeout: float = .5, *, focus: bool = False) -> dict[str, Any] | None:
        """Find a real Yandex Music page even after its title becomes Track — Artist."""
        deadline = time.monotonic() + max(.15, float(timeout))
        while time.monotonic() < deadline:
            for window in self._windows():
                title = str(window.get("title") or "")
                title_n = self._norm(title)
                class_n = self._norm(window.get("class_name"))
                direct = bool(re.search(r"(?:яндекс\s+музык|yandex\s+music|music\s+yandex)", title_n))
                browser = bool(
                    direct
                    or re.search(r"(?:chrome|edge|firefox|opera|browser|samsung|yandex)", f"{title_n} {class_n}")
                    or "chrome widgetwin" in class_n
                )
                if not browser:
                    continue
                handle = int(window.get("handle") or 0) or None
                rows = self._elements(title, limit=420, handle=handle)
                blobs = [self._norm(self._element_blob(row)) for row in rows if row.get("visible", True)]
                joined = " | ".join(blobs)
                structural = any(mark in joined for mark in (
                    "vibeplayercontrols", "barbelow", "playercontrols", "music yandex ru",
                ))
                semantic_hits = sum(1 for mark in (
                    "моя волна", "следующая песня", "предыдущая песня", "нравится",
                    "не нравится", "воспроизведение", "пауза", "коллекция",
                ) if mark in joined)
                if not (direct or structural or semantic_hits >= 3):
                    background_tab = next((
                        row for row in rows
                        if self._norm(row.get("control_type")) == "tabitem"
                        and re.search(
                            r"(?:яндекс\s+музык|yandex\s+music|music\s*yandex|music\.yandex)",
                            self._norm(self._element_blob(row)),
                        )
                    ), None)
                    if background_tab is None:
                        continue
                    if not focus:
                        return {
                            **window, "handle": int(handle or 0), "title": title,
                            "surface": "yandex_music", "background_tab": True,
                        }
                    if handle:
                        self.tools.execute("window_focus", {"handle": handle})
                    if not self.click_element(title, background_tab, goal="yandex_activate_tab"):
                        continue
                    time.sleep(.38)
                    refreshed = self._elements(title, limit=420, handle=handle)
                    refreshed_blob = " | ".join(
                        self._norm(self._element_blob(row)) for row in refreshed if row.get("visible", True)
                    )
                    refreshed_hits = sum(1 for mark in (
                        "моя волна", "следующая песня", "предыдущая песня", "нравится",
                        "не нравится", "воспроизведение", "пауза", "коллекция",
                    ) if mark in refreshed_blob)
                    if not (
                        any(mark in refreshed_blob for mark in (
                            "vibeplayercontrols", "barbelow", "playercontrols", "music yandex ru",
                        ))
                        or refreshed_hits >= 3
                    ):
                        continue
                if focus and handle:
                    self.tools.execute("window_focus", {"handle": handle})
                return {**window, "handle": int(handle or 0), "title": title, "surface": "yandex_music"}
            time.sleep(.12)
        return None

    @staticmethod
    def _clock_seconds(value: str) -> list[int]:
        out: list[int] = []
        for hours, minutes, seconds in re.findall(r"(?:(\d{1,2}):)?(\d{1,3}):(\d{2})", str(value or "")):
            out.append((int(hours or 0) * 3600) + int(minutes) * 60 + int(seconds))
        return out

    def yandex_player_control(self, request: str) -> dict[str, Any]:
        """Execute one grounded Yandex Music control and verify the post-state."""
        clean = self._norm(request)
        surface = self.yandex_surface(timeout=.7, focus=True)
        if not surface:
            raise RuntimeError("Открытая Яндекс Музыка не найдена")
        handle = int(surface.get("handle") or 0) or None
        title = str(surface.get("title") or "Яндекс Музыка")

        def rows() -> list[dict[str, Any]]:
            return self._elements(title, limit=520, handle=handle)

        def usable(element: dict[str, Any], *, roles: tuple[str, ...] = ("button",)) -> bool:
            if not element.get("visible", True) or not element.get("enabled", True):
                return False
            if self._norm(element.get("control_type")) not in roles:
                return False
            rect = element.get("rectangle") or []
            return len(rect) == 4 and int(rect[3]) > 150 and self._rect_area(rect) >= 80

        def blob(element: dict[str, Any]) -> str:
            return self._norm(self._element_blob(element))

        def find_control(aliases: tuple[str, ...], *, reject: tuple[str, ...] = ()) -> dict[str, Any] | None:
            wanted = tuple(self._norm(item) for item in aliases)
            rejected = tuple(self._norm(item) for item in reject)
            candidates: list[tuple[float, dict[str, Any]]] = []
            for element in rows():
                if not usable(element, roles=("button", "slider", "progressbar")):
                    continue
                value = blob(element)
                if any(term and term in value for term in rejected):
                    continue
                score = max((self._score(value, [term]) for term in wanted), default=0.0)
                if any(term == value for term in wanted):
                    score += 1.0
                if score >= .60:
                    candidates.append((score, element))
            return max(candidates, key=lambda item: item[0])[1] if candidates else None

        def playback_state(current: list[dict[str, Any]] | None = None) -> str:
            current = current if current is not None else rows()
            for element in current:
                if not usable(element):
                    continue
                value = blob(element)
                if re.search(r"(?:^| )(?:пауза|pause)(?: |$)|vibeplayercontrols playbutton playing", value):
                    return "playing"
            for element in current:
                if not usable(element):
                    continue
                value = blob(element)
                if re.search(r"(?:^| )(?:воспроизвести|воспроизведение|play|resume)(?: |$)|vibeplayercontrols playbutton", value):
                    return "paused"
            return "unknown"

        def track_signature(current: list[dict[str, Any]] | None = None) -> str:
            current = current if current is not None else rows()
            values: list[str] = []
            for element in current:
                if not element.get("visible", True):
                    continue
                if self._norm(element.get("control_type")) in {"button", "slider", "progressbar"}:
                    continue
                value = self._norm(element.get("name") or element.get("value") or "")
                if 2 < len(value) < 180 and value not in values:
                    values.append(value)
            return "|".join(values[:20])[:1800]

        action = ""
        if re.search(r"\b(?:дизлайк|не\s+нравится)\b", clean):
            action = "dislike"
        elif re.search(r"\b(?:лайк|нравится)\b", clean):
            action = "like"
        elif re.search(r"\bследующ", clean):
            action = "next"
        elif re.search(r"\bпредыдущ", clean):
            action = "previous"
        elif re.search(r"\b(?:перемот|промот)\w*", clean):
            action = "seek"
        elif re.search(r"\b(?:повтор|зацикл)", clean):
            action = "repeat"
        elif re.search(r"\b(?:перемеша|случайн\w*\s+поряд)", clean):
            action = "shuffle"
        elif re.search(r"\b(?:какая\s+песня|какой\s+трек|что\s+играет)", clean):
            action = "info"
        elif re.search(r"\b(?:пауз|приостанов)", clean):
            action = "pause"
        elif re.search(r"\b(?:продолж|возобнов|играй|включ|воспроизвед|запуст)", clean):
            action = "play"
        if not action:
            raise RuntimeError("Не распознала команду плеера")

        before_rows = rows()
        before_state = playback_state(before_rows)
        before_track = track_signature(before_rows)
        if action == "info":
            window_rect = surface.get("rectangle") or []
            bottom = int(window_rect[3]) if len(window_rect) == 4 else 1000
            floor = bottom - max(260, int((bottom - int(window_rect[1] if len(window_rect) == 4 else 0)) * .30))
            positioned: list[tuple[int, int, str]] = []
            generic = re.compile(
                r"^(?:главная|моя\s+волна|коллекция|треки|альбомы|исполнители|подкасты|"
                r"следующ\w*|предыдущ\w*|пауза|воспроизвед\w*|нравится|не\s+нравится)$",
                re.I,
            )
            for element in before_rows:
                if not element.get("visible", True) or self._norm(element.get("control_type")) not in {"text", "hyperlink"}:
                    continue
                name = str(element.get("name") or "").strip()
                rect = element.get("rectangle") or []
                if not (2 < len(name) < 120 and len(rect) == 4 and int(rect[1]) >= floor):
                    continue
                if generic.match(self._norm(name)) or re.fullmatch(r"\d{1,2}:\d{2}", name):
                    continue
                positioned.append((int(rect[1]), int(rect[0]), name))
            labels: list[str] = []
            for _y, _x, name in sorted(positioned):
                if name not in labels:
                    labels.append(name)
            if not labels:
                labels = [
                    str(element.get("name") or "").strip() for element in before_rows
                    if element.get("visible", True)
                    and self._norm(element.get("control_type")) in {"text", "hyperlink"}
                    and 2 < len(str(element.get("name") or "").strip()) < 120
                    and not generic.match(self._norm(element.get("name")))
                ]
            return {"ok": bool(labels), "completed": False, "verified": bool(labels), "action": action, "track": " — ".join(labels[:2])}

        if action in {"play", "pause"}:
            desired = "playing" if action == "play" else "paused"
            if before_state == desired:
                return {"ok": True, "completed": False, "verified": True, "action": action, "before_state": before_state, "after_state": before_state, "method": "already"}
            target = find_control(("Воспроизведение", "Воспроизвести", "Play", "Resume", "VibePlayerControls_playButton")) if action == "play" else find_control(("Пауза", "Pause", "VibePlayerControls_playButton_playing"))
        elif action == "like":
            target = find_control(("Нравится", "Like"), reject=("Не нравится", "Dislike"))
        elif action == "dislike":
            target = find_control(("Не нравится", "Dislike"))
        elif action == "next":
            target = find_control(("Следующая песня", "Следующий трек", "Next"))
        elif action == "previous":
            target = find_control(("Предыдущая песня", "Предыдущий трек", "Previous", "Prev"))
        elif action == "repeat":
            target = find_control(("Повтор", "Зациклить", "Repeat"))
        elif action == "shuffle":
            target = find_control(("Перемешать", "Случайный порядок", "Shuffle"))
        else:
            target = None

        if action == "seek":
            amount_match = re.search(r"(\d{1,4})\s*(?:сек|секунд|с\b)", clean)
            if amount_match:
                amount = min(3600, int(amount_match.group(1)))
            else:
                word_amounts = {
                    "одну": 1, "один": 1, "две": 2, "два": 2, "три": 3,
                    "пять": 5, "десять": 10, "пятнадцать": 15, "двадцать": 20,
                    "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
                }
                amount = next((value for word, value in word_amounts.items() if re.search(rf"\b{word}\b", clean)), 10)
            delta = -amount if re.search(r"\b(?:назад|обратно)\b", clean) else amount
            timelines: list[tuple[int, dict[str, Any], list[int]]] = []
            for element in before_rows:
                if not usable(element, roles=("slider", "progressbar")):
                    continue
                rect = element.get("rectangle") or []
                width = int(rect[2]) - int(rect[0]) if len(rect) == 4 else 0
                clocks = self._clock_seconds(f"{element.get('name','')} {element.get('value','')}")
                value = blob(element)
                score = width + (1000 if len(clocks) >= 2 else 0) + (800 if re.search(r"позици|время|трек|seek|progress", value) else 0)
                timelines.append((score, element, clocks))
            if not timelines:
                raise RuntimeError("Ползунок трека не найден; перемотку не выполняла")
            _, timeline, clocks = max(timelines, key=lambda item: item[0])
            if len(clocks) < 2 or clocks[-1] <= 0:
                raise RuntimeError("Плеер не показал текущую позицию и длительность; перемотку не выполняла")
            current, duration = clocks[0], clocks[-1]
            wanted = max(0, min(duration, current + delta))
            rect = [int(value) for value in timeline.get("rectangle")]
            x = int(rect[0] + (wanted / duration) * max(1, rect[2] - rect[0]))
            y = int((rect[1] + rect[3]) / 2)
            clicked = bool(self.tools.execute("click", {"x": x, "y": y}).get("ok"))
            if not clicked:
                raise RuntimeError("Не удалось изменить позицию трека")
            time.sleep(.35)
            after_rows = rows()
            after_clock: list[int] = []
            for element in after_rows:
                if self._norm(element.get("control_type")) in {"slider", "progressbar"}:
                    values = self._clock_seconds(f"{element.get('name','')} {element.get('value','')}")
                    if len(values) >= 2:
                        after_clock = values
                        break
            after_position = after_clock[0] if after_clock else -1
            verified = after_position >= 0 and (after_position > current if delta > 0 else after_position < current)
            self._trace("OPERATOR_VERIFY", app="yandex_music", action=action, delta=delta, before=current, after=after_position, verified=verified)
            return {"ok": verified, "completed": True, "verified": verified, "action": action, "seconds": delta, "before_position": current, "after_position": after_position, "method": "timeline"}

        if target is None:
            raise RuntimeError(f"Кнопка плеера для действия «{action}» не найдена")
        before_target = blob(target)
        if not self.click_element(title, target, goal=f"yandex_{action}"):
            raise RuntimeError(f"Кнопку «{action}» нашла, но нажать не получилось")
        time.sleep(.32)
        after_rows = rows()
        after_state = playback_state(after_rows)
        after_track = track_signature(after_rows)
        if action in {"play", "pause"}:
            desired = "playing" if action == "play" else "paused"
            deadline = time.monotonic() + 2.2
            while after_state != desired and time.monotonic() < deadline:
                time.sleep(.16); after_rows = rows(); after_state = playback_state(after_rows)
            verified = after_state == desired
        elif action in {"next", "previous"}:
            deadline = time.monotonic() + 3.0
            while before_track == after_track and time.monotonic() < deadline:
                time.sleep(.16)
                after_rows = rows()
                after_track = track_signature(after_rows)
            verified = bool(before_track and after_track and before_track != after_track)
        else:
            target_rect = target.get("rectangle") or []
            post_target = None
            if len(target_rect) == 4:
                cx = (int(target_rect[0]) + int(target_rect[2])) // 2
                cy = (int(target_rect[1]) + int(target_rect[3])) // 2
                candidates = []
                for element in after_rows:
                    if not usable(element):
                        continue
                    rect = element.get("rectangle") or []
                    if len(rect) != 4:
                        continue
                    ex = (int(rect[0]) + int(rect[2])) // 2
                    ey = (int(rect[1]) + int(rect[3])) // 2
                    distance = abs(ex - cx) + abs(ey - cy)
                    if distance <= 120:
                        candidates.append((distance, element))
                if candidates:
                    post_target = min(candidates, key=lambda item: item[0])[1]
            verified = bool(post_target is not None and blob(post_target) != before_target)
        self._trace("OPERATOR_VERIFY", app="yandex_music", action=action, before_state=before_state, after_state=after_state, verified=verified)
        return {"ok": bool(verified), "completed": True, "verified": bool(verified), "action": action, "before_state": before_state, "after_state": after_state, "before_track": before_track, "after_track": after_track, "method": "semantic_control", "error": "Кнопка нажата один раз, но изменение состояния не подтвердилось" if not verified else ""}

    def yandex_wave(self) -> dict[str, Any]:
        """Start Yandex Music once, using the stable browser HWND as source of truth.

        Samsung Browser changes the tab title while music.yandex.ru loads. r15.5 kept
        querying UIA by the stale title and concluded that the window disappeared, which
        caused repeated URL opens. This implementation binds to the existing window handle,
        performs at most one navigation to ``Моя волна`` and at most one Play click.
        """
        aliases=["Яндекс Музыка","Yandex Music","music.yandex"]
        window=self.yandex_surface(timeout=.35, focus=False)
        opened=False
        if not window:
            from .system_browser import open_url
            open_url("https://music.yandex.ru/")
            opened=True
            window=self.yandex_surface(timeout=8.0, focus=False)
        if not window:
            raise RuntimeError("Яндекс Музыка не появилась в браузере по умолчанию")
        handle=int(window.get("handle") or 0) or None
        title=str(window.get("title") or "Яндекс Музыка")
        if handle:
            self.tools.execute("window_focus",{"handle":handle})
        time.sleep(.25)

        def live_rows(limit: int = 260) -> list[dict[str, Any]]:
            nonlocal title
            try:
                fg=self.tools.execute("foreground_window",{})
                if fg.get("ok"):
                    row=dict(fg.get("result") or {})
                    if not handle or int(row.get("handle") or 0)==handle:
                        title=str(row.get("title") or title)
            except Exception:
                pass
            return self._elements(title,limit=limit,handle=handle)

        def find_live(terms: list[str], *, types: tuple[str,...]=( "Button", "Hyperlink", "ListItem", "Text")) -> dict[str, Any] | None:
            wanted={self._norm(t) for t in types}
            best: tuple[float,dict[str,Any]]|None=None
            for el in live_rows():
                if not el.get("visible",True) or not el.get("enabled",True):
                    continue
                if wanted and self._norm(el.get("control_type")) not in wanted:
                    continue
                rect=el.get("rectangle") or []
                if len(rect)==4 and int(rect[3])<=150:
                    continue
                blob=f"{el.get('name','')} {el.get('automation_id','')} {el.get('class_name','')}"
                score=self._score(blob,terms)
                if best is None or score>best[0]: best=(score,el)
            return best[1] if best and best[0]>=.62 else None

        def exact_play_button(rows: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
            rows = rows if rows is not None else live_rows()
            for e in rows:
                if not e.get("visible", True) or not e.get("enabled", True):
                    continue
                if self._norm(e.get("control_type")) != "button":
                    continue
                rect=e.get("rectangle") or []
                if len(rect)==4 and int(rect[3])<=150:
                    continue
                name=self._norm(e.get("name")); cls=self._norm(e.get("class_name"))
                if name in {"воспроизведение","воспроизвести","play","playback"} or "vibeplayercontrols playbutton" in cls:
                    return e
            return None

        def playback_state() -> str:
            rows=live_rows()
            for el in rows:
                rect=el.get("rectangle") or []
                if len(rect)==4 and int(rect[3])<=150:
                    continue
                blob=self._norm(f"{el.get('name','')} {el.get('automation_id','')} {el.get('class_name','')}")
                if re.search(r"(?:^| )(?:пауза|pause)(?: |$)|vibeplayercontrols playbutton playing",blob):
                    return "playing"
            root=" ".join(self._norm(x.get("name")) for x in rows[:4])
            if "воспроизводится аудио" in root or "playing audio" in root:
                return "playing"
            for el in rows:
                rect=el.get("rectangle") or []
                if len(rect)==4 and int(rect[3])<=150:
                    continue
                blob=self._norm(f"{el.get('name','')} {el.get('automation_id','')} {el.get('class_name','')}")
                if re.search(r"(?:^| )(?:воспроизвести|воспроизведение|play)(?: |$)|vibeplayercontrols playbutton",blob):
                    return "paused"
            return "unknown"

        # Allow the SPA to settle without relying on a changing title.
        settle=time.monotonic()+8.0
        state="unknown"
        while time.monotonic()<settle:
            state=playback_state()
            if state!="unknown" or find_live(["Моя волна","NavbarDesktop_title","my vibe"],types=("Hyperlink","ListItem","Text","Button")):
                break
            time.sleep(.2)
        if state=="playing":
            return {"playing":True,"verified":True,"browser":"system_default","client":"existing","already_playing":True,"opened":opened}

        # Dismiss page popups only; never touch browser chrome.
        for labels in (["не сейчас","позже"],["понятно","хорошо"]):
            el=find_live(labels,types=("Button",))
            if el:
                self.click_element(title,el,goal="dismiss_popup"); time.sleep(.15)

        rows_now=live_rows()
        play=exact_play_button(rows_now)
        if not play:
            # Give a just-opened SPA a short bounded chance to expose the real player.
            deadline=time.monotonic()+10.0
            while time.monotonic()<deadline and not play:
                time.sleep(.16)
                play=exact_play_button()
        if not play:
            # The page also contains a huge Text heading 'Моя волна'. Navigate only via
            # the left sidebar link/list item, never by a content title.
            wave=None
            for e in live_rows():
                rect=e.get("rectangle") or []
                if len(rect)!=4 or int(rect[0])>650: continue
                if self._norm(e.get("control_type")) not in {"hyperlink","listitem"}: continue
                if self._norm(e.get("name"))=="моя волна": wave=e; break
            if wave:
                self.click_element(title,wave,goal="yandex_my_wave")
                # Navigation can change title/tree; keep polling the same HWND.
                end=time.monotonic()+10.0
                while time.monotonic()<end:
                    state=playback_state()
                    if state=="playing":
                        self._trace("OPERATOR_VERIFY",app="yandex_music",action="play",verified=True,method="wave_autoplay")
                        return {"playing":True,"verified":True,"browser":"system_default","client":"visible_desktop","opened":opened,"method":"wave_autoplay"}
                    play=exact_play_button()
                    if play: break
                    time.sleep(.2)
        if not play:
            raise RuntimeError("На текущем экране Яндекс Музыки не нашла кнопку воспроизведения")
        if not self.click_element(title,play,goal="yandex_play"):
            raise RuntimeError("Кнопку воспроизведения Яндекс Музыки нашла, но нажать не получилось")
        end=time.monotonic()+15.0
        verified=False
        while time.monotonic()<end:
            if playback_state()=="playing":
                verified=True; break
            time.sleep(.2)
        self._trace("OPERATOR_VERIFY",app="yandex_music",action="play",verified=verified,method="semantic_play")
        if not verified:
            raise RuntimeError("Нажала Play один раз, но Яндекс Музыка не подтвердила воспроизведение")
        return {"playing":True,"verified":True,"browser":"system_default","client":"visible_desktop","opened":opened,"method":"semantic_play"}

    def current_page_search(self, text: str, *, submit: bool = False, max_scrolls: int = 4) -> dict[str, Any]:
        """Find/reveal a real page search field, focus it, verify text, then optionally submit."""
        text=str(text or "").strip()
        if not text: raise RuntimeError("Не указан текст для поиска")
        acquired=self.acquire_input(
            purpose="search",
            aliases=["поиск","search","query","find","search input","search field"],
            trigger_aliases=["Поиск","Search","Найти","лупа","search button"],
            max_scrolls=max_scrolls,
            visual_fallback=True,
        )
        if not acquired.get("ok"):
            raise RuntimeError(str(acquired.get("error") or "Поле поиска не найдено"))
        result=self.type_verified(acquired,text,submit=submit,require_verified=True)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "Текст в поиске не подтверждён"))
        self._trace("OPERATOR_PAGE_SEARCH",title=acquired.get("title"),text=text[:120],submit=submit,scrolls=acquired.get("scrolls",0),method="grounded")
        return {**result,"scrolls":acquired.get("scrolls",0),"method":"grounded"}

    def yandex_play_query(self, query: str) -> dict[str, Any]:
        """Search a named Yandex Music item and commit one grounded Play action."""
        query = str(query or "").strip().strip("«»\"'")
        if not query:
            raise RuntimeError("Не указано название трека")
        window = self.wait_window(["Яндекс Музыка", "Yandex Music", "music.yandex"], .4)
        if not window:
            skills = getattr(self.services, "app_skills", None)
            opened = dict(skills.open("Яндекс Музыка") or {}) if skills is not None else {}
            window = dict(opened.get("window") or {}) or self.wait_window(["Яндекс Музыка", "Yandex Music", "music.yandex"], 5.0)
        if not window:
            raise RuntimeError("Яндекс Музыка не открылась")
        handle = int(window.get("handle") or 0) or None
        title = str(window.get("title") or "Яндекс Музыка")
        if handle:
            self.tools.execute("window_focus", {"handle": handle})
        self.current_page_search(query, submit=True, max_scrolls=0)
        deadline = time.monotonic() + 4.5
        rows: list[dict[str, Any]] = []
        target = None
        while time.monotonic() < deadline and target is None:
            rows = self._elements(title, limit=520, handle=handle)
            target = self.resolve_element(
                title, [query], handle=handle,
                roles=("Hyperlink", "Button", "ListItem", "Group"),
                purpose="activate", content_only=True, rows=rows,
            )
            if target is None:
                time.sleep(.16)
        if target is None:
            raise RuntimeError(f"В результатах не нашла «{query}»")
        target_rect = target.get("rectangle") or []
        play_candidates: list[tuple[float, dict[str, Any]]] = []
        for element in rows:
            if self._norm(element.get("control_type")) != "button" or not element.get("visible", True) or not element.get("enabled", True):
                continue
            blob = self._norm(f"{element.get('name','')} {element.get('automation_id','')} {element.get('class_name','')}")
            if not re.search(r"\b(?:play|воспроизвести|слушать|играть)\b", blob, re.I):
                continue
            rect = element.get("rectangle") or []
            if len(rect) != 4:
                continue
            distance = 9999.0
            if len(target_rect) == 4:
                ty = (int(target_rect[1]) + int(target_rect[3])) / 2
                ey = (int(rect[1]) + int(rect[3])) / 2
                distance = abs(ty - ey)
                if distance > 130:
                    continue
            play_candidates.append((distance, element))
        commit_target = min(play_candidates, key=lambda item: item[0])[1] if play_candidates else target
        before = list(rows)
        if not self.click_element(title, commit_target, goal=f"yandex_play_named:{query}"):
            raise RuntimeError("Результат нашла, но запустить его не получилось")
        state = self.wait_for_state(handle=handle, title=title, before_rows=before, timeout=3.2, stable_for=.2, expected=[query])
        after = list(state.get("rows") or self._elements(title, limit=420, handle=handle))
        playing = any(
            self._norm(element.get("control_type")) == "button"
            and re.search(r"\b(?:pause|пауза|приостановить)\b", self._norm(f"{element.get('name','')} {element.get('class_name','')}"), re.I)
            for element in after if element.get("visible", True)
        )
        verified = bool(playing or state.get("changed"))
        self._trace("OPERATOR_VERIFY", app="yandex_music", action="play_named", query=query,
                    verified=verified, playing=playing, matched=str(target.get("name") or ""))
        return {"ok": True, "completed": True, "verified": verified, "playing": playing,
                "query": query, "matched": str(target.get("name") or ""), "method": "search+semantic-play"}

    def telegram_thread_context(self, limit: int = 18) -> dict[str, Any]:
        """Read the visible active Telegram thread without assuming one screen size."""
        win=self.wait_window(["Telegram","web.telegram","Телеграм"],.5)
        if not win:
            return {"ok":False,"error":"Telegram не открыт"}
        title=str(win.get("title") or "Telegram"); handle=int(win.get("handle") or 0) or None
        rows=self._elements(title,limit=460,handle=handle)
        win_rect=win.get("rectangle") or []
        if len(win_rect)==4:
            left,top,right,bottom=[int(v) for v in win_rect]
        else:
            rects=[e.get("rectangle") or [] for e in rows if len(e.get("rectangle") or [])==4]
            left=min((int(r[0]) for r in rects),default=0); top=min((int(r[1]) for r in rects),default=0)
            right=max((int(r[2]) for r in rects),default=1920); bottom=max((int(r[3]) for r in rects),default=1080)
        width=max(640,right-left); height=max(480,bottom-top)
        chat_left=left+int(width*.30)
        content_top=top+min(190,max(90,int(height*.10)))
        content_bottom=bottom-min(70,max(40,int(height*.05)))
        owner_split=left+int(width*.66)

        active=[]
        for e in rows:
            blob=self._norm(self._element_blob(e)); cls=self._norm(e.get("class_name")); name=self._norm(e.get("name"))
            if "chatlist" in cls and "active" in cls:
                active.append(e)
            elif self._norm(e.get("control_type")) in {"listitem","button","treeitem"} and e.get("selected") and name:
                active.append(e)
        recipient=self._norm((active[0].get("name") if active else ""))
        if not recipient:
            # Fall back to the top-bar title of the current chat, not the browser tab.
            headers=[]
            for e in rows:
                rect=e.get("rectangle") or []
                name=str(e.get("name") or "").strip()
                if not name or len(rect)!=4 or self._is_browser_chrome(e):
                    continue
                if int(rect[0])>=chat_left and top+45<=int(rect[1])<=content_top+30 and self._norm(e.get("control_type")) in {"text","button","group"}:
                    headers.append((self._rect_area(rect),name))
            if headers:
                recipient=self._norm(max(headers,key=lambda item:item[0])[1])

        msgs=[]
        for e in rows:
            typ=self._norm(e.get("control_type"))
            if typ not in {"text","document","group"}:
                continue
            name=str(e.get("name") or "").strip(); rect=e.get("rectangle") or []
            if not name or len(rect)!=4 or self._is_browser_chrome(e):
                continue
            x1,y1,x2,y2=[int(v) for v in rect]
            if x2<chat_left or y1<content_top or y1>content_bottom:
                continue
            normalized=self._norm(name)
            if re.fullmatch(r"\d{1,2}:\d{2}",name) or len(name)>1600:
                continue
            if normalized in {"today","yesterday","сегодня","вчера","message","сообщение","user info","информация"}:
                continue
            if any(mark in normalized for mark in ("write a message","input message","написать сообщение")):
                continue
            center=(x1+x2)//2
            side="owner" if center>=owner_split else "peer"
            msgs.append({"side":side,"text":name,"x":x1,"y":y1})
        msgs=sorted(msgs,key=lambda x:(x["y"],x["x"]))[-max(4,int(limit)):]
        return {"ok":bool(msgs),"recipient":recipient,"messages":msgs,"title":title,"window":{"left":left,"top":top,"right":right,"bottom":bottom}}

    def answer_discord_call(self) -> dict[str, Any]:
        launched = self.tools.execute("launch_application", {"application": "Discord"})
        if not launched.get("ok"):
            raise RuntimeError(str(launched.get("error") or "Discord не найден"))
        window = self.wait_window(["Discord"], 4.0)
        if not window:
            raise RuntimeError("Окно Discord не появилось")
        title = str(window.get("title") or "Discord")
        self.tools.execute("window_focus", {"handle": window.get("handle")})
        ok = self.click_keywords(title, ["принять", "ответить", "join call", "answer", "accept", "подключиться"], goal="discord_answer_call", types=("Button", "Text"))
        if not ok:
            ok = self.visual_click("Кнопка принять или ответить на текущий входящий звонок Discord", ["Принять", "Ответить", "Accept", "Answer", "Join call"])
        if not ok:
            raise RuntimeError("На текущем экране Discord не нашла кнопку ответа на звонок")
        time.sleep(.35)
        verified = self.verify_visible_goal("Discord звонок принят: виден активный голосовой звонок без кнопки входящего вызова", timeout=3.5)
        if not verified:
            raise RuntimeError("Нажала кнопку Discord, но подключение к звонку не подтвердилось")
        return {"answered": True, "verified": True}
