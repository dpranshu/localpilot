"""Assistant loop: hear -> think -> say -> do each step -> report back.

handle(text) returns the final thing to say. Progress is reported through on_event(kind, data):
  thinking | plan {thought, reply, steps, intent} | step {i, n, step} | step_done {i, ok, final}
  | say {text} | done {text}
"""
from __future__ import annotations

from . import brain
from .agent import UnfinishedRun, run as run_agent


def _ok(final) -> bool:
    return not isinstance(final, UnfinishedRun) and not str(final).lower().startswith(
        ("stopped", "aborted", "max steps", "error"))


def _why(final) -> str:
    s = str(final)
    for p in ("stopped:", "aborted:", "error:"):
        if s.lower().startswith(p):
            s = s[len(p):]
    return s.strip()[:140] or "it didn't work"


def handle(text: str, cfg, on_event=lambda kind, data: None, preferences: str = "",
           conversation: list | None = None) -> str:
    on_event("thinking", {"text": text})
    plan = brain.think(cfg, text, preferences, conversation)
    on_event("plan", plan)
    on_event("say", {"text": plan["reply"]})
    if plan["intent"] != "do" or not plan["steps"]:
        on_event("done", {"text": plan["reply"], "ok": True})
        return plan["reply"]

    n = len(plan["steps"])
    for i, step in enumerate(plan["steps"], 1):
        hold = getattr(cfg, "hold_event", None)
        if hold is not None and hold.is_set():           # the user is talking: wait for it
            import time as _t
            end = _t.time() + 10
            while hold.is_set() and _t.time() < end and not cfg.stop_event.is_set():
                _t.sleep(0.05)
        if cfg.stop_event is not None and cfg.stop_event.is_set():
            msg = "Okay, I stopped."
            on_event("done", {"text": msg, "ok": False}); return msg
        on_event("step", {"i": i, "n": n, "step": step})
        try:
            final = run_agent(step, cfg)
        except Exception as e:
            final = UnfinishedRun(f"error: {e}")
        ok = _ok(final)
        on_event("step_done", {"i": i, "ok": ok, "final": str(final)})
        if not ok:
            done_part = f"I did {i - 1} of {n} steps, but " if i > 1 else ""
            msg = f"{done_part}I couldn't {step[0].lower() + step[1:]}: {_why(final)}."
            on_event("say", {"text": msg}); on_event("done", {"text": msg, "ok": False})
            return msg
    msg = _report(final, n)                    # say what was VERIFIED, not what was planned
    on_event("say", {"text": msg}); on_event("done", {"text": msg, "ok": True})
    return msg


def _report(final, n: int) -> str:
    """'done: search results for 'x' are open' -> 'Done - search results for 'x' are open.'"""
    detail = str(final)
    if detail.lower().startswith("done:"):
        detail = detail[5:].strip()
    detail = detail.rstrip(". ")
    prefix = "Done" if n == 1 else f"All {n} steps done"
    if not detail or len(detail) > 160 or detail.lower().startswith(("dry-run", "{")):
        return prefix + "."
    return f"{prefix} - {detail[0].lower() + detail[1:]}."
