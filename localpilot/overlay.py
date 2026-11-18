"""Live animated 'AI agent' overlay.

A transparent, click-through, always-on-top window that shows a small CARTOON AI
character reacting in real time as the agent works:
  - thinking:  blue glowing ring + bouncing "..." + wide blinking eyes
  - acting:    green ring, a speech bubble of what it's doing, and a pointer
               line from the character to a glowing box around the target control
  - done:      green, happy
  - stopped:   red, flat

Engineered to NEVER steal focus (so it can't break perception): created hidden,
flagged WS_EX_NOACTIVATE | WS_EX_TRANSPARENT, shown with SW_SHOWNOACTIVATE, and
tagged with a sentinel title so perception can skip it.

tkinter runs on its own daemon thread; the agent talks to it via a queue; an
animation tick (~20 fps) redraws the character and effects.
"""
from __future__ import annotations

import ctypes
import math
import queue
import threading

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
GA_ROOT = 2
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010

SENTINEL = "LocalPilotOverlay"
TRANSPARENT_KEY = "#010101"
BANNER_BG = "#101014"

# State colors
C_THINK = "#3b9bff"
C_ACT = "#35d07f"
C_DONE = "#35d07f"
C_STOP = "#ff3b30"
C_IDLE = "#8a8a99"


def _toplevel_hwnd(child_hwnd: int) -> int:
    return ctypes.windll.user32.GetAncestor(child_hwnd, GA_ROOT) or child_hwnd


def _make_click_through(hwnd: int):
    u = ctypes.windll.user32
    ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)


def _show_no_activate(hwnd: int):
    u = ctypes.windll.user32
    u.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                   SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def _lerp_color(c1, c2, t):
    def h2(c):
        c = c.lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    a, b = h2(c1), h2(c2)
    r = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return "#%02x%02x%02x" % r


class Overlay:
    def __init__(self):
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._ready = threading.Event()
        self._ok = False
        # shared animated state (only written on tk thread via _apply)
        self._state = "idle"       # idle|thinking|acting|done|stopped
        self._step = ""
        self._say = ""
        self._label = ""
        self._rect = None          # target control rect for acting
        self._frame = 0
        self._closed = False
        # agent cursor state
        self._cur_target = None     # (x, y) where the cursor should go
        self._cur_pos = None        # current animated (x, y)
        self._click_f = -999        # frame of last click burst
        self._scroll_f = -999       # frame of last scroll hint
        self._scroll_dir = "down"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(5)

    # ---------- public, thread-safe API ----------
    def think(self, step: str, title: str = ""):
        self._q.put(("state", "thinking", {"step": step, "say": title}))

    def act(self, rect, label: str, say: str):
        self._q.put(("state", "acting", {"rect": rect, "label": label, "say": say}))

    def done(self, text: str):
        self._q.put(("state", "done", {"say": text}))

    def stopped(self, text: str = "stopped"):
        self._q.put(("state", "stopped", {"say": text}))

    # backward-compatible helpers (used by vision backend)
    def status(self, text: str):
        self._q.put(("state", "thinking", {"step": text, "say": ""}))

    def highlight(self, rect, label: str):
        self._q.put(("state", "acting", {"rect": rect, "label": label, "say": label}))

    def clear(self):
        self._q.put(("clearhl",))

    # agent cursor API
    def cursor_move(self, x: int, y: int):
        self._q.put(("cursor", int(x), int(y)))

    def click_burst(self):
        self._q.put(("click",))

    def scroll_hint(self, direction: str = "down"):
        self._q.put(("scrollhint", direction))

    def close(self):
        self._q.put(("close",))

    @property
    def ok(self) -> bool:
        return self._ok

    # ---------- tk thread ----------
    def _run(self):
        try:
            import tkinter as tk
        except Exception:
            self._ready.set(); return
        try:
            self.tk = tk
            self.root = tk.Tk()
            self.root.withdraw()
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
            try:
                self.root.attributes("-transparentcolor", TRANSPARENT_KEY)
            except Exception:
                pass
            self.sw = self.root.winfo_screenwidth()
            self.sh = self.root.winfo_screenheight()
            self.root.geometry(f"{self.sw}x{self.sh}+0+0")
            self.root.config(bg=TRANSPARENT_KEY)
            self.canvas = tk.Canvas(self.root, bg=TRANSPARENT_KEY, highlightthickness=0,
                                    width=self.sw, height=self.sh)
            self.canvas.pack(fill="both", expand=True)
            self.root.update_idletasks()
            hwnd = _toplevel_hwnd(self.root.winfo_id())
            _make_click_through(hwnd)
            _show_no_activate(hwnd)
            ctypes.windll.user32.SetWindowTextW(hwnd, SENTINEL)
            # avatar home position (bottom-right)
            self.ax = self.sw - 120
            self.ay = self.sh - 150
            self._ok = True
        except Exception:
            self._ready.set(); return

        self._ready.set()
        self._tick()
        try:
            self.root.mainloop()
        except Exception:
            pass

    def _drain(self):
        try:
            while True:
                self._apply(self._q.get_nowait())
        except queue.Empty:
            pass

    def _apply(self, cmd):
        kind = cmd[0]
        if kind == "close":
            self._closed = True
            try:
                self.root.destroy()
            except Exception:
                pass
            return
        if kind == "clearhl":
            self._rect = None
            return
        if kind == "cursor":
            self._cur_target = (cmd[1], cmd[2])
            if self._cur_pos is None:
                # first placement: start from the avatar so it "launches" out
                self._cur_pos = (self.ax, self.ay)
            return
        if kind == "click":
            self._click_f = self._frame
            return
        if kind == "scrollhint":
            self._scroll_f = self._frame
            self._scroll_dir = cmd[1]
            return
        if kind == "state":
            self._state = cmd[1]
            data = cmd[2]
            if "step" in data:
                self._step = data["step"]
            if "rect" in data:
                self._rect = data["rect"]
            if "label" in data:
                self._label = data["label"]
            if "say" in data and data["say"]:
                self._say = data["say"]

    def _tick(self):
        if self._closed:
            return
        self._drain()
        self._frame += 1
        try:
            self._draw()
        except Exception:
            pass
        try:
            self.root.after(50, self._tick)   # ~20 fps
        except Exception:
            pass

    # ---------- drawing ----------
    def _draw(self):
        c = self.canvas
        c.delete("all")
        f = self._frame
        state = self._state
        accent = {"thinking": C_THINK, "acting": C_ACT, "done": C_DONE,
                  "stopped": C_STOP, "idle": C_IDLE}.get(state, C_IDLE)

        # --- target highlight + pointer (acting) ---
        if state == "acting" and self._rect:
            l, t, r, b = self._rect
            glow = 2 + (math.sin(f * 0.3) + 1) * 2.5          # breathing width
            for i, col in enumerate([_lerp_color(accent, TRANSPARENT_KEY, 0.6), accent]):
                c.create_rectangle(l - i * 3, t - i * 3, r + i * 3, b + i * 3,
                                   outline=col, width=int(glow) if i else 2)
            # label chip
            ly = t - 24 if t > 30 else b + 4
            w = max(90, 8 * len(self._label) + 12)
            c.create_rectangle(l, ly, l + w, ly + 22, fill=accent, outline="")
            c.create_text(l + 6, ly + 11, anchor="w", fill="white",
                          text=self._label, font=("Segoe UI", 9, "bold"))
            # pointer line from avatar to target center
            tx, ty = (l + r) // 2, (t + b) // 2
            c.create_line(self.ax, self.ay, tx, ty, fill=accent, width=2,
                          dash=(6, 6), smooth=True)

        # --- status banner (top-left) ---
        if self._step:
            txt = self._step
            c.create_rectangle(10, 10, 10 + 9 * len(txt) + 16, 38,
                               fill=BANNER_BG, outline=accent)
            c.create_text(18, 24, anchor="w", fill="white", text=txt,
                          font=("Segoe UI", 10, "bold"))

        # --- speech bubble (what it's "saying") ---
        say = self._say[:60]
        if say and state in ("thinking", "acting", "done", "stopped"):
            self._bubble(self.ax, self.ay - 62, say, accent)

        # --- the cartoon AI character ---
        self._avatar(self.ax, self.ay, accent, state, f)

        # --- the agent's own cursor ---
        self._cursor_draw(accent, f)

    def _bubble(self, cx, cy, text, accent):
        c = self.canvas
        w = max(120, 7 * len(text) + 20)
        x0, y0 = cx - w, cy - 20
        x1, y1 = cx + 46, cy + 18
        c.create_rectangle(x0, y0, x1, y1, fill=BANNER_BG, outline=accent, width=2)
        # little tail toward the avatar
        c.create_polygon(cx + 10, y1, cx + 28, y1, cx + 16, y1 + 12,
                         fill=BANNER_BG, outline=accent)
        c.create_text((x0 + x1) // 2, (y0 + y1) // 2, fill="white", text=text,
                      font=("Segoe UI", 9), width=w - 14)

    def _avatar(self, x, y, accent, state, f):
        c = self.canvas
        # pulsing glow ring
        pulse = (math.sin(f * 0.18) + 1) / 2                  # 0..1
        R = 34
        for i in range(4, 0, -1):
            rr = R + i * 4 + pulse * 6
            col = _lerp_color(accent, TRANSPARENT_KEY, 0.55 + i * 0.1)
            c.create_oval(x - rr, y - rr, x + rr, y + rr, outline=col, width=2)
        # head
        c.create_oval(x - R, y - R, x + R, y + R, fill="#1b1b22", outline=accent, width=3)
        # antenna
        ax2 = x + math.sin(f * 0.25) * 4
        c.create_line(x, y - R, ax2, y - R - 14, fill=accent, width=3)
        c.create_oval(ax2 - 5, y - R - 19, ax2 + 5, y - R - 9, fill=accent, outline="")

        # eyes (blink every ~2.5s)
        blink = (f % 50) >= 47
        eye_dx, eye_dy, ew = 13, -4, 7
        # eyes follow the target a touch when acting
        look = 0
        if state == "acting" and self._rect:
            look = -3 if (self._rect[0] + self._rect[2]) / 2 < x else 3
        for sx in (-eye_dx, eye_dx):
            ex = x + sx + look
            if blink:
                c.create_line(ex - ew, y + eye_dy, ex + ew, y + eye_dy, fill="white", width=3)
            else:
                c.create_oval(ex - ew, y + eye_dy - ew, ex + ew, y + eye_dy + ew,
                              fill="white", outline="")
                c.create_oval(ex - 3 + look, y + eye_dy - 3, ex + 3 + look, y + eye_dy + 3,
                              fill="#1b1b22", outline="")

        # mouth by mood
        my = y + 15
        if state == "stopped":
            c.create_line(x - 10, my + 3, x + 10, my - 3, fill="white", width=3)   # frown-ish
        elif state in ("done", "acting"):
            c.create_arc(x - 12, my - 10, x + 12, my + 8, start=200, extent=140,
                         style="arc", outline="white", width=3)                    # smile
        else:  # thinking
            c.create_line(x - 8, my, x + 8, my, fill="white", width=3)

        # thinking dots above the bubble area
        if state == "thinking":
            for i in range(3):
                on = (f // 6) % 3 == i
                dy = -6 if on else 0
                c.create_oval(x - 16 + i * 16, y - R - 34 + dy,
                              x - 10 + i * 16, y - R - 28 + dy,
                              fill=accent if on else C_IDLE, outline="")


    def _cursor_draw(self, accent, f):
        if self._cur_pos is None or self._cur_target is None:
            return
        c = self.canvas
        # ease current position toward target (smooth glide)
        cx, cy = self._cur_pos
        tx, ty = self._cur_target
        cx += (tx - cx) * 0.34
        cy += (ty - cy) * 0.34
        self._cur_pos = (cx, cy)
        x, y = int(cx), int(cy)

        # click ripple (expanding rings)
        dt = f - self._click_f
        if 0 <= dt < 12:
            for i in range(2):
                rr = 6 + (dt + i * 3) * 4
                c.create_oval(x - rr, y - rr, x + rr, y + rr, outline=accent, width=2)

        # scroll hint (chevrons)
        ds = f - self._scroll_f
        if 0 <= ds < 12:
            up = self._scroll_dir in ("up", "left")
            for i in range(3):
                oy = (-1 if up else 1) * (10 + i * 10 + (ds % 6) * 2)
                c.create_line(x - 8, y + oy, x, y + oy - (6 if up else -6),
                              fill=accent, width=3)
                c.create_line(x, y + oy - (6 if up else -6), x + 8, y + oy,
                              fill=accent, width=3)

        # glow halo
        for i in (10, 6):
            c.create_oval(x - i, y - i, x + i, y + i,
                          outline=_lerp_color(accent, TRANSPARENT_KEY, 0.4), width=2)
        # the pointer arrow (distinct agent cursor)
        pts = [x, y, x, y + 20, x + 5, y + 15, x + 9, y + 23,
               x + 12, y + 21, x + 8, y + 13, x + 14, y + 13]
        c.create_polygon(pts, fill="white", outline=accent, width=2)
        # small accent dot at the tip
        c.create_oval(x - 3, y - 3, x + 3, y + 3, fill=accent, outline="white")


class NullOverlay:
    ok = False
    def think(self, *a, **k): pass
    def act(self, *a, **k): pass
    def done(self, *a, **k): pass
    def stopped(self, *a, **k): pass
    def status(self, *a, **k): pass
    def highlight(self, *a, **k): pass
    def clear(self, *a, **k): pass
    def cursor_move(self, *a, **k): pass
    def click_burst(self, *a, **k): pass
    def scroll_hint(self, *a, **k): pass
    def close(self, *a, **k): pass
