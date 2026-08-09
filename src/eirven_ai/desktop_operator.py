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
        self._lock = threading.RLock()

    @staticmethod
    def _norm(text: str) -> str:
        text = str(text or "").casefold().replace("ё", "е")
        text = re.sub(r"[^a-zа-я0-9]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

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
        if "telegram" in self._norm(title):
            send = self._telegram_send_button(rows, ready_only=True)
            if send is not None:
                ok = self.click_element(title, send, goal="telegram_send_message_commit")
                self._trace("OPERATOR_COMMIT", app="telegram", method="send_button", ok=ok)
                return {
                    "ok": ok, "committed": ok, "method": "send_button",
                    "error": "Кнопка отправки Telegram найдена, но клик не выполнился" if not ok else "",
                }
            # Native Telegram clients do not always publish a Send button through UIA.
            # Enter remains a single-shot fallback only when no web btn-send exists.
            empty_button = self._telegram_send_button(rows, ready_only=False)
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
    ) -> dict[str, Any]:
        """Find, reveal and focus an input. Never type blindly after only a visual click."""
        fg=self.tools.execute("foreground_window",{})
        if not fg.get("ok"): return {"ok":False,"error":"Не вижу активное окно"}
        win=dict(fg.get("result") or {}); title=str(win.get("title") or ""); handle=int(win.get("handle") or 0) or None
        def rows(): return self._elements(title,limit=360,handle=handle)
        def field(current):
            roles=("Edit","ComboBox","Group","Document")
            return self.resolve_element(title,aliases,handle=handle,roles=roles,purpose=purpose,content_only=True,rows=current)
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
        telegram_composer=purpose=="composer" and "telegram" in self._norm(title)
        ready_before=bool(self._telegram_send_button(list(acquired.get("rows") or []),ready_only=True)) if telegram_composer else False
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
        time.sleep(.18)
        field_evidence,visible_evidence,focused,send_ready=inspect_evidence()
        verified=bool(field_evidence or (focused and visible_evidence) or send_ready)
        paste_retry=False
        if not verified and clipboard_used:
            # Ctrl+V can be swallowed by an overlapping web-composer layer.  Re-ground
            # the caret at the left placeholder zone and replace once through the native
            # Shift+Insert paste gesture. Ctrl+A makes the retry idempotent if the first
            # paste actually landed but Chromium failed to expose its value through UIA.
            self._click_input_rect(field)
            self.tools.execute("hotkey",{"keys":["ctrl","a"]})
            paste_retry=bool(self.tools.execute("hotkey",{"keys":["shift","insert"]}).get("ok"))
            time.sleep(.18)
            field_evidence,visible_evidence,focused,send_ready=inspect_evidence()
            verified=bool(field_evidence or (focused and visible_evidence) or send_ready)
        clipboard_roundtrip=False
        if not verified and typed:
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
        app. It uses the tiny disposable multimodal model and normalized screen coords.
        """
        path,_ = self._screenshot_digest()
        if not path:
            return False
        try:
            image=self._vision_image_b64(path)
            installed={str(x).casefold():str(x) for x in self.gateway.installed_models()}
            # Qwen 0.8B follows coordinate JSON better than tiny caption models. Use it
            # only as a short last-resort grounder; never run the old 12–22 s CPU retry.
            model=installed.get("qwen3.5:0.8b") or installed.get(str(self.services.settings.vision_model).casefold()) or self.services.settings.vision_model
            # Free heavier resident models so 4-GB GPUs cannot OOM on a single screenshot.
            for resident in list(installed.values()):
                if str(resident).casefold()!=str(model).casefold() and any(k in str(resident).casefold() for k in ("gemma","qwen","gpt-oss","devstral")):
                    try:self.gateway.unload(resident)
                    except Exception:pass
            schema={"type":"object","properties":{"found":{"type":"boolean"},"x":{"type":"number"},"y":{"type":"number"},"label":{"type":"string"}},"required":["found","x","y","label"]}
            prompt=(
                "Ты управляешь текущим экраном как пользователь. Найди ОДИН видимый интерактивный элемент для цели: " + goal +
                ". Подходящие подписи: " + ", ".join(labels) +
                ". Верни found=true и координаты центра x,y НОРМАЛИЗОВАННЫЕ от 0 до 1 относительно всего изображения. "
                "Не придумывай невидимый элемент; если его нет — found=false."
            )
            choice={}
            try:
                candidate=self.gateway.json([{ "role":"user","content":prompt,"images":[image]}],model=model,temperature=0.0,schema=schema,num_ctx=512,num_predict=64,keep_alive="0",timeout_seconds=min(4.0,max(1.5,timeout)))
                if isinstance(candidate,dict): choice=candidate
            except Exception as exc:
                self._trace("OPERATOR_VISUAL_ERROR",goal=goal,execution="oneshot",error=str(exc)[:900])
            finally:
                try:self.gateway.unload(model)
                except Exception:pass
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
            model = installed.get("qwen3.5:0.8b") or installed.get(str(self.services.settings.vision_model).casefold()) or self.services.settings.vision_model
            for resident in list(installed.values()):
                low = str(resident).casefold()
                if low != str(model).casefold() and any(k in low for k in ("gemma", "qwen", "gpt-oss", "devstral", "moondream")):
                    try: self.gateway.unload(resident)
                    except Exception: pass
            try:
                choice = self.gateway.json(
                    [{"role": "user", "content": prompt, "images": [image]}],
                    model=model, temperature=0.0, schema=schema,
                    num_ctx=512, num_predict=72, keep_alive="0", timeout_seconds=min(4.2,max(1.5,timeout)),
                )
                if isinstance(choice,dict):
                    self._trace("OPERATOR_VISUAL_RESULT",execution="oneshot",goal=prompt[:160])
                    return dict(choice)
            except Exception as exc:
                self._trace("OPERATOR_VISUAL_ERROR",execution="oneshot",goal=prompt[:180],error=str(exc)[:900])
            finally:
                try:self.gateway.unload(model)
                except Exception:pass
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
        action -> inspect again. The tiny disposable VLM chooses only coordinates/action
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
        """Inspect the current visible desktop with the small disposable vision model."""
        path, _ = self._screenshot_digest()
        if not path:
            return "Не смогла получить снимок текущего экрана."
        chat = getattr(self.services, "chat", None)
        if chat is not None:
            return chat._vision_for_path(path, question)
        return "Снимок сделан, но vision-контур недоступен."

    def _telegram_result_score(self, element: dict[str, Any], recipient: str) -> float | None:
        """Score only a real Telegram search-result row, never a name chip/label."""
        rec_n=self._norm(recipient); typ=self._norm(element.get("control_type")); cls=self._norm(element.get("class_name")); name=self._norm(element.get("name")); rect=element.get("rectangle") or []
        if not element.get("visible",True) or not element.get("enabled",True) or len(rect)!=4 or not rec_n or rec_n not in name:
            return None
        modern_row=typ=="button" and "listitem button" in cls
        legacy_row=typ in {"hyperlink","listitem"} and "chatlist" in cls
        if not (modern_row or legacy_row): return None
        width=max(0,int(rect[2])-int(rect[0])); height=max(0,int(rect[3])-int(rect[1]))
        if modern_row and (width<420 or height<70): return None
        score=8.0 if modern_row else 6.0
        if name==rec_n: score+=4.0
        if name.startswith(rec_n+" "): score+=2.5
        if name.startswith(rec_n+" "+rec_n+" "): score+=1.5
        if name.startswith("@") and rec_n in name: score+=2.0
        if any(marker in name or marker in cls for marker in ("subscribers","subscriber","channel","members","member","участник","подписчик")): score-=5.0
        if int(rect[0])<800: score+=1.0
        score += max(0.0,1.4-max(0,int(rect[1])-420)/900.0)
        return score

    def _telegram_ready(self, rows: list[dict[str, Any]]) -> bool:
        """Telegram is interactive as soon as search/chat affordances are exposed.

        Do not wait for the whole SPA tree to become stable: timestamps, presence and
        animation can keep changing even though the owner can already interact.
        """
        for e in rows:
            if not e.get("visible", True) or self._is_browser_chrome(e):
                continue
            name=self._norm(e.get("name")); aid=self._norm(e.get("automation_id")); cls=self._norm(e.get("class_name"))
            if aid=="telegram-search-input" or name in {"search","поиск","chats","чаты"}:
                return True
            if "chatlist" in cls or "listitem button" in cls:
                return True
        return False

    def _telegram_chat_evidence(self, rows: list[dict[str, Any]], recipient: str, selected_username: str = "") -> tuple[bool, dict[str, bool]]:
        rec_n=self._norm(recipient); address_match=False; header_match=False; active_match=False; composer=False
        for e in rows:
            if not e.get("visible", True):
                continue
            name=self._norm(e.get("name")); cls=self._norm(e.get("class_name")); aid=self._norm(e.get("automation_id")); rect=e.get("rectangle") or []
            raw=str(e.get("name") or "")
            if selected_username and ("omnibox" in cls or "адресная строка" in name) and selected_username.casefold() in raw.casefold():
                address_match=True
            if "chatlist" in cls and "active" in cls and rec_n and rec_n in name:
                active_match=True
            if len(rect)==4 and int(rect[0])>=760 and 150<=int(rect[1])<=390 and rec_n and rec_n in name:
                header_match=True
            if aid in {"editable-message-text","input-message-input"} or any(x in name for x in ("write a message","сообщение")):
                composer=True
        return bool(address_match or active_match or header_match), {
            "address":address_match,"header":header_match,"active":active_match,"composer":composer,
        }

    def telegram_send(self, recipient: str, text: str) -> dict[str, Any]:
        """Grounded Telegram send: loaded -> search -> result -> active chat -> composer -> send."""
        recipient=str(recipient or "").strip(); text=str(text or "").strip()
        if not recipient or not text: raise RuntimeError("Нужны получатель и текст сообщения")
        aliases=["Telegram","Телеграм","web.telegram"]
        window=self.wait_window(aliases,.35); client="existing"
        if not window:
            launched=self.tools.execute("launch_application",{"application":"Telegram"}); client="desktop"
            if not launched.get("ok"):
                from .system_browser import open_url
                open_url("https://web.telegram.org/a/"); client="web_default"
            window=self.wait_window(aliases,7.0)
        if not window: raise RuntimeError("Не появилось окно Telegram Desktop/Web")
        handle=int(window.get("handle") or 0) or None; title=str(window.get("title") or "Telegram")
        if handle: self.tools.execute("window_focus",{"handle":handle})
        # Loading time is state-based: wait until Telegram exposes search/chat content.
        initial=self._elements(title,limit=360,handle=handle)
        if not self._telegram_ready(initial):
            deadline=time.monotonic()+5.0
            while time.monotonic()<deadline and not self._telegram_ready(initial):
                time.sleep(.15)
                initial=self._elements(title,limit=360,handle=handle)
            if not self._telegram_ready(initial):
                raise RuntimeError("Telegram открылся, но интерфейс ещё не готов")

        search=self.acquire_input(
            purpose="search",aliases=["input-search-input","search","поиск"],
            trigger_aliases=["Search","Поиск"],max_scrolls=0,visual_fallback=False,
        )
        if not search.get("ok"): raise RuntimeError("Telegram открылся, но поле поиска ещё не готово")
        typed=self.type_verified(search,recipient,submit=False,require_verified=False)
        if not typed.get("typed"):
            raise RuntimeError("Поле поиска Telegram найдено, но ввести имя не получилось")
        time.sleep(.18)
        # A Telegram HTML search box may expose no ValuePattern at all.  Treat an actual
        # clickable matching result row as the verification of the non-commit search text.
        rec_n=self._norm(recipient); selected=None; rows=[]; end=time.monotonic()+6.0
        while time.monotonic()<end and selected is None:
            rows=self._elements(title,limit=380,handle=handle)
            candidates=[]
            for el in rows:
                score=self._telegram_result_score(el,recipient)
                if score is not None: candidates.append((score,el))
            if candidates: selected=max(candidates,key=lambda x:x[0])[1]
            else: time.sleep(.18)
        if selected is None: raise RuntimeError(f"Нашла поиск Telegram, но не нашла однозначный чат «{recipient}»")
        selected_name=str(selected.get("name") or ""); match=re.search(r"@[A-Za-z0-9_]{3,}",selected_name); selected_username=match.group(0) if match else ""
        before=list(rows)
        if not self.click_element(title,selected,goal="telegram_open_chat"):
            raise RuntimeError(f"Чат «{recipient}» найден, но открыть его не получилось")
        # Telegram has live timestamps/presence, so waiting for a globally stable UI
        # can burn the full timeout even when the requested chat is already open.
        opened_rows=[]; evidence={}; deadline=time.monotonic()+3.4
        while time.monotonic()<deadline:
            opened_rows=self._elements(title,limit=380,handle=handle)
            confirmed,evidence=self._telegram_chat_evidence(opened_rows,recipient,selected_username)
            if confirmed:
                break
            time.sleep(.15)
        else:
            confirmed=False
        if not confirmed:
            raise RuntimeError(f"Чат «{recipient}» найден, но Telegram не подтвердил переход в него")
        self._trace("OPERATOR_VERIFY",app="telegram",action="open_chat",recipient=recipient,verified=True,**evidence)

        composer=self.acquire_input(
            purpose="composer",aliases=["input-message-input","write a message","сообщение","message","composer","contenteditable"],
            trigger_aliases=None,max_scrolls=0,visual_fallback=False,
        )
        if not composer.get("ok"):
            raise RuntimeError(f"Чат «{recipient}» открыт, но поле сообщения не найдено")
        typed=self.type_verified(composer,text,submit=False,require_verified=True)
        if not typed.get("ok"):
            raise RuntimeError("Поле сообщения найдено, но Telegram не подтвердил появление текста; отправлять вслепую не стала")
        before_send=self._elements(title,limit=380,handle=handle); before_sig=self._ui_fingerprint(before_send)
        commit=self.commit_composer(composer)
        if not commit.get("ok"):
            raise RuntimeError(str(commit.get("error") or "Не удалось нажать кнопку отправки Telegram"))
        # Verify only after the one commit; never press Enter/click Send a second time.
        end=time.monotonic()+5.0; verified=False; composer_empty=False; text_visible=False; send_reset=False
        while time.monotonic()<end:
            current=self._elements(title,limit=380,handle=handle)
            for e in current:
                if self._is_browser_chrome(e): continue
                rect=e.get("rectangle") or []; blob=self._norm(f"{e.get('name','')} {e.get('value','')} {e.get('class_name','')}")
                if len(rect)==4 and int(rect[0])>=760 and int(rect[1])>=250 and self._norm(text) in blob: text_visible=True
                if any(x in blob for x in ("input message","input-message","write a message","сообщение")) and self._norm(text) not in blob:
                    composer_empty=True
            button=self._telegram_send_button(current,ready_only=False)
            if button is not None:
                tokens={token for token in re.split(r"\s+",str(button.get("class_name") or "").casefold()) if token}
                send_reset="record" in tokens and "send" not in tokens
            if text_visible or (send_reset and self._ui_fingerprint(current)!=before_sig) or (composer_empty and self._ui_fingerprint(current)!=before_sig):
                verified=True; break
            time.sleep(.18)
        self._trace("OPERATOR_VERIFY",app="telegram",action="send",recipient=recipient,verified=verified,text_visible=text_visible,composer_empty=composer_empty,send_reset=send_reset,commit_method=commit.get("method"),active_chat=True)
        if not verified:
            return {"sent":True,"verified":False,"completed":True,"recipient":recipient,"client":client,"method":commit.get("method"),"error":"Кнопка отправки нажата один раз, но появление bubble не подтверждено"}
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
            trigger_aliases=["Search", "Поиск"], max_scrolls=0, visual_fallback=False,
        )
        if not search.get("ok"):
            raise RuntimeError("Telegram открыт, но поле поиска не найдено")
        typed = self.type_verified(search, recipient, submit=False, require_verified=True)
        if not typed.get("ok"):
            raise RuntimeError("Имя получателя не подтвердилось в поиске Telegram")
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
                if self._norm(el.get("control_type")) not in {"button", "hyperlink", "listitem", "menuitem"}:
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

    def yandex_wave(self) -> dict[str, Any]:
        """Start Yandex Music once, using the stable browser HWND as source of truth.

        Samsung Browser changes the tab title while music.yandex.ru loads. r15.5 kept
        querying UIA by the stale title and concluded that the window disappeared, which
        caused repeated URL opens. This implementation binds to the existing window handle,
        performs at most one navigation to ``Моя волна`` and at most one Play click.
        """
        aliases=["Яндекс Музыка","Yandex Music","music.yandex"]
        window=self.wait_window(aliases,.35)
        opened=False
        if not window:
            from .system_browser import open_url
            open_url("https://music.yandex.ru/")
            opened=True
            window=self.wait_window(aliases,6.0)
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
        settle=time.monotonic()+3.0
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
            deadline=time.monotonic()+2.8
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
                end=time.monotonic()+3.0
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
        end=time.monotonic()+2.5
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

    def telegram_thread_context(self, limit: int = 18) -> dict[str, Any]:
        """Read visible active Telegram chat messages and separate likely owner/peer sides."""
        win=self.wait_window(["Telegram","web.telegram","Телеграм"],.25)
        if not win: return {"ok":False,"error":"Telegram не открыт"}
        title=str(win.get("title") or "Telegram"); handle=int(win.get("handle") or 0) or None
        rows=self._elements(title,limit=360,handle=handle)
        active=[e for e in rows if "chatlist chat" in self._norm(e.get("class_name")) and " active" in (" "+self._norm(e.get("class_name")))]
        recipient=self._norm((active[0].get("name") if active else ""))
        msgs=[]
        for e in rows:
            if self._norm(e.get("control_type"))!="text": continue
            name=str(e.get("name") or "").strip(); rect=e.get("rectangle") or []
            if not name or len(rect)!=4: continue
            x1,y1,x2,y2=[int(v) for v in rect]
            if x1<780 or y1<280 or y1>1565: continue
            if re.fullmatch(r"\d{1,2}:\d{2}",name) or len(name)>1200: continue
            if name.casefold() in {"today","yesterday","message","user info"}: continue
            side="owner" if x1>=1800 else "peer"
            msgs.append({"side":side,"text":name,"x":x1,"y":y1})
        msgs=sorted(msgs,key=lambda x:x["y"])[-max(4,int(limit)):]
        return {"ok":bool(msgs),"recipient":recipient,"messages":msgs,"title":title}

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
