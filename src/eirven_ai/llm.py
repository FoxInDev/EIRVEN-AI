from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Generator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .llm_arbiter import CompositeStop, GLOBAL_LLM_ARBITER


class LLMError(RuntimeError):
    pass


def _http_error_detail(response: httpx.Response, limit: int = 1000) -> str:
    """Read an HTTPX response safely, including responses opened in streaming mode."""
    try:
        response.read()
    except Exception:
        pass
    try:
        text = response.text
    except Exception:
        try:
            text = response.content.decode(response.encoding or "utf-8", errors="replace")
        except Exception:
            text = ""
    clean = (text or "").strip()
    return clean[:limit] or response.reason_phrase or "без текста ошибки"


class LLMPreempted(LLMError):
    pass


def _request_timeout(timeout_seconds: float | None) -> httpx.Timeout | None:
    """Translate EIRVEN's per-model budget to HTTPX without a hidden 5s read cap.

    HTTPX's read timeout is an inactivity timeout, not a whole-request deadline.  The
    caller-side loops still enforce elapsed deadlines where streaming is used, but the
    transport must never silently shrink a 9 second model budget to 5 seconds.
    """
    if not timeout_seconds or float(timeout_seconds) <= 0:
        return None
    budget = max(0.5, float(timeout_seconds))
    return httpx.Timeout(
        budget,
        connect=min(4.0, budget),
        read=budget,
        write=min(10.0, budget),
        pool=min(5.0, budget),
    )


def extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [pos for pos in (text.find("{"), text.find("[")) if pos >= 0]
    if not starts:
        raise LLMError("Модель не вернула JSON")
    start = min(starts)
    for end in range(len(text), start, -1):
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
    raise LLMError("Не удалось разобрать JSON модели")


@dataclass(slots=True)
class GenerationMetrics:
    model: str
    total_seconds: float = 0.0
    load_seconds: float = 0.0
    prompt_tokens: int = 0
    generated_tokens: int = 0
    tokens_per_second: float = 0.0
    first_token_seconds: float = 0.0
    prompt_eval_seconds: float = 0.0
    generation_seconds: float = 0.0
    thinking_chars: int = 0
    stopped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OllamaBackend:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.ollama_url
        self.default_model = settings.model
        self._local = threading.local()

    @property
    def last_metrics(self) -> GenerationMetrics | None:
        return getattr(self._local, "last_metrics", None)

    def health(self) -> dict[str, Any]:
        try:
            response = httpx.get(f"{self.base_url}/api/version", timeout=3, trust_env=False)
            response.raise_for_status()
            return {"ok": True, "backend": "ollama", **response.json()}
        except Exception as exc:
            return {"ok": False, "backend": "ollama", "error": str(exc)}

    def models(self) -> list[str]:
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=5, trust_env=False)
            response.raise_for_status()
            return [item["name"] for item in response.json().get("models", [])]
        except Exception:
            return []

    def model_details(self) -> list[dict[str, Any]]:
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=5, trust_env=False)
            response.raise_for_status()
            return list(response.json().get("models", []))
        except Exception:
            return []

    def model_capabilities(self, model: str) -> list[str]:
        """Return Ollama-declared model capabilities when the server exposes them."""
        try:
            response = httpx.post(
                f"{self.base_url}/api/show", json={"model": model}, timeout=3, trust_env=False
            )
            response.raise_for_status()
            values = response.json().get("capabilities", [])
            return [str(x).casefold() for x in values if str(x).strip()]
        except Exception:
            return []

    def warm(self, model: str, keep_alive: str | None = None, num_gpu: int | None = None) -> None:
        payload = {
            "model": model,
            "messages": [],
            "stream": False,
            "keep_alive": keep_alive or self.settings.keep_alive,
        }
        if num_gpu is not None:
            payload["options"] = {"num_gpu": int(num_gpu)}
        try:
            response = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=300, trust_env=False)
            response.raise_for_status()
        except Exception as exc:
            raise LLMError(f"Не удалось загрузить модель {model}: {exc}") from exc

    def unload(self, model: str) -> None:
        payload = {"model": model, "messages": [], "stream": False, "keep_alive": 0}
        try:
            httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=60, trust_env=False).raise_for_status()
        except Exception as exc:
            raise LLMError(f"Не удалось выгрузить модель {model}: {exc}") from exc

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        *,
        think: bool = False,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        keep_alive: str | None = None,
        stop_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
        num_gpu: int | None = None,
    ) -> Generator[str, None, None]:
        selected = model or self.default_model
        payload = {
            "model": selected,
            "messages": messages,
            "stream": True,
            "keep_alive": keep_alive or self.settings.keep_alive,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx or self.settings.chat_num_ctx,
                "num_predict": num_predict or self.settings.chat_num_predict,
            },
        }
        if num_gpu is not None:
            payload["options"]["num_gpu"] = int(num_gpu)
        has_images = any(bool(message.get("images")) for message in messages)
        if not has_images:
            payload["think"] = think
        started = time.perf_counter()
        first_token_at: float | None = None
        thinking_chars = 0
        final_event: dict[str, Any] = {}
        stopped = False
        try:
            request_timeout = _request_timeout(timeout_seconds)
            with httpx.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=request_timeout,
                trust_env=False,
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if timeout_seconds and time.perf_counter() - started > float(timeout_seconds):
                        raise LLMError(f"Локальная модель не закончила ответ за {int(float(timeout_seconds))} сек.")
                    if stop_event and stop_event.is_set():
                        stopped = True
                        break
                    if not line:
                        continue
                    event = json.loads(line)
                    final_event = event
                    message = event.get("message", {})
                    thinking_chars += len(message.get("thinking", "") or "")
                    content = message.get("content", "") or ""
                    if content:
                        if first_token_at is None:
                            first_token_at = time.perf_counter()
                        yield content
        except httpx.ConnectError as exc:
            raise LLMError(
                "Не удалось подключиться к Ollama. Запустите Ollama и скачайте модель."
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMError(f"Локальная модель не начала/продолжила ответ за {int(timeout_seconds or 0)} сек.") from exc
        except httpx.HTTPStatusError as exc:
            detail = _http_error_detail(exc.response)
            raise LLMError(f"Ollama вернула ошибку {exc.response.status_code}: {detail}") from exc
        except Exception as exc:
            raise LLMError(f"Ошибка Ollama: {exc}") from exc
        finally:
            ended = time.perf_counter()
            eval_count = int(final_event.get("eval_count") or 0)
            eval_duration = float(final_event.get("eval_duration") or 0) / 1_000_000_000
            self._local.last_metrics = GenerationMetrics(
                model=selected,
                total_seconds=round(ended - started, 3),
                load_seconds=round(float(final_event.get("load_duration") or 0) / 1_000_000_000, 3),
                prompt_tokens=int(final_event.get("prompt_eval_count") or 0),
                generated_tokens=eval_count,
                tokens_per_second=round(eval_count / eval_duration, 2) if eval_duration > 0 else 0.0,
                first_token_seconds=round((first_token_at or ended) - started, 3),
                prompt_eval_seconds=round(float(final_event.get("prompt_eval_duration") or 0) / 1_000_000_000, 3),
                generation_seconds=round(eval_duration, 3),
                thinking_chars=thinking_chars,
                stopped=stopped,
            )

    def chat_once(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.3,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | str | None = None,
        *,
        think: bool = False,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        keep_alive: str | None = None,
        timeout_seconds: float | None = None,
        num_gpu: int | None = None,
    ) -> dict[str, Any]:
        selected = model or self.default_model
        payload: dict[str, Any] = {
            "model": selected,
            "messages": messages,
            "stream": False,
            "keep_alive": keep_alive or self.settings.keep_alive,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx or self.settings.task_num_ctx,
                "num_predict": num_predict or self.settings.task_num_predict,
            },
        }
        if num_gpu is not None:
            payload["options"]["num_gpu"] = int(num_gpu)
        has_images = any(bool(message.get("images")) for message in messages)
        if not has_images:
            payload["think"] = think
        if tools:
            payload["tools"] = tools
        if response_format:
            payload["format"] = response_format
        started = time.perf_counter()
        try:
            request_timeout = _request_timeout(timeout_seconds)
            response = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=request_timeout, trust_env=False)
            response.raise_for_status()
            body = response.json()
            message = body.get("message", {})
            eval_count = int(body.get("eval_count") or 0)
            eval_duration = float(body.get("eval_duration") or 0) / 1_000_000_000
            self._local.last_metrics = GenerationMetrics(
                model=selected,
                total_seconds=round(time.perf_counter() - started, 3),
                load_seconds=round(float(body.get("load_duration") or 0) / 1_000_000_000, 3),
                prompt_tokens=int(body.get("prompt_eval_count") or 0),
                generated_tokens=eval_count,
                tokens_per_second=round(eval_count / eval_duration, 2) if eval_duration > 0 else 0.0,
                first_token_seconds=round(time.perf_counter() - started, 3),
                prompt_eval_seconds=round(float(body.get("prompt_eval_duration") or 0) / 1_000_000_000, 3),
                generation_seconds=round(eval_duration, 3),
                thinking_chars=len(message.get("thinking", "") or ""),
            )
            return message
        except httpx.ConnectError as exc:
            raise LLMError(
                "Не удалось подключиться к Ollama. Запустите Ollama и скачайте модель."
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMError(f"Локальная модель не ответила за {int(timeout_seconds or 0)} сек.") from exc
        except httpx.HTTPStatusError as exc:
            detail = _http_error_detail(exc.response)
            raise LLMError(f"Ollama вернула ошибку {exc.response.status_code}: {detail}") from exc
        except Exception as exc:
            raise LLMError(f"Ошибка Ollama: {exc}") from exc

    def embed(self, text: str, model: str | None = None) -> list[float]:
        payload = {
            "model": model or self.settings.embedding_model,
            "input": text,
            "keep_alive": self.settings.keep_alive,
        }
        try:
            response = httpx.post(f"{self.base_url}/api/embed", json=payload, timeout=300, trust_env=False)
            response.raise_for_status()
            embeddings = response.json().get("embeddings") or []
            return list(embeddings[0]) if embeddings else []
        except Exception as exc:
            raise LLMError(f"Не удалось получить embedding: {exc}") from exc


class ClaudeCodeLocalBackend(OllamaBackend):
    """Use the official Claude Code harness with Ollama's local Anthropic endpoint.

    Claude Code is an agent/CLI, not a downloadable copy of Anthropic's proprietary
    Claude weights. The selected ``--model`` is therefore an Ollama model chosen by the
    hardware profile. Structured EIRVEN tool/JSON calls intentionally keep using the
    inherited native Ollama API; conversational streaming uses this harness first.
    """

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.command = str(os.getenv("EIRVEN_CLAUDE_CODE_COMMAND", "claude") or "claude").strip()
        self.fallback_enabled = str(os.getenv("EIRVEN_CLAUDE_CODE_FALLBACK", "1")).strip().casefold() not in {"0", "false", "no", "off"}
        self.claude_fast_model = str(os.getenv("EIRVEN_CLAUDE_CODE_FAST_MODEL", "") or "").strip()
        self.claude_main_model = str(os.getenv("EIRVEN_CLAUDE_CODE_MODEL", "") or "").strip()
        self.harness_mode = str(os.getenv("EIRVEN_CLAUDE_CODE_MODE", "agentic") or "agentic").strip().casefold()

    def _claude_model(self, requested: str | None = None) -> str:
        """Map EIRVEN's fast/main lanes to context-sized Ollama aliases."""
        if requested and requested == self.settings.fast_model and self.claude_fast_model:
            return self.claude_fast_model
        if requested and requested == self.settings.model and self.claude_main_model:
            return self.claude_main_model
        return requested or self.claude_main_model or self.default_model

    def _command_path(self) -> str:
        found = shutil.which(self.command) or shutil.which("claude.cmd") or shutil.which("claude.exe")
        if found:
            return found
        candidates: list[Path] = []
        home = Path.home()
        candidates.extend([home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude"])
        appdata = os.getenv("APPDATA", "").strip()
        if appdata:
            candidates.append(Path(appdata) / "npm" / "claude.cmd")
        local = os.getenv("LOCALAPPDATA", "").strip()
        if local:
            candidates.extend([Path(local) / "Programs" / "claude" / "claude.exe", Path(local) / "claude" / "claude.exe"])
        return next((str(path) for path in candidates if path.is_file()), "")

    def health(self) -> dict[str, Any]:
        ollama = super().health()
        command = self._command_path()
        version = ""
        if command:
            try:
                completed = subprocess.run(
                    [command, "--version"], capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                version = (completed.stdout or completed.stderr or "").strip()[:200]
            except Exception:
                version = ""
        ok = bool(ollama.get("ok") and command)
        result = {
            **ollama,
            "ok": ok,
            "backend": "claude_code_local",
            "claude_code": bool(command),
            "claude_code_path": command,
            "claude_code_version": version,
            "local_model": self.claude_main_model or self.default_model,
            "fast_local_model": self.claude_fast_model or self.settings.fast_model,
            "harness_mode": self.harness_mode,
            "cloud_api": False,
        }
        if not command:
            result["error"] = "Claude Code CLI не установлен; доступен резервный прямой Ollama-контур"
        return result

    @staticmethod
    def _message_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(part for part in parts if part)
        return str(content or "")

    def _claude_prompt(self, messages: list[dict[str, Any]]) -> tuple[str, str]:
        systems: list[str] = []
        turns: list[str] = []
        for message in messages:
            role = str(message.get("role") or "user").casefold()
            text = self._message_text(message.get("content"))
            if not text:
                continue
            if role == "system":
                systems.append(text)
            else:
                label = "Владелец" if role == "user" else "Эрви"
                turns.append(f"{label}: {text}")
        system = "\n\n".join(systems).strip() or "Ты локальный ассистент EIRVEN. Отвечай на языке владельца."
        prompt = (
            "Продолжи этот диалог одним ответом Эрви. Не повторяй метки ролей и не описывай внутренний процесс.\n\n"
            + "\n\n".join(turns)
            + "\n\nЭрви:"
        )
        return system, prompt

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        *,
        think: bool = False,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        keep_alive: str | None = None,
        stop_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
        num_gpu: int | None = None,
    ) -> Generator[str, None, None]:
        requested = model or self.default_model
        use_harness = self.harness_mode == "all" or (
            self.harness_mode == "agentic"
            and (bool(think) or requested in {self.settings.code_model, self.settings.deep_model})
        )
        # Claude Code is a capable agent shell but starting a fresh CLI process for a
        # greeting adds latency without changing the local weights. Real-time dialogue
        # therefore talks to the exact same Ollama model directly; code/deep lanes retain
        # the Claude Code harness and its project-oriented context handling.
        if not use_harness:
            yield from super().stream_chat(
                messages, model, temperature, think=think, num_ctx=num_ctx,
                num_predict=num_predict, keep_alive=keep_alive, stop_event=stop_event,
                timeout_seconds=timeout_seconds, num_gpu=num_gpu,
            )
            return
        command = self._command_path()
        if not command:
            if self.fallback_enabled:
                yield from super().stream_chat(
                    messages, model, temperature, think=think, num_ctx=num_ctx,
                    num_predict=num_predict, keep_alive=keep_alive, stop_event=stop_event,
                    timeout_seconds=timeout_seconds, num_gpu=num_gpu,
                )
                return
            raise LLMError("Claude Code CLI не найден")

        selected = self._claude_model(model)
        system, prompt = self._claude_prompt(messages)
        started = time.perf_counter()
        first_token_at: float | None = None
        generated_chars = 0
        prompt_tokens = 0
        output_tokens = 0
        stopped = False
        yielded = False
        system_path = ""
        process: subprocess.Popen[str] | None = None
        errors: list[str] = []
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", prefix="eirven-claude-system-", delete=False) as handle:
                handle.write(system)
                system_path = handle.name
            args = [
                command, "-p", "--input-format", "text", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages", "--model", selected,
                "--system-prompt-file", system_path, "--tools", "", "--strict-mcp-config",
                "--no-session-persistence", "--bare",
            ]
            env = os.environ.copy()
            env.update({
                "ANTHROPIC_AUTH_TOKEN": "ollama",
                "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_BASE_URL": self.base_url,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            })
            process = subprocess.Popen(
                args, cwd=str(self.settings.root_dir), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process.stdin is not None:
                process.stdin.write(prompt)
                process.stdin.close()

            events: queue.Queue[tuple[str, str]] = queue.Queue()

            def _read_stdout() -> None:
                try:
                    if process is not None and process.stdout is not None:
                        for line in process.stdout:
                            events.put(("stdout", line))
                finally:
                    events.put(("stdout_done", ""))

            def _read_stderr() -> None:
                try:
                    if process is not None and process.stderr is not None:
                        for line in process.stderr:
                            errors.append(line)
                finally:
                    events.put(("stderr_done", ""))

            threading.Thread(target=_read_stdout, daemon=True, name="claude-code-stdout").start()
            threading.Thread(target=_read_stderr, daemon=True, name="claude-code-stderr").start()
            stdout_done = False
            final_text = ""
            while not stdout_done or (process.poll() is None):
                if stop_event and stop_event.is_set():
                    stopped = True
                    process.terminate()
                    break
                if timeout_seconds and time.perf_counter() - started > float(timeout_seconds):
                    process.terminate()
                    raise LLMError(f"Claude Code Local не закончил ответ за {int(float(timeout_seconds))} сек.")
                try:
                    kind, line = events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if kind == "stdout_done":
                    stdout_done = True
                    continue
                if kind != "stdout" or not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = str(event.get("type") or "")
                if event_type == "stream_event":
                    inner = event.get("event") or {}
                    delta = inner.get("delta") or {}
                    if str(delta.get("type") or "") == "text_delta":
                        content = str(delta.get("text") or "")
                        if content:
                            yielded = True
                            generated_chars += len(content)
                            first_token_at = first_token_at or time.perf_counter()
                            yield content
                elif event_type == "result":
                    final_text = str(event.get("result") or "")
                    usage = event.get("usage") or {}
                    prompt_tokens = int(usage.get("input_tokens") or 0)
                    output_tokens = int(usage.get("output_tokens") or 0)
            return_code = process.wait(timeout=3) if process.poll() is None else int(process.returncode or 0)
            if return_code != 0 and not stopped:
                detail = "".join(errors).strip()[-1600:] or f"код {return_code}"
                raise LLMError(f"Claude Code Local завершился с ошибкой: {detail}")
            if final_text and not yielded and not stopped:
                yielded = True
                generated_chars += len(final_text)
                first_token_at = first_token_at or time.perf_counter()
                yield final_text
        except Exception as exc:
            if process is not None and process.poll() is None:
                try: process.terminate()
                except Exception: pass
            if self.fallback_enabled and not yielded and not (stop_event and stop_event.is_set()):
                yield from super().stream_chat(
                    messages, model, temperature, think=think, num_ctx=num_ctx,
                    num_predict=num_predict, keep_alive=keep_alive, stop_event=stop_event,
                    timeout_seconds=timeout_seconds, num_gpu=num_gpu,
                )
                return
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Ошибка Claude Code Local: {exc}") from exc
        finally:
            if system_path:
                try: Path(system_path).unlink(missing_ok=True)
                except Exception: pass
            if not (self.fallback_enabled and not yielded and self.last_metrics is not None):
                ended = time.perf_counter()
                elapsed = max(0.001, ended - started)
                self._local.last_metrics = GenerationMetrics(
                    model=f"claude-code-local/{selected}", total_seconds=round(elapsed, 3),
                    prompt_tokens=prompt_tokens, generated_tokens=output_tokens or max(1, generated_chars // 4),
                    tokens_per_second=round((output_tokens or max(1, generated_chars // 4)) / elapsed, 2),
                    first_token_seconds=round((first_token_at or ended) - started, 3), stopped=stopped,
                )


class LlamaCppBackend:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model_path = (
            Path(settings.gguf_model_path).expanduser() if settings.gguf_model_path else None
        )
        self.context_size = settings.context_size
        self.gpu_layers = settings.gpu_layers
        self._llama: Any = None
        self._lock = threading.RLock()
        self._local = threading.local()

    @property
    def last_metrics(self) -> GenerationMetrics | None:
        return getattr(self._local, "last_metrics", None)

    def _load(self) -> Any:
        with self._lock:
            if self._llama is not None:
                return self._llama
            if not self.model_path or not self.model_path.exists():
                raise LLMError(
                    "Для llama_cpp укажите существующий EIRVEN_GGUF_MODEL_PATH в .env"
                )
            try:
                from llama_cpp import Llama
            except ImportError as exc:
                raise LLMError(
                    "Установите поддержку llama.cpp: pip install -e '.[llama-cpp]'"
                ) from exc
            self._llama = Llama(
                model_path=str(self.model_path),
                n_ctx=self.context_size,
                n_gpu_layers=self.gpu_layers,
                verbose=False,
            )
            return self._llama

    def health(self) -> dict[str, Any]:
        return {
            "ok": bool(self.model_path and self.model_path.exists()),
            "backend": "llama_cpp",
            "model_path": str(self.model_path or ""),
        }

    def models(self) -> list[str]:
        return [self.model_path.name] if self.model_path and self.model_path.exists() else []

    def model_details(self) -> list[dict[str, Any]]:
        return [{"name": name} for name in self.models()]

    def warm(self, model: str, keep_alive: str | None = None, num_gpu: int | None = None) -> None:
        self._load()

    def unload(self, model: str) -> None:
        with self._lock:
            self._llama = None

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        *,
        think: bool = False,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        keep_alive: str | None = None,
        stop_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
        num_gpu: int | None = None,
    ) -> Generator[str, None, None]:
        llama = self._load()
        started = time.perf_counter()
        first: float | None = None
        generated = 0
        stream = llama.create_chat_completion(
            messages=messages,
            temperature=temperature,
            max_tokens=num_predict or self.settings.chat_num_predict,
            stream=True,
        )
        for event in stream:
            if stop_event and stop_event.is_set():
                break
            delta = event.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content", "")
            if content:
                first = first or time.perf_counter()
                generated += 1
                yield content
        elapsed = time.perf_counter() - started
        self._local.last_metrics = GenerationMetrics(
            model=model or (self.model_path.name if self.model_path else "gguf"),
            total_seconds=round(elapsed, 3),
            first_token_seconds=round((first or time.perf_counter()) - started, 3),
            generated_tokens=generated,
            stopped=bool(stop_event and stop_event.is_set()),
        )

    def chat_once(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.3,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | str | None = None,
        *,
        think: bool = False,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        keep_alive: str | None = None,
        timeout_seconds: float | None = None,
        num_gpu: int | None = None,
    ) -> dict[str, Any]:
        llama = self._load()
        kwargs: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": num_predict or self.settings.task_num_predict,
        }
        if tools:
            kwargs["tools"] = tools
        if response_format:
            kwargs["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        result = llama.create_chat_completion(**kwargs)
        self._local.last_metrics = GenerationMetrics(
            model=model or (self.model_path.name if self.model_path else "gguf"),
            total_seconds=round(time.perf_counter() - started, 3),
        )
        return result.get("choices", [{}])[0].get("message", {})

    def embed(self, text: str, model: str | None = None) -> list[float]:
        llama = self._load()
        result = llama.create_embedding(text)
        data = result.get("data") or []
        return list(data[0].get("embedding") or []) if data else []


class ModelGateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        if settings.llm_backend == "llama_cpp":
            self.backend: OllamaBackend | LlamaCppBackend = LlamaCppBackend(settings)
        elif settings.llm_backend in {"claude_code", "claude_code_local", "claude-local"}:
            self.backend = ClaudeCodeLocalBackend(settings)
        else:
            self.backend = OllamaBackend(settings)
        self._context = threading.local()

    def _priority(self) -> str:
        return getattr(self._context, "priority", "interactive")

    def _cancel_event(self) -> threading.Event | None:
        return getattr(self._context, "cancel_event", None)

    def background(self, cancel_event: threading.Event | None = None):
        gateway = self
        class _Scope:
            def __enter__(self_inner):
                self_inner.prev_priority = getattr(gateway._context, "priority", None)
                self_inner.prev_cancel = getattr(gateway._context, "cancel_event", None)
                gateway._context.priority = "background"
                gateway._context.cancel_event = cancel_event
                return gateway
            def __exit__(self_inner, *_args):
                if self_inner.prev_priority is None:
                    try: del gateway._context.priority
                    except AttributeError: pass
                else:
                    gateway._context.priority = self_inner.prev_priority
                if self_inner.prev_cancel is None:
                    try: del gateway._context.cancel_event
                    except AttributeError: pass
                else:
                    gateway._context.cancel_event = self_inner.prev_cancel
        return _Scope()

    @property
    def last_metrics(self) -> GenerationMetrics | None:
        return self.backend.last_metrics

    def health(self) -> dict[str, Any]:
        return self.backend.health()

    def installed_models(self) -> list[str]:
        return self.backend.models()

    def models(self) -> list[str]:
        values = self.installed_models()
        for item in [self.settings.fast_model, self.settings.model, self.settings.vision_model, self.settings.code_model, self.settings.deep_model]:
            if item and item not in values:
                values.append(item)
        return values

    def model_details(self) -> list[dict[str, Any]]:
        return self.backend.model_details()

    def model_capabilities(self, model: str) -> list[str]:
        method = getattr(self.backend, "model_capabilities", None)
        if method is None:
            return []
        try:
            return list(method(model) or [])
        except Exception:
            return []

    def warm(self, model: str, keep_alive: str | None = None, num_gpu: int | None = None) -> None:
        self.backend.warm(model, keep_alive=keep_alive, num_gpu=num_gpu)

    def unload(self, model: str) -> None:
        self.backend.unload(model)

    def stream_chat(self, messages: list[dict[str, Any]], model: str | None = None, temperature: float = 0.7, **kwargs: Any) -> Generator[str, None, None]:
        priority = kwargs.pop("priority", self._priority())
        external = kwargs.pop("cancel_event", None) or kwargs.get("stop_event") or self._cancel_event()
        while True:
            with GLOBAL_LLM_ARBITER.acquire(priority) as lease:
                combined = CompositeStop(external, lease.preempt)
                kwargs["stop_event"] = combined
                yielded = False
                for chunk in self.backend.stream_chat(messages, model, temperature, **kwargs):
                    if lease.preempt.is_set() and priority == "background" and not (external and external.is_set()):
                        break
                    yielded = True
                    yield chunk
                if lease.preempt.is_set() and priority == "background" and not (external and external.is_set()):
                    # Streaming background output cannot be safely replayed after partial
                    # emission. Callers use background chat/json for long work; if someone
                    # explicitly streams in background, stop at the preemption boundary.
                    return
                return

    def chat(self, messages: list[dict[str, Any]], model: str | None = None, temperature: float = 0.3, tools: list[dict[str, Any]] | None = None, response_format: dict[str, Any] | str | None = None, **kwargs: Any) -> dict[str, Any]:
        priority = kwargs.pop("priority", self._priority())
        external = kwargs.pop("cancel_event", None) or self._cancel_event()
        timeout_seconds = float(kwargs.pop("timeout_seconds", 0) or 0)
        # Foreground calls keep the existing fast non-streaming path. Background
        # calls use a cancellable stream on Ollama so foreground chat can pre-empt.
        if priority != "background" or not isinstance(self.backend, OllamaBackend):
            with GLOBAL_LLM_ARBITER.acquire(priority):
                return self.backend.chat_once(
                    messages, model, temperature, tools, response_format,
                    timeout_seconds=timeout_seconds or None, **kwargs
                )

        while True:
            if external and external.is_set():
                raise LLMError("Задача остановлена пользователем")
            with GLOBAL_LLM_ARBITER.acquire("background") as lease:
                selected = model or self.backend.default_model
                payload: dict[str, Any] = {
                    "model": selected, "messages": messages, "stream": True,
                    "think": bool(kwargs.get("think", False)),
                    "keep_alive": kwargs.get("keep_alive") or self.settings.keep_alive,
                    "options": {
                        "temperature": temperature,
                        "num_ctx": kwargs.get("num_ctx") or self.settings.task_num_ctx,
                        "num_predict": kwargs.get("num_predict") or self.settings.task_num_predict,
                    },
                }
                if kwargs.get("num_gpu") is not None:
                    payload["options"]["num_gpu"] = int(kwargs.get("num_gpu"))
                if tools: payload["tools"] = tools
                if response_format: payload["format"] = response_format
                started = time.perf_counter(); content = ""; final_message: dict[str, Any] = {}; final_event: dict[str, Any] = {}
                preempted = False
                try:
                    request_timeout = (
                        _request_timeout(timeout_seconds)
                    )
                    with httpx.stream(
                        "POST", f"{self.backend.base_url}/api/chat", json=payload,
                        timeout=request_timeout, trust_env=False
                    ) as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if timeout_seconds and time.perf_counter() - started > timeout_seconds:
                                raise LLMError(f"Локальная модель не закончила ответ за {int(timeout_seconds)} сек.; переключаю способ выполнения")
                            if external and external.is_set():
                                raise LLMError("Задача остановлена пользователем")
                            if lease.preempt.is_set():
                                preempted = True
                                break
                            if not line: continue
                            event = json.loads(line); final_event = event
                            msg = event.get("message") or {}
                            if msg.get("content"): content += str(msg.get("content"))
                            if msg.get("tool_calls"): final_message["tool_calls"] = msg.get("tool_calls")
                    if preempted:
                        continue
                    final_message["content"] = content
                    eval_count = int(final_event.get("eval_count") or 0)
                    eval_duration = float(final_event.get("eval_duration") or 0) / 1_000_000_000
                    self.backend._local.last_metrics = GenerationMetrics(
                        model=selected, total_seconds=round(time.perf_counter()-started,3),
                        load_seconds=round(float(final_event.get("load_duration") or 0)/1_000_000_000,3),
                        prompt_tokens=int(final_event.get("prompt_eval_count") or 0), generated_tokens=eval_count,
                        tokens_per_second=round(eval_count/eval_duration,2) if eval_duration>0 else 0.0,
                        first_token_seconds=round(time.perf_counter()-started,3),
                        prompt_eval_seconds=round(float(final_event.get("prompt_eval_duration") or 0)/1_000_000_000,3),
                        generation_seconds=round(eval_duration,3),
                        thinking_chars=len(str((final_event.get("message") or {}).get("thinking") or "")),
                    )
                    return final_message
                except httpx.HTTPStatusError as exc:
                    detail = _http_error_detail(exc.response)
                    raise LLMError(f"Ollama вернула ошибку {exc.response.status_code}: {detail}") from exc
                except httpx.TimeoutException as exc:
                    raise LLMError(
                        f"Локальная модель не ответила за {int(timeout_seconds) if timeout_seconds else 'допустимое время'} сек."
                    ) from exc
                except httpx.ConnectError as exc:
                    raise LLMError("Не удалось подключиться к Ollama") from exc

    def json(self, messages: list[dict[str, Any]], model: str | None = None, temperature: float = 0.2, schema: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        message = self.chat(messages, model=model, temperature=temperature, response_format=schema or "json", think=False, **kwargs)
        return extract_json(message.get("content", ""))

    def embed(self, text: str, model: str | None = None) -> list[float]:
        with GLOBAL_LLM_ARBITER.acquire("background"):
            return self.backend.embed(text, model)
