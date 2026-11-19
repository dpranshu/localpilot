"""Remember the user's panel settings between launches.

Stored in %APPDATA%\\LocalPilot\\settings.json. Only user-facing knobs are saved;
anything unknown or the wrong type is ignored, so an old or hand-edited file can
never break startup.
"""
from __future__ import annotations

import json
import os

DEFAULT_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                            "LocalPilot", "settings.json")

# knob -> type it must have
SAVED = {
    "backend": str, "model": str, "max_steps": int, "harness_enabled": bool,
    "use_memory": bool, "confirm_each_step": bool, "dry_run": bool,
    "show_cursor": bool, "show_overlay": bool, "real_cursor": bool,
}
_CHOICES = {"backend": {"uia", "vision", "hybrid"}}


def load_into(cfg, path: str | None = None):
    """Apply saved settings onto cfg (in place). Returns cfg."""
    try:
        with open(path or DEFAULT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return cfg
    if not isinstance(data, dict):
        return cfg
    for k, typ in SAVED.items():
        v = data.get(k)
        if isinstance(v, typ) and not (typ is int and isinstance(v, bool)):
            if k in _CHOICES and v not in _CHOICES[k]:
                continue
            if k == "max_steps" and not 1 <= v <= 200:
                continue
            if k == "model" and not v.strip():
                continue
            setattr(cfg, k, v)
    return cfg


def save_from(cfg, path: str | None = None):
    path = path or DEFAULT_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: getattr(cfg, k) for k in SAVED}, f, indent=1)
        os.replace(tmp, path)
        return True
    except Exception:
        return False
