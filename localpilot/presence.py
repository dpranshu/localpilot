"""Presence orb: LocalPilot's always-visible face while it runs -- window open or in the background.

A small dotted sphere (the same DotSphere as the app) in its own per-pixel-alpha, always-on-top,
never-focus-stealing window, bottom-right by default:

  idle / ready     -> slow spin            (LocalPilot is running and listening)
  thinking         -> faster + breathing   (understanding, planning, researching)
  acting           -> fast + ripple        (operating the PC / browsing)
  done             -> calm                 (just finished)
  stopped / paused -> frozen, dimmed       (paused task, or the brain isn't reachable)

  click            -> open LocalPilot      drag -> move (remembered)
  right-click      -> Open / Pause or Continue / Stop / Hide orb / Quit LocalPilot

While a task's own HUD orb is on screen (hud.HudOverlay), this orb hides so there is only one face.
Runs on its own thread; every public method is thread-safe.
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from ctypes import wintypes

from .dotsphere import DotSphere
from .hud import (_Layered, user32, WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST,
                  WM_MOUSEACTIVATE, MA_NOACTIVATE, WM_SETCURSOR, WM_LBUTTONDOWN, WM_MOUSEMOVE, MK_LBUTTON,
                  WM_LBUTTONUP, WM_CAPTURECHANGED, WM_RBUTTONUP, WM_NULL, MF_STRING, MF_GRAYED,
                  MF_SEPARATOR, TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD, PM_REMOVE)

IDC_HAND = 32649
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
POS_FILE = os.path.join(os.environ.get("APPDATA", "."), "LocalPilot", "presence_pos.json")
STATES = ("idle", "thinking", "acting", "done", "stopped")


class PresenceOrb:
    def __init__(self, diameter: int = 110, on_open=None, on_pause=None, on_resume=None, on_stop=None,
                 on_quit=None, pos_file: str = POS_FILE):
        self.diameter = diameter
        self.cb = {"open": on_open, "pause": on_pause, "resume": on_resume, "stop": on_stop, "quit": on_quit}
        self.pos_file = pos_file
        self.state = "idle"
        self.tip = "LocalPilot"
        self.busy = False
        self.paused = False
        self.hidden_by_user = False
        self.ok = False
        self.frames = 0
        self.visible = False
        self.menu_log: list[int] = []
        self._closed = False
        self._drag = None
        self._moved = False
        self._ready = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True, name="LocalPilot-Presence")

    # ---------------- public (thread-safe) ----------------
    def start(self):
        self._t.start()
        self._ready.wait(5)
        return self

    def set_state(self, state: str, busy: bool | None = None, paused: bool | None = None):
        if state in STATES:
            self.state = state
        if busy is not None:
            self.busy = busy
        if paused is not None:
            self.paused = paused

    def show(self):
        self.hidden_by_user = False

    def hide(self):
        self.hidden_by_user = True

    def close(self):
        self._closed = True
        self._t.join(3)

    # ---------------- geometry ----------------
    def _clamp(self, x, y):
        W, H = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        S = self.sphere.S
        return max(0, min(W - S, int(x))), max(0, min(H - S, int(y)))

    def _default_pos(self):
        W, H = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        S = self.sphere.S
        return self._clamp(W - S - 18, H - S - 64)

    def _load_pos(self):
        try:
            with open(self.pos_file, encoding="utf-8") as f:
                d = json.load(f)
            return self._clamp(d["x"], d["y"])
        except Exception:
            return self._default_pos()

    def _save_pos(self):
        try:
            os.makedirs(os.path.dirname(self.pos_file), exist_ok=True)
            l, t, _, _ = self.win.rect()
            with open(self.pos_file, "w", encoding="utf-8") as f:
                json.dump({"x": l, "y": t}, f)
        except Exception:
            pass

    # ---------------- window procedure ----------------
    def _proc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_SETCURSOR:
                user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND)))
                return 1
            if msg == WM_LBUTTONDOWN:
                pt = wintypes.POINT(); user32.GetCursorPos(ctypes.byref(pt))
                l, t, _, _ = self.win.rect()
                self._drag, self._moved = (pt.x - l, pt.y - t, pt.x, pt.y), False
                user32.SetCapture(hwnd)
                return 0
            if msg == WM_MOUSEMOVE and self._drag and (wp & MK_LBUTTON):
                pt = wintypes.POINT(); user32.GetCursorPos(ctypes.byref(pt))
                if abs(pt.x - self._drag[2]) + abs(pt.y - self._drag[3]) > 4:
                    self._moved = True
                if self._moved:
                    self.win.move(*self._clamp(pt.x - self._drag[0], pt.y - self._drag[1]))
                return 0
            if msg in (WM_LBUTTONUP, WM_CAPTURECHANGED):
                if self._drag:
                    moved, self._drag = self._moved, None
                    user32.ReleaseCapture()
                    if moved:
                        self._save_pos()
                    elif msg == WM_LBUTTONUP:
                        self._call("open")                     # a click (not a drag) opens the app
                return 0
            if msg == WM_RBUTTONUP:
                self._menu(hwnd)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _call(self, name):
        fn = self.cb.get(name)
        if fn:
            threading.Thread(target=fn, daemon=True).start()

    def _menu(self, hwnd):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, 1, "Open LocalPilot")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        if self.paused:
            user32.AppendMenuW(menu, MF_STRING, 3, "Continue task")
        else:
            user32.AppendMenuW(menu, MF_STRING | (0 if self.busy else MF_GRAYED), 2, "Pause task")
        user32.AppendMenuW(menu, MF_STRING | (0 if self.busy else MF_GRAYED), 4, "Stop task")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, 5, "Hide orb (until LocalPilot opens)")
        user32.AppendMenuW(menu, MF_STRING, 6, "Reset position")
        user32.AppendMenuW(menu, MF_STRING, 7, "Quit LocalPilot")
        pt = wintypes.POINT(); user32.GetCursorPos(ctypes.byref(pt))
        prev = user32.GetForegroundWindow()
        user32.SetForegroundWindow(hwnd)
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if prev and prev != hwnd:
            user32.SetForegroundWindow(prev)
        self.menu_log.append(cmd)
        self._command(cmd)

    def _command(self, cmd: int):
        if cmd == 1:
            self._call("open")
        elif cmd == 2:
            self._call("pause")
        elif cmd == 3:
            self._call("resume")
        elif cmd == 4:
            self._call("stop")
        elif cmd == 5:
            self.hide()
        elif cmd == 6:
            self.win.move(*self._default_pos()); self._save_pos()
        elif cmd == 7:
            self._call("quit")

    # ---------------- loop ----------------
    def _should_show(self) -> bool:
        if self.hidden_by_user:
            return False
        try:
            from .hud import HudOverlay
            if getattr(HudOverlay, "active", 0) > 0:     # a task's HUD orb is the face right now
                return False
        except Exception:
            pass
        return True

    def _run(self):
        try:
            # the ORIGINAL fine halftone look (36 dot rows, as on the task HUD) -- at small sizes
            # DotSphere would otherwise switch to a few coarse dots
            self.sphere = DotSphere(diameter=self.diameter, rows=36)
            x, y = self._load_pos()
            S = self.sphere.S
            self.win = _Layered(S, S, WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
                                self._proc, x, y, title="LocalPilotPresence")
            self.ok = True
            self.visible = True
        except Exception:
            self.ok = False
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        last_top = 0.0
        last_img = None
        try:
            while not self._closed:
                t0 = time.monotonic()
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                show = self._should_show()
                if show != self.visible:
                    user32.ShowWindow(self.win.hwnd, SW_SHOWNOACTIVATE if show else SW_HIDE)
                    self.visible = show
                if show:
                    st = "stopped" if self.paused else self.state
                    img = self.sphere.render(st, now=t0)
                    if img is not last_img:
                        self.win.present(img)
                        last_img = img
                        self.frames += 1
                    if t0 - last_top > 1.5:
                        self.win.raise_topmost(); last_top = t0
                # ~25 fps while working, ~12 fps when idle (cheap: it's a tiny image)
                busy = self.state in ("thinking", "acting")
                time.sleep(max(0.005, (0.04 if busy else 0.08) - (time.monotonic() - t0)))
        finally:
            try:
                self.win.destroy()
            except Exception:
                pass
