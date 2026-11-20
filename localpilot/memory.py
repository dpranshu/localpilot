"""Personal memory: the user's task history + their own notes, kept on this PC.

Every run is recorded (goal, outcome, the steps taken). When a new task starts, the
agent gets a short PERSONAL CONTEXT block:

  * the user's own notes ("my browser is Brave", "I like Punjabi music"),
  * the most similar past tasks -- what worked step by step (a recipe to reuse) and
    what failed (to avoid repeating it).

Stored as JSON in %APPDATA%\\LocalPilot\\memory.json (nothing leaves the machine).
Secrets are never stored: typed text going into password/card/PIN fields and
card-like digit runs are redacted before saving.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import threading
import time
import uuid

DEFAULT_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                            "LocalPilot", "memory.json")
MAX_RUNS = 300
MAX_FACTS = 200
MAX_TURNS = 80
_SECRETISH = re.compile(r"\b(password|passcode|pin|otp|cvv|card number|api key|token|secret)\b", re.I)
_STOP = set("a an the to and or of in on at for with my me please then into from is it this that "
            "open go click type".split())
_WORD = re.compile(r"[a-z0-9]+")
_DIGITS = re.compile(r"\d[\d -]{5,}\d")
_lock = threading.Lock()


def _tokens(s: str) -> set:
    return {w for w in _WORD.findall((s or "").lower()) if w not in _STOP}


def redact(text: str) -> str:
    return _DIGITS.sub("[number]", text or "")


def outcome_of(final) -> str:
    from .agent import UnfinishedRun
    f = str(final or "")
    if isinstance(final, UnfinishedRun) or f.lower().startswith(("stopped", "aborted", "error")):
        return "stopped" if "declined" in f or "by user" in f or "F12" in f else "failed"
    return "success"


class Memory:
    def __init__(self, path: str | None = None):
        self.path = path or DEFAULT_PATH

    # ---------------- storage ----------------
    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("bad shape")
        except FileNotFoundError:
            data = {}
        except Exception:                          # corrupt: keep a copy, start fresh
            try:
                os.replace(self.path, self.path + f".corrupt-{int(time.time())}")
            except OSError:
                pass
            data = {}
        data.setdefault("version", 1)
        data.setdefault("preferences", "")
        data.setdefault("runs", [])
        data.setdefault("facts", [])               # what LocalPilot has learned about the user
        data.setdefault("conversation", [])        # recent turns, kept across restarts
        data.setdefault("summary", "")             # rolling summary of older conversation
        return data

    def _save(self, data: dict):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)                 # atomic: never a half-written file

    # ---------------- runs ----------------
    def runs(self) -> list[dict]:
        with _lock:
            return list(reversed(self._load()["runs"]))     # newest first

    def add_run(self, goal, final, steps, backend="uia", duration=0.0) -> dict:
        run = {
            "id": uuid.uuid4().hex[:10],
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "goal": goal.strip(),
            "outcome": outcome_of(final),
            "final": redact(str(final))[:300],
            "backend": backend,
            "duration_s": round(float(duration), 1),
            "steps": [redact(s)[:160] for s in steps][:40],
        }
        with _lock:
            data = self._load()
            data["runs"].append(run)
            data["runs"] = data["runs"][-MAX_RUNS:]
            self._save(data)
        return run

    def delete(self, run_id: str):
        with _lock:
            data = self._load()
            data["runs"] = [r for r in data["runs"] if r.get("id") != run_id]
            self._save(data)

    def clear(self):
        with _lock:
            data = self._load()
            data["runs"] = []
            self._save(data)

    # ---------------- preferences ----------------
    def preferences(self) -> str:
        with _lock:
            return self._load()["preferences"]

    def set_preferences(self, text: str):
        with _lock:
            data = self._load()
            data["preferences"] = (text or "").strip()[:2000]
            self._save(data)

    # ---------------- personal facts ----------------
    def facts(self) -> list[dict]:
        with _lock:
            return list(self._load()["facts"])

    def add_fact(self, text: str, source: str = "said", key: str = "") -> dict | None:
        """Store one fact about the user. A fact with the same `key` (e.g. "name") replaces the old
        one; an identical fact is not duplicated. Secrets are never stored."""
        text = redact((text or "").strip())[:300]
        if not text or _SECRETISH.search(text):
            return None
        with _lock:
            data = self._load()
            facts = data["facts"]
            if key:
                facts = [f for f in facts if f.get("key") != key]
            norm = text.lower().rstrip(".")
            facts = [f for f in facts if f["text"].lower().rstrip(".") != norm]
            fact = {"id": uuid.uuid4().hex[:8], "text": text, "key": key, "source": source,
                    "ts": time.strftime("%Y-%m-%d %H:%M")}
            facts.append(fact)
            data["facts"] = facts[-MAX_FACTS:]
            self._save(data)
        return fact

    def forget(self, what: str) -> list[str]:
        """Remove facts matching `what` (words or the fact's key). Returns the removed texts."""
        q = _tokens(what)
        with _lock:
            data = self._load()
            keep, gone = [], []
            for f in data["facts"]:
                t = _tokens(f["text"])
                hit = (f.get("key") and f["key"] in q) or (q and len(q & t) / len(q) >= 0.6)
                (gone if hit else keep).append(f)
            data["facts"] = keep
            self._save(data)
        return [f["text"] for f in gone]

    def forget_all(self):
        with _lock:
            data = self._load()
            data["facts"], data["conversation"], data["summary"] = [], [], ""
            self._save(data)

    def fact(self, key: str) -> str:
        return next((f["text"] for f in reversed(self.facts()) if f.get("key") == key), "")

    # ---------------- conversation (survives restarts) ----------------
    def conversation(self) -> list[dict]:
        with _lock:
            return list(self._load()["conversation"])

    def save_conversation(self, turns: list[dict]):
        with _lock:
            data = self._load()
            data["conversation"] = [{"role": t["role"], "content": redact(str(t["content"]))[:4000],
                                     "ts": t.get("ts", time.strftime("%Y-%m-%d %H:%M"))}
                                    for t in turns][-MAX_TURNS:]
            self._save(data)

    def summary(self) -> str:
        with _lock:
            return self._load()["summary"]

    def set_summary(self, text: str):
        with _lock:
            data = self._load()
            data["summary"] = (text or "").strip()[:3000]
            self._save(data)

    def profile(self, query: str = "", k: int = 30) -> str:
        """The 'about the user' block for the brain: their notes + the facts most relevant to
        `query` (all of them while there are few)."""
        parts = []
        prefs = self.preferences()
        if prefs:
            parts.append(prefs)
        facts = self.facts()
        if len(facts) > k and query:
            q = _tokens(query)
            pinned = [f for f in facts if f.get("key") in ("name", "location", "job")]
            scored = sorted((f for f in facts if f not in pinned),
                            key=lambda f: -len(q & _tokens(f["text"])))
            facts = pinned + scored[:k - len(pinned)]
        parts += [f"- {f['text']}" for f in facts[-k:]]
        return "\n".join(parts)

    # ---------------- recall ----------------
    def relevant(self, goal: str, k: int = 3, min_score: float = 0.34) -> list[dict]:
        q = _tokens(goal)
        scored = []
        for r in self.runs():
            t = _tokens(r["goal"])
            overlap = len(q & t) / max(1, len(q | t))
            ratio = difflib.SequenceMatcher(None, goal.lower(), r["goal"].lower()).ratio()
            score = 0.6 * overlap + 0.4 * ratio + (0.05 if r["outcome"] == "success" else 0)
            if score >= min_score:
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        seen, out = set(), []
        for _, r in scored:                         # one entry per distinct goal+outcome
            key = (r["goal"].lower(), r["outcome"])
            if key not in seen:
                seen.add(key)
                out.append(r)
            if len(out) == k:
                break
        return out

    def context_for(self, goal: str, k: int = 3) -> str:
        parts = []
        prefs = self.preferences()
        if prefs:
            parts.append("About the user (their own notes):\n" + prefs)
        rel = self.relevant(goal, k)
        if rel:
            lines = []
            for r in rel:
                if r["outcome"] == "success":
                    steps = compact(r["steps"], 8) or "(no steps recorded)"
                    lines.append(f"- \"{r['goal']}\" SUCCEEDED in {len(r['steps'])} steps: {steps}")
                else:
                    steps = compact(r["steps"], 4) or "(no steps recorded)"
                    lines.append(f"- \"{r['goal']}\" did NOT finish ({r['final'][:60]}). "
                                 f"It went: {steps}. Do not repeat that; if the screen already "
                                 f"shows the goal is met, answer done.")
            parts.append("Similar tasks this user ran before (reuse what worked):\n" + "\n".join(lines))
        return "\n\n".join(parts)


def compact(steps, limit: int) -> str:
    """Collapse runs of the same action kind ('type x' 4 times -> 'type x (x4)')."""
    out = []
    for s in steps:
        kind = s.split(" ", 1)[0]
        if out and out[-1][0] == kind:
            out[-1][2] += 1
        else:
            out.append([kind, s[:80], 1])
    parts = [f"{s} (x{n})" if n > 1 else s for _, s, n in out]
    more = len(parts) - limit
    return " -> ".join(parts[:limit]) + (f" -> ... ({more} more)" if more > 0 else "")


def summarize_event(rec: dict) -> str | None:
    """Turn one log event into a short step line for history ('' = skip)."""
    e = rec.get("event")
    if e == "action":
        a = rec.get("action")
        if isinstance(a, dict):
            kind = a.get("action", "?")
            if kind in ("done", "wait"):
                return None
            arg = (rec.get("target") if kind in ("click", "double", "right") else "")                 or a.get("name") or a.get("keys") or a.get("text") or a.get("say") or ""
            if kind in ("type", "type_text") and _is_secretish(a):
                arg = "[hidden]"
            return f"{kind} {str(arg).strip()[:70]}".strip()
        res = str(rec.get("result", ""))           # vision: result string only
        return res[:70] if res else None
    if e == "harness" and rec.get("decision") in ("skip", "stop"):
        return f"[{rec['decision']}ped by user: {str(rec.get('note',''))[:50]}]"
    return None


def _is_secretish(action: dict) -> bool:
    try:
        from .harness import classify
        return classify(action, None)[1] == "credential" or "password" in str(action.get("say", "")).lower()
    except Exception:
        return False
