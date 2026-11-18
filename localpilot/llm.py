"""LLM client: talks to a local Ollama server and returns a parsed action dict.

Uses Ollama's native /api/chat with format=json, which reliably forces the model
to emit a single JSON object (no code fences, no prose).
"""
from __future__ import annotations

import json
import re
import requests

from .prompts import SYSTEM_PROMPT, build_user_message


class LLMError(Exception):
    pass


def _extract_json(text: str) -> dict:
    """Best-effort: parse a JSON object out of the model's reply."""
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # Find the first {...} block.
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    raise LLMError(f"Could not parse JSON from model reply: {text[:300]!r}")


def ping(cfg) -> tuple[bool, str]:
    """Check the Ollama server is up and the model is available."""
    try:
        r = requests.get(f"{cfg.ollama_url}/api/tags", timeout=5)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
        have = any(cfg.model.split(":")[0] in n for n in names)
        return True, (
            f"Ollama up. Models: {names}. "
            + ("Target model present." if have else f"WARNING: '{cfg.model}' not pulled yet.")
        )
    except Exception as e:
        return False, f"Ollama not reachable at {cfg.ollama_url}: {e}"


def decide(cfg, goal: str, title: str, rendered_elements: str, history: list[str]) -> dict:
    user = build_user_message(goal, title, rendered_elements, history)
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "format": "json",
        "stream": False,
        "options": {"temperature": cfg.temperature},
    }
    try:
        r = requests.post(
            f"{cfg.ollama_url}/api/chat", json=payload, timeout=cfg.request_timeout
        )
        r.raise_for_status()
    except requests.RequestException as e:
        raise LLMError(f"Ollama request failed: {e}") from e

    content = r.json().get("message", {}).get("content", "")
    action = _extract_json(content)
    if "action" not in action:
        raise LLMError(f"Model reply missing 'action': {action}")
    return action
