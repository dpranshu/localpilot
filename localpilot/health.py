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
        return NOT_INSTALLED, ("The AI engine (Ollama) is not installed. Click "
                               "'Install AI engine'.")
    want = cfg.vision_model if cfg.backend == "vision" else cfg.model
    base = want.split(":")[0].split("/")[-1].lower()
    if not any(base in n.lower() for n in names):
        return MODEL_MISSING, (f"The model '{want}' is not downloaded yet. Click "
                               f"'Download model' (about 5 GB, one time).")
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


OLLAMA_SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"


def install_ollama(progress=lambda msg: None) -> bool:
    """Install Ollama for the current user: winget first, then the official installer."""
    if find_ollama():
        return True
    if shutil.which("winget"):
        progress("Installing the AI engine with winget...")
        try:
            subprocess.run(["winget", "install", "--id", "Ollama.Ollama", "-e", "--silent",
                            "--accept-package-agreements", "--accept-source-agreements"],
                           creationflags=0x08000000, timeout=900,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        if find_ollama():
            return True
    import tempfile
    dest = os.path.join(tempfile.mkdtemp(prefix="localpilot_"), "OllamaSetup.exe")
    try:
        with requests.get(OLLAMA_SETUP_URL, stream=True, timeout=30) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        progress(f"Downloading the AI engine... {done * 100 // total}%")
        progress("Installing the AI engine...")
        subprocess.run([dest, "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], timeout=900)
    except Exception:
        return False
    return bool(find_ollama())


def pull_model(cfg, model: str | None = None, progress=lambda msg: None) -> bool:
    """Download a model through the running Ollama server, reporting % progress."""
    import json
    want = model or (cfg.vision_model if cfg.backend == "vision" else cfg.model)
    try:
        with requests.post(f"{cfg.ollama_url}/api/pull", json={"name": want, "stream": True},
                           stream=True, timeout=(10, 600)) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                d = json.loads(line)
                if d.get("error"):
                    progress(f"Download failed: {d['error']}")
                    return False
                tot, got = d.get("total"), d.get("completed")
                if tot and got:
                    progress(f"Downloading model... {got * 100 // tot}% "
                             f"({got / 1e9:.1f} of {tot / 1e9:.1f} GB)")
                elif d.get("status"):
                    progress(f"Model: {d['status']}")
                if d.get("status") == "success":
                    return True
    except Exception as e:
        progress(f"Download failed: {e}")
        return False
    return False
