"""True-transparency, interactive HUD (per-pixel alpha layered windows).

Two layered windows share one render thread:

  1. MAIN HUD  - full-screen, **click-through** (WS_EX_TRANSPARENT). Draws the status
     line, outlined speech text, the highlight box around the target control, the dashed
     pointer line and the agent's own cursor. Clicks (including the agent's own) pass
     straight through it.

  2. ORB window - small, holds the gradient orb. Layered windows are hit-tested
     by alpha, so fully transparent pixels let the mouse through.
       - drag it anywhere (position is remembered between runs)
       - right-click -> Stop task / Open LocalPilot app / Reset position
     It never takes focus (WS_EX_NOACTIVATE + MA_NOACTIVATE), so interacting with it does
     not disturb the window the agent is working in.

Both are rendered with PIL as RGBA and pushed with UpdateLayeredWindow (premultiplied
BGRA): genuinely transparent backgrounds, no black box. Titles carry a sentinel so
perception ignores them.

Public API (unchanged): think / act / done / stopped / status / highlight / clear /
cursor_move / click_burst / scroll_hint / close / ok.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import sys
import textwrap
import threading
import time
from ctypes import wintypes

from PIL import Image, ImageDraw, ImageFont

from .dotsphere import DotSphere

SENTINEL = "LocalPilotOverlay"

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0010
ULW_ALPHA = 2
AC_SRC_OVER, AC_SRC_ALPHA = 0, 1
PM_REMOVE = 1
# window messages
WM_NULL, WM_SETCURSOR, WM_MOUSEACTIVATE = 0x0000, 0x0020, 0x0021
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONUP = 0x0200, 0x0201, 0x0202, 0x0205
WM_CAPTURECHANGED = 0x0215
MA_NOACTIVATE = 3
MK_LBUTTON = 0x0001
IDC_SIZEALL = 32646
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0000, 0x0001, 0x0800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x0002, 0x0080, 0x0100

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


# --- ctypes signatures (64-bit safe) ---
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                                       ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
                                       ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetCapture.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.SetCursor.argtypes = [wintypes.HANDLE]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def _font(size, bold=False):
    for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


# ---------------------------------------------------------------- layered window
class _Layered:
    """A per-pixel-alpha layered window backed by a DIB section."""

    _n = 0

    def __init__(self, w, h, ex_style, proc, x=0, y=0, title=SENTINEL):
        _Layered._n += 1
        self.w, self.h = w, h
        self._proc = WNDPROC(proc)                       # keep a reference (no GC)
        self._cls = f"LocalPilotHud{os.getpid()}_{_Layered._n}"
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = self._cls
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise OSError("RegisterClassW failed")
        self._hinst = hinst
        self.hwnd = user32.CreateWindowExW(ex_style, self._cls, title, WS_POPUP, x, y, w, h,
                                           None, None, hinst, None)
        if not self.hwnd:
            raise OSError("CreateWindowExW failed")
        self._screen_dc = user32.GetDC(None)
        self._mem_dc = gdi32.CreateCompatibleDC(self._screen_dc)
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = w, -h                # top-down
        bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
        self._bits = ctypes.c_void_p()
        self._hbmp = gdi32.CreateDIBSection(self._screen_dc, ctypes.byref(bmi), 0,
                                            ctypes.byref(self._bits), None, 0)
        if not self._hbmp:
            raise OSError("CreateDIBSection failed")
        self._old = gdi32.SelectObject(self._mem_dc, self._hbmp)
        self._blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        self._pt_src = wintypes.POINT(0, 0)
        self._size = wintypes.SIZE(w, h)
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        self.raise_topmost()

    def raise_topmost(self):
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def move(self, x, y):
        user32.SetWindowPos(self.hwnd, None, int(x), int(y), 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def rect(self):
        r = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    def present(self, img: Image.Image):
        """img: RGBA straight alpha, size (w, h) -> premultiplied BGRA -> window.
        Position is left unchanged (pptDst = NULL) so dragging never snaps back."""
        if img.size != (self.w, self.h):
            raise ValueError("Layered window image must match the window size")
        raw = img.convert("RGBa").tobytes("raw", "BGRa")
        ctypes.memmove(self._bits, raw, len(raw))
        user32.UpdateLayeredWindow(self.hwnd, self._screen_dc, None, ctypes.byref(self._size),
                                   self._mem_dc, ctypes.byref(self._pt_src), 0,
                                   ctypes.byref(self._blend), ULW_ALPHA)

    def destroy(self):
        try:
            gdi32.SelectObject(self._mem_dc, self._old)
            gdi32.DeleteObject(self._hbmp)
            gdi32.DeleteDC(self._mem_dc)
            user32.ReleaseDC(None, self._screen_dc)
            user32.DestroyWindow(self.hwnd)
            user32.UnregisterClassW(self._cls, self._hinst)
        except Exception:
            pass


def _hex(c, a=255):
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), a)


# accent per state (drives highlight box / pointer line / cursor colour)
ACCENT = {"idle": "#cfd8ff", "thinking": "#7fb8ff", "acting": "#5be3ff",
          "done": "#7dffb0", "stopped": "#aab4c0"}

_POS_FILE = os.path.join(os.environ.get("APPDATA", "."), "LocalPilot", "orb_pos.json")


class HudOverlay:
    """Per-pixel-alpha HUD with a draggable orb and a right-click menu."""

    def __init__(self, cfg=None):
        self._cfg = cfg
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._ready = threading.Event()
        self._ok = False
        self._closed = False
        self._state = "idle"
        self._step = ""
        self._say = ""
        self._label = ""
        self._rect = None
        self._frame = 0
        self._cur_target = None
        self._cur_pos = None
        self._click_f = -999
        self._scroll_f = -999
        self._scroll_dir = "down"
        self._drag = None
        self._blank_sent = False
        self._main_key = None
        self.last_frame_ms = 0.0
        self.menu_log = []                                # for tests / debugging
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(6)

    # ---------------- public API (thread-safe) ----------------
    @property
    def ok(self) -> bool:
        return self._ok

    def think(self, step: str, title: str = ""):
        self._q.put(("state", "thinking", {"step": step, "say": title}))

    def act(self, rect, label: str, say: str):
        self._q.put(("state", "acting", {"rect": rect, "label": label, "say": say}))

    def done(self, text: str):
        self._q.put(("state", "done", {"say": text}))

    def stopped(self, text: str = "stopped"):
        self._q.put(("state", "stopped", {"say": text}))

    def status(self, text: str):
        self._q.put(("state", "thinking", {"step": text, "say": ""}))

    def highlight(self, rect, label: str):
        self._q.put(("state", "acting", {"rect": rect, "label": label, "say": label}))

    def clear(self):
        self._q.put(("clearhl",))

    def cursor_move(self, x: int, y: int):
        self._q.put(("cursor", int(x), int(y)))

    def click_burst(self):
        self._q.put(("click",))

    def scroll_hint(self, direction: str = "down"):
        self._q.put(("scrollhint", direction))

    def close(self):
        self._q.put(("close",))

    # ---------------- geometry ----------------
    def _center(self):
        l, t, r, b = self._orb.rect()
        return (l + r) / 2, (t + b) / 2 + self.sphere.lift

    def _default_pos(self):
        S = self.sphere.size
        return self._clamp(self.W - S - 24, self.H - S - 64)

    def _clamp(self, x, y):
        S = self.sphere.size
        return max(0, min(self.W - S, x)), max(0, min(self.H - S, y))

    def _load_pos(self):
        try:
            with open(_POS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return self._clamp(int(d["x"]), int(d["y"]))
        except Exception:
            return self._default_pos()

    def _save_pos(self):
        try:
            os.makedirs(os.path.dirname(_POS_FILE), exist_ok=True)
            l, t, _, _ = self._orb.rect()
            with open(_POS_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": l, "y": t}, f)
        except Exception:
            pass

    # ---------------- setup / teardown ----------------
    def _setup(self):
        self.W = user32.GetSystemMetrics(0)
        self.H = user32.GetSystemMetrics(1)
        size = getattr(self._cfg, "orb_size", 150) or 150
        size = max(16, min(int(size), min(self.W, self.H) - 32))
        self.sphere = DotSphere(diameter=size)
        ex_main = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE |
                   WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
        ex_orb = WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
        self._main = _Layered(self.W, self.H, ex_main,
                              lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
        x, y = self._load_pos()
        S = self.sphere.size
        self._orb = _Layered(S, S, ex_orb, self._orb_proc, x, y)
        self.f_txt = _font(15)
        self.f_bold = _font(14, bold=True)
        self.f_small = _font(12, bold=True)

    def _teardown(self):
        for w in (getattr(self, "_orb", None), getattr(self, "_main", None)):
            if w is not None:
                w.destroy()

    # ---------------- orb window: mouse handling ----------------
    def _orb_proc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE                      # never steal focus
            if msg == WM_SETCURSOR:
                user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(IDC_SIZEALL)))
                return 1
            if msg == WM_LBUTTONDOWN:
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                l, t, _, _ = self._orb.rect()
                self._drag = (pt.x - l, pt.y - t)
                user32.SetCapture(hwnd)
                return 0
            if msg == WM_MOUSEMOVE and self._drag and (wp & MK_LBUTTON):
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                x, y = self._clamp(pt.x - self._drag[0], pt.y - self._drag[1])
                self._orb.move(x, y)
                return 0
            if msg in (WM_LBUTTONUP, WM_CAPTURECHANGED):
                if self._drag:
                    self._drag = None
                    user32.ReleaseCapture()
                    self._save_pos()
                return 0
            if msg == WM_RBUTTONUP:
                self._show_menu(hwnd)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _show_menu(self, hwnd):
        running = self._state in ("thinking", "acting")
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING | (0 if running else MF_GRAYED), 1, "Stop task")
        user32.AppendMenuW(menu, MF_STRING, 2, "Open LocalPilot app")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, 3, "Reset position")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        prev = user32.GetForegroundWindow()
        user32.SetForegroundWindow(hwnd)                  # needed so the menu dismisses properly
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        # hand the foreground back to the window the agent is working in
        if prev and prev != hwnd and prev != self._main.hwnd:
            user32.SetForegroundWindow(prev)
        self.menu_log.append(cmd)
        if cmd == 1:
            self._do_stop()
        elif cmd == 2:
            self._do_open_app()
        elif cmd == 3:
            x, y = self._default_pos()
            self._orb.move(x, y)
            self._save_pos()

    def _do_stop(self):
        ev = getattr(self._cfg, "stop_event", None)
        if ev is not None:
            ev.set()
        self._step = "stopping after this step..."

    def _do_open_app(self):
        def go():
            cb = getattr(self._cfg, "open_app_callback", None)
            if cb:
                try:
                    cb()
                    return
                except Exception:
                    pass
            import subprocess
            try:
                if getattr(sys, "frozen", False):
                    subprocess.Popen([sys.executable])
                else:
                    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                    exe = pyw if os.path.exists(pyw) else sys.executable
                    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    subprocess.Popen([exe, "-m", "localpilot.gui"], cwd=root)
            except Exception:
                pass
        threading.Thread(target=go, daemon=True).start()

    # ---------------- main loop ----------------
    def _run(self):
        try:
            self._setup()
            self._ok = True
        except Exception:
            self._ok = False
            self._ready.set()
            self._teardown()
            return
        self._ready.set()
        msg = wintypes.MSG()
        last_top = time.monotonic()
        last_image = None
        try:
            while not self._closed:
                t0 = time.monotonic()
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                self._drain()
                if self._closed:
                    break
                self._frame += 1
                try:
                    image = self.sphere.render(self._state, now=t0)
                    if image is not last_image:
                        self._orb.present(image)
                        last_image = image
                    self._present_main()
                except Exception:
                    pass
                if t0 - last_top > 1.0:                   # stay above other topmost windows
                    self._main.raise_topmost()
                    self._orb.raise_topmost()
                    last_top = t0
                dt = time.monotonic() - t0
                self.last_frame_ms = dt * 1000
                time.sleep(max(0.004, 0.040 - dt))
        finally:
            self._teardown()

    def _drain(self):
        try:
            while True:
                cmd = self._q.get_nowait()
                k = cmd[0]
                if k == "close":
                    self._closed = True
                elif k == "clearhl":
                    self._rect = None
                elif k == "cursor":
                    self._cur_target = (cmd[1], cmd[2])
                    if self._cur_pos is None:
                        self._cur_pos = self._center()
                elif k == "click":
                    self._click_f = self._frame
                elif k == "scrollhint":
                    self._scroll_f, self._scroll_dir = self._frame, cmd[1]
                elif k == "state":
                    self._state = cmd[1]
                    d = cmd[2]
                    if "step" in d:
                        self._step = d["step"]
                    self._rect = d.get("rect")
                    self._label = d.get("label", "")
                    self._say = d.get("say", "")
                    if self._state in ("done", "stopped"):
                        self._step = ""
                        self._cur_pos = self._cur_target = None
        except queue.Empty:
            pass

    # ---------------- main HUD drawing ----------------
    def _has_content(self) -> bool:
        return bool(self._step or self._say or self._rect or self._cur_pos)

    def _present_main(self):
        if not self._has_content():
            if not self._blank_sent:                       # nothing to draw: send one blank frame
                self._main.present(Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0)))
                self._blank_sent = True
                self._main_key = None
            return
        self._blank_sent = False
        animated = (self._state == "acting" and self._rect) or self._cur_pos
        key = (self._state, self._step, self._say, self._center() if self._say else None)
        if animated or key != self._main_key:
            self._main.present(self._compose())
            self._main_key = None if animated else key

    def _text(self, d, xy, s, fill=(255, 255, 255, 255), font=None, anchor="la"):
        d.text(xy, s, font=font or self.f_txt, fill=fill, anchor=anchor,
               stroke_width=2, stroke_fill=(0, 0, 0, 215))

    def _compose(self) -> Image.Image:
        W, H, f = self.W, self.H, self._frame
        state = self._state
        accent = _hex(ACCENT.get(state, ACCENT["idle"]))
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cx, cy = self._center()
        half = self.sphere.size / 2

        if state == "acting" and self._rect:               # highlight box + pointer line
            l, t, r, b = self._rect
            breathe = 2 + (math.sin(f * 0.3) + 1) * 1.8
            d.rounded_rectangle((l - 4, t - 4, r + 4, b + 4), radius=8,
                                outline=accent[:3] + (70,), width=int(breathe) + 4)
            d.rounded_rectangle((l, t, r, b), radius=6, outline=accent, width=3)
            if self._label:
                ly = t - 22 if t > 30 else b + 6
                self._text(d, (l, ly), self._label, font=self.f_small)
            self._dashed(d, (cx, cy), ((l + r) / 2, (t + b) / 2), accent)

        if self._step:                                     # status line (top-left)
            d.ellipse((16, 17, 26, 27), fill=accent)
            self._text(d, (34, 13), self._step, font=self.f_bold)

        say = self._say[:90]                               # outlined speech text, no box
        if say and state in ("thinking", "acting", "done", "stopped"):
            lines = textwrap.wrap(say, 38)[:3]
            left_side = cx > 380
            x = cx - half - 8 if left_side else cx + half + 8
            y = cy - 10 - (len(lines) - 1) * 10
            for ln in lines:
                self._text(d, (x, y), ln, anchor="ra" if left_side else "la")
                y += 20

        self._cursor(d, accent, f)
        return img

    def _dashed(self, d, p0, p1, color, dash=9, gap=7):
        x0, y0 = p0
        x1, y1 = p1
        dist = math.hypot(x1 - x0, y1 - y0)
        if dist < 1:
            return
        ux, uy = (x1 - x0) / dist, (y1 - y0) / dist
        pos = 0.0
        while pos < dist:
            e = min(pos + dash, dist)
            seg = ((x0 + ux * pos, y0 + uy * pos), (x0 + ux * e, y0 + uy * e))
            d.line(seg, fill=(0, 0, 0, 70), width=4)
            d.line(seg, fill=color[:3] + (225,), width=2)
            pos += dash + gap

    def _cursor(self, d, accent, f):
        if self._cur_pos is None or self._cur_target is None:
            return
        cx, cy = self._cur_pos
        tx, ty = self._cur_target
        cx += (tx - cx) * 0.34
        cy += (ty - cy) * 0.34
        self._cur_pos = (cx, cy)
        x, y = int(cx), int(cy)
        dt = f - self._click_f
        if 0 <= dt < 12:                                   # click ripple
            for i in range(2):
                rr = 6 + (dt + i * 3) * 4
                d.ellipse((x - rr, y - rr, x + rr, y + rr), outline=accent[:3] + (200,), width=2)
        ds = f - self._scroll_f
        if 0 <= ds < 12:                                   # scroll chevrons
            up = self._scroll_dir in ("up", "left")
            for i in range(3):
                oy = (-1 if up else 1) * (10 + i * 10 + (ds % 6) * 2)
                tipy = y + oy - (6 if up else -6)
                d.line((x - 8, y + oy, x, tipy), fill=accent, width=3)
                d.line((x, tipy, x + 8, y + oy), fill=accent, width=3)
        for rr, a in ((11, 60), (7, 90)):                  # soft halo
            d.ellipse((x - rr, y - rr, x + rr, y + rr), outline=accent[:3] + (a,), width=2)
        pts = [(x, y), (x, y + 20), (x + 5, y + 15), (x + 9, y + 23),
               (x + 12, y + 21), (x + 8, y + 13), (x + 14, y + 13)]
        d.polygon(pts, fill=(255, 255, 255, 255), outline=(20, 20, 30, 255))
        d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=accent, outline=(255, 255, 255, 255))
