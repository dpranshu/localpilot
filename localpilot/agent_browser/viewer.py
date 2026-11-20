"""Live viewer for LocalPilot's research browser, built on agent-browser's OFFICIAL stream.

  StreamClient  -- consumes agent-browser's WebSocket stream of the SAME session the agent drives:
                   frame (JPEG), url, tabs, status, console, command/result (the activity feed the
                   official dashboard shows). Reconnects by itself (e.g. after a browser recycle).
                   VIEW-ONLY: it sends input only while the user has explicitly taken control.
  PipWindow     -- Picture-in-Picture: compact, draggable, resizable, optional always-on-top. Live page,
                   current action, Pause/Resume, Stop, Expand.
  ExpandedView  -- large live view + Activity, Console, Session, Sources and Security; Take Control.

Nothing here re-renders pages or invents activity: frames and events come from agent-browser, the task
state from LocalPilot's controller. No extra Chromium is started. Screenshot polling is used only as a
fallback when the stream can't connect. Security stays below this layer (guard proxy, URL gate,
allow-list); the stream itself is switched on only while a viewer is open.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
import time
import tkinter as tk
from collections import deque

from .ui import FONT, FONT_B, FONT_S, STATE_COL


# ======================================================================= stream client
class StreamClient:
    def __init__(self, controller, max_fps: float = 4.0):
        self.c = controller
        self.max_fps = max_fps
        self.connected = False
        self.endpoint = ""
        self.frame: bytes | None = None
        self.frame_seq = 0
        self.frame_meta: dict = {}
        self.frame_at = 0.0
        self.url = ""
        self.tabs: list = []
        self.status: dict = {}
        self.activity: deque = deque(maxlen=400)       # {"at","action","params","ok","ms"}
        self.console: deque = deque(maxlen=400)
        self.reconnects = 0
        self.last_error = ""
        self.frames_received = 0
        self._pending: dict = {}
        self._ws = None
        self._loop = None
        self._stop = threading.Event()
        self._grant = None                              # Take Control token from the controller
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name="LocalPilot-StreamClient")

    # ---------------- lifecycle
    def start(self):
        self.c.viewer_attach()
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        try:
            if self._loop and self._ws:
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
        except Exception:
            pass
        self.c.viewer_detach()

    def set_fps(self, fps: float):
        self.max_fps = fps
        self._send({"type": "config", "maxFps": max(1, int(round(fps)))})

    # ---------------- input (Take Control only)
    def set_control(self, token: str | None):
        """The controller's grant (after the user explicitly confirmed Take Control), or None."""
        self._grant = token or None

    @property
    def controlling(self) -> bool:
        return self.c.input_allowed(self._grant)

    def send_input(self, msg: dict) -> bool:
        """Forwarded ONLY with a valid grant: the user explicitly took control, the agent can't act
        (paused / no task), and the browser hasn't been touched by a foreign client."""
        if msg.get("type") not in ("input_mouse", "input_keyboard", "input_touch"):
            return False
        if not self.c.input_allowed(self._grant):
            return False
        return self._send(msg)

    def _send(self, msg: dict) -> bool:
        if not (self.connected and self._loop and self._ws):
            return False
        try:
            asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(msg)), self._loop)
            return True
        except Exception:
            return False

    # ---------------- the socket
    def _run(self):
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _main(self):
        import websockets
        while not self._stop.is_set():
            ep = await asyncio.get_running_loop().run_in_executor(None, self.c.stream_endpoint)
            if not ep:
                self.connected = False
                await asyncio.sleep(0.5)
                continue
            try:
                url = f"{ep}/?maxFps={max(1, int(round(self.max_fps)))}"
                async with websockets.connect(url, max_size=None, ping_interval=10, ping_timeout=10,
                                              open_timeout=5) as ws:
                    self._ws, self.endpoint, self.connected = ws, ep, True
                    self.last_error = ""
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 1.0)
                        except asyncio.TimeoutError:
                            if self.c.stream_endpoint() != ep:          # session restarted elsewhere
                                break
                            continue
                        self._handle(raw)
            except Exception as e:
                self.last_error = str(e)[:160]
                if not self.connected:                       # couldn't connect at all: stale port
                    await asyncio.get_running_loop().run_in_executor(None, self.c.stream_lost)
            self.connected, self._ws = False, None
            if not self._stop.is_set():
                self.reconnects += 1
                await asyncio.sleep(0.6)

    def _handle(self, raw):
        try:
            m = json.loads(raw)
        except Exception:
            return
        t = m.get("type")
        if t == "frame":
            try:
                data = base64.b64decode(m.get("data") or "")
            except Exception:
                return
            with self._lock:
                self.frame, self.frame_seq = data, m.get("seq", self.frame_seq + 1)
                self.frame_meta, self.frame_at = m.get("metadata") or {}, time.time()
                self.frames_received += 1
        elif t == "url":
            self.url = m.get("url") or self.url
        elif t == "tabs":
            self.tabs = m.get("tabs") or []
            act = next((t for t in self.tabs if t.get("active")), None)
            if act and act.get("url"):
                self.url = act["url"]                       # a fresh connection gets tabs, not url
        elif t == "status":
            self.status = {k: v for k, v in m.items() if k != "type"}
        elif t == "console":
            self.console.append({"at": _hms(m.get("timestamp")), "level": m.get("level", ""),
                                 "text": str(m.get("text", ""))[:300]})
        elif t == "command":
            p = {k: v for k, v in (m.get("params") or {}).items() if k not in ("action", "id")}
            row = {"at": _hms(m.get("timestamp")), "action": m.get("action", ""), "params": _short(p),
                   "ok": None, "ms": None}
            self._pending[m.get("id")] = row
            self.activity.append(row)
        elif t == "result":
            row = self._pending.pop(m.get("id"), None)
            if row is not None:
                # 0.43's stream reports success=false even for commands that returned data;
                # an error field is the reliable signal
                row["ok"] = bool(m.get("success")) or (not m.get("error") and m.get("data") is not None)
                row["ms"] = m.get("duration_ms")

    def latest_frame(self):
        with self._lock:
            return self.frame, self.frame_seq, dict(self.frame_meta)


def _hms(ts) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts) / 1000.0))
    except Exception:
        return time.strftime("%H:%M:%S")


def _short(p: dict) -> str:
    s = " ".join(f"{k}={v}" for k, v in p.items() if v not in (None, "", [], {}))
    return s[:160]


# ======================================================================= dark controls
class FlatButton(tk.Label):
    """A dark, flat button (the native Windows ttk theme ignores the dark palette)."""

    def __init__(self, parent, text, command, pal, kind="normal"):
        self.command = command
        fg = {"stop": pal["err"], "accent": pal["accent"]}.get(kind, pal["fg"])
        super().__init__(parent, text=text, bg=pal["pill"], fg=fg, font=FONT_S, padx=10, pady=4, cursor="hand2")
        self.bind("<Enter>", lambda e: self.configure(bg=pal["line"]))
        self.bind("<Leave>", lambda e: self.configure(bg=pal["pill"]))
        self.bind("<Button-1>", lambda e: self.command())


class Tabs(tk.Frame):
    """A dark segmented tab bar over read-only text panes."""

    def __init__(self, parent, pal, names):
        super().__init__(parent, bg=pal["bg"])
        self.pal, self.btn, self.pane, self._last = pal, {}, {}, {}
        bar = tk.Frame(self, bg=pal["bg"]); bar.pack(fill="x")
        body = tk.Frame(self, bg=pal["log"]); body.pack(fill="both", expand=True, pady=(4, 0))
        for n in names:
            b = tk.Label(bar, text=n, bg=pal["bg"], fg=pal["subtle"], font=FONT_S, padx=10, pady=4, cursor="hand2")
            b.pack(side="left"); b.bind("<Button-1>", lambda e, n=n: self.show(n))
            t = tk.Text(body, bg=pal["log"], fg=pal["fg"], font=("Consolas", 9), relief="flat", wrap="none",
                        borderwidth=0, padx=8, pady=6, height=10, state="disabled")
            self.btn[n], self.pane[n] = b, t
        self.current = None
        self.show(names[0])

    def show(self, name):
        if self.current:
            self.pane[self.current].pack_forget()
            self.btn[self.current].configure(bg=self.pal["bg"], fg=self.pal["subtle"])
        self.current = name
        self.pane[name].pack(fill="both", expand=True)
        self.btn[name].configure(bg=self.pal["pill"], fg=self.pal["fg"])

    def set_text(self, name, text, follow=True):
        if self._last.get(name) == text:
            return
        self._last[name] = text
        t = self.pane[name]
        top = t.yview()[0]
        t.configure(state="normal"); t.delete("1.0", "end"); t.insert("1.0", text); t.configure(state="disabled")
        t.yview_moveto(1.0 if follow else top)
        t.xview_moveto(0.0)

    def text(self, name) -> str:
        return self._last.get(name, "")


# ======================================================================= shared drawing
class _Live:
    """Draws the newest stream frame onto a canvas (only when it changed), else a clear state."""

    def __init__(self, canvas: tk.Canvas, client: StreamClient, controller, pal):
        self.cv, self.client, self.c, self.pal = canvas, client, controller, pal
        self._photo, self._seq, self._size = None, -1, (0, 0)
        self.scale, self.offset = 1.0, (0, 0)
        self.frames_drawn = 0
        self._fallback_at = 0.0
        self._fallback_frame = None

    def draw(self):
        from PIL import Image, ImageTk
        cw, ch = max(40, self.cv.winfo_width()), max(40, self.cv.winfo_height())
        data, seq, meta = self.client.latest_frame()
        source = "stream"
        if data is not None and not self.client.connected:
            if time.time() - self.client.frame_at < 6:
                source = "reconnecting"                     # keep the last live frame briefly
            else:
                data = None                                 # stream really down: screenshots instead
        if data is None:
            # fallback ONLY if the stream can't be used: a real screenshot of the same session
            if self.c.browser_running and time.time() - self._fallback_at > 1.5:
                self._fallback_at = time.time()
                f = self.c.preview_frame()
                if f:
                    self._fallback_frame = f
            if self._fallback_frame and self.c.browser_running:
                data, seq, source = self._fallback_frame, -2 - int(self._fallback_at), "screenshot"
        if data is None or not self.c.browser_running:
            if self._seq != -1 or self._size != (cw, ch):
                self.cv.delete("all"); self._photo = None
                msg = ("The research browser isn't running.\nIt starts when LocalPilot needs to browse."
                       if not self.c.browser_running else "Connecting to the live view…")
                self.cv.create_text(cw // 2, ch // 2, text=msg, fill=self.pal["subtle"], font=FONT, justify="center")
                self._seq, self._size = -1, (cw, ch)
            return source if data is not None else "none"
        if seq == self._seq and self._size == (cw, ch):
            return source
        try:
            img = Image.open(io.BytesIO(data))
            iw, ih = img.size
            self.scale = min(cw / iw, ch / ih)
            nw, nh = max(1, int(iw * self.scale)), max(1, int(ih * self.scale))
            img = img.resize((nw, nh), Image.BILINEAR)
            self.offset = ((cw - nw) // 2, (ch - nh) // 2)
            self._photo = ImageTk.PhotoImage(img)
            self.cv.delete("all")
            self.cv.create_image(self.offset[0], self.offset[1], image=self._photo, anchor="nw")
            self._seq, self._size = seq, (cw, ch)
            self.frames_drawn += 1
        except Exception:
            pass
        return source

    def to_page(self, x, y):
        """Canvas -> page CSS pixels (for Take Control input)."""
        return int((x - self.offset[0]) / max(self.scale, 1e-6)), int((y - self.offset[1]) / max(self.scale, 1e-6))


# ======================================================================= Picture-in-Picture
class PipWindow:
    """Compact, draggable, resizable, optionally always-on-top live view of the agent's browser."""

    def __init__(self, root, controller, session, pal, settings: dict, on_expand, on_closed=lambda: None):
        self.root, self.c, self.session, self.pal, self.settings = root, controller, session, pal, settings
        self.on_expand, self.on_closed = on_expand, on_closed
        self.win = None
        self.client = None

    @property
    def visible(self) -> bool:
        return bool(self.win and self.win.winfo_exists())

    def open(self):
        if self.visible:
            self.win.deiconify(); self.win.lift(); return
        pal = self.pal
        w = self.win = tk.Toplevel(self.root)
        w.title("LocalPilot - live browser")
        w.overrideredirect(True)                          # our own compact chrome
        geo = self.settings.get("pip_geometry") or "420x300-24-64"
        w.geometry(geo)
        w.minsize(260, 190)
        w.configure(bg=pal["line"])
        w.attributes("-topmost", bool(self.settings.get("pip_on_top", True)))
        body = tk.Frame(w, bg=pal["card"]); body.pack(fill="both", expand=True, padx=1, pady=1)
        head = tk.Frame(body, bg=pal["card"]); head.pack(fill="x", padx=8, pady=(6, 2))
        self.dot = tk.Canvas(head, width=10, height=10, bg=pal["card"], highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 6))
        self.title = tk.Label(head, text="Agent browser", bg=pal["card"], fg=pal["fg"], font=FONT_B)
        self.title.pack(side="left")
        self.live = tk.Label(head, text="", bg=pal["card"], fg=pal["accent"], font=FONT_S)
        self.live.pack(side="left", padx=6)
        for txt, cmd, tip in (("✕", self.close, "Close"), ("⤢", self._expand, "Expand"),
                              ("📌", self._toggle_top, "Always on top")):
            b = tk.Label(head, text=txt, bg=pal["card"], fg=pal["subtle"], font=FONT, cursor="hand2", padx=4)
            b.pack(side="right"); b.bind("<Button-1>", lambda e, c=cmd: c())
        self.pin = head.winfo_children()[-1]
        # controls are packed at the bottom FIRST, so a small window never squeezes them out
        bar = tk.Frame(body, bg=pal["card"]); bar.pack(side="bottom", fill="x", padx=6, pady=(2, 6))
        self.action = tk.Label(body, text="", bg=pal["card"], fg=pal["subtle"], font=FONT_S, anchor="w")
        self.action.pack(side="bottom", fill="x", padx=8, pady=(3, 0))
        self.canvas = tk.Canvas(body, bg=pal["log"], highlightthickness=0, cursor="arrow")
        self.canvas.pack(fill="both", expand=True, padx=6)
        self.b_pause = FlatButton(bar, "⏸ Pause", self._pause_resume, pal)
        self.b_pause.pack(side="left")
        FlatButton(bar, "■ Stop", lambda: self.session.interrupt("stop"), pal, "stop").pack(side="left", padx=4)
        grip = tk.Label(bar, text="◢", bg=pal["card"], fg=pal["subtle"], cursor="size_nw_se", font=FONT_S)
        grip.pack(side="right")
        FlatButton(bar, "Expand ⤢", self._expand, pal, "accent").pack(side="right", padx=(0, 6))
        # drag anywhere on the header; resize from the grip
        for wdg in (head, self.title, self.live, self.dot):
            wdg.bind("<ButtonPress-1>", self._drag_start); wdg.bind("<B1-Motion>", self._drag_move)
        grip.bind("<ButtonPress-1>", self._resize_start); grip.bind("<B1-Motion>", self._resize_move)
        w.bind("<Escape>", lambda e: self.session.interrupt("stop"))
        self._top = bool(self.settings.get("pip_on_top", True))
        self._paint_pin()
        self.client = StreamClient(self.c, max_fps=float(self.settings.get("pip_fps", 4))).start()
        self.view = _Live(self.canvas, self.client, self.c, pal)
        self._tick()

    def close(self):
        if self.visible:
            self.settings["pip_geometry"] = self.win.geometry()
            self.win.destroy()
        self.win = None
        if self.client:
            self.client.close(); self.client = None
        self.on_closed()

    def _expand(self):
        self.close()
        self.on_expand()

    def _toggle_top(self):
        self._top = not self._top
        self.settings["pip_on_top"] = self._top
        self.win.attributes("-topmost", self._top); self._paint_pin()

    def _paint_pin(self):
        self.pin.configure(fg=self.pal["accent"] if self._top else self.pal["subtle"])

    def _pause_resume(self):
        self.session.resume() if self.c.hold_event.is_set() else self.session.hold()

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y()

    def _drag_move(self, e):
        self.win.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _resize_start(self, e):
        self._rx, self._ry = e.x_root, e.y_root
        self._rw, self._rh = self.win.winfo_width(), self.win.winfo_height()

    def _resize_move(self, e):
        w = max(260, self._rw + e.x_root - self._rx); h = max(190, self._rh + e.y_root - self._ry)
        self.win.geometry(f"{w}x{h}")

    def _tick(self):
        if not self.visible:
            return
        snap = self.c.snapshot()
        col = self.pal[STATE_COL.get(snap["state"], "subtle")]
        self.dot.delete("all"); self.dot.create_oval(1, 1, 9, 9, fill=col, outline=col)
        src = self.view.draw()
        self.live.configure(text={"stream": "● LIVE", "reconnecting": "● reconnecting",
                                  "screenshot": "● screenshots"}.get(src, ""),
                            fg=self.pal["accent"] if src == "stream" else self.pal["warn"])
        site = snap["domain"] or ""
        act = snap.get("action") if snap["busy"] else ""
        self.title.configure(text=(site or "Agent browser")[:34])
        self.action.configure(text=(f"{snap['state']}: {act or snap.get('detail') or ''}")[:90], fg=col)
        self.b_pause.configure(text="▶ Resume" if snap["state"] == "Paused" else "⏸ Pause")
        self.win.after(120, self._tick)


# ======================================================================= expanded view
class ExpandedView:
    """Large live view with the useful parts of the official dashboard: activity feed, console,
    session details -- from the same stream -- plus LocalPilot's sources, security and controls."""

    def __init__(self, root, controller, session, pal, settings: dict, on_collapse, on_closed=lambda: None):
        self.root, self.c, self.session, self.pal, self.settings = root, controller, session, pal, settings
        self.on_collapse, self.on_closed = on_collapse, on_closed
        self.win = None
        self.client = None
        self.controlling = False
        from tkinter import messagebox
        self.confirm = lambda title, text: messagebox.askyesno(title, text, parent=self.win, icon="warning")

    @property
    def visible(self) -> bool:
        return bool(self.win and self.win.winfo_exists())

    def open(self):
        if self.visible:
            self.win.deiconify(); self.win.lift(); return
        pal = self.pal
        w = self.win = tk.Toplevel(self.root)
        w.title("LocalPilot - agent browser (live)"); w.configure(bg=pal["bg"])
        w.geometry(self.settings.get("expanded_geometry") or "1180x720"); w.minsize(760, 480)
        w.protocol("WM_DELETE_WINDOW", self.close)
        bar = tk.Frame(w, bg=pal["bg"]); bar.pack(fill="x", padx=10, pady=(10, 4))
        self.pill = tk.Label(bar, text="Idle", bg=pal["pill"], fg=pal["subtle"], font=FONT_S, padx=10, pady=3)
        self.pill.pack(side="left")
        self.urlbar = tk.Label(bar, text="", bg=pal["field"], fg=pal["fg"], font=("Segoe UI", 10), anchor="w", padx=8)
        self.urlbar.pack(side="left", fill="x", expand=True, padx=8, ipady=4)
        self.b_pause = FlatButton(bar, "⏸ Pause", self._pause_resume, pal)
        self.b_pause.pack(side="left", padx=2)
        FlatButton(bar, "■ Stop", self._stop_task, pal, "stop").pack(side="left", padx=2)
        self.b_ctrl = FlatButton(bar, "✋ Take control", self._toggle_control, pal)
        self.b_ctrl.pack(side="left", padx=2)
        FlatButton(bar, "⧉ Picture-in-picture", self._collapse, pal, "accent").pack(side="left", padx=(2, 0))
        body = tk.PanedWindow(w, orient="horizontal", bg=pal["bg"], sashwidth=6, bd=0)
        body.pack(fill="both", expand=True, padx=10, pady=(4, 4))
        left = tk.Frame(body, bg=pal["log"])
        self.canvas = tk.Canvas(left, bg=pal["log"], highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.caption = tk.Label(left, text="", bg=pal["log"], fg=pal["subtle"], font=FONT_S, anchor="w")
        self.caption.pack(fill="x")
        body.add(left, minsize=420, stretch="always")
        self.tabs = Tabs(body, pal, ["Activity", "Console", "Sources", "Security", "Session"])
        body.add(self.tabs, minsize=360)
        self.footer = tk.Label(w, text="", bg=pal["bg"], fg=pal["subtle"], font=FONT_S, anchor="w")
        self.footer.pack(fill="x", padx=10, pady=(0, 8))
        w.bind("<Escape>", lambda e: self._stop_task())
        # Take Control input (ignored unless control was explicitly taken)
        self.canvas.bind("<Button-1>", lambda e: self._mouse(e, "left"))
        self.canvas.bind("<MouseWheel>", self._wheel)
        w.bind("<Key>", self._key)
        self.client = StreamClient(self.c, max_fps=float(self.settings.get("expanded_fps", 8))).start()
        self.view = _Live(self.canvas, self.client, self.c, pal)
        self._tick()

    def close(self):
        if self.controlling:
            self._toggle_control()
        if self.visible:
            self.settings["expanded_geometry"] = self.win.geometry()
            self.win.destroy()
        self.win = None
        if self.client:
            self.client.close(); self.client = None
        self.on_closed()

    def _collapse(self):
        self.close()
        self.on_collapse()

    def _pause_resume(self):
        self.session.resume() if self.c.hold_event.is_set() else self.session.hold()

    # ---------------- Take Control: the agent is PAUSED first; it never resumes by itself
    def _toggle_control(self):
        if not self.controlling:
            self.session.hold()                              # pause BEFORE any human input
            if not self.confirm(
                    "Take control of the research browser?",
                    "LocalPilot's task is paused. Your clicks, scrolling and typing will go to the page in "
                    "the research browser.\n\nDon't enter passwords, payment details or personal "
                    "information there.\n\nThe task stays paused after you give control back, until you "
                    "press Resume or say 'continue'."):
                return                                       # no authorization: stays view-only (and paused)
            token = self.c.grant_control()
            if token is None:
                return                                       # agent still acting / browser not usable
            self.controlling = True
            self.client.set_control(token)
            self.b_ctrl.configure(text="↩ Give back control", fg=self.pal["warn"])
            self.canvas.configure(cursor="hand2")
        else:
            self.controlling = False
            self.client.set_control(None)
            self.c.revoke_control()
            self.b_ctrl.configure(text="✋ Take control", fg=self.pal["fg"])
            self.canvas.configure(cursor="arrow")
            # the agent STAYS paused: press Resume (or say "continue") to let it go on

    def _stop_task(self):
        if self.controlling:
            self._toggle_control()                           # Stop also ends human control
        self.session.interrupt("stop")

    def _mouse(self, e, button):
        if not self.controlling:
            return
        x, y = self.view.to_page(e.x, e.y)
        for kind in ("mousePressed", "mouseReleased"):
            self.client.send_input({"type": "input_mouse", "eventType": kind, "x": x, "y": y,
                                    "button": button, "clickCount": 1})

    def _wheel(self, e):
        if not self.controlling:
            return
        x, y = self.view.to_page(e.x, e.y)
        self.client.send_input({"type": "input_mouse", "eventType": "mouseWheel", "x": x, "y": y,
                                "deltaX": 0, "deltaY": -e.delta})

    def _key(self, e):
        if not self.controlling or e.keysym == "Escape":
            return
        key = e.char if e.char and e.char.isprintable() else e.keysym
        for kind in ("keyDown", "keyUp"):
            msg = {"type": "input_keyboard", "eventType": kind, "key": key}
            if kind == "keyDown" and e.char and e.char.isprintable():
                msg["text"] = e.char
            self.client.send_input(msg)

    # ---------------- refresh
    def _tick(self):
        if not self.visible:
            return
        if self.controlling and getattr(self.session, "busy", False) and not self.c.hold_event.is_set():
            self.session.hold()                              # a task started while the user has control
        if self.controlling and self.c._grant is None:      # revoked elsewhere (foreign client, Stop)
            self._toggle_control()
        snap = self.c.snapshot()
        cl = self.client
        col = self.pal[STATE_COL.get(snap["state"], "subtle")]
        self.pill.configure(text=("You have control" if self.controlling else snap["state"]),
                            fg=self.pal["warn"] if self.controlling else col)
        self.urlbar.configure(text=cl.url or snap["url"] or "")
        self.b_pause.configure(text="▶ Resume" if snap["state"] == "Paused" else "⏸ Pause")
        src = self.view.draw()
        self.caption.configure(text=(
            {"stream": "Live: agent-browser's official viewport stream",
             "reconnecting": "Live stream reconnecting…",
             "screenshot": "Live view via screenshots (stream unavailable)"}.get(src, "")
            + (f"  ·  now: {snap.get('action')}" if snap["busy"] and snap.get("action") else "")))
        T = self.tabs
        T.set_text("Activity", "\n".join(
            f"{r['at']}  {'…' if r['ok'] is None else ('✓' if r['ok'] else '✗')}  {r['action']:<12}"
            f"{'' if r['ms'] is None else str(r['ms']) + ' ms':>9}  {r['params']}" for r in list(cl.activity)[-300:])
            or "Commands agent-browser runs for LocalPilot appear here.")
        T.set_text("Console", "\n".join(f"{r['at']}  {r['level']:<7} {r['text']}" for r in list(cl.console)[-300:])
                   or "The page's console output appears here.")
        T.set_text("Sources", "\n\n".join(f"[{x.n}] {x.title[:90]}\n     {x.url}\n     published: "
                                          f"{x.published or 'unknown'}" for x in snap["sources"])
                   or "Pages LocalPilot reads for this task appear here.", follow=False)
        T.set_text("Security", "\n".join(f"{e['at']}  {e['kind']:<14} {e['url'][:70]}  {e['why']}"
                                         for e in snap["events"]) or "No blocked pages or warnings yet.")
        st, mem = cl.status, snap.get("memory") or {}
        tabs = "\n".join(f"   {'▶' if t.get('active') else ' '} {t.get('title', '')[:50]}  -  {t.get('url', '')[:70]}"
                         for t in cl.tabs) or "   (none)"
        T.set_text("Session", (
            f"Engine: agent-browser ({st.get('engine', 'chrome')}), headless, isolated temporary profile\n"
            f"Stream: {'connected' if cl.connected else 'not connected'}  {cl.endpoint}  "
            f"(reconnects: {cl.reconnects})\n"
            f"Viewport: {st.get('viewportWidth', '?')} x {st.get('viewportHeight', '?')}   ·   "
            f"screencasting: {st.get('screencasting', False)}\n"
            f"Frames received: {cl.frames_received}   ·   drawn here: {self.view.frames_drawn}\n"
            f"Memory: {mem.get('current_mb', '?')} / {mem.get('budget_mb', '?')} MB (hard limit)\n"
            f"Input: {'YOUR CONTROL (agent paused)' if self.controlling else 'view-only'}\n\nTabs:\n{tabs}"),
                   follow=False)
        self.footer.configure(text=f"Ads/trackers blocked: {snap['ads_blocked']}   ·   unsafe blocked: "
                                   f"{snap['security_blocked']}   ·   downloads blocked: {snap['downloads_blocked']}"
                                   f"   ·   sources: {len(snap['sources'])}")
        self.win.after(150, self._tick)


# ======================================================================= manager
class LiveViewer:
    """One entry point for the app: PiP <-> expanded, never both at once."""

    def __init__(self, root, controller, session, pal, settings: dict):
        self.pip = PipWindow(root, controller, session, pal, settings, on_expand=self.expand)
        self.big = ExpandedView(root, controller, session, pal, settings, on_collapse=self.show_pip)

    @property
    def mode(self) -> str:
        return "expanded" if self.big.visible else "pip" if self.pip.visible else "closed"

    def show_pip(self):
        if self.big.visible:
            self.big.close()
        self.pip.open()

    def expand(self):
        if self.pip.visible:
            self.pip.close()
        self.big.open()

    def toggle(self):
        if self.mode == "closed":
            self.show_pip()
        else:
            self.close()

    def close(self):
        self.pip.close() if self.pip.visible else None
        self.big.close() if self.big.visible else None
