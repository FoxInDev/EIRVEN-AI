# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
"""Сетевой доктор: почему телефон не открывает Эрви — и что с этим сделать.

Симптом, ради которого он появился: QR-адрес открывается на компьютере, а на
телефоне — нет. Это сбивает с толку, потому что проверка на самом компьютере
ничего не доказывает: запрос к собственному адресу проходит через брандмауэр
иначе, чем запрос с другого устройства. Поэтому доктор смотрит не на «открылось
ли на ПК», а на то, что реально решает судьбу запроса с телефона.

Что он проверяет — одним вызовом PowerShell, с ответом в JSON:
  — слушает ли Эрви всю локальную сеть, а не только сам компьютер;
  — есть ли включённое правило брандмауэра для ЭТОЙ программы и ЭТОГО порта
    (правило привязано к пути Python: после переустановки путь мог измениться,
    и старое правило перестало подходить, хотя файл состояния говорил «готово»);
  — не стоит ли сторонний брандмауэр (Kaspersky, ESET и другие): у них свои
    правила, и разрешение Windows они могут игнорировать;
  — тип сети (частная или общественная) — для пояснения.

Что чинит сам: создаёт недостающее правило брандмауэра — с одним запросом UAC,
не чаще раза в десять минут, чтобы не донимать окнами.
Чего починить не может, но объясняет: сторонний брандмауэр, изоляция устройств
на роутере (гостевой Wi-Fi), телефон в другой сети или на мобильном интернете.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .trace import log_event

_CACHE_SECONDS = 30.0
_REPAIR_COOLDOWN = 600.0

_PROBE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$port = __PORT__
$program = '__PROGRAM__'
$name = "EIRVEN Mobile LAN ($port)"
$rules = @(Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Where-Object { $_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' })
$ruleOk = $false; $ruleProgram = ''
foreach ($r in $rules) {
  $app = ($r | Get-NetFirewallApplicationFilter).Program
  $pf = $r | Get-NetFirewallPortFilter
  if ($pf.Protocol -eq 'TCP' -and ($pf.LocalPort -contains "$port")) {
    $ruleProgram = [string]$app
    if (-not $app -or $app -eq 'Any' -or ($app -ieq $program)) { $ruleOk = $true }
  }
}
$profiles = @(Get-NetConnectionProfile | ForEach-Object { [string]$_.NetworkCategory })
$thirdParty = @(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName FirewallProduct |
  Where-Object { $_.displayName -notmatch 'Windows' } | ForEach-Object { [string]$_.displayName })
[pscustomobject]@{ rule_ok = $ruleOk; rule_count = $rules.Count; rule_program = $ruleProgram;
  profiles = $profiles; third_party = $thirdParty } | ConvertTo-Json -Compress
"""


class NetworkDoctor:
    """Проверка и починка доступа к Эрви с телефона. Безопасна для разных потоков."""

    def __init__(self, root_dir: Path, port: int, host: str) -> None:
        self.root_dir = Path(root_dir)
        self.port = int(port)
        self.host = str(host or "")
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._last_repair = 0.0
        # Фоновая проверка: экран телефона не должен ждать PowerShell и ответа на UAC.
        self._worker: threading.Thread | None = None

    def _log(self, event: str, **fields: Any) -> None:
        try:
            log_event(self.root_dir, event, **fields)
        except Exception:
            pass

    # ------------------------------------------------------------ проверка
    def _probe(self) -> dict[str, Any]:
        if os.name != "nt":
            return {"rule_ok": True, "profiles": [], "third_party": [], "skipped": True}
        script = _PROBE.replace("__PORT__", str(self.port)).replace(
            "__PROGRAM__", str(Path(sys.executable).resolve()).replace("'", "''"))
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            line = (out.stdout or "").strip().splitlines()[-1] if (out.stdout or "").strip() else ""
            data = json.loads(line) if line.startswith("{") else {}
        except Exception as exc:
            self._log("NETDOC_PROBE_FAILED", error=str(exc)[:200])
            return {"rule_ok": None, "profiles": [], "third_party": [], "probe_error": str(exc)[:200]}
        # PowerShell отдаёт одиночный элемент не списком — приводим.
        for key in ("profiles", "third_party"):
            value = data.get(key)
            data[key] = value if isinstance(value, list) else ([value] if value else [])
        return data

    # ------------------------------------------------------------ починка
    def _repair(self) -> bool:
        """Создать правило брандмауэра. Сначала без прав; нужны права — один UAC."""
        if os.name != "nt":
            return True
        script = self.root_dir / "scripts" / "ensure_mobile_firewall.ps1"
        if not script.is_file():
            return False
        status_file = self.root_dir / "logs" / "mobile_firewall.status"
        status_file.parent.mkdir(parents=True, exist_ok=True)
        args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                "-Program", str(Path(sys.executable).resolve()), "-Port", str(self.port),
                "-StatusPath", str(status_file)]
        try:
            result = subprocess.run(["powershell", *args], capture_output=True, timeout=25,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0:
                return True
            if result.returncode != 5:
                return False
            # Код 5 — нужны права администратора. Просим их только для этого шага.
            import ctypes
            quoted = " ".join(f'"{a}"' if " " in a else a for a in args)
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell", quoted, str(self.root_dir), 0)
            if int(rc) <= 32:
                return False
            for _ in range(40):              # ждём, пока человек ответит на UAC
                time.sleep(0.5)
                if status_file.exists():
                    # Скрипт брандмауэра пишет при успехе «READY».
                    return "ready" in status_file.read_text(encoding="utf-8", errors="replace").lower()
            return False
        except Exception as exc:
            self._log("NETDOC_REPAIR_FAILED", error=str(exc)[:200])
            return False

    # ------------------------------------------------------------ итог
    def status(self, candidates: list[dict[str, Any]] | None = None, *, allow_repair: bool = True) -> dict[str, Any]:
        """Итог для экрана телефона: готов ли доступ и что делать человеку."""
        with self._lock:
            if self._cached is not None and time.monotonic() - self._cached_at < _CACHE_SECONDS:
                return self._cached
        problems: list[str] = []
        fixed: list[str] = []

        if self.host not in {"0.0.0.0", "::", ""}:
            problems.append("Эрви сейчас слушает только этот компьютер, поэтому телефон её не видит. "
                            "Перезапусти Эрви ярлыком — она включит доступ из локальной сети.")

        probe = self._probe()
        rule_ok = probe.get("rule_ok")
        if rule_ok is False and allow_repair and time.monotonic() - self._last_repair > _REPAIR_COOLDOWN:
            self._last_repair = time.monotonic()
            self._log("NETDOC_REPAIR_START", port=self.port, previous_program=probe.get("rule_program", ""))
            if self._repair():
                probe = self._probe()
                rule_ok = probe.get("rule_ok")
                if rule_ok:
                    fixed.append("Разрешила вход с телефона в брандмауэре Windows.")
        if rule_ok is False:
            problems.append("Брандмауэр Windows не пускает телефон. Перезапусти Эрви ярлыком и "
                            "подтверди один системный запрос — правило будет только для локальной сети.")

        third = [str(x) for x in (probe.get("third_party") or []) if str(x).strip()]
        if third:
            problems.append(f"У тебя стоит {', '.join(third)} — у него свой брандмауэр, и разрешение "
                            f"Windows он может не учитывать. Разреши в нём Эрви или порт {self.port} для локальной сети.")

        virtual = bool(candidates) and str((candidates or [{}])[0].get("kind") or "") == "virtual"
        if virtual:
            problems.append("Для QR выбран адрес виртуальной сети или VPN — телефон его не видит. "
                            "Выбери ниже адрес Wi-Fi или отключи VPN.")

        # То, что с компьютера проверить нельзя, но что чаще всего мешает.
        tips = ("Если всё выше в порядке, а телефон всё равно не открывает: он должен быть в той же "
                "Wi-Fi сети, что и компьютер, не на мобильном интернете; гостевые сети роутера "
                "изолируют устройства друг от друга — подключись к основной.")

        ready: bool | None = None if rule_ok is None and not problems else (not problems)
        parts = fixed + problems
        if parts:
            detail = " ".join(parts)
        elif rule_ok is None:
            # Проверка не удалась — значит, мы НЕ знаем, готова ли сеть. Говорить
            # «готова» здесь нельзя: это ложное успокоение.
            detail = ("Не удалось проверить брандмауэр Windows. Если телефон не открывает "
                      "страницу, перезапусти Эрви ярлыком и подтверди один системный запрос.")
        else:
            detail = "Локальная сеть готова. " + tips
        if problems or rule_ok is None:
            detail += " " + tips
        result = {
            "firewall_ready": ready,
            "detail": detail,
            "doctor": {"rule_ok": rule_ok, "third_party": third, "profiles": probe.get("profiles") or [],
                       "fixed": fixed, "problems": len(problems)},
        }
        self._log("NETDOC_STATUS", ready=ready, rule_ok=rule_ok, third_party=third, fixed=len(fixed), problems=len(problems))
        with self._lock:
            self._cached, self._cached_at = result, time.monotonic()
        return result

    def invalidate(self) -> None:
        """Сбросить кэш — например, после нажатия «Проверить снова»."""
        with self._lock:
            self._cached = None
            self._last_repair = 0.0

    def cached(self) -> dict[str, Any] | None:
        """Свежий результат, если он есть, иначе None."""
        with self._lock:
            if self._cached is not None and time.monotonic() - self._cached_at < _CACHE_SECONDS:
                return self._cached
        return None

    def check_for_screen(self, candidates: list[dict[str, Any]] | None, wait: float = 6.0) -> dict[str, Any] | None:
        """Для экрана телефона: подождать проверку не дольше wait секунд.

        Проверка обычно укладывается в 1–3 секунды. Не уложилась — экран сразу
        получит прежний статус, а проверка и починка доделаются в фоне: починка
        может ждать ответа на запрос UAC, и держать из-за неё экран нельзя.
        """
        fresh = self.cached()
        if fresh is not None:
            return fresh
        with self._lock:
            worker = self._worker
            if worker is None or not worker.is_alive():
                worker = threading.Thread(target=self._run_background, args=(candidates,),
                                          daemon=True, name="eirven-netdoc")
                self._worker = worker
                worker.start()
        worker.join(max(0.0, float(wait)))
        return self.cached()

    def _run_background(self, candidates: list[dict[str, Any]] | None) -> None:
        try:
            self.status(candidates)
        except Exception as exc:
            self._log("NETDOC_FAILED", error=str(exc)[:200])

