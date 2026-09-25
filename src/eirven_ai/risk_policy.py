# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RiskDecision:
    requires_confirmation: bool
    fingerprint: str = ""
    summary: str = ""
    category: str = "reversible"
    blocked: bool = False
    reason: str = ""


class RiskPolicy:
    """Deterministic gate evaluated after a tool call has been grounded.

    Model prompts are not a safety boundary.  This policy sees the exact tool and the
    exact grounded label/arguments immediately before execution, then binds a one-use
    confirmation fingerprint to that action.  Read-only and reversible navigation are
    never blocked; irreversible external commits are.
    """

    _COMMIT_TEXT = re.compile(
        r"(?:оплат|pay(?:ment)?|place\s+order|заказ\w*|оформ\w*\s+заказ|куп(?:и|ить)|"
        r"приобрет\w*|purchase|checkout|вызов\w*\s+(?:такси|машин)|такси|confirm\s+ride|book\s+ride|"
        r"отправ\w*|напиш\w*|ответ\w*|скин\w*|сообщ\w*|перешл\w*|reply|forward|send|publish|опублик\w*|post|"
        r"удал(?:ить|и)|delete|remove|стереть|"
        r"позвон(?:ить|и)|call|подтверд(?:ить|и)|confirm)",
        re.I,
    )
    _PAYMENT_TEXT = re.compile(
        r"(?:оплат|покуп|куп(?:и|ить)|приобрет|payment|pay\s+now|checkout|place\s+order|"
        r"перейти\s+к\s+оплат|банк|card|карта|cvv)",
        re.I,
    )
    _RIDE_TEXT = re.compile(r"(?:такси|вызов\w*\s+машин|закаж\w*\s+машин|book\s+ride|confirm\s+ride)", re.I)
    _ORDER_TEXT = re.compile(r"(?:закаж\w*|заброни\w*|оформ\w*\s+заказ|\border\b|\bbook(?:ing)?\b)", re.I)
    _COMMUNICATION_TEXT = re.compile(
        r"(?:отправ\w*|напиш\w*|ответ\w*|скин\w*|сообщ\w*|перешл\w*|send|reply|forward|publish|post|позвон\w*|\bcall\b)",
        re.I,
    )
    _DELETE_TEXT = re.compile(r"(?:удал\w*|стер\w*|delete|remove)", re.I)
    _DISCLOSURE_TEXT = re.compile(
        r"(?:загруз\w*|прикреп\w*|поделит\w*|предостав\w*\s+доступ|приглас\w*|"
        r"upload|attach|share|grant\s+access|invite)",
        re.I,
    )
    _REVERSIBLE_NAV_TEXT = re.compile(
        r"(?:каталог|поиск|фильтр|товар|карточк|корзин|адрес|способ\s+достав|назад|back|menu|catalog|search|filter|details)",
        re.I,
    )
    _AMBIGUOUS_COMMIT_TEXT = re.compile(
        r"(?:^|\W)(?:готово|продолжить|продолжай|далее|оформить|завершить|подтвердить|submit|continue|done)(?:$|\W)",
        re.I,
    )
    _DESTRUCTIVE_SHELL = re.compile(
        r"(?:\bremove-item\b|\bclear-content\b|\brm(?:dir)?\b|\brd\b|\bdel(?:ete)?\b|"
        r"\bformat(?:-volume)?\b|\bdiskpart\b|\bstop-computer\b|\bshutdown\b|"
        r"\bgit\s+(?:push|reset\s+--hard|clean\s+-f)\b|\bwinget\s+uninstall\b|"
        r"\bsc(?:\.exe)?\s+delete\b|\breg(?:\.exe)?\s+delete\b|"
        r"\b(?:invoke-restmethod|invoke-webrequest)\b[^\r\n]*(?:-method\s+(?:post|put|patch|delete))|"
        r"(?:^|[;&|]\s*)&?\s*[^\r\n]+\.ps1\b|"
        r"\bcurl(?:\.exe)?\b[^\r\n]*(?:-x|--request)\s*(?:post|put|patch|delete))",
        re.I,
    )
    _KNOWN_REVERSIBLE_TOOLS = {
        "launch_application", "open_service", "open_default_url", "default_search",
        "system_open_named", "system_open_path", "window_focus", "mouse_move", "scroll",
        "browser_open", "browser_search", "mail_stage_draft",
    }
    _READ_TOOL_NAME = re.compile(
        r"^(?:get|list|read|find|search|inspect|observe|describe|query|check|fetch|lookup)_|"
        r"(?:_status|_list|_snapshot|_search|_find|_details|_info|_price|_drafts|_review|_state)$",
        re.I,
    )
    _OBSERVATION_TOOLS = {
        "list_files", "read_file", "screenshot", "desktop_state", "access_status",
        "application_list", "mail_status", "mail_drafts", "process_list",
        "system_find", "system_list_files", "system_read_file", "system_diagnostics",
        "command_available", "foreground_window", "window_list", "window_elements",
        "window_wait", "explorer_current_folder", "explorer_selected_files",
        "browser_snapshot", "browser_screenshot", "web_search", "crypto_price", "wait",
    }
    _DESKTOP_ACCESS_TOOLS = {
        # Visible Windows observation/control.
        "screenshot", "desktop_state", "foreground_window", "window_list",
        "window_elements", "window_wait", "window_focus", "window_click", "window_type",
        "click", "type_text", "mouse_move", "mouse_drag", "scroll", "press_key", "hotkey",
        "application_list", "launch_application", "open_service", "close_application",
        "close_browsers", "close_user_apps", "reinstall_application", "set_dark_theme",
        "toggle_quick_setting", "media_control", "system_volume", "system_brightness",
        "system_power", "process_list", "process_terminate", "explorer_current_folder",
        "explorer_selected_files", "system_find", "system_open_named", "system_open_path",
        "system_list_files", "system_read_file", "system_write_file", "system_batch_rename",
        "powershell", "run_command", "git_publish", "open_default_url", "default_search",
        "browser_open", "browser_search", "browser_snapshot", "browser_click_text",
        "browser_fill", "browser_press", "browser_upload", "browser_screenshot",
        "write_file", "make_directory",
    }

    @classmethod
    def is_read_only(cls, tool_name: str, arguments: dict[str, Any] | None = None) -> bool:
        """Classify the exact typed call, not a word in the user's sentence."""
        name = str(tool_name or "").strip()
        args = dict(arguments or {})
        if name == "media_control":
            return str(args.get("action") or "status").casefold() in {"status", "list", "sessions", "inspect"}
        if name == "mail_review":
            return not bool(args.get("move_spam") or args.get("prepare_replies"))
        if name in cls._OBSERVATION_TOOLS:
            return True
        # Future connector observations retain a conservative naming fallback, but
        # visible navigation/search is deliberately excluded from this shortcut.
        if name.startswith("browser_") or name in {"default_search", "open_default_url"}:
            return False
        return bool(cls._READ_TOOL_NAME.search(name))

    @classmethod
    def requires_desktop_control(
        cls, tool_name: str, arguments: dict[str, Any] | None = None,
    ) -> bool:
        """Whether this typed capability reads or changes the owner's local computer."""
        del arguments
        return str(tool_name or "").strip() in cls._DESKTOP_ACCESS_TOOLS

    @staticmethod
    def _fingerprint(tool_name: str, arguments: dict[str, Any], context: dict[str, Any] | None = None) -> str:
        canonical = json.dumps(
            {"tool": tool_name, "arguments": arguments, "surface": dict(context or {})},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def evaluate(
        cls,
        task: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> RiskDecision:
        name = str(tool_name or "").strip()
        args = dict(arguments or {})
        task_text = str(task or "")
        grounded = " ".join(
            str(args.get(key) or "")
            for key in (
                "element_text", "automation_id", "text", "value", "action",
                "recipient", "target", "query", "command", "url",
                "key", "keys", "button", "label", "name",
            )
        )
        combined = f"{task_text}\n{grounded}"
        category = "reversible"
        needs = False

        desktop_enabled = bool((context or {}).get("desktop_control_enabled", True))
        if not desktop_enabled and cls.requires_desktop_control(name, args):
            return RiskDecision(
                False,
                category="permission",
                blocked=True,
                reason="Управление компьютером отключено в настройках",
            )

        if name == "system_power" and str(args.get("action") or "").casefold() != "cancel":
            needs, category = True, "system_power"
        elif name in {"process_terminate", "close_user_apps", "reinstall_application", "git_publish"}:
            needs, category = True, "destructive_system"
        elif name in {"powershell", "run_command"} and cls._DESTRUCTIVE_SHELL.search(str(args.get("command") or "")):
            needs, category = True, "destructive_command"
        elif name == "browser_upload":
            needs, category = True, "external_disclosure"
        elif name in {"window_click", "click", "press_key", "hotkey", "browser_press", "browser_click_text"}:
            # Engine-owned media recovery is a reversible, freshly grounded control
            # action.  Do not classify its Play/Pause click as external communication
            # merely because the surface title or task contains a service name.
            if not bool((context or {}).get("media_recovery")):
                task_category = ""
                if cls._PAYMENT_TEXT.search(task_text):
                    task_category = "payment"
                elif cls._RIDE_TEXT.search(task_text):
                    task_category = "ride_booking"
                elif cls._ORDER_TEXT.search(task_text):
                    task_category = "external_order"
                elif cls._COMMUNICATION_TEXT.search(task_text):
                    task_category = "external_communication"
                elif cls._DELETE_TEXT.search(task_text):
                    task_category = "deletion"
                if cls._PAYMENT_TEXT.search(grounded):
                    needs, category = True, "payment"
                elif cls._RIDE_TEXT.search(grounded):
                    needs, category = True, "ride_booking"
                elif cls._ORDER_TEXT.search(grounded):
                    needs, category = True, "external_order"
                elif cls._COMMUNICATION_TEXT.search(grounded):
                    needs, category = True, "external_communication"
                elif cls._DELETE_TEXT.search(grounded):
                    needs, category = True, "deletion"
                elif cls._COMMIT_TEXT.search(grounded):
                    needs, category = True, "external_commit"
                elif task_category and (
                    not grounded.strip()
                    or bool(re.search(r"(?:^|\W)(?:enter|return|ctrl\s*\+?\s*enter)(?:$|\W)", grounded, re.I))
                    or bool(cls._AMBIGUOUS_COMMIT_TEXT.search(grounded))
                ):
                    # Meaningful non-commit labels are navigation. An icon-only
                    # coordinate or Enter in a risky flow fails closed.
                    needs, category = True, task_category
        elif name in {"browser_fill", "window_type", "type_text"}:
            # Typing into a form remains reversible.  The later submit/Enter is gated.
            needs = False
        elif name not in cls._KNOWN_REVERSIBLE_TOOLS and not cls._READ_TOOL_NAME.search(name):
            # Future connector tools are admitted by the universal engine. Their names
            # and grounded arguments still pass through the same deterministic safety
            # boundary, so a newly installed send/order/upload/delete capability cannot
            # bypass confirmation merely because this release has no explicit branch.
            generic_grounded = f"{name} {grounded}"
            if cls._PAYMENT_TEXT.search(generic_grounded):
                needs, category = True, "payment"
            elif cls._RIDE_TEXT.search(generic_grounded):
                needs, category = True, "ride_booking"
            elif cls._ORDER_TEXT.search(generic_grounded):
                needs, category = True, "external_order"
            elif cls._COMMUNICATION_TEXT.search(generic_grounded):
                needs, category = True, "external_communication"
            elif cls._DELETE_TEXT.search(generic_grounded):
                needs, category = True, "deletion"
            elif cls._DISCLOSURE_TEXT.search(generic_grounded):
                needs, category = True, "external_disclosure"
            elif cls._COMMIT_TEXT.search(generic_grounded):
                needs, category = True, "external_commit"

        confirmation_mode = str((context or {}).get("confirmation_mode") or "full").casefold()
        if confirmation_mode not in {"every", "critical", "full"}:
            confirmation_mode = "full"
        if not needs and confirmation_mode == "every" and not cls.is_read_only(name, args):
            needs, category = True, "action_confirmation"
        if not needs:
            return RiskDecision(False)

        fingerprint = cls._fingerprint(name, args, context)
        label = str(args.get("element_text") or args.get("action") or args.get("command") or name).strip()
        surface_title = str((context or {}).get("surface_title") or "").strip()
        summary = f"{category}: {label[:180]}" + (f" в «{surface_title[:100]}»" if surface_title else "")
        return RiskDecision(True, fingerprint=fingerprint, summary=summary, category=category)
