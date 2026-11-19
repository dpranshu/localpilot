"""First-run / startup health check: is everything the agent needs actually there?

Returns one clear state with a plain-language message and the fix, so a buyer never
sees silent failures (e.g. the model server simply not running).
"""
from __future__ import annotations

import os
import shutil
import subprocess

import requests

OK, NOT_INSTALLED, NOT_RUNNING, MODEL_MISSING = "ok", "not_installed", "not_running", "model_missing"


def find_ollama() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    for p in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
              r"C:\Program Files\Ollama\ollama.exe"):
        if os.path.exists(p):
            return p
    return None


def check(cfg) -> tuple[str, str]:
    """(state, message). Never raises."""
    try:
        r = requests.get(f"{cfg.ollama_url}/api/tags", timeout=4)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception:
        if find_ollama():
            return NOT_RUNNING, ("The AI engine (Ollama) is installed but not running. "
                                 "Click 'Start AI engine'.")
        return NOT_INSTALLED, ("The AI engine (Ollama) is not installed. Run install.bat "
                               "(or python install.py) once, then restart LocalPilot.")
    want = cfg.vision_model if cfg.backend == "vision" else cfg.model
    base = want.split(":")[0].split("/")[-1].lower()
    if not any(base in n.lower() for n in names):
        return MODEL_MISSING, (f"The model '{want}' is not downloaded yet. Run install.bat "
                               f"(or: ollama pull {want}).")
    return OK, "Ready."


def start_ollama() -> bool:
    """Start 'ollama serve' in the background (no console window)."""
    exe = find_ollama()
    if not exe:
        return False
    try:
        subprocess.Popen([exe, "serve"], creationflags=0x08000000,   # CREATE_NO_WINDOW
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False
