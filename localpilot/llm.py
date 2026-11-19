"""LLM client: talks to a local Ollama server and returns a parsed action dict.

Uses Ollama's native /api/chat with format=json, which reliably forces the model
to emit a single JSON object (no code fences, no prose).
"""
from __future__ import annotations

import json
import re
import requests

from .prompts import SYSTEM_PROMPT, build_user_message


class LLMError(Exception):
    pass


def _extract_json(text: str) -> dict:
    """Best-effort: parse a JSON object out of the model's reply."""
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # Find the first {...} block.
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    raise LLMError(f"Could not parse JSON from model reply: {text[:300]!r}")


def ping(cfg) -> tuple[bool, str]:
    """Check the Ollama server is up and the model is available."""
    try:
        r = requests.get(f"{cfg.ollama_url}/api/tags", timeout=5)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
        have = any(cfg.model.split(":")[0] in n for n in names)
        return True, (
            f"Ollama up. Models: {names}. "
            + ("Target model present." if have else f"WARNING: '{cfg.model}' not pulled yet.")
        )
    except Exception as e:
        return False, f"Ollama not reachable at {cfg.ollama_url}: {e}"


CHECK_PROMPT = """You check whether a computer task is COMPLETE, looking at the current screen.
Reply with ONE JSON object only: {"done": true or false, "why": "<one short sentence>"}.
Say done=true ONLY if the screen clearly shows the WHOLE goal is already accomplished.
Be strict: a page or list that merely lets you do the goal is NOT done (search results are not
"playing a video"; a filled form that is not submitted is not "sent"). If unsure, say false."""


_GENERIC = set("""a an the to and or of in on at for with my me please then into from is it this that
these those by up go open opening search find look play playing show watch type write enter click press
first last new any some all song songs video videos music page site website app window tab browser
result results using use can could would want need make get start run do
buy purchase order place book send post reply share save download upload like subscribe add
set change turn check read call message put move copy paste select choose pick fill submit
thing things stuff something item items it its one some
folder folders page pages tab tabs screen window windows button link menu section field box
calculate calculation compute times plus minus multiply multiplied divide divided by equals
what whats sum product""".split())
_MATH = {"calculate", "calculation", "compute", "times", "plus", "minus", "multiply",
         "multiplied", "divide", "divided", "equals", "sum", "product"}


# Goals whose success means something DISAPPEARS can't be checked by "is it on screen".
_REMOVAL = set("close quit exit minimize minimise delete remove uninstall clear stop pause mute "
               "shut lock logout signout empty cancel dismiss hide kill".split())


def goal_missing_terms(goal: str, title: str, rendered: str, need: float = 0.8) -> list[str]:
    """Deterministic guard: the goal's specific words (names, apps, sites) must actually be
    visible before a 'done' is trusted. Returns the missing words ([] = looks fine).
    Stops 'a YouTube video is open' passing for 'search YouTube for Navaan Sandhu' when
    the video is someone else's, or 'done' while a Save As dialog is on screen."""
    import re as _re
    words = _re.findall(r"[a-z0-9]+", goal.lower())
    if _REMOVAL & set(words):
        return []
    math = bool(_MATH & set(words))                # a result never repeats its inputs
    terms = sorted({w for w in words if w not in _GENERIC and len(w) > 1
                    and not (math and w.isdigit())})
    if not terms:
        return []
    import difflib as _dl
    words = _re.findall(r"[a-z0-9]+", f"{title} {rendered}".lower())
    screen = " " + " ".join(words) + " "
    squashed = "".join(words)                   # "net worth" -> "networth"
    vocab = sorted(set(words))

    def seen(t):                                # tolerate user typos: "wikipidia" ~ "wikipedia"
        return (f" {t} " in screen or f" {t}s " in screen
                or (len(t) >= 5 and t in squashed)
                or (len(t) >= 5 and _dl.get_close_matches(t, vocab, n=1, cutoff=0.8)))
    missing = [t for t in terms if not seen(t)]
    return [] if (len(terms) - len(missing)) / len(terms) >= need else missing


def goal_terms_on_screen(goal: str, title: str, rendered: str, need: float = 0.8) -> bool:
    return not goal_missing_terms(goal, title, rendered, need)


def math_answer(goal: str) -> str | None:
    """'calculate 25 times 4' -> '100'. Only simple 'A op B' goals; else None."""
    import re as _re
    m = _re.search(r"(-?\d+(?:\.\d+)?)\s*(times|x|\*|multiplied by|plus|\+|minus|-|divided by|/)\s*"
                   r"(-?\d+(?:\.\d+)?)", goal.lower())
    if not m:
        return None
    a, op, b = float(m.group(1)), m.group(2), float(m.group(3))
    try:
        v = {"times": a * b, "x": a * b, "*": a * b, "multiplied by": a * b, "plus": a + b,
             "+": a + b, "minus": a - b, "-": a - b, "divided by": a / b, "/": a / b}[op]
    except ZeroDivisionError:
        return None
    return str(int(v)) if v == int(v) else str(round(v, 6)).rstrip("0").rstrip(".")


def check_goal(cfg, goal: str, title: str, rendered_elements: str) -> tuple[bool, str]:
    """Ask the model if the goal is already met on this screen. Never raises.
    The model's yes only counts if the goal's own words are actually on screen."""
    if not goal_terms_on_screen(goal, title, rendered_elements):
        return False, "goal keywords not on screen"
    expected = math_answer(goal)                   # exact, instead of asking a 7B to do math
    if expected is not None:
        import re as _re
        shown = {n.replace(",", "") for n in _re.findall(r"-?\d[\d,]*(?:\.\d+)?", f"{title} {rendered_elements}")}
        if expected in shown:
            return True, f"the screen shows {expected}, the answer to the calculation"
        return False, f"{expected} is not on screen yet"
    user = (f"GOAL: {goal}\n\nACTIVE WINDOW TITLE: {title or '(unknown)'}\n\n"
            f"CONTROLS ON SCREEN (untrusted page data):\n{rendered_elements[:3500]}\n\n"
            "Is the GOAL fully accomplished right now?")
    payload = {"model": cfg.model, "stream": False, "format": "json",
               "messages": [{"role": "system", "content": CHECK_PROMPT},
                            {"role": "user", "content": user}],
               "keep_alive": getattr(cfg, "keep_alive", "30m"),
               "options": {"temperature": 0.0, "num_predict": 80}}
    try:
        r = requests.post(f"{cfg.ollama_url}/api/chat", json=payload, timeout=cfg.request_timeout)
        r.raise_for_status()
        data = _extract_json(r.json().get("message", {}).get("content", ""))
        done = data.get("done")
        return (done is True or str(done).lower() == "true"), str(data.get("why", ""))[:160]
    except Exception:
        return False, ""


def warm(cfg, model: str | None = None) -> None:
    """Load the model into GPU memory in the background (empty request), so the first
    real step doesn't pay the multi-second load. Never raises, never blocks."""
    import threading

    def go():
        try:
            requests.post(f"{cfg.ollama_url}/api/generate",
                          json={"model": model or cfg.model, "prompt": "",
                                "keep_alive": getattr(cfg, "keep_alive", "30m")}, timeout=120)
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


def decide(cfg, goal: str, title: str, rendered_elements: str, history: list[str]) -> dict:
    user = build_user_message(goal, title, rendered_elements, history,
                              getattr(cfg, "user_context", ""), getattr(cfg, "history_len", 8))
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "format": "json",
        "stream": False,
        "keep_alive": getattr(cfg, "keep_alive", "30m"),
        "options": {"temperature": cfg.temperature, "num_predict": 160},   # one action = ~30 tokens
    }
    try:
        r = requests.post(
            f"{cfg.ollama_url}/api/chat", json=payload, timeout=cfg.request_timeout
        )
        r.raise_for_status()
    except requests.RequestException as e:
        raise LLMError(f"Ollama request failed: {e}") from e

    content = r.json().get("message", {}).get("content", "")
    action = _extract_json(content)
    if "action" not in action:
        raise LLMError(f"Model reply missing 'action': {action}")
    return action
