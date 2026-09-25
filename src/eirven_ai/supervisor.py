# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        return result.returncode == 0 and str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _process_command_line(pid: int) -> str:
    """Best-effort process identity check for stale/reused PID files.

    A bare PID is not an identity: Windows can reuse it immediately after STOP EIRVEN
    force-terminates the old supervisor.  Treat the pid file as authoritative only when
    that PID still belongs to an EIRVEN supervisor process.
    """
    if pid <= 0:
        return ""
    if os.name == "nt":
        script = (
            f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" "
            "-ErrorAction SilentlyContinue; if ($p) { [Console]::Out.Write($p.CommandLine) }"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2.0,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return str(result.stdout or "").strip()
        except Exception:
            return ""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ")
        return raw.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def is_eirven_supervisor(pid: int) -> bool:
    if not pid_alive(pid):
        return False
    command = _process_command_line(pid).casefold()
    return "eirven_ai.supervisor" in command


def _acquire_global_runtime_lock(root: Path) -> BinaryIO | None:
    """Allow one EIRVEN supervisor across venv and system Python installations."""
    try:
        if os.name == "nt":
            import msvcrt
            local = os.environ.get("LOCALAPPDATA", "").strip()
            directory = (Path(local) / "EIRVEN") if local else (root / "data")
            directory.mkdir(parents=True, exist_ok=True)
            handle = (directory / "runtime.lock").open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0"); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return handle
        import fcntl
        (root / "data").mkdir(parents=True, exist_ok=True)
        handle = (root / "data" / "runtime.lock").open("a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except Exception:
        try:
            handle.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return None


def main() -> int:
    root = Path(os.getenv("EIRVEN_ROOT_DIR", Path.cwd())).resolve()
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    runtime_lock = _acquire_global_runtime_lock(root)
    if runtime_lock is None:
        with (logs / "supervisor.log").open("a", encoding="utf-8") as output:
            output.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} duplicate supervisor blocked by global runtime lock\n")
        return 0
    supervisor_pid = logs / "supervisor.pid"
    stop_file = logs / "stop.request"
    try:
        previous = int(supervisor_pid.read_text(encoding="ascii").strip())
        if is_eirven_supervisor(previous):
            runtime_lock.close()
            return 0
        # Dead/reused PIDs are stale state, not proof that EIRVEN is running.
        supervisor_pid.unlink(missing_ok=True)
    except Exception:
        supervisor_pid.unlink(missing_ok=True)
    # STOP EIRVEN intentionally leaves a stop marker while the previous processes die.
    # A fresh, identity-checked supervisor owns the next lifecycle and can clear it.
    stop_file.unlink(missing_ok=True)
    supervisor_pid.write_text(str(os.getpid()), encoding="ascii")
    shutting_down = False
    child: subprocess.Popen | None = None

    def _end_child_tree(proc: subprocess.Popen | None, timeout: float = 6.0) -> None:
        """Terminate the server and everything it spawned.

        proc.terminate() only ends the direct child. The API server starts its own
        voice and TTS worker processes; those survive and keep the listening port
        bound plus the frozen build's _MEI temp directory locked, so the next launch
        picks a different port and Windows reports a leftover temp folder.
        """
        if proc is None or proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=timeout, check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def stop_handler(*_args) -> None:
        nonlocal shutting_down
        shutting_down = True
        stop_file.touch(exist_ok=True)
        _end_child_tree(child)

    signal.signal(signal.SIGTERM, stop_handler)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, stop_handler)

    backoff = [1, 2, 4, 8, 15]
    restart_index = 0
    try:
        while not shutting_down and not stop_file.exists():
            log = (logs / "server.log").open("a", encoding="utf-8")
            env = {**os.environ, "EIRVEN_ROOT_DIR": str(root), "EIRVEN_OPEN_BROWSER": "false"}
            # Keep source checkouts importable even when the supervised child uses
            # the user's data directory as cwd. Installed wheels do not need this,
            # but an absolute path is harmless and makes recovery tests reliable.
            package_parent = str(Path(__file__).resolve().parents[1])
            current_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = package_parent + (os.pathsep + current_pythonpath if current_pythonpath else "")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            child = subprocess.Popen(
                [sys.executable, "-m", "eirven_ai.app"],
                cwd=root,
                env=env,
                stdout=log,
                stderr=log,
                creationflags=flags,
            )
            started = time.monotonic()
            # Never block forever in child.wait(): the Settings shutdown button communicates
            # through stop.request, so the supervisor must observe it while the child lives.
            while child.poll() is None:
                if shutting_down or stop_file.exists():
                    shutting_down = True
                    _end_child_tree(child)
                    break
                time.sleep(.12)
            code = child.poll()
            if code is None:
                try:
                    code = child.wait(timeout=1.0)
                except Exception:
                    code = -1
            log.close()
            if shutting_down or stop_file.exists():
                break
            runtime = time.monotonic() - started
            if runtime > 120:
                restart_index = 0
            delay = backoff[min(restart_index, len(backoff) - 1)]
            restart_index += 1
            with (logs / "supervisor.log").open("a", encoding="utf-8") as output:
                output.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} server exited {code}; restart in {delay}s\n")
            time.sleep(delay)
    finally:
        supervisor_pid.unlink(missing_ok=True)
        stop_file.unlink(missing_ok=True)
        runtime_lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
