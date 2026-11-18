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
from .agent import run as run_agent
from . import llm

BG = "#15151c"
FG = "#e8e8ef"
ACCENT = "#35d07f"
SUBTLE = "#9a9aa8"


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
        return f"=== {rec.get('final','')[:120]} ==="
    return None


class App:
    def __init__(self):
        self.cfg = Config()
        self.stop_event = threading.Event()
        self.cfg.stop_event = self.stop_event
        self.logq: "queue.Queue[dict]" = queue.Queue()
        self.cfg.log_callback = self.logq.put
        self.running = False
        self._build()

    # ---------- UI ----------
    def _build(self):
        self.root = tk.Tk()
        self.root.title("LocalPilot")
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

        pad = {"padx": 10, "pady": 6}
        head = ttk.Label(self.root, text="LocalPilot  — local AI desktop agent",
                         font=("Segoe UI", 13, "bold"))
        head.pack(anchor="w", **pad)

        # task row
        tf = tk.Frame(self.root, bg=BG)
        tf.pack(fill="x", padx=10)
        ttk.Label(tf, text="Task").pack(side="left")
        self.task = tk.Entry(tf, bg="#1f1f29", fg=FG, insertbackground=FG,
                             relief="flat", font=("Segoe UI", 11))
        self.task.pack(side="left", fill="x", expand=True, padx=8, ipady=5)
        self.task.bind("<Return>", lambda e: self.on_run())
        self.run_btn = ttk.Button(tf, text="Run", style="Accent.TButton", command=self.on_run)
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
        self.model.grid(row=0, column=3, sticky="w", padx=6, ipady=3)
        ttk.Label(sf, text="Max steps").grid(row=0, column=4, sticky="w", padx=(16, 0))
        self.steps = tk.Spinbox(sf, from_=1, to=200, width=5, bg="#1f1f29", fg=FG,
                                relief="flat", buttonbackground="#1f1f29")
        self.steps.delete(0, "end"); self.steps.insert(0, str(self.cfg.max_steps))
        self.steps.grid(row=0, column=5, sticky="w", padx=6)

        # toggles
        tg = tk.Frame(self.root, bg=BG)
        tg.pack(fill="x", padx=10)
        self.v_confirm = tk.BooleanVar(value=self.cfg.confirm_each_step)
        self.v_cursor = tk.BooleanVar(value=self.cfg.show_cursor)
        self.v_overlay = tk.BooleanVar(value=self.cfg.show_overlay)
        self.v_dry = tk.BooleanVar(value=self.cfg.dry_run)
        self.v_real = tk.BooleanVar(value=self.cfg.real_cursor)
        for text, var in [("Confirm each step", self.v_confirm), ("Agent cursor", self.v_cursor),
                          ("Overlay", self.v_overlay), ("Dry run", self.v_dry),
                          ("Real cursor", self.v_real)]:
            ttk.Checkbutton(tg, text=text, variable=var).pack(side="left", padx=(0, 12))

        # status
        self.status = ttk.Label(self.root, text="Ready.", style="Sub.TLabel")
        self.status.pack(anchor="w", padx=10, pady=(4, 0))

        # log
        lf = tk.Frame(self.root, bg=BG)
        lf.pack(fill="both", expand=True, padx=10, pady=8)
        self.log = tk.Text(lf, bg="#0f0f16", fg=FG, relief="flat", wrap="word",
                           font=("Consolas", 9), state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(lf, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)

        # bottom
        bf = tk.Frame(self.root, bg=BG)
        bf.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bf, text="Check Ollama", command=self.on_check).pack(side="left")
        ttk.Button(bf, text="Clear log", command=self.clear_log).pack(side="left", padx=6)
        ttk.Button(bf, text="Quit", command=self.on_quit).pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)
        self.root.after(120, self._drain)

    # ---------- helpers ----------
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

    # ---------- actions ----------
    def on_run(self):
        if self.running:
            return
        goal = self.task.get().strip()
        if not goal:
            self.status.config(text="Type a task first.")
            return
        self._apply()
        self.stop_event.clear()
        self.running = True
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status.config(text="Running… focus your target window during the 3s countdown.")
        self.append(f"\n=== RUN ({self.cfg.backend}): {goal} ===")
        self.root.iconify()   # get the panel out of the way so it isn't acted on

        def worker():
            try:
                final = run_agent(goal, self.cfg)
            except Exception as e:
                final = f"error: {e}"
            self.logq.put({"event": "_final", "final": final})
        threading.Thread(target=worker, daemon=True).start()

    def on_stop(self):
        self.stop_event.set()
        self.status.config(text="Stopping after this step…")

    def on_check(self):
        ok, msg = llm.ping(self.cfg)
        self.status.config(text=msg)
        self.append(msg)

    def on_quit(self):
        self.stop_event.set()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _drain(self):
        try:
            while True:
                rec = self.logq.get_nowait()
                if rec.get("event") == "_final":
                    self.running = False
                    self.run_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.status.config(text=rec.get("final", "done")[:90])
                    try:
                        self.root.deiconify()
                    except Exception:
                        pass
                line = _fmt(rec)
                if line:
                    self.append(line)
        except queue.Empty:
            pass
        try:
            self.root.after(120, self._drain)
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()
