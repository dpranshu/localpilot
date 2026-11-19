"""Installed-app index: open ANY app by name in well under a second.

Windows lists every Start-menu app (desktop .exe *and* Store apps like WhatsApp,
Spotify, Calculator) through `Get-StartApps` with a launchable AppID. We cache that
list in %APPDATA%\\LocalPilot\\apps.json and launch via `shell:AppsFolder\\<AppID>`,
so opening an app never needs the model, a Start-menu search, or typing.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import threading
import time

CACHE = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "LocalPilot", "apps.json")
MAX_AGE = 24 * 3600
_lock = threading.Lock()
_apps: list[dict] | None = None

# what people say -> how the Start menu names it
ALIASES = {"chrome": "google chrome", "edge": "microsoft edge", "word": "word",
           "excel": "excel", "vs code": "visual studio code", "vscode": "visual studio code",
           "cmd": "command prompt", "terminal": "terminal", "store": "microsoft store",
           "explorer": "file explorer", "files": "file explorer", "calc": "calculator",
           "whatsapp": "whatsapp", "photos": "photos", "mail": "outlook"}
# Start-menu entries that are documentation/uninstallers, never what "open X" means
_JUNK = re.compile(r"\b(uninstall|readme|documentation|faqs?|release notes|help|manual|website)\b", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+ ]", " ", (s or "").lower()).split().__str__() and \
        " ".join(re.sub(r"[^a-z0-9+ ]", " ", (s or "").lower()).split())


def _scan() -> list[dict]:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-StartApps | ConvertTo-Json -Compress"],
                             capture_output=True, text=True, timeout=30,
                             creationflags=0x08000000).stdout
        data = json.loads(out or "[]")
        if isinstance(data, dict):
            data = [data]
        return [{"name": d["Name"], "id": d["AppID"]} for d in data
                if d.get("Name") and d.get("AppID")]
    except Exception:
        return []


def _save(apps):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "apps": apps}, f)
    except Exception:
        pass


def refresh() -> list[dict]:
    global _apps
    apps = _scan()
    if apps:
        with _lock:
            _apps = apps
        _save(apps)
    return _apps or []


def all_apps() -> list[dict]:
    """Cached list; loads from disk, scans if missing, refreshes stale caches in background."""
    global _apps
    if _apps is not None:
        return _apps
    try:
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
        _apps = d.get("apps") or None
        if _apps and time.time() - d.get("t", 0) > MAX_AGE:
            threading.Thread(target=refresh, daemon=True).start()
    except Exception:
        _apps = None
    return _apps if _apps else refresh()


def find(name: str, apps: list[dict] | None = None) -> dict | None:
    """Best Start-menu entry for what the user called the app, or None (never a wild guess)."""
    q = _norm(name)
    q = re.sub(r"^(the|my)\s+|\s+(app|application|program)$", "", q).strip()
    if not q:
        return None
    q = ALIASES.get(q, q)
    apps = [a for a in (apps if apps is not None else all_apps()) if not _JUNK.search(a["name"])]
    by = {_norm(a["name"]): a for a in apps}
    if q in by:                                                    # exact
        return by[q]
    starts = sorted((n for n in by if n.startswith(q + " ")), key=len)
    if starts:                                                     # "photoshop" -> no; "brave" -> "brave beta"
        return by[starts[0]]
    words = sorted((n for n in by if q in n.split() or (len(q) > 3 and f" {q}" in f" {n}")), key=len)
    if words:                                                      # "chrome" -> "google chrome"
        return by[words[0]]
    close = difflib.get_close_matches(q, list(by), n=1, cutoff=0.85)  # typos: "calculater"
    return by[close[0]] if close else None


def launch(entry: dict) -> bool:
    try:
        os.startfile("shell:AppsFolder\\" + entry["id"])
        return True
    except Exception:
        return False
