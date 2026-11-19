"""System-tray launcher for LocalPilot.

A little icon by the clock. Click it to:
  - Run task...   (type a task; it runs with a 3s countdown so you can focus the target)
  - pick the backend (uia / vision / hybrid)
  - toggle confirm / cursor / overlay
  - probe (see what the agent perceives) / check the model
  - quit

Launch:  python -m localpilot.tray     (or double-click start_tray.bat)

The agent runs on a background thread so the tray stays responsive; only one task
runs at a time. Balloon notifications report start/finish.
"""
from __future__ import annotations

import threading

from PIL import Image, ImageDraw

from .config import Config
from .agent import UnfinishedRun, run as run_agent
from . import llm

cfg = Config()
_run_lock = threading.Lock()
_icon = None


# ---------------- tray icon image ----------------
def _make_image(accent=(53, 208, 127)) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # glowing ring + head
    d.ellipse((6, 6, 58, 58), outline=accent, width=3)
    d.ellipse((16, 16, 48, 48), fill=(27, 27, 34, 255), outline=accent, width=2)
    # eyes
    d.ellipse((24, 27, 30, 33), fill=(255, 255, 255, 255))
    d.ellipse((34, 27, 40, 33), fill=(255, 255, 255, 255))
    # smile
    d.arc((24, 30, 40, 42), start=20, end=160, fill=(255, 255, 255, 255), width=2)
    # antenna
    d.line((32, 16, 32, 8), fill=accent, width=2)
    d.ellipse((29, 3, 35, 9), fill=accent)
    return img


# ---------------- actions ----------------
def _notify(title, msg):
    try:
        if _icon is not None:
            _icon.notify(msg[:200], title)
    except Exception:
        pass


def _ask_task() -> str | None:
    """Small top-most input box for the task (runs on the tray/main thread)."""
    try:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        goal = simpledialog.askstring("LocalPilot", "What should the agent do?", parent=root)
        root.destroy()
        return goal.strip() if goal else None
    except Exception as e:
        _notify("LocalPilot", f"input error: {e}")
        return None


def _run_task(goal: str):
    def worker():
        if not _run_lock.acquire(blocking=False):
            _notify("LocalPilot", "A task is already running.")
            return
        try:
            _notify("LocalPilot", f"Running ({cfg.backend}): {goal[:60]}\nFocus your target window now.")
            final = run_agent(goal, cfg)
            status = "stopped" if isinstance(final, UnfinishedRun) else "done"
            _notify(f"LocalPilot - {status}", final[:160])
        except Exception as e:
            _notify("LocalPilot - error", str(e)[:160])
        finally:
            _run_lock.release()
    threading.Thread(target=worker, daemon=True).start()


def _on_run(icon, item):
    goal = _ask_task()
    if goal:
        _run_task(goal)


def _set_backend(name):
    def fn(icon, item):
        cfg.backend = name
        _notify("LocalPilot", f"Backend: {name}")
    return fn


def _toggle(attr):
    def fn(icon, item):
        setattr(cfg, attr, not getattr(cfg, attr))
    return fn


def _on_probe(icon, item):
    def worker():
        if not _run_lock.acquire(blocking=False):
            _notify("LocalPilot", "Busy."); return
        try:
            import time
            from .perception import perceive
            _notify("LocalPilot", "Probing in 3s - focus a window...")
            time.sleep(3)
            title, elements, _ = perceive(cfg)
            _notify("LocalPilot probe", f"{title}: {len(elements)} controls")
        finally:
            _run_lock.release()
    threading.Thread(target=worker, daemon=True).start()


def _on_check(icon, item):
    ok, msg = llm.ping(cfg)
    _notify("LocalPilot", msg)


def _on_panel(icon, item):
    """Open the desktop control panel as a separate process (own Tk loop)."""
    import os
    import subprocess
    import sys
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]                      # LocalPilot.exe opens the panel
    else:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        cmd = [pyw if os.path.exists(pyw) else sys.executable, "-m", "localpilot.gui"]
    try:
        subprocess.Popen(cmd,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    except Exception as e:
        _notify("LocalPilot", f"panel error: {e}")


def _on_quit(icon, item):
    icon.visible = False
    icon.stop()


# ---------------- build + run ----------------
def build_icon():
    from pystray import Icon, Menu, MenuItem

    backend_menu = Menu(
        MenuItem("uia (fast text)", _set_backend("uia"),
                 checked=lambda i: cfg.backend == "uia", radio=True),
        MenuItem("vision (UI-TARS)", _set_backend("vision"),
                 checked=lambda i: cfg.backend == "vision", radio=True),
        MenuItem("hybrid (auto-switch)", _set_backend("hybrid"),
                 checked=lambda i: cfg.backend == "hybrid", radio=True),
    )
    menu = Menu(
        MenuItem("LocalPilot", None, enabled=False),
        Menu.SEPARATOR,
        MenuItem("Run task…", _on_run, default=True),
        MenuItem("Open control panel…", _on_panel),
        MenuItem("Backend", backend_menu),
        Menu.SEPARATOR,
        MenuItem("Confirm each step", _toggle("confirm_each_step"),
                 checked=lambda i: cfg.confirm_each_step),
        MenuItem("Show agent cursor", _toggle("show_cursor"),
                 checked=lambda i: cfg.show_cursor),
        MenuItem("Show overlay", _toggle("show_overlay"),
                 checked=lambda i: cfg.show_overlay),
        Menu.SEPARATOR,
        MenuItem("Probe (what I see)", _on_probe),
        MenuItem("Check model", _on_check),
        Menu.SEPARATOR,
        MenuItem("Quit", _on_quit),
    )
    return Icon("LocalPilot", _make_image(), "LocalPilot", menu)


def main():
    global _icon
    _icon = build_icon()
    _icon.run()


if __name__ == "__main__":
    main()
