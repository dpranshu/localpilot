"""LocalPilot desktop control panel.

A window to run tasks, switch backend, toggle options, watch a live log, and
Stop a running task -- no terminal needed.

    python -m localpilot.gui

The agent runs on a worker thread; Stop sets a software stop-event that the loop
checks each step. The window minimizes itself while a task runs so it isn't the
window the agent acts on (focus your target during the 3s countdown).
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from .config import Config
from .agent import UnfinishedRun, run as run_agent
from . import llm

try:
    from PIL import ImageTk
    from .dotsphere import DotSphere
    _HAS_ORB = True
except Exception:
    _HAS_ORB = False

BG = "#15151c"
FG = "#e8e8ef"
ACCENT = "#35d07f"
SUBTLE = "#9a9aa8"
OK_COL = "#35d07f"
ERR_COL = "#ff5a52"


def _fmt(rec: dict) -> str | None:
    e = rec.get("event")
    if e == "step":
        return f"— step {rec.get('step')} [{rec.get('mode','uia')}] {rec.get('title','')[:50]}"
    if e == "action":
        a = rec.get("action")
        return f"    {a if a else ''}  ->  {rec.get('result','')}"[:200]
    if e == "decide":
        a = rec.get("action", {})
        return f"    vision: {a.get('action')} {a.get('xy','') if isinstance(a,dict) else ''}"
    if e == "error":
        return f"    ! {rec.get('message','')[:160]}"
    if e == "end":
        return f"=== finished: {rec.get('final','')[:80]} ==="
    if e == "ping":
        return rec.get("message", "")
    if e == "_final":
        if rec.get("quiet"):                     # brain mode: the reply was already shown
            return None
        return f"=== {rec.get('final','')[:120]} ==="
    if e == "_you":
        return f"\nYou:  {rec.get('text','')}"
    if e == "_plan":
        p = rec.get("plan", {})
        out = f"  (thinking) {p.get('thought','')}" if p.get("thought") else ""
        if p.get("steps"):
            out += "\n  Plan:\n" + "\n".join(f"    {i}. {s}" for i, s in enumerate(p["steps"], 1))
        return out or None
    if e == "_say":
        return f"LocalPilot:  {rec.get('text','')}"
    if e == "_step":
        return f"  > step {rec.get('i')}/{rec.get('n')}: {rec.get('step','')}"
    if e == "_step_done":
        return f"    {'OK' if rec.get('ok') else 'X'}  {rec.get('final','')[:110]}"
    return None


class App:
    def __init__(self):
        self.cfg = Config()
        from . import settings as _settings
        _settings.load_into(self.cfg)           # remember the user's choices
        self.stop_event = threading.Event()
        self.cfg.stop_event = self.stop_event
        self.logq: "queue.Queue[dict]" = queue.Queue()
        self.cfg.log_callback = self.logq.put
        from .memory import Memory
        self.memory = Memory(self.cfg.memory_path or None)
        from .session import Session
        self.session = Session(self.cfg, self.memory, self._session_event)
        self.voice = None                              # created on first use (loads models)
        self._hist_win = None
        self._hist_runs = []
        self.running = False
        self._orb_state = "idle"
        self.orb = DotSphere(diameter=72, margin=6) if _HAS_ORB else None
        self._orb_photo = None
        self._orb_frame = None
        self._orb_idle_timer = None
        self.cfg.open_app_callback = lambda: self.logq.put({"event": "_show"})
        self._build()

    # ---------- UI ----------
    def _build(self):
        self.root = tk.Tk()
        from . import __version__
        self.root.title(f"LocalPilot {__version__}")
        self.root.configure(bg=BG)
        self.root.geometry("600x600")
        self.root.minsize(520, 480)

        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Sub.TLabel", background=BG, foreground=SUBTLE)
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.configure("TButton", padding=6)
        st.configure("Accent.TButton", foreground="#07110a")
        st.map("Accent.TButton", background=[("!disabled", ACCENT)])

        try:
            import os
            from .config import resource_path
            ico = resource_path("localpilot.ico")
            if os.path.exists(ico):
                self.root.iconbitmap(ico)
        except Exception:
            pass

        pad = {"padx": 10, "pady": 6}
        # header: live orb + title
        hf = tk.Frame(self.root, bg=BG)
        hf.pack(fill="x", padx=10, pady=(10, 2))
        self.orb_label = tk.Label(hf, bg=BG, bd=0)
        self.orb_label.pack(side="left", padx=(0, 10))
        tcol = tk.Frame(hf, bg=BG)
        tcol.pack(side="left", anchor="w")
        tk.Label(tcol, text="LocalPilot", bg=BG, fg=FG,
                 font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(tcol, text="local AI desktop agent", bg=BG, fg=SUBTLE,
                 font=("Segoe UI", 9)).pack(anchor="w")

        # task row
        tf = tk.Frame(self.root, bg=BG)
        tf.pack(fill="x", padx=10)
        ttk.Label(tf, text="Ask").pack(side="left")
        self.task = tk.Entry(tf, bg="#1f1f29", fg=FG, insertbackground=FG,
                             relief="flat", font=("Segoe UI", 11))
        self.task.pack(side="left", fill="x", expand=True, padx=8, ipady=5)
        self.task.bind("<Return>", lambda e: self.on_run())
        self._ph = "Ask or tell me anything -- e.g. I'm bored / what's the weather / set me up for work"
        self._ph_on = False
        self._show_ph()
        self.task.bind("<FocusIn>", lambda e: self._clear_ph())
        self.task.bind("<FocusOut>", lambda e: self._show_ph())
        self.mic_btn = ttk.Button(tf, text="\U0001F3A4", width=3, command=self.on_mic)
        self.mic_btn.pack(side="left", padx=(0, 4))
        self.run_btn = ttk.Button(tf, text="Go", style="Accent.TButton", command=self.on_run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(tf, text="Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        # settings
        sf = tk.Frame(self.root, bg=BG)
        sf.pack(fill="x", **pad)
        ttk.Label(sf, text="Backend").grid(row=0, column=0, sticky="w")
        self.backend = tk.StringVar(value=self.cfg.backend)
        ttk.OptionMenu(sf, self.backend, self.cfg.backend, "uia", "vision", "hybrid").grid(
            row=0, column=1, sticky="w", padx=6)
        ttk.Label(sf, text="Model").grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.model = tk.Entry(sf, bg="#1f1f29", fg=FG, insertbackground=FG, relief="flat", width=26)
        self.model.insert(0, self.cfg.model)
        sf.columnconfigure(3, weight=1)
        self.model.grid(row=0, column=3, sticky="ew", padx=6, ipady=3)
        ttk.Label(sf, text="Max steps").grid(row=0, column=4, sticky="w", padx=(16, 0))
        self.steps = tk.Spinbox(sf, from_=1, to=200, width=5, bg="#1f1f29", fg=FG,
                                relief="flat", buttonbackground="#1f1f29")
        self.steps.delete(0, "end"); self.steps.insert(0, str(self.cfg.max_steps))
        self.steps.grid(row=0, column=5, sticky="w", padx=6)
        from .voice import KOKORO_VOICES
        ttk.Label(sf, text="Voice").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._voice_names = {v: k for k, v in KOKORO_VOICES.items()}
        self.voice_pick = ttk.Combobox(sf, values=list(KOKORO_VOICES.values()), state="readonly", width=18)
        self.voice_pick.set(KOKORO_VOICES.get(self.cfg.tts_voice, "Heart (US, warm)"))
        self.voice_pick.grid(row=1, column=1, columnspan=2, sticky="w", padx=6, pady=(6, 0))
        self.voice_pick.bind("<<ComboboxSelected>>", lambda e: self._voice_changed())

        # toggles
        tg = tk.Frame(self.root, bg=BG)
        tg.pack(fill="x", padx=10)
        self.v_confirm = tk.BooleanVar(value=self.cfg.confirm_each_step)
        self.v_cursor = tk.BooleanVar(value=self.cfg.show_cursor)
        self.v_overlay = tk.BooleanVar(value=self.cfg.show_overlay)
        self.v_dry = tk.BooleanVar(value=self.cfg.dry_run)
        self.v_real = tk.BooleanVar(value=self.cfg.real_cursor)
        self.v_harness = tk.BooleanVar(value=self.cfg.harness_enabled)
        self.v_mem = tk.BooleanVar(value=self.cfg.use_memory)
        self.v_think = tk.BooleanVar(value=self.cfg.think_first)
        self.v_speak = tk.BooleanVar(value=self.cfg.speak_replies)
        self.v_handsfree = tk.BooleanVar(value=False)
        toggles = [("Think first", self.v_think), ("Speak replies", self.v_speak),
                   ("Hands-free", self.v_handsfree),
                   ("Ask before risky actions", self.v_harness), ("Use my history", self.v_mem),
                   ("Confirm each step", self.v_confirm), ("Dry run", self.v_dry),
                   ("Agent cursor", self.v_cursor), ("Overlay", self.v_overlay),
                   ("Real cursor", self.v_real)]
        # grid, 4 per row: a single packed row overflowed and hid the last toggles
        self.v_handsfree.trace_add("write", lambda *a: self.root.after(10, self._sync_handsfree))
        for i, (text, var) in enumerate(toggles):
            ttk.Checkbutton(tg, text=text, variable=var).grid(
                row=i // 4, column=i % 4, sticky="w", padx=(0, 14), pady=1)

        # status (tk.Label so we can color it)
        self.status = tk.Label(self.root, text="Ready.", bg=BG, fg=SUBTLE,
                               font=("Segoe UI", 10), anchor="w")
        self.status.pack(fill="x", padx=10, pady=(4, 0))
        self._build_browser_panel()

        # log
        lf = tk.Frame(self.root, bg=BG)
        self.log = tk.Text(lf, bg="#0f0f16", fg=FG, relief="flat", wrap="word",
                           font=("Consolas", 9), state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(lf, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)

        # bottom
        bf = tk.Frame(self.root, bg=BG)
        bf.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        ttk.Button(bf, text="Check setup", command=self.on_check).pack(side="left")
        self.fix_btn = ttk.Button(bf, text="Start AI engine", command=self.on_start_engine)
        ttk.Button(bf, text="Clear log", command=self.clear_log).pack(side="left", padx=6)
        ttk.Button(bf, text="History…", command=self.open_history).pack(side="left")
        ttk.Button(bf, text="My context…", command=self.open_context).pack(side="left", padx=6)
        self._quit_btn = ttk.Button(bf, text="Quit", command=self.on_quit)
        self._quit_btn.pack(side="right")
        lf.pack(fill="both", expand=True, padx=10, pady=8)

        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)
        self.root.bind("<Escape>", lambda e: self.on_stop())
        self.root.after(120, self._drain)
        self.root.after(500, self.on_check)     # first-run / startup health check

    # ---------- helpers ----------
    def _show_ph(self):
        if not self.task.get():
            self._ph_on = True
            self.task.insert(0, self._ph)
            self.task.config(fg=SUBTLE)

    def _clear_ph(self):
        if self._ph_on:
            self._ph_on = False
            self.task.delete(0, "end")
            self.task.config(fg=FG)

    def task_text(self):
        return "" if self._ph_on else self.task.get().strip()

    def set_status(self, text, kind="info"):
        col = {"ok": OK_COL, "err": ERR_COL, "run": FG, "info": SUBTLE}.get(kind, SUBTLE)
        self.status.config(text=text[:120], fg=col)

    def append(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def clear_log(self):
        self.log.config(state="normal"); self.log.delete("1.0", "end"); self.log.config(state="disabled")

    def _apply(self):
        self.cfg.backend = self.backend.get()
        self.cfg.model = self.model.get().strip() or self.cfg.model
        try:
            self.cfg.max_steps = int(self.steps.get())
        except ValueError:
            pass
        self.cfg.confirm_each_step = self.v_confirm.get()
        self.cfg.show_cursor = self.v_cursor.get()
        self.cfg.show_overlay = self.v_overlay.get()
        self.cfg.dry_run = self.v_dry.get()
        self.cfg.real_cursor = self.v_real.get()
        self.cfg.harness_enabled = self.v_harness.get()
        self.cfg.use_memory = self.v_mem.get()
        self.cfg.think_first = self.v_think.get()
        self.cfg.speak_replies = self.v_speak.get()
        if self.voice is not None:
            self.voice.speak_enabled = self.cfg.speak_replies

    # ---------- history & personal context ----------
    def open_history(self):
        if self._hist_win is not None and self._hist_win.winfo_exists():
            self._hist_win.lift(); self.refresh_history(); return
        w = tk.Toplevel(self.root); w.title("LocalPilot - History"); w.configure(bg=BG)
        w.geometry("760x460"); self._hist_win = w
        tk.Label(w, text="Your task history (stored only on this PC). Similar past tasks are "
                 "used as context.", bg=BG, fg=SUBTLE, anchor="w").pack(fill="x", padx=10, pady=(8, 2))
        body = tk.Frame(w, bg=BG); body.pack(fill="both", expand=True, padx=10, pady=4)
        self.hist_list = tk.Listbox(body, bg="#0f0f16", fg=FG, selectbackground="#2b4a7a",
                                    relief="flat", font=("Consolas", 9), width=52, activestyle="none")
        self.hist_list.pack(side="left", fill="y")
        self.hist_list.bind("<<ListboxSelect>>", lambda e: self._show_run())
        self.hist_list.bind("<Double-Button-1>", lambda e: self.run_again())
        self.hist_detail = tk.Text(body, bg="#0f0f16", fg=FG, relief="flat", wrap="word",
                                   font=("Consolas", 9), state="disabled")
        self.hist_detail.pack(side="left", fill="both", expand=True, padx=(8, 0))
        bar = tk.Frame(w, bg=BG); bar.pack(fill="x", padx=10, pady=(2, 10))
        ttk.Button(bar, text="Run again", command=self.run_again).pack(side="left")
        ttk.Button(bar, text="Delete", command=self.delete_run).pack(side="left", padx=6)
        ttk.Button(bar, text="Clear all", command=self.clear_history).pack(side="left")
        self.hist_count = tk.Label(bar, bg=BG, fg=SUBTLE); self.hist_count.pack(side="right")
        self.refresh_history()

    def refresh_history(self):
        if self._hist_win is None or not self._hist_win.winfo_exists():
            return
        self._hist_runs = self.memory.runs()
        self.hist_list.delete(0, "end")
        mark = {"success": "OK ", "stopped": "-- ", "failed": "X  "}
        for r in self._hist_runs:
            self.hist_list.insert("end", f"{mark.get(r['outcome'], '?  ')}{r['ts'][5:]}  {r['goal'][:40]}")
        ok = sum(r["outcome"] == "success" for r in self._hist_runs)
        self.hist_count.config(text=f"{len(self._hist_runs)} tasks, {ok} succeeded")
        self._set_detail("Select a task to see its steps. Double-click to run it again.")

    def _selected_run(self):
        sel = self.hist_list.curselection() if self._hist_win else ()
        return self._hist_runs[sel[0]] if sel else None

    def _set_detail(self, text):
        self.hist_detail.config(state="normal"); self.hist_detail.delete("1.0", "end")
        self.hist_detail.insert("end", text); self.hist_detail.config(state="disabled")

    def _show_run(self):
        r = self._selected_run()
        if r:
            steps = "\n".join(f"  {i}. {s}" for i, s in enumerate(r["steps"], 1)) or "  (none)"
            self._set_detail(f"{r['goal']}\n\n{r['ts']}  |  {r['outcome'].upper()}  |  "
                             f"{r['backend']}  |  {r['duration_s']}s\nResult: {r['final']}\n\nSteps:\n{steps}")

    def run_again(self):
        r = self._selected_run()
        if r and not self.running:
            self._clear_ph(); self.task.delete(0, "end"); self.task.insert(0, r["goal"])
            self.on_run()

    def delete_run(self):
        r = self._selected_run()
        if r:
            self.memory.delete(r["id"]); self.refresh_history()

    def clear_history(self):
        from tkinter import messagebox
        if messagebox.askyesno("Clear history", "Delete all task history? This cannot be undone.",
                               parent=self._hist_win):
            self.memory.clear(); self.refresh_history()

    def open_context(self):
        w = tk.Toplevel(self.root); w.title("LocalPilot - My context"); w.configure(bg=BG)
        w.geometry("520x320")
        tk.Label(w, text="Notes about you that every task can use (stored only on this PC).\n"
                 "e.g.  My browser is Brave.  I like Punjabi music.  My email app is Outlook.",
                 bg=BG, fg=SUBTLE, justify="left").pack(anchor="w", padx=10, pady=(10, 4))
        txt = tk.Text(w, bg="#0f0f16", fg=FG, insertbackground=FG, relief="flat",
                      wrap="word", font=("Segoe UI", 10))
        txt.pack(fill="both", expand=True, padx=10)
        txt.insert("1.0", self.memory.preferences())
        def save():
            self.memory.set_preferences(txt.get("1.0", "end"))
            self.set_status("Saved your context.", "ok"); w.destroy()
        ttk.Button(w, text="Save", command=save).pack(anchor="e", padx=10, pady=8)

    # ---------- actions ----------
    def on_run(self):
        goal = self.task_text()
        if not goal:
            self.set_status("Type a task first.", "err")
            return
        self._apply()
        from . import settings as _settings
        _settings.save_from(self.cfg)
        if self.cfg.think_first:
            # the session decides: "stop" works even mid-task; busy/idle comes from its events
            self._clear_ph(); self.task.delete(0, "end")
            self._ask(goal)
            return
        if self.running:
            return
        from . import settings as _settings
        _settings.save_from(self.cfg)
        self.stop_event.clear()
        self.running = True
        if self._orb_idle_timer is not None:
            self.root.after_cancel(self._orb_idle_timer)
            self._orb_idle_timer = None
        self._orb_state = "thinking"
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.set_status("Running… focus your target window during the 3s countdown.", "run")
        self.append(f"\n=== RUN ({self.cfg.backend}): {goal} ===")
        self.root.iconify()   # get the panel out of the way so it isn't acted on

        def worker():
            try:
                final = run_agent(goal, self.cfg)
            except Exception as e:
                final = UnfinishedRun(f"error: {e}")
            self.logq.put({"event": "_final", "final": final})
        threading.Thread(target=worker, daemon=True).start()

    def _ask(self, text):
        """Brain mode: the shared session decides (control / inspect / answer / command)."""
        self.set_status("Thinking…", "run")
        self.session.submit(text, "text")

    # ---------- research browser (Vercel agent-browser) ----------
    def _build_browser_panel(self):
        """Compact card in this window + an expandable live view. They only SHOW the controller's
        real state and call its methods; all security is enforced below them."""
        try:
            from .agent_browser import ui as abui, uisettings
            self.ab_settings = uisettings.load()
            self.ab_pal = abui.palette(self.ab_settings.get("theme", "dark"))
            abui.apply_theme(self.root, self.ab_pal, abui.DARK)    # main window is built in DARK colours
            ctl = self.session.browser                      # lazy: Chromium is NOT started here
            ctl.apply_settings(self.ab_settings)
            self.cfg.research_mode = self.ab_settings.get("research_mode", "quick")
            self.cfg.research_max_pages = int(self.ab_settings.get("max_pages", 0) or 0)
            self.cfg.research_max_seconds = int(self.ab_settings.get("task_timeout_s", 0) or 0)
            from .agent_browser.viewer import LiveViewer   # agent-browser's official live stream
            self.ab_workspace = LiveViewer(self.root, ctl, self.session, self.ab_pal, self.ab_settings)
            self.ab_panel = abui.CompactPanel(self.root, ctl, self.session, self.ab_pal,
                                              open_workspace=self._toggle_workspace,
                                              open_settings=self._open_browser_settings)
            self.ab_panel.pack(fill="x", padx=10, pady=(6, 0))
            self.root.bind("<Control-Shift-B>", lambda e: self._toggle_workspace())
            self.root.bind("<Control-Shift-b>", lambda e: self._toggle_workspace())
        except Exception as e:                               # the app still works without the panel
            self.ab_panel = self.ab_workspace = None
            print("browser panel unavailable:", e)

    def _toggle_workspace(self):
        if self.ab_workspace is not None and self.ab_settings.get("preview_enabled", True):
            self.ab_workspace.toggle()
        elif self.ab_workspace is not None:
            self.set_status("Live preview is turned off in the browser settings.", "info")

    def _open_browser_settings(self):
        from .agent_browser import ui as abui, uisettings

        def saved(values):
            self.ab_settings = values
            uisettings.save(values)
            self.session.browser.apply_settings(values)
            self.ab_workspace.pip.settings = self.ab_workspace.big.settings = values
            self.cfg.research_mode = values.get("research_mode", "quick")
            self.cfg.research_max_pages = int(values.get("max_pages", 0) or 0)
            self.cfg.research_max_seconds = int(values.get("task_timeout_s", 0) or 0)
            self.set_status("Browser settings saved.", "ok")
        abui.SettingsDialog(self.root, self.ab_settings, self.ab_pal, saved, self.session.browser)

    def _session_event(self, kind, data):
        """Session events (text AND voice turns) -> panel log + voice. Runs on worker threads."""
        if self.voice is not None:
            self.voice.on_session_event(kind, data)
        if kind == "you":
            self.logq.put({"event": "_you", "text": ("🎤 " if data.get("source") == "voice" else "")
                           + data.get("text", "")})
        elif kind == "plan":
            self.logq.put({"event": "_plan", "plan": data})
            if data.get("steps"):
                self.logq.put({"event": "_hide"})          # get out of the way of the PC
        elif kind == "say":
            self.logq.put({"event": "_say", "text": data.get("text", "")})
        elif kind in ("step", "step_done"):
            self.logq.put({"event": "_" + kind, **data})
        elif kind == "busy":
            self.logq.put({"event": "_busy", "busy": data.get("busy")})
        elif kind == "paused":
            self.logq.put({"event": "_note", "text": "(paused) say or type 'continue' to go on, "
                                                     "or 'stop' to cancel"})
            self.logq.put({"event": "_setup", "message": "Paused - say or type 'continue', or 'stop'."})
        elif kind == "resumed":
            self.logq.put({"event": "_setup", "message": "Continuing…"})
        elif kind == "research_progress":
            self.logq.put({"event": "_setup", "message": data.get("text", "")})
        elif kind == "research_plan":
            if self.ab_workspace is not None and self.ab_settings.get("preview_enabled", True):
                self.root.after(0, lambda: self.ab_workspace.mode == "closed" and self.ab_workspace.show_pip())
            qs = "; ".join(data.get("queries") or [])
            self.logq.put({"event": "_note", "text": f"(research - {data.get('mode')}) searching: {qs}"})
        elif kind == "research_done":
            r = data["result"]
            from .research.engine import sources_block
            text = "\n" + r.answer.strip()
            if r.evidence:
                text += "\n\nSources (pages LocalPilot actually opened):\n" + sources_block(r.evidence)
            if r.report_path:
                text += f"\n\nFull report: {r.report_path}"
            for w in r.warnings[:4]:
                text += f"\n  ! {w}"
            self.logq.put({"event": "_note", "text": text})

    def _voice_changed(self):
        self.cfg.tts_voice = self._voice_names.get(self.voice_pick.get(), "af_heart")
        from . import settings as _settings
        _settings.save_from(self.cfg)
        if self.voice is not None:                     # switch now, and say hello in the new voice
            from .voice import Speaker
            self.voice.speaker.stop()
            self.voice.speaker = Speaker(self.cfg.tts_engine, self.cfg.tts_voice)
        else:
            self._ensure_voice()
        self.voice.speaker.say("Hi, this is my new voice.")

    def _ensure_voice(self) -> bool:
        if self.voice is None and self.cfg.tts_engine == "kokoro":
            from . import voice as _v
            if not _v.available().get("kokoro_voice"):
                # first use: fetch the natural voice in the background; Zira speaks meanwhile
                threading.Thread(target=lambda: _v.ensure_kokoro(
                    lambda m: self.logq.put({"event": "_setup", "message": m})), daemon=True).start()
        if self.voice is None:
            from . import voice
            self.voice = voice.Voice(self.session, speak=self.cfg.speak_replies,
                                     log=lambda m: self.logq.put({"event": "_note", "text": m}),
                                     engine=self.cfg.tts_engine, voice=self.cfg.tts_voice)
            self.voice.whisper_model = self.cfg.whisper_model
        return True

    def _preload_speech(self, visible: bool = False):
        """Get Whisper ready in the background so the first spoken command isn't slow.
        At startup: only if it's already downloaded (quiet). On first voice use: download if
        needed, with progress in the status bar."""
        try:
            from . import voice as _v
            t = _v.get_transcriber(self.cfg.whisper_model)
        except Exception as e:
            if visible:
                self.logq.put({"event": "_setup", "message": f"Voice unavailable: {e}"})
            return
        if t.state in ("ready", "downloading", "loading"):
            return
        if not visible and not t.is_cached():
            return                                   # never download unasked at startup
        report = (lambda m: self.logq.put({"event": "_setup", "message": m})) if visible else                  (lambda m: self.logq.put({"event": "_note", "text": f"(voice) {m}"}) if "ready" in m else None)
        t.preload(report)

    def _speech_ready_note(self) -> str:
        from . import voice as _v
        t = _v.get_transcriber(self.cfg.whisper_model)
        if t.state == "ready":
            return ""
        self._preload_speech(visible=True)
        return " (getting speech recognition ready - talk anyway, I'll catch up)"

    def on_mic(self):
        """Push to talk: listen for one request (Silero VAD + Whisper, all local)."""
        self._ensure_voice()
        if self.voice.listener is not None and self.voice.mode == "push":
            self.voice.stop_listening(); self.set_status("Stopped listening.", "info"); return
        err = self.voice.start_listening("push")
        self.set_status(err or "Listening… speak now (I'll stop when you pause)." + self._speech_ready_note(),
                        "err" if err else "run")

    def _sync_handsfree(self):
        on = self.v_handsfree.get()
        if on:
            self._ensure_voice()
            if self.voice.mode != "hands-free":
                self.voice.stop_listening()
                err = self.voice.start_listening("hands-free")
                self.set_status(err or "Hands-free: I'm listening. Say \"stop\" anytime."
                                + self._speech_ready_note(),
                                "err" if err else "ok")
                if err:
                    self.v_handsfree.set(False)
        elif self.voice is not None and self.voice.mode == "hands-free":
            self.voice.stop_listening(); self.set_status("Hands-free off.", "info")

    def on_stop(self):
        self.session.interrupt("stop")                  # speech AND task, immediately
        self.stop_event.set()
        self.set_status("Stopping…", "info")

    def on_check(self):
        self._apply()
        threading.Thread(target=self._health_worker, daemon=True).start()

    def _health_worker(self):
        from . import health
        state, msg = health.check(self.cfg)
        self.logq.put({"event": "_health", "state": state, "message": msg})

    def _show_health(self, state, msg):
        self.set_status(msg, "ok" if state == "ok" else "err")
        if state == "ok" and not getattr(self, "_warmed", False):
            self._warmed = True                     # ready before the user hits Run
            from . import llm, apps
            llm.warm(self.cfg)
            threading.Thread(target=apps.all_apps, daemon=True).start()
            threading.Thread(target=self._preload_speech, daemon=True).start()
        if state != "ok":
            self.append(f"[setup] {msg}")
        label = {"not_running": "Start AI engine", "not_installed": "Install AI engine",
                 "model_missing": "Download model"}.get(state)
        self._health_state = state
        if label:
            self.fix_btn.config(text=label, state="normal")
            self.fix_btn.pack(side="left", padx=6, before=self._quit_btn)
        else:
            self.fix_btn.pack_forget()
        self.run_btn.config(state="normal" if state == "ok" and not self.running else
                            ("disabled" if self.running else "normal"))

    def on_start_engine(self):
        from . import health
        state = getattr(self, "_health_state", "not_running")
        if state in ("not_installed", "model_missing"):
            self.fix_btn.config(state="disabled")

            def say(msg):
                self.logq.put({"event": "_setup", "message": msg})

            def work():
                ok = True
                if state == "not_installed":
                    ok = health.install_ollama(say) and health.start_ollama()
                    import time; time.sleep(4)
                    if ok and health.check(self.cfg)[0] == "model_missing":
                        ok = health.pull_model(self.cfg, progress=say)
                else:
                    ok = health.pull_model(self.cfg, progress=say)
                say("Setup finished." if ok else "Setup did not finish - see the log above.")
                self._health_worker()
            threading.Thread(target=work, daemon=True).start()
            return
        if health.start_ollama():
            self.set_status("Starting the AI engine...", "info")
            self.root.after(5000, self.on_check)
        else:
            self.set_status("Could not start the AI engine. Run install.bat.", "err")

    def on_quit(self):
        try:
            self._apply()
            from . import settings as _settings
            _settings.save_from(self.cfg)
        except Exception:
            pass
        self.stop_event.set()
        try:
            if self.voice is not None:
                self.voice.stop_listening(); self.voice.speaker.stop()
        except Exception:
            pass
        try:
            self.session.shutdown()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _drain(self):
        try:
            while True:
                rec = self.logq.get_nowait()
                e = rec.get("event")
                if e == "_busy":
                    if rec["busy"]:
                        self.running = True
                        self.run_btn.config(state="disabled"); self.stop_btn.config(state="normal")
                    else:
                        self.running = False
                        self.run_btn.config(state="normal"); self.stop_btn.config(state="disabled")
                        self._orb_state = "done"
                        self._orb_idle_timer = self.root.after(2600, self._idle_orb)
                        self.refresh_history()
                        try:
                            self.root.deiconify()
                        except Exception:
                            pass
                    continue
                if e == "_note":
                    self.append("  " + rec["text"])
                    if rec["text"].startswith("(heard)") and self.voice is not None \
                            and self.voice.mode == "push":
                        self.set_status("Got it.", "ok")
                    continue
                if e == "_say":
                    self.set_status(rec["text"], "ok")
                if e == "_hide":
                    try:
                        self.root.iconify()
                    except Exception:
                        pass
                    continue
                if e == "_setup":
                    self.set_status(rec["message"], "info")
                    continue
                if e == "_health":
                    self._show_health(rec["state"], rec["message"])
                    continue
                if e == "_show":                           # orb menu: "Open LocalPilot app"
                    try:
                        self.root.deiconify()
                        self.root.lift()
                        self.root.focus_force()
                    except Exception:
                        pass
                elif e == "step":
                    self._orb_state = "thinking"
                elif e in ("action", "decide"):
                    self._orb_state = "acting"
                elif e == "_final":
                    self.running = False
                    self.run_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    final = rec.get("final", "done")
                    bad = isinstance(final, UnfinishedRun)
                    self._orb_state = "stopped" if bad else "done"
                    self.set_status(final, "err" if bad else "ok")
                    self._orb_idle_timer = self.root.after(2600, self._idle_orb)
                    self.refresh_history()
                    try:
                        self.root.deiconify()
                    except Exception:
                        pass
                line = _fmt(rec)
                if line:
                    self.append(line)
        except queue.Empty:
            pass
        self._animate_orb()
        if getattr(self, "ab_panel", None) is not None and getattr(self.session, "_browser", None) is not None:
            try:
                self.ab_panel.refresh(self.session._browser.snapshot(), self.ab_pal)
            except Exception:
                pass
        try:
            self.root.after(60, self._drain)
        except Exception:
            pass

    def _idle_orb(self):
        self._orb_idle_timer = None
        if not self.running:
            self._orb_state = "idle"

    def _animate_orb(self):
        if not self.orb or not self.root.winfo_viewable():
            return
        try:
            img = self.orb.render(self._orb_state)
            if img is self._orb_frame:
                return
            self._orb_photo = ImageTk.PhotoImage(img)
            self.orb_label.config(image=self._orb_photo)
            self._orb_frame = img
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()
