"""The brain: understand what the user MEANS, think, plan, then act -- like an assistant.

  user words --> brain.think() --> {intent, thought, reply, steps}
                   |  answer   : reply directly (maths, time, knowledge, chat) -- no apps opened
                   |  do       : a short plan of concrete steps, each in a phrasing the fast
                   |             skills understand ("Search YouTube for X and play the first video")
                   |  clarify  : ask one short question when the request is genuinely ambiguous
  assistant.handle() runs the steps one by one through agent.run and reports back.

Direct commands skip the model entirely (they are already concrete), so simple tasks stay fast.
"""
from __future__ import annotations

import datetime as _dt
import json
import re

import requests

from . import actions, llm, web

INTENTS = ("answer", "do", "research", "clarify", "chat")

BRAIN_PROMPT = """You are LocalPilot, a helpful personal assistant that lives on the user's Windows PC
(like JARVIS). You can talk, answer questions, and operate the computer. Think about what the user
actually MEANS and what would genuinely help them, then reply with ONE JSON object:

{"intent": "answer" | "do" | "research" | "clarify" | "chat",
 "thought": "<1-2 sentences: how you understood the request and why this plan>",
 "reply": "<what you say to the user, warm and brief, 1-2 sentences, spoken aloud>",
 "steps": ["<concrete step>", ...]}

intent:
- "answer": a question you can answer from knowledge or simple reasoning (facts, definitions,
  advice, maths, conversions). Put the full answer in "reply". steps = [].
- "research": the question needs CURRENT or LIVE information you cannot know (news, weather,
  prices, scores, releases, "latest"/"today" anything, a specific project or company's recent
  state) or a researched comparison. LocalPilot researches it in its own background browser and
  answers with sources. steps = []. "reply" says you'll look it up.
- "do": the user wants something done on the PC (open, play, type, show a page on screen)
  -> plan steps. "reply" says what you're about to do.
- "clarify": genuinely ambiguous AND acting would be a guess that could annoy them -> ask ONE short
  question in "reply". steps = []. Prefer a sensible default over asking.
- "chat": greetings, thanks, small talk -> friendly "reply", steps = [].

Each step must be ONE of these forms (they are executed by reliable skills -- use them exactly):
  "Open <app>"                                   e.g. Open Spotify / Open Calculator / Open Notepad
  "Open youtube.com"                             (any website)
  "Search YouTube for <q>"  /  "Search YouTube for <q> and play the first video"
  "Search Google for <q>"   /  "Search the web for <q>"
  "Search Wikipedia for <q> and open the article"
  "Search Amazon for <q>" / "Search Flipkart for <q>" / "Search GitHub for <q>" / "Search Reddit for <q>"
  "Search Google Maps for <place>"
  "Open Settings and go to the <Display|Sound|Bluetooth|Wi-Fi|Mouse|Battery|Windows Update|...> page"
  "Open Settings and search for <q>"
  "Open File Explorer and go to the <Downloads|Documents|Desktop|Pictures|Music|Videos> folder"
  "Open File Explorer, go to <folder> and search for <q>"
  "Open Calculator and calculate <a> <times|plus|minus|divided by> <b>"
  "Open Notepad and type: <text>"
  "Open Notepad, type <text>, and save it as <full path>"
  anything else: a short, literal instruction ("Open Outlook and start a new email")
Use 1-4 steps. Never invent steps the user didn't ask for. Never do anything risky (buying,
sending, deleting, posting, passwords) unless the user clearly asked -- and even then the PC will
ask them to confirm. Use the user's preferences below (their browser, music taste, apps).
Output JSON only."""

EXAMPLES = [
    ("I'm bored", {"intent": "do", "thought": "They want something fun; their notes say they like Punjabi music.",
                   "reply": "Let's fix that - putting on some Punjabi music for you.",
                   "steps": ["Search YouTube for latest Punjabi songs and play the first video"]}),
    ("what's the weather like in Delhi?", {"intent": "research", "thought": "Weather is live information, so I need to look it up.",
                                           "reply": "Let me look up the weather in Delhi.",
                                           "steps": []}),
    ("who wrote the theory of relativity?", {"intent": "answer", "thought": "A fact I know.",
                                             "reply": "Albert Einstein - special relativity in 1905 and general relativity in 1915.",
                                             "steps": []}),
    ("get me ready to code", {"intent": "do", "thought": "A coding setup: their editor plus docs to look things up.",
                              "reply": "Setting up your coding workspace.",
                              "steps": ["Open Visual Studio Code", "Open github.com"]}),
    ("thanks!", {"intent": "chat", "thought": "Small talk.", "reply": "Anytime! Anything else?", "steps": []}),
]


def _direct(utterance: str) -> bool:
    """Already a concrete command one of the fast skills handles -> no need to think."""
    u = utterance.strip()
    if re.search(r",\s*(?:and\s+)?then\b|\band then\b|;\s*\w|,\s*(?:and\s+)?(?:open|search|play|go)\b", u, re.I):
        return False                                  # several things -> let the planner split it
    p = web.parse(u)
    if p and p.get("rest") and not web.wants_first_result(p["rest"]):
        return False
    return bool(web.parse(u) or actions.settings_page_from_goal(u) or actions.folder_from_goal(u)
                or actions.app_search_from_goal(u)
                or (actions.app_from_goal(u) and (actions.is_only_open(u) or llm.math_expression(u)
                                                  or llm.typed_items(u))))


def _local_answer(utterance: str) -> str | None:
    """Questions answered instantly and exactly, without the model."""
    u = utterance.lower().strip(" ?!.")
    if re.search(r"\b(what(?:'s| is)? the )?time\b", u) and not re.search(r"\b(in|at)\s+[a-z]", u) \
            and len(u.split()) <= 6:
        return "It's " + _dt.datetime.now().strftime("%I:%M %p").lstrip("0") + "."
    if re.search(r"\b(today'?s date|what(?:'s| is) the date|what day is (it|today))\b", u):
        return "Today is " + _dt.datetime.now().strftime("%A, %d %B %Y") + "."
    if re.match(r"^(what(?:'s| is)|calculate|compute|how much is|work out)\b", u) and \
            not re.search(r"\b(open|calculator)\b", u):
        ans = llm.math_answer(u)
        if ans is not None:
            return f"That's {ans}."
    return None


def think(cfg, utterance: str, preferences: str = "", conversation: list | None = None) -> dict:
    """Decide what to do. Never raises: falls back to treating the words as a task."""
    u = (utterance or "").strip()
    if not u:
        return {"intent": "chat", "thought": "", "reply": "I'm listening.", "steps": []}
    local = _local_answer(u)
    if local:
        return {"intent": "answer", "thought": "I can answer this exactly, no apps needed.",
                "reply": local, "steps": [], "source": "local"}
    # a how-to / knowledge question that isn't about the PC or live info: just answer it
    # (the 7B planner otherwise sometimes "plans" physical steps like "Boil water")
    if _QUESTION.match(u) and not _PC_VERB.search(u):
        a = answer(cfg, u)
        if a:
            return {"intent": "answer", "thought": "A question I can answer - no apps needed.",
                    "reply": a, "steps": [], "source": "answer"}
    if _direct(u):
        return {"intent": "do", "thought": "A clear command - doing it directly.",
                "reply": "On it.", "steps": [u], "source": "direct"}

    now = _dt.datetime.now().strftime("%A %d %B %Y, %I:%M %p")
    msgs = [{"role": "system", "content": BRAIN_PROMPT +
             f"\n\nNow: {now}.\nUser preferences:\n{(preferences or '(none yet)').strip()[:800]}"}]
    for q, a in EXAMPLES:
        msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": json.dumps(a)}]
    for turn in (conversation or [])[-6:]:
        msgs.append({"role": turn["role"], "content": turn["content"][:500]})
    msgs.append({"role": "user", "content": u})
    plan, err = None, ""
    for attempt in range(2):                 # the engine may still be loading the model
        try:
            r = requests.post(f"{cfg.ollama_url}/api/chat", timeout=cfg.request_timeout, json={
                "model": cfg.model, "messages": msgs, "format": "json", "stream": False,
                "keep_alive": getattr(cfg, "keep_alive", "30m"),
                "options": {"temperature": 0.3, "num_predict": 300}})
            r.raise_for_status()
            plan = llm._extract_json(r.json().get("message", {}).get("content", ""))
            break
        except Exception as e:
            err = str(e)
            import time as _t
            _t.sleep(2)
    if plan is None:
        return {"intent": "do", "thought": f"(couldn't think it through: {err[:80]}) - trying it as a task.",
                "reply": "Let me try that.", "steps": [u], "source": "fallback"}
    out = _clean(plan, u)
    if out.pop("needs_answer", False):
        out["reply"] = answer(cfg, u) or out["reply"]
    return out


def answer(cfg, question: str) -> str:
    """A proper spoken-style answer to a question (plain text, a few sentences)."""
    try:
        r = requests.post(f"{cfg.ollama_url}/api/chat", timeout=cfg.request_timeout, json={
            "model": cfg.model, "stream": False, "keep_alive": getattr(cfg, "keep_alive", "30m"),
            "options": {"temperature": 0.3, "num_predict": 220},
            "messages": [{"role": "system", "content": "You are a helpful assistant. Answer clearly "
                          "and concisely in plain text (no markdown, no lists), at most 4 short "
                          "sentences, as if speaking aloud."},
                         {"role": "user", "content": question}]})
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "").strip()[:700]
    except Exception:
        return ""


# installed-app fallbacks: the web version of popular apps
WEB_APPS = {"spotify": "open.spotify.com", "whatsapp": "web.whatsapp.com", "outlook": "outlook.live.com",
            "mail": "mail.google.com", "gmail": "mail.google.com", "netflix": "netflix.com",
            "teams": "teams.microsoft.com", "microsoft teams": "teams.microsoft.com",
            "discord": "discord.com/app", "telegram": "web.telegram.org", "instagram": "instagram.com",
            "word": "office.com/launch/word", "excel": "office.com/launch/excel",
            "powerpoint": "office.com/launch/powerpoint", "youtube": "youtube.com",
            "chatgpt": "chatgpt.com", "notion": "notion.so", "slack": "app.slack.com"}
_QUESTION = re.compile(r"^(how (do|can|to|does|should)|what('s| is| are| does)|why|who|when|where|"
                       r"which|explain|tell me|define|is it true)\b", re.I)
_PC_VERB = re.compile(r"\b(open|play|search|find|show|check|launch|start|set|turn|put|look up|"
                      r"on (my|the) (pc|computer|screen)|weather|news|price|latest|today)\b", re.I)


_DOABLE = re.compile(r"\s*(open|search|play|go to|type|write|save|close|click|launch|start|find|"
                     r"show|google|look up|visit|watch|listen|switch|set|turn|check|create|new|"
                     r"send|reply|download|install|scroll|press|copy|paste|select|delete|rename|move)\b", re.I)


def _fix_steps(steps: list[str], reply: str, utterance: str = "") -> tuple[list[str], str]:
    """Make a plan executable on THIS PC: web versions for missing apps, merge Notepad lines,
    drop redundant steps."""
    from . import apps
    out, notes = [], []
    # only steps a PC can do; "Boil water" / "Get a cup" are dropped
    steps = [st for st in steps if _DOABLE.match(st or "")]
    steps = [re.sub(r"[\x00-\x1f]", "", s) for s in steps]          # JSON "\b" etc.
    for s in steps:
        # never add a save the user didn't ask for
        if re.match(r"\s*save\b", s, re.I) and not re.search(r"\bsave\b", utterance, re.I):
            continue
        # "Open Notepad and type: X" + "Save it as P"  ->  "Open Notepad, type X, and save it as P"
        sm = re.match(r"\s*save\s+(?:it|this|the\s+(?:file|note))?\s*as\s+(.+?)\s*\.?$", s, re.I)
        tw = re.match(r"\s*open notepad and type:\s*(.+)$", out[-1], re.I) if out else None
        if sm and tw:
            out[-1] = f"Open Notepad, type {tw.group(1).strip()}, and save it as {sm.group(1)}"
            continue
        # "Open Notepad" + "Type: X"  ->  one reliable step
        tm = re.match(r"\s*(?:type|write)\s*:?\s*(.+)$", s, re.I)
        if tm and out and re.fullmatch(r"\s*open\s+notepad\s*\.?", out[-1], re.I):
            out[-1] = f"Open Notepad and type: {tm.group(1).strip()}"
            continue
        m = re.fullmatch(r"\s*open\s+(?:the\s+)?(.+?)(?:\s+app)?\s*\.?", s, re.I)
        if m and not web.parse(s) and not re.search(r"\.\w{2,}", m.group(1)):
            name = m.group(1).strip()
            if not actions.known_app(name) and apps.find(name) is None:
                site = WEB_APPS.get(name.lower())
                if site:
                    s = f"Open {site}"
                    notes.append(f"{name} isn't installed, so I'll use the web version")
                else:
                    notes.append(f"{name} isn't installed on this PC")
                    continue
        # don't add "play" to a search the user only asked to SEARCH
        if re.search(r"\s+and\s+play the first video\s*$", s, re.I) and utterance and \
                not re.search(r"\b(play|listen|watch|put on|music|song|songs|video)\b", utterance, re.I):
            s = re.sub(r"\s+and\s+play the first video\s*$", "", s, flags=re.I)
        nm = re.match(r"\s*open notepad and type:\s*(.+)$", s, re.I)
        if nm and out and re.match(r"\s*open notepad and type:", out[-1], re.I):
            out[-1] = out[-1].rstrip(" .") + "; " + nm.group(1).strip()   # one note, not three
            continue
        out.append(s)
    # "Open youtube.com" right before "Search YouTube for ..." is redundant
    out = [s for i, s in enumerate(out) if not (
        i + 1 < len(out) and (p := web.parse(s)) and p.get("query") is None
        and (q := web.parse(out[i + 1])) and q.get("site")
        and q["site"].lower() in (p.get("site") or "").lower())]
    if notes:
        note = "; ".join(notes)
        reply = (reply.rstrip(".") + ". " if reply else "") + note[:1].upper() + note[1:] + "."
    return out, reply


def _clean(plan: dict, utterance: str) -> dict:
    intent = str(plan.get("intent", "")).lower()
    if intent not in INTENTS:
        intent = "do" if plan.get("steps") else "answer"
    steps = [str(s).strip() for s in (plan.get("steps") or []) if str(s).strip()][:4]
    reply = str(plan.get("reply") or "").strip()
    # a how-to / knowledge question is answered, not acted out in Notepad
    if intent == "do" and _QUESTION.match(utterance.strip()) and not _PC_VERB.search(utterance) \
            and all(re.match(r"\s*open notepad", s, re.I) for s in steps):
        intent, steps = "answer", []
        plan["_needs_answer"] = True
    if intent == "do" and not steps:
        steps = [utterance]
    if intent != "do":
        steps = []
    steps, reply = _fix_steps(steps, reply, utterance)
    if intent == "do" and not steps:
        intent = "answer"
    reply = reply or ("On it." if steps else "Okay.")
    out = {"intent": intent, "thought": str(plan.get("thought") or "").strip()[:300],
            "reply": reply[:600], "steps": steps, "source": "model"}
    if plan.get("_needs_answer"):
        out["needs_answer"] = True
    return out
