"""Small Qwen (Ollama) helpers for the research engine. Never raises: returns "" / {} on failure."""
from __future__ import annotations

import json
import re

import requests


def chat(cfg, system: str, user: str, as_json: bool = False, max_tokens: int = 500,
         temperature: float = 0.2, timeout: float | None = None) -> str:
    body = {"model": cfg.model, "stream": False, "keep_alive": getattr(cfg, "keep_alive", "30m"),
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": getattr(cfg, "num_ctx", 8192)},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if as_json:
        body["format"] = "json"
    try:
        r = requests.post(f"{cfg.ollama_url}/api/chat", json=body, timeout=timeout or cfg.request_timeout)
        r.raise_for_status()
        return (r.json().get("message") or {}).get("content", "").strip()
    except Exception:
        return ""


def chat_json(cfg, system: str, user: str, max_tokens: int = 400) -> dict:
    text = chat(cfg, system, user, as_json=True, max_tokens=max_tokens)
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0)) if m else {}
        except Exception:
            return {}
