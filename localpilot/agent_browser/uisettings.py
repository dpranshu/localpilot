"""Persisted Agent Browser settings (%APPDATA%/LocalPilot/agent_browser.json), validated on load."""
from __future__ import annotations

import json
import os

from .ui import SETTINGS_DEFAULTS

PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "LocalPilot", "agent_browser.json")
_CHOICES = {"theme": {"system", "dark", "light"}, "profile": {"eco", "balanced", "performance"},
            "research_mode": {"quick", "research", "deep"}}
_RANGES = {"max_tabs": (1, 3), "memory_mb": (500, 2500), "preview_fps": (0.5, 2.0), "max_pages": (3, 40),
           "task_timeout_s": (30, 900)}


def load(path: str = PATH) -> dict:
    out = dict(SETTINGS_DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return out
    if not isinstance(data, dict):
        return out
    for k, d in SETTINGS_DEFAULTS.items():
        v = data.get(k)
        if v is None or (isinstance(d, bool) != isinstance(v, bool)):
            continue
        if k in _CHOICES and v not in _CHOICES[k]:
            continue
        if k in _RANGES:
            lo, hi = _RANGES[k]
            if not isinstance(v, (int, float)) or not lo <= v <= hi:
                continue
        out[k] = v
    return out


def save(values: dict, path: str = PATH) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: values.get(k, d) for k, d in SETTINGS_DEFAULTS.items()}, f, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        return False
