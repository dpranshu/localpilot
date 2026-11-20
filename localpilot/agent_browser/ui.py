"""Agent Browser interface for LocalPilot's existing Tkinter app (no new dependencies).

* CompactPanel  -- small card in the main window: state, task, site, sources, security, Stop/Pause/Expand.
* Workspace     -- expandable window of the SAME app: real screenshot preview (only while visible),
                   address bar, back/forward/refresh, activity timeline, sources, security events,
                   Pause/Resume/Stop/Take control/Collapse.
* SettingsDialog -- General / Performance / Privacy & Security / Research.
* Themes        -- dark / light / system, applied by re-mapping widget colours at runtime.

The UI never enforces security and never invents activity: it shows controller.snapshot() (real
state) and calls controller / session methods. Worker threads never touch Tk widgets.
"""
from __future__ import annotations

import io
import threading
import time
import tkinter as tk
from tkinter import ttk

DARK = {"bg": "#15151c", "card": "#1d1d26", "field": "#1f1f29", "log": "#0f0f16", "fg": "#e8e8ef",
        "subtle": "#9a9aa8", "accent": "#35d07f", "warn": "#f5b83d", "err": "#ff5a52", "line": "#2a2a36",
        "pill": "#262632", "info": "#6ea8ff"}
LIGHT = {"bg": "#f4f5f8", "card": "#ffffff", "field": "#fbfbfd", "log": "#fdfdfe", "fg": "#1b1d22",
         "subtle": "#5d6170", "accent": "#178a50", "warn": "#a86a00", "err": "#c62f28", "line": "#dfe1e8",
         "pill": "#eceef3", "info": "#2563c9"}
STATE_COL = {"Idle": "subtle", "Starting": "info", "Writing": "info", "Searching": "info", "Reading": "info", "Verifying": "info", "Paused": "warn",
             "Blocked": "err", "Completed": "accent", "Stopped": "warn", "Error": "err"}
FONT = ("Segoe UI", 10)
FONT_S = ("Segoe UI", 9)
FONT_B = ("Segoe UI Semibold", 10)


def system_theme() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "dark"


def palette(name: str) -> dict:
    name = system_theme() if name == "system" else name
    return LIGHT if name == "light" else DARK


def apply_theme(root: tk.Misc, new: dict, old: dict):
    """Re-colour every tk widget from the old palette to the new one, and restyle ttk."""
    remap = {o.lower(): new[k] for k, o in old.items()}     # every palette colour is unique
    opts = ("bg", "background", "fg", "foreground", "insertbackground", "selectbackground",
            "activebackground", "activeforeground", "highlightbackground", "highlightcolor", "troughcolor")

    def walk(w):
        for o in opts:
            try:
                v = str(w.cget(o)).lower()
            except tk.TclError:
                continue
            if v in remap:
                try:
                    w.configure(**{o: remap[v]})
                except tk.TclError:
                    pass
        if isinstance(w, tk.Canvas) and hasattr(w, "_lp_redraw"):
            w._lp_redraw()
        for c in w.winfo_children():
            walk(c)
    walk(root)
    st = ttk.Style(root)
    st.configure(".", background=new["bg"], foreground=new["fg"])
    for s in ("TLabel", "TCheckbutton", "TRadiobutton", "TFrame", "TNotebook"):
        st.configure(s, background=new["bg"], foreground=new["fg"])
    st.configure("Sub.TLabel", background=new["bg"], foreground=new["subtle"])
    st.configure("TNotebook.Tab", background=new["pill"], foreground=new["fg"], padding=(10, 4))
    st.map("TNotebook.Tab", background=[("selected", new["card"])])
    st.configure("Treeview", background=new["log"], fieldbackground=new["log"], foreground=new["fg"],
                 bordercolor=new["line"], rowheight=22)
    st.configure("Treeview.Heading", background=new["pill"], foreground=new["fg"])
    st.configure("LP.TButton", padding=(10, 4))
    st.configure("LPStop.TButton", padding=(10, 4), foreground=new["err"])
    st.configure("TCombobox", fieldbackground=new["field"], foreground=new["fg"])


class Card(tk.Canvas):
    """A rounded card: draws its own rounded background, hosts a Frame."""

    def __init__(self, master, pal, radius=12, pad=10, **kw):
        super().__init__(master, highlightthickness=0, bd=0, bg=pal["bg"], **kw)
        self.pal, self.radius, self.pad = pal, radius, pad
        self.inner = tk.Frame(self, bg=pal["card"])
        self._win = self.create_window(pad, pad, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._fit)
        self.bind("<Configure>", self._redraw)

    def _fit(self, _=None):
        self.configure(height=self.inner.winfo_reqheight() + 2 * self.pad)
        self._redraw()

    def _lp_redraw(self):
        self.pal = {"card": self.inner.cget("bg")}
        self._redraw()

    def _redraw(self, _=None):
        w, h, r = self.winfo_width(), self.winfo_height(), self.radius
        self.delete("bgshape")
        col = self.inner.cget("bg")
        if w < 4 or h < 4:
            return
        for x0, y0, x1, y1, st in ((0, 0, 2 * r, 2 * r, 90), (w - 2 * r, 0, w, 2 * r, 0),
                                   (0, h - 2 * r, 2 * r, h, 180), (w - 2 * r, h - 2 * r, w, h, 270)):
            self.create_arc(x0, y0, x1, y1, start=st, extent=90, fill=col, outline=col, tags="bgshape")
        self.create_rectangle(r, 0, w - r, h, fill=col, outline=col, tags="bgshape")
        self.create_rectangle(0, r, w, h - r, fill=col, outline=col, tags="bgshape")
        self.tag_lower("bgshape")
        self.itemconfigure(self._win, width=max(10, w - 2 * self.pad))


# ======================================================================= compact panel
class CompactPanel:
    def __init__(self, master, controller, session, pal, open_workspace, open_settings):
        self.c, self.session, self.pal = controller, session, pal
        self.open_workspace, self.open_settings = open_workspace, open_settings
        self.card = Card(master, pal)
        f = self.card.inner
        top = tk.Frame(f, bg=pal["card"]); top.pack(fill="x")
        self.dot = tk.Canvas(top, width=12, height=12, bg=pal["card"], highlightthickness=0)
        self.dot.pack(side="left", padx=(2, 6))
        tk.Label(top, text="Agent Browser", bg=pal["card"], fg=pal["fg"], font=FONT_B).pack(side="left")
        self.state = tk.Label(top, text="Idle", bg=pal["card"], fg=pal["subtle"], font=FONT_S)
        self.state.pack(side="left", padx=8)
        self.shield = tk.Label(top, text="", bg=pal["card"], fg=pal["accent"], font=FONT_S)
        self.shield.pack(side="left", padx=4)
        self.btn_expand = ttk.Button(top, text="Expand  ⤢", style="LP.TButton", command=open_workspace)
        self.btn_expand.pack(side="right")
        self.btn_stop = ttk.Button(top, text="■ Stop", style="LPStop.TButton", command=self.stop)
        self.btn_pause = ttk.Button(top, text="⏸ Pause", style="LP.TButton", command=self.pause_resume)
        ttk.Button(top, text="⚙", width=3, style="LP.TButton", command=open_settings).pack(side="right", padx=4)
        self.task = tk.Label(f, text="No browser task. Chromium starts only when needed.", bg=pal["card"],
                             fg=pal["fg"], font=FONT_S, anchor="w")
        self.task.pack(fill="x", pady=(4, 0))
        self.info = tk.Label(f, text="", bg=pal["card"], fg=pal["subtle"], font=FONT_S, anchor="w")
        self.info.pack(fill="x")
        self.doing = tk.Label(f, text="", bg=pal["card"], fg=pal["subtle"], font=FONT_S, anchor="w")
        self.doing.pack(fill="x")
        self.warn = tk.Label(f, text="", bg=pal["card"], fg=pal["warn"], font=FONT_S, anchor="w")
        self._last = None
        for w, key in ((self.btn_expand, "Expand the Agent Browser (Ctrl+Shift+B)"),
                       (self.btn_stop, "Stop the browser task now (Esc)")):
            w.bind("<Enter>", lambda e, k=key: self.info.configure(text=k))

    def pack(self, **kw):
        self.card.pack(**kw)

    def stop(self):
        self.session.interrupt("stop")

    def pause_resume(self):
        if self.c.hold_event.is_set():
            self.session.resume()
        else:
            self.session.hold()

    def refresh(self, snap: dict, pal: dict):
        key = (snap["state"], snap["task"], snap["url"], len(snap["sources"]), snap["security_blocked"],
               snap["ads_blocked"], snap["busy"], snap["threats"].get("warning"), id(pal), snap.get("action"),
               snap.get("detail"))
        if key == self._last:
            return
        self._last = key
        col = pal[STATE_COL.get(snap["state"], "subtle")]
        self.dot.delete("all"); self.dot.configure(bg=pal["card"])
        self.dot.create_oval(1, 1, 11, 11, fill=col, outline=col)
        self.state.configure(text=snap["state"], fg=col)
        th = snap["threats"]
        self.shield.configure(text="🛡 protected" if th["phishing"] == "active" else "⚠ limited protection",
                              fg=pal["accent"] if th["phishing"] == "active" else pal["warn"])
        self.task.configure(text=(snap["task"] or "No browser task. Chromium starts only when needed.")[:110])
        bits = []
        if snap["domain"]:
            bits.append(snap["domain"])
        bits.append(f"{len(snap['sources'])} sources")
        bits.append(f"{snap['ads_blocked']} ads/trackers blocked")
        if snap["security_blocked"]:
            bits.append(f"⚠ {snap['security_blocked']} unsafe blocked")
        self.info.configure(text="  ·  ".join(bits))
        doing = snap.get("detail") or ""
        if snap["busy"] and snap.get("action"):
            doing = f"Now: {snap['action']}" + (f"  -  {doing}" if doing else "")
        self.doing.configure(text=doing[:120])
        if th.get("warning"):
            self.warn.configure(text="⚠ " + th["warning"]); self.warn.pack(fill="x")
        else:
            self.warn.pack_forget()
        active = snap["busy"]
        for b, show in ((self.btn_stop, active), (self.btn_pause, active)):
            if show and not b.winfo_ismapped():
                b.pack(side="right", padx=4, before=self.btn_expand)
            elif not show and b.winfo_ismapped():
                b.pack_forget()
        self.btn_pause.configure(text="▶ Resume" if snap["state"] == "Paused" else "⏸ Pause")


# ======================================================================= workspace
class Workspace:
    def __init__(self, root, controller, session, pal, settings, open_visible):
        self.root, self.c, self.session, self.pal, self.settings = root, controller, session, pal, settings
        self.open_visible = open_visible
        self.win = None
        self._photo = None
        self._frame_bytes = None
        self._fetching = False
        self._last_frame_at = 0.0
        self.frames_shown = 0

    @property
    def visible(self) -> bool:
        return bool(self.win and self.win.winfo_exists() and self.win.state() == "normal"
                    and self.win.winfo_viewable())

    def toggle(self):
        if self.win and self.win.winfo_exists():
            self.close()
        else:
            self.open()

    def open(self):
        pal = self.pal
        w = self.win = tk.Toplevel(self.root)
        w.title("LocalPilot - Agent Browser"); w.configure(bg=pal["bg"]); w.geometry("1100x700")
        w.minsize(720, 460)
        w.protocol("WM_DELETE_WINDOW", self.close)
        bar = tk.Frame(w, bg=pal["bg"]); bar.pack(fill="x", padx=10, pady=(10, 4))
        for txt, step, tip in (("←", -1, "Back"), ("→", 1, "Forward"), ("⟳", 0, "Refresh")):
            b = ttk.Button(bar, text=txt, width=3, style="LP.TButton", command=lambda s=step: self._hist(s))
            b.pack(side="left", padx=(0, 4))
        self.addr = tk.Entry(bar, bg=pal["field"], fg=pal["fg"], insertbackground=pal["fg"], relief="flat",
                             font=("Segoe UI", 10))
        self.addr.pack(side="left", fill="x", expand=True, ipady=5, padx=6)
        self.addr.bind("<Return>", lambda e: self._go())
        self.pill = tk.Label(bar, text="Idle", bg=pal["pill"], fg=pal["subtle"], font=FONT_S, padx=10, pady=3)
        self.pill.pack(side="left", padx=6)
        self.b_pause = ttk.Button(bar, text="⏸ Pause", style="LP.TButton", command=self._pause_resume)
        self.b_pause.pack(side="left", padx=2)
        ttk.Button(bar, text="■ Stop", style="LPStop.TButton",
                   command=lambda: self.session.interrupt("stop")).pack(side="left", padx=2)
        ttk.Button(bar, text="Minimize ⤡", style="LP.TButton", command=self.close).pack(side="left", padx=(2, 0))
        body = tk.PanedWindow(w, orient="horizontal", bg=pal["bg"], sashwidth=6, bd=0)
        body.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        left = tk.Frame(body, bg=pal["log"])
        self.canvas = tk.Canvas(left, bg=pal["log"], highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.caption = tk.Label(left, text="", bg=pal["log"], fg=pal["subtle"], font=FONT_S, anchor="w")
        self.caption.pack(fill="x")
        body.add(left, minsize=360, stretch="always")
        nb = ttk.Notebook(body)
        self.t_activity = self._tree(nb, ("time", "state", "detail"), (70, 90, 300))
        self.t_sources = self._tree(nb, ("n", "title", "site", "date", "via"), (30, 220, 120, 90, 50))
        self.t_security = self._tree(nb, ("time", "kind", "where", "why"), (70, 110, 150, 220))
        self.t_pages = self._tree(nb, ("url",), (480,))
        for tree, name in ((self.t_activity, "Activity"), (self.t_sources, "Sources"),
                           (self.t_security, "Security"), (self.t_pages, "Pages")):
            nb.add(tree.master, text=name)
        self.t_sources.bind("<<TreeviewSelect>>", lambda e: self._show_source())
        self.src_detail = tk.Label(self.t_sources.master, text="", bg=pal["bg"], fg=pal["subtle"], font=FONT_S,
                                   justify="left", anchor="nw", wraplength=420)
        self.src_detail.pack(fill="x", padx=4, pady=4)
        body.add(nb, minsize=320)
        self.sec_status = tk.Label(self.t_security.master, text="", bg=pal["bg"], fg=pal["subtle"], font=FONT_S,
                                   justify="left", anchor="w", wraplength=440)
        self.sec_status.pack(fill="x", padx=4, pady=4)
        w.bind("<Escape>", lambda e: self.session.interrupt("stop"))
        w.bind("<Control-l>", lambda e: (self.addr.focus_set(), self.addr.select_range(0, "end")))
        self._last_counts = (-1, -1, -1, -1)
        self._tick()

    def _tree(self, nb, cols, widths):
        holder = tk.Frame(nb, bg=self.pal["bg"])
        t = ttk.Treeview(holder, columns=cols, show="headings", height=12)
        for c, wd in zip(cols, widths):
            t.heading(c, text=c.title()); t.column(c, width=wd, anchor="w", stretch=c in ("detail", "title", "why", "url"))
        t.pack(fill="both", expand=True)
        return t

    def close(self):
        if self.win and self.win.winfo_exists():
            self.win.destroy()
        self.win = None
        self._photo = None                    # frames live only in memory, dropped on collapse

    def _go(self):
        url = self.addr.get().strip()
        if url:
            threading.Thread(target=self.c.navigate, args=(url,), daemon=True).start()

    def _hist(self, step):
        threading.Thread(target=self.c.history, args=(step,), daemon=True).start()

    def _pause_resume(self):
        self.session.resume() if self.c.hold_event.is_set() else self.session.hold()

    def _show_source(self):
        sel = self.t_sources.selection()
        if not sel:
            return
        n = int(self.t_sources.item(sel[0])["values"][0])
        s = next((x for x in self.c.snapshot()["sources"] if x.n == n), None)
        if s:
            self.src_detail.configure(text=f"[{s.n}] {s.title}\n{s.url}\nPublished: {s.published or 'unknown'}"
                                           f" ({s.published_source or 'no metadata'}) · Retrieved: {s.retrieved_at}"
                                           f"\n{'; '.join(s.flags) or 'No flags'}\n\n{s.excerpt}")

    # ---------------------------------------------------------------- refresh + preview
    def _tick(self):
        if not (self.win and self.win.winfo_exists()):
            return
        snap = self.c.snapshot()
        pal = self.pal
        col = pal[STATE_COL.get(snap["state"], "subtle")]
        self.pill.configure(text=snap["state"], fg=col)
        self.b_pause.configure(text="▶ Resume" if snap["state"] == "Paused" else "⏸ Pause")
        if self.root.focus_get() is not self.addr:
            self.addr.delete(0, "end"); self.addr.insert(0, snap["url"])
        counts = (len(snap["timeline"]), len(snap["sources"]), len(snap["events"]), len(snap["visited"]))
        if counts != self._last_counts:
            self._last_counts = counts
            self._fill(self.t_activity, [(e["at"], e["state"], e["detail"]) for e in snap["timeline"][-200:]])
            self._fill(self.t_sources, [(s.n, s.title[:80], s.site, s.published or "unknown", s.method)
                                        for s in snap["sources"]])
            self._fill(self.t_security, [(e["at"], e["kind"], e["url"][:60], e["why"]) for e in snap["events"]])
            self._fill(self.t_pages, [(u,) for u in snap["visited"]])
        th = snap["threats"]
        mem = snap["memory"] or {}
        self.sec_status.configure(
            text=f"Phishing protection: {th['phishing']}"
                 + (f" ({th['feeds']['phishing'].get('entries', 0):,} domains, {th['feeds']['phishing'].get('age_hours', '?')} h old)"
                    if th["phishing"] != "unavailable" else "") + f"   ·   Malware intelligence: {th['malware']}\n"
                 f"Ads/trackers blocked: {snap['ads_blocked']}   ·   Downloads blocked: {snap['downloads_blocked']}"
                 f"   ·   Unsafe blocked: {snap['security_blocked']}\n"
                 f"Browser: {'running' if snap['browser_running'] else 'not running'} ({snap['profile']})"
                 + (f"   ·   memory {mem.get('current_mb')} / {mem.get('budget_mb')} MB" if mem.get("enforced") else ""),
            fg=pal["warn"] if th.get("warning") else pal["subtle"])
        self._preview(snap)
        self.win.after(250, self._tick)

    @staticmethod
    def _fill(tree, rows):
        tree.delete(*tree.get_children())
        for r in rows[-300:]:
            tree.insert("", "end", values=r)

    def _preview(self, snap):
        fps = max(0.2, min(2.0, float(self.settings.get("preview_fps", 1.0))))
        if not self.visible:
            return                                     # nothing captured while hidden / minimized
        if self._frame_bytes is not None:
            data, self._frame_bytes = self._frame_bytes, None
            try:
                from PIL import Image, ImageTk
                img = Image.open(io.BytesIO(data))
                cw, ch = max(50, self.canvas.winfo_width()), max(50, self.canvas.winfo_height())
                img.thumbnail((cw, ch))
                self._photo = ImageTk.PhotoImage(img)
                self.canvas.delete("all")
                self.canvas.create_image(cw // 2, ch // 2, image=self._photo)
                self.frames_shown += 1
                act = (self.c.snapshot().get("action") or "")[:80]
                self.caption.configure(text=f"Live view of the agent's browser (real screenshot) · "
                                            f"{time.strftime('%H:%M:%S')}" + (f" · now: {act}" if act else ""))
            except Exception:
                pass
        if not snap["browser_running"]:
            self.canvas.delete("all"); self._photo = None
            self.canvas.create_text(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                                    text="No live view: the research browser isn't running.\n"
                                         "It starts only when LocalPilot needs to browse.",
                                    fill=self.pal["subtle"], font=FONT, justify="center")
            self.caption.configure(text="")
            return
        if not self._fetching and time.time() - self._last_frame_at >= 1.0 / fps:
            self._fetching, self._last_frame_at = True, time.time()

            def grab():
                try:
                    self._frame_bytes = self.c.preview_frame()     # None when the browser is busy
                finally:
                    self._fetching = False
            threading.Thread(target=grab, daemon=True).start()


# ======================================================================= settings
SETTINGS_DEFAULTS = {"theme": "dark", "start_minimized": False, "auto_close_idle": True, "profile": "balanced",
                     "max_tabs": 2, "memory_mb": 1100, "preview_fps": 1.0, "preview_enabled": True,
                     "research_mode": "quick", "max_pages": 15, "task_timeout_s": 180}


class SettingsDialog:
    def __init__(self, root, values: dict, pal: dict, on_save, controller):
        self.v = dict(SETTINGS_DEFAULTS, **values)
        self.on_save, self.c, self.pal = on_save, controller, pal
        w = self.win = tk.Toplevel(root)
        w.title("Agent Browser settings"); w.configure(bg=pal["bg"]); w.geometry("520x470"); w.resizable(False, False)
        nb = ttk.Notebook(w); nb.pack(fill="both", expand=True, padx=10, pady=10)
        self.vars = {}

        def page(name):
            f = tk.Frame(nb, bg=pal["bg"]); nb.add(f, text=name); return f

        def row(f, label, widget):
            r = tk.Frame(f, bg=pal["bg"]); r.pack(fill="x", padx=12, pady=6)
            tk.Label(r, text=label, bg=pal["bg"], fg=pal["fg"], font=FONT, anchor="w", width=30).pack(side="left")
            widget(r).pack(side="left")

        def check(key):
            self.vars[key] = tk.BooleanVar(value=bool(self.v[key]))
            return lambda r: ttk.Checkbutton(r, variable=self.vars[key])

        def combo(key, values):
            self.vars[key] = tk.StringVar(value=str(self.v[key]))
            return lambda r: ttk.Combobox(r, textvariable=self.vars[key], values=values, state="readonly", width=14)

        def spin(key, lo, hi, inc=1):
            self.vars[key] = tk.StringVar(value=str(self.v[key]))
            return lambda r: tk.Spinbox(r, from_=lo, to=hi, increment=inc, textvariable=self.vars[key], width=8,
                                        bg=pal["field"], fg=pal["fg"], relief="flat", buttonbackground=pal["field"])

        def locked(text, ok=True):
            return lambda r: tk.Label(r, text=text, bg=pal["bg"], fg=pal["accent"] if ok else pal["warn"], font=FONT_S)

        g = page("Browser")
        row(g, "Headless browsing", locked("Always (watch it in the preview)"))
        row(g, "Close idle browser automatically", check("auto_close_idle"))
        row(g, "Live preview", check("preview_enabled"))
        row(g, "Preview update rate (FPS)", spin("preview_fps", 0.5, 2.0, 0.5))
        row(g, "Theme", combo("theme", ["system", "dark", "light"]))
        p = page("Performance")
        row(p, "Profile", combo("profile", ["eco", "balanced", "performance"]))
        row(p, "Maximum active tabs", spin("max_tabs", 1, 3))
        row(p, "Browser memory budget (MB)", spin("memory_mb", 500, 2500, 100))
        tk.Label(p, text="Eco: 1 tab, no images, 700 MB · Balanced: 2 tabs, 1100 MB · Performance: 3 tabs, 1700 MB",
                 bg=pal["bg"], fg=pal["subtle"], font=FONT_S, wraplength=470, justify="left").pack(padx=12, anchor="w")
        s = page("Privacy & Security")
        th = controller.threats.status()
        row(s, "Ad and tracker blocking", locked("Always on"))
        row(s, "Phishing protection", locked({"active": "On (list current)", "stale": "On - list OUT OF DATE",
                                              "unavailable": "UNAVAILABLE - list not downloaded"}[th["phishing"]],
                                             th["phishing"] == "active"))
        row(s, "Malware URL intelligence", locked("Unavailable (no free keyless source)", False))
        row(s, "Downloads", locked("Blocked"))
        row(s, "Website permissions", locked("Denied (camera, mic, location, clipboard...)"))
        row(s, "Forms, logins, purchases, uploads", locked("Never (links only; nothing is typed)"))
        row(s, "Private network / localhost", locked("Blocked (incl. redirects, DNS rebinding)"))
        btns = tk.Frame(s, bg=pal["bg"]); btns.pack(fill="x", padx=12, pady=8)
        ttk.Button(btns, text="Update phishing list now", style="LP.TButton", command=self._update_threats).pack(side="left")
        ttk.Button(btns, text="Clear temporary browser data", style="LP.TButton", command=self._clear).pack(side="left", padx=6)
        self.msg = tk.Label(s, text="", bg=pal["bg"], fg=pal["subtle"], font=FONT_S, wraplength=470, justify="left")
        self.msg.pack(padx=12, anchor="w")
        rs = page("Research")
        row(rs, "Default mode", combo("research_mode", ["quick", "research", "deep"]))
        row(rs, "Maximum sources / pages", spin("max_pages", 3, 40))
        row(rs, "Maximum research time (seconds)", spin("task_timeout_s", 30, 900, 30))
        row(rs, "Citations", locked("Always required (only pages actually visited)"))
        tk.Label(rs, text="Quick: 1 search, ~3 pages · Research: 2-3 searches, ~6 pages, notes per source · "
                          "Deep: sub-questions, gap-filling, contradictions, a Markdown report in "
                          "Documents/LocalPilot Research.", bg=pal["bg"], fg=pal["subtle"], font=FONT_S,
                 wraplength=470, justify="left").pack(padx=12, anchor="w")
        bar = tk.Frame(w, bg=pal["bg"]); bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bar, text="Save", style="LP.TButton", command=self.save).pack(side="right")
        ttk.Button(bar, text="Cancel", style="LP.TButton", command=w.destroy).pack(side="right", padx=6)
        w.bind("<Escape>", lambda e: w.destroy())

    def values(self) -> dict:
        out = dict(self.v)
        for k, var in self.vars.items():
            val = var.get()
            d = SETTINGS_DEFAULTS[k]
            try:
                out[k] = bool(val) if isinstance(d, bool) else type(d)(float(val)) if isinstance(d, (int, float)) else val
            except (TypeError, ValueError):
                out[k] = d
        out["max_tabs"] = max(1, min(3, int(out["max_tabs"])))
        out["memory_mb"] = max(500, min(2500, int(out["memory_mb"])))
        out["preview_fps"] = max(0.5, min(2.0, float(out["preview_fps"])))
        out["max_pages"] = max(3, min(40, int(out["max_pages"])))
        out["task_timeout_s"] = max(30, min(900, int(out["task_timeout_s"])))
        return out

    def save(self):
        self.on_save(self.values())
        self.win.destroy()

    def _update_threats(self):
        self.msg.configure(text="Updating...")

        def go():
            r = self.c.threats.update(force=True)
            self.msg.after(0, lambda: self.msg.configure(text=f"Phishing list: {r.get('phishing')}"))
        threading.Thread(target=go, daemon=True).start()

    def _clear(self):
        self.c.close_browser()
        with self.c._lock:
            self.c.sources, self.c.events, self.c.timeline, self.c.visited = [], [], [], []
        self.msg.configure(text="Browser closed; its temporary profile, cookies and cache were deleted.")
