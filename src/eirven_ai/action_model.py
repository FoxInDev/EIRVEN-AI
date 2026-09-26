from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path
from typing import Any


def _policy_path(settings: Any) -> Path:
    return Path(settings.data_dir) / "action_model_policy.json"


def read_action_model_policy(settings: Any, *, model: str = "") -> dict[str, Any]:
    """Return the measured action-model placement policy for this machine.

    An explicit EIRVEN_ACTION_NUM_GPU environment variable always wins.  Otherwise a
    policy produced by the Windows self-check is used only on the same computer and for
    the same action model.  Moving the data directory to another PC therefore falls back
    to Ollama auto placement instead of carrying a stale CPU/GPU decision with it.
    """
    raw = str(os.environ.get("EIRVEN_ACTION_NUM_GPU", "")).strip()
    if raw:
        try:
            value = max(0, int(raw))
            return {"source": "env", "num_gpu": value, "model": model}
        except Exception:
            pass

    path = _policy_path(settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"source": "auto", "num_gpu": None, "model": model}
    if not isinstance(data, dict):
        return {"source": "auto", "num_gpu": None, "model": model}

    policy_model = str(data.get("model") or "").casefold()
    if model and policy_model and policy_model != str(model).casefold():
        return {"source": "auto", "num_gpu": None, "model": model, "ignored": "model_changed"}
    policy_pc = str(data.get("computer_name") or "").casefold()
    current_pc = str(os.environ.get("COMPUTERNAME") or platform.node() or "").casefold()
    if policy_pc and current_pc and policy_pc != current_pc:
        return {"source": "auto", "num_gpu": None, "model": model, "ignored": "computer_changed"}

    value = data.get("num_gpu")
    if value is None:
        return {**data, "source": str(data.get("source") or "selfcheck"), "num_gpu": None}
    try:
        value = max(0, int(value))
    except Exception:
        return {"source": "auto", "num_gpu": None, "model": model, "ignored": "invalid_policy"}
    return {**data, "source": str(data.get("source") or "selfcheck"), "num_gpu": value}


def action_num_gpu(settings: Any, *, model: str = "") -> int | None:
    return read_action_model_policy(settings, model=model).get("num_gpu")


def write_action_model_policy(
    settings_or_data_dir: Any,
    *,
    model: str,
    label: str,
    num_gpu: int | None,
    metrics: dict[str, Any] | None = None,
    source: str = "selfcheck",
) -> Path:
    data_dir = Path(getattr(settings_or_data_dir, "data_dir", settings_or_data_dir))
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "action_model_policy.json"
    payload = {
        "format": "EIRVEN_ACTION_MODEL_POLICY_V1",
        "model": str(model),
        "label": str(label),
        "num_gpu": None if num_gpu is None else max(0, int(num_gpu)),
        "computer_name": str(os.environ.get("COMPUTERNAME") or platform.node() or ""),
        "source": str(source),
        "updated_at": time.time(),
        "metrics": metrics or {},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path
