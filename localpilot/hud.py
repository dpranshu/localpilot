"""True-transparency HUD overlay (per-pixel alpha layered window).

tkinter can only do a single "color key" transparency, so soft edges / glows blend
against black and show up as a dark halo or box on bright pages. This HUD instead
renders every frame with PIL as RGBA and pushes it to a Win32 *layered window* with
UpdateLayeredWindow (premultiplied BGRA). Result: genuinely transparent background,
smooth glow over ANY content, no boxes.

It stays click-through (WS_EX_TRANSPARENT) and never takes focus (WS_EX_NOACTIVATE),
and is titled with a sentinel so perception skips it -- same safety as before.

Public API is identical to the tk overlay: think / act / done / stopped / status /
highlight / clear / cursor_move / click_burst / scroll_hint / close / ok.
"""
from __future__ import annotations

import ctypes
import math
import queue
import textwrap
import threading
import time
from ctypes import wintypes

from PIL import Image, ImageDraw, ImageFont

from .orb import Orb, STATE_MODE, PALETTES, SPEED

SENTINEL = "LocalPilotOverlay"

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
ULW_ALPHA = 2
AC_SRC_OVER, AC_SRC_ALPHA = 0, 1
PM_REMOVE = 1

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
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                                       ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
                                       ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.DestroyWindow.argtypes = [wintypes.HWND]
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


def _hex(c, a=255):
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), a)


class HudOverlay:
    """Per-pixel-alpha, click-through, non-activating overlay."""

    def __init__(self):
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
        self._phase = 0.0
        self._cur_target = None
        self._cur_pos = None
        self._click_f = -999
        self._scroll_f = -999
        self._scroll_dir = "down"
        self.last_frame_ms = 0.0
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

    # ---------------- window plumbing ----------------
    def _setup(self):
        self.W = user32.GetSystemMetrics(0)
        self.H = user32.GetSystemMetrics(1)
        self._wndproc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
        self._cls = f"LocalPilotHud{id(self)}"
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = self._cls
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise OSError("RegisterClassW failed")
        ex = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE |
              WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
        self.hwnd = user32.CreateWindowExW(ex, self._cls, SENTINEL, WS_POPUP, 0, 0,
                                           self.W, self.H, None, None, wc.hInstance, None)
        if not self.hwnd:
            raise OSError("CreateWindowExW failed")
        # DIB section we copy each frame into
        self._screen_dc = user32.GetDC(None)
        self._mem_dc = gdi32.CreateCompatibleDC(self._screen_dc)
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = self.W, -self.H          # top-down
        bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
        self._bits = ctypes.c_void_p()
        self._hbmp = gdi32.CreateDIBSection(self._screen_dc, ctypes.byref(bmi), 0,
                                            ctypes.byref(self._bits), None, 0)
        if not self._hbmp:
            raise OSError("CreateDIBSection failed")
        self._old = gdi32.SelectObject(self._mem_dc, self._hbmp)
        self._blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        self._pt_dst = wintypes.POINT(0, 0)
        self._pt_src = wintypes.POINT(0, 0)
        self._size = wintypes.SIZE(self.W, self.H)
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        # assets
        self.orb = Orb(diameter=126)
        self.f_txt = _font(15)
        self.f_bold = _font(14, bold=True)
        self.f_small = _font(12, bold=True)
        self.ax = self.W - 135
        self.ay = self.H - 175

    def _present(self, img: Image.Image):
        """img: RGBA (straight alpha) -> premultiplied BGRA -> layered window."""
        pm = img.convert("RGBa")
        r, g, b, a = pm.split()
        raw = Image.merge("RGBa", (b, g, r, a)).tobytes()
        ctypes.memmove(self._bits, raw, len(raw))
        user32.UpdateLayeredWindow(self.hwnd, self._screen_dc, ctypes.byref(self._pt_dst),
                                   ctypes.byref(self._size), self._mem_dc,
                                   ctypes.byref(self._pt_src), 0,
                                   ctypes.byref(self._blend), ULW_ALPHA)

    def _teardown(self):
        try:
            gdi32.SelectObject(self._mem_dc, self._old)
            gdi32.DeleteObject(self._hbmp)
            gdi32.DeleteDC(self._mem_dc)
            user32.ReleaseDC(None, self._screen_dc)
            user32.DestroyWindow(self.hwnd)
            user32.UnregisterClassW(self._cls, kernel32.GetModuleHandleW(None))
        except Exception:
            pass

    # ---------------- main loop ----------------
    def _run(self):
        try:
            self._setup()
            self._ok = True
        except Exception:
            self._ok = False
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        last_top = time.time()
        try:
            while not self._closed:
                t0 = time.time()
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                self._drain()
                if self._closed:
                    break
                self._frame += 1
                try:
                    self._present(self._compose())
                except Exception:
                    pass
                if time.time() - last_top > 2.0:       # stay above other topmost windows
                    user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                    last_top = time.time()
                dt = time.time() - t0
                self.last_frame_ms = dt * 1000
                time.sleep(max(0.005, 0.045 - dt))
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
                        self._cur_pos = (self.ax, self.ay)
                elif k == "click":
                    self._click_f = self._frame
                elif k == "scrollhint":
                    self._scroll_f, self._scroll_dir = self._frame, cmd[1]
                elif k == "state":
                    self._state = cmd[1]
                    d = cmd[2]
                    if "step" in d:
                        self._step = d["step"]
                    if "rect" in d:
                        self._rect = d["rect"]
                    if "label" in d:
                        self._label = d["label"]
                    if d.get("say"):
                        self._say = d["say"]
        except queue.Empty:
            pass

    # ---------------- drawing ----------------
    def _text(self, d, xy, s, fill=(255, 255, 255, 255), font=None, anchor="la"):
        d.text(xy, s, font=font or self.f_txt, fill=fill, anchor=anchor,
               stroke_width=2, stroke_fill=(0, 0, 0, 215))

    def _compose(self) -> Image.Image:
        W, H, f = self.W, self.H, self._frame
        state = self._state
        mode = STATE_MODE.get(state, 5)
        accent = _hex(PALETTES[mode][2])
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        # target highlight + pointer line (acting)
        if state == "acting" and self._rect:
            l, t, r, b = self._rect
            breathe = 2 + (math.sin(f * 0.3) + 1) * 1.8
            d.rounded_rectangle((l - 4, t - 4, r + 4, b + 4), radius=8,
                                outline=accent[:3] + (70,), width=int(breathe) + 4)
            d.rounded_rectangle((l, t, r, b), radius=6, outline=accent, width=3)
            if self._label:
                ly = t - 22 if t > 30 else b + 6
                self._text(d, (l, ly), self._label, font=self.f_small)
            tx, ty = (l + r) / 2, (t + b) / 2
            self._dashed(d, (self.ax, self.ay), (tx, ty), accent)

        # status line (top-left): colored dot + outlined text
        if self._step:
            d.ellipse((16, 17, 26, 27), fill=accent)
            self._text(d, (34, 13), self._step, font=self.f_bold)

        # speech text left of the orb (no bubble box -- outlined text only)
        say = self._say[:90]
        if say and state in ("thinking", "acting", "done", "stopped"):
            lines = textwrap.wrap(say, 38)[:3]
            y = self.ay - 10 - (len(lines) - 1) * 10
            for ln in lines:
                self._text(d, (self.ax - 112, y), ln, anchor="ra")
                y += 20

        # the orb (RGBA with baked soft glow -> truly transparent edges)
        self._phase += max(0.02, SPEED[mode % len(SPEED)] * 0.11)
        lift = int(-4 * (0.5 + 0.5 * math.sin(f * 0.07)))
        o = self.orb.render(mode, self._phase)
        img.alpha_composite(o, (int(self.ax - o.width / 2), int(self.ay + lift - o.height / 2)))

        # the agent's own cursor
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
