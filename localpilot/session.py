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
            self.release()
            return "control"
        self.release()                    # not a stop: let a held task continue
        with self._lock:
            if self.busy:
                self._emit("say", {"text": f"I'm still working on: {self._task}. Say stop to cancel it."})
                return "busy"
            self.cfg.stop_event.clear()
            self._task = text
            self._worker = threading.Thread(target=self._run, args=(text, source), daemon=True)
            self._worker.start()
        return "started"

    def hold(self):
        """The user started speaking: pause before the next action until we hear what it was."""
        if self.busy:
            self.cfg.hold_event.set()

    def release(self):
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
