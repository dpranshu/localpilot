"""One LocalPilot session for every way of talking to it (typed or spoken).

Text and voice call the same Session.submit(), so they share one conversation, one memory and
one running task -- never two assistants that don't know about each other.

Routing (decided before anything else, so "stop" is never stuck behind a busy model):
  control  "stop", "cancel", "wait, don't change that file"  -> halt speech + task NOW
  inspect  "what's happening on my computer?"                -> read real PC state, explain
  else     brain.think -> answer / chat / command (plan -> act -> verify -> report)

Events (on_event(kind, data)) for the UI and the voice: you, plan, say, step, step_done, done,
stop_speaking, busy.
"""
from __future__ import annotations

import re
import threading

from . import assistant, inspect_pc

_CONTROL = re.compile(
    r"^\s*(?:oh\s+|no\s+|hey\s+|okay\s+|ok\s+)?(stop|cancel|abort|halt|wait|hold\s+on|hang\s+on|pause|"
    r"never\s*mind|nevermind|don'?t|do\s+not|enough|quiet|shut\s+up|that'?s\s+enough|forget\s+it)\b",
    re.IGNORECASE)


_RESUME = re.compile(r"^\s*(?:ok(?:ay)?[\s,]+|yes[\s,]+|yeah[\s,]+|please[\s,]+|you can[\s,]+)?"
                     r"(continue|resume|go on|carry on|keep going|proceed|go ahead|unpause)\b", re.I)


def is_resume(text: str) -> bool:
    """An explicit go-ahead to continue a PAUSED task. Nothing else resumes one."""
    return bool(_RESUME.match(text or ""))


def is_control(text: str) -> bool:
    """A request to halt. Kept deliberately broad: stopping wrongly is cheap, not stopping isn't."""
    t = (text or "").strip()
    return bool(_CONTROL.match(t)) or t.lower().strip(" .!") in ("no", "no no", "stop it", "stop stop")


class Session:
    def __init__(self, cfg, memory=None, on_event=lambda kind, data: None):
        import threading as _t
        self.cfg = cfg
        if cfg.stop_event is None:
            cfg.stop_event = _t.Event()
        if getattr(cfg, "hold_event", None) is None:
            cfg.hold_event = _t.Event()
        self.memory = memory
        self.on_event = on_event
        self.conversation: list[dict] = []          # shared by text AND voice
        self._worker: threading.Thread | None = None
        self._task = ""
        self._lock = threading.Lock()

    # ---------- state ----------
    @property
    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _emit(self, kind, data=None):
        try:
            self.on_event(kind, data or {})
        except Exception:
            pass

    # ---------- the one entry point ----------
    def submit(self, text: str, source: str = "text") -> str:
        """Returns 'control' | 'busy' | 'started' | 'empty'."""
        text = (text or "").strip()
        if source == "voice":
            text = text.rstrip(" .!,")
        if not text:
            return "empty"
        self._emit("you", {"text": text, "source": source})
        if is_control(text):
            self.interrupt(text)
            return "control"
        if is_resume(text):
            return self.resume()
        if self.paused:
            # Anything that isn't "continue" or "stop" keeps the task paused -- it must never
            # resume because the user said something else.
            self._say_paused()
            return "paused"
        with self._lock:
            if self.busy:
                self._emit("say", {"text": f"I'm still working on: {self._task}. Say stop to cancel it."})
                return "busy"
            self.cfg.stop_event.clear()
            self._task = text
            self._worker = threading.Thread(target=self._run, args=(text, source), daemon=True)
            self._worker.start()
        return "started"

    @property
    def paused(self) -> bool:
        return self.busy and self.cfg.hold_event.is_set()

    def hold(self):
        """The user started speaking: pause before the next action. Only an explicit
        'continue' (resume()) or 'stop' (interrupt()) ends the pause -- never a timeout."""
        if self.busy and not self.cfg.hold_event.is_set():
            self.cfg.hold_event.set()
            self._emit("paused", {"task": self._task})

    def resume(self) -> str:
        """Explicit permission to continue a paused task."""
        if not self.paused:
            self._emit("say", {"text": "There's nothing paused right now."})
            return "nothing"
        self.cfg.hold_event.clear()
        self._emit("resumed", {"task": self._task})
        self._emit("say", {"text": f"Continuing: {self._task}."})
        return "resumed"

    def unheard(self):
        """Speech was detected but nothing usable was transcribed: stay paused, say how to go on."""
        if self.paused:
            self._say_paused(prefix="I didn't catch that. ")

    def _say_paused(self, prefix: str = ""):
        self._emit("say", {"text": f"{prefix}I've paused: {self._task}. Say continue to carry on, "
                                   "or stop to cancel it."})

    def release(self):
        """Internal: clear a pause because the task ended or was cancelled (not a resume)."""
        self.cfg.hold_event.clear()

    def interrupt(self, text: str = "stop"):
        """Halt speech immediately and the task before its next action."""
        self._emit("stop_speaking")
        was_busy = self.busy
        self.cfg.stop_event.set()
        self.cfg.hold_event.clear()
        if was_busy:
            changes = re.search(r"\b(change|edit|delete|touch|modify|save|send|close)\b", text, re.I)
            msg = ("Stopped. I won't make any more changes." if changes else "Stopped.")
        else:
            msg = "Okay."
        self._emit("say", {"text": msg, "interrupt": True})
        self._remember(text, msg, "control")

    def wait(self, timeout=None):
        if self._worker is not None:
            self._worker.join(timeout)

    # ---------- work ----------
    def _run(self, text: str, source: str):
        self._emit("busy", {"busy": True, "task": text})
        reply = ""
        try:
            if inspect_pc.is_inspect(text):
                self._emit("plan", {"intent": "inspect", "thought": "Looking at what's actually on your PC.",
                                    "reply": "", "steps": []})
                reply = inspect_pc.explain(self.cfg, text)
                self._emit("say", {"text": reply})
                self._emit("done", {"text": reply, "ok": True})
            else:
                prefs = self.memory.preferences() if (self.memory and self.cfg.use_memory) else ""
                reply = assistant.handle(text, self.cfg, self._relay, prefs, list(self.conversation))
        except Exception as e:
            reply = f"Something went wrong: {e}"
            self._emit("say", {"text": reply}); self._emit("done", {"text": reply, "ok": False})
        finally:
            self.cfg.hold_event.clear()            # the task is over; nothing left to pause
            self._remember(text, reply, source)
            self._emit("busy", {"busy": False})

    def _relay(self, kind, data):
        if kind == "step" and data.get("n", 1) > 1:     # streaming progress for longer plans
            self._emit("say", {"text": f"Step {data['i']} of {data['n']}: {data['step']}.", "progress": True})
        if kind != "thinking":
            self._emit(kind, data)

    def _remember(self, user, reply, source="text"):
        self.conversation += [{"role": "user", "content": user, "source": source},
                              {"role": "assistant", "content": str(reply)}]
        self.conversation = self.conversation[-16:]
