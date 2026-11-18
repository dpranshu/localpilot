"""One-command setup for LocalPilot.

    python install.py              # deps + Ollama + text model
    python install.py --vision     # also pull the UI-TARS vision model (big)
    python install.py --startup    # also install the auto-start tray shortcut

Steps (each is skipped if already done):
  1. pip install -r requirements.txt
  2. ensure Ollama is installed (winget, else silent download from ollama.com)
  3. ensure the Ollama server is running (localhost:11434)
  4. pull the text model (and UI-TARS with --vision)
  5. verify everything is reachable
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

PROJECT = os.path.dirname(os.path.abspath(__file__))
TEXT_MODEL = "qwen2.5:7b-instruct-q4_K_M"
VISION_MODEL = "hf.co/Mungert/UI-TARS-1.5-7B-GGUF:Q4_K_M"
OLLAMA_URL = "http://localhost:11434"
OLLAMA_SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"

CREATE_NO_WINDOW = 0x08000000


def step(msg):
    print(f"\n==> {msg}")


def ok(msg):
    print(f"   [ok] {msg}")


def warn(msg):
    print(f"   [!] {msg}")


# ---------------- 1. python deps ----------------
def install_deps():
    step("Installing Python dependencies (requirements.txt)")
    req = os.path.join(PROJECT, "requirements.txt")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", req])
    if r.returncode == 0:
        ok("Python dependencies ready")
    else:
        warn("pip install returned non-zero; check output above")
    return r.returncode == 0


# ---------------- 2. ollama install ----------------
def find_ollama():
    exe = shutil.which("ollama")
    if exe:
        return exe
    for p in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
              r"C:\Program Files\Ollama\ollama.exe"):
        if os.path.exists(p):
            return p
    return None


def install_ollama():
    step("Checking for Ollama")
    exe = find_ollama()
    if exe:
        ok(f"Ollama already installed: {exe}")
        return exe
    print("   Ollama not found. Installing...")
    if shutil.which("winget"):
        print("   trying winget...")
        subprocess.run(["winget", "install", "--id", "Ollama.Ollama", "-e",
                        "--accept-source-agreements", "--accept-package-agreements",
                        "--disable-interactivity"])
        exe = find_ollama()
        if exe:
            ok(f"Installed via winget: {exe}")
            return exe
    # fallback: download the installer and run it silently
    print(f"   downloading {OLLAMA_SETUP_URL} ...")
    dst = os.path.join(os.environ.get("TEMP", PROJECT), "OllamaSetup.exe")
    try:
        urllib.request.urlretrieve(OLLAMA_SETUP_URL, dst)
        print("   running installer (silent)...")
        subprocess.run([dst, "/VERYSILENT", "/NORESTART"])
    except Exception as e:
        warn(f"download/install failed: {e}")
    exe = find_ollama()
    if exe:
        ok(f"Installed: {exe}")
    else:
        warn("Could not install Ollama automatically. Install it from https://ollama.com/download")
    return exe


# ---------------- 3. server ----------------
def server_up():
    try:
        urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=3)
        return True
    except Exception:
        return False


def start_server(ollama):
    step("Starting the Ollama server")
    if server_up():
        ok("Server already running")
        return True
    if not ollama:
        warn("No ollama binary to start")
        return False
    try:
        subprocess.Popen([ollama, "serve"], creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        warn(f"could not launch 'ollama serve': {e}")
    for _ in range(30):
        if server_up():
            ok("Server is up")
            return True
        time.sleep(1)
    warn("Server did not come up in time")
    return False


# ---------------- 4. models ----------------
def have_model(tag):
    try:
        d = json.load(urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=5))
        names = [m.get("name", "") for m in d.get("models", [])]
        base = tag.split(":")[0].split("/")[-1].lower()
        return any(base in n.lower() for n in names)
    except Exception:
        return False


def pull(ollama, tag):
    if have_model(tag):
        ok(f"Model already present: {tag}")
        return True
    print(f"   pulling {tag}  (this can take a while)...")
    r = subprocess.run([ollama, "pull", tag])
    if r.returncode == 0:
        ok(f"Pulled {tag}")
        return True
    warn(f"Failed to pull {tag}")
    return False


# ---------------- 5. verify ----------------
def verify(want_vision):
    step("Verifying")
    up = server_up()
    print(f"   server: {'up' if up else 'DOWN'}")
    print(f"   text model  ({TEXT_MODEL}): {'present' if have_model(TEXT_MODEL) else 'MISSING'}")
    if want_vision:
        print(f"   vision model: {'present' if have_model(VISION_MODEL) else 'MISSING'}")
    return up and have_model(TEXT_MODEL)


def main():
    want_vision = "--vision" in sys.argv
    want_startup = "--startup" in sys.argv
    print("=" * 60)
    print(" LocalPilot setup")
    print("=" * 60)

    install_deps()
    ollama = install_ollama()
    start_server(ollama)
    if ollama:
        step("Pulling model(s)")
        pull(ollama, TEXT_MODEL)
        if want_vision:
            pull(ollama, VISION_MODEL)

    if want_startup:
        step("Installing auto-start tray shortcut")
        try:
            import install_startup
            print("  ", install_startup.install())
        except Exception as e:
            warn(f"startup shortcut failed: {e}")

    good = verify(want_vision)
    print("\n" + "=" * 60)
    if good:
        print(" DONE. Launch the app:")
        print("   python localpilot_app.py        (desktop control panel)")
        print("   python -m localpilot.tray       (system tray)")
        print("   python run.py -i                (interactive CLI)")
    else:
        print(" Setup incomplete -- see warnings above.")
        print(" Ensure Ollama is installed/running, then re-run: python install.py")
    print("=" * 60)
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
