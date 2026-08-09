from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


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


def main() -> int:
    root = Path(os.getenv("EIRVEN_ROOT_DIR", Path.cwd())).resolve()
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    supervisor_pid = logs / "supervisor.pid"
    stop_file = logs / "stop.request"
    try:
        previous = int(supervisor_pid.read_text(encoding="ascii").strip())
        if is_eirven_supervisor(previous):
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

    def stop_handler(*_args) -> None:
        nonlocal shutting_down
        shutting_down = True
        stop_file.touch(exist_ok=True)
        if child and child.poll() is None:
            child.terminate()

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
            code = child.wait()
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
