"""Personal profile: what LocalPilot learns about its user, from what they SAY.

* Explicit:  "remember that I prefer Brave", "forget my birthday", "forget everything about me",
             "what do you know about me?", "what's my name?"  -> handled here, deterministically.
* Implicit:  "my name is Pranshu", "call me P", "I live in Pune", "I work at X", "I'm a student",
             "I like / love / prefer / hate X", "my favourite X is Y", "my birthday is ..." -> stored
             quietly as facts; the reply itself is still produced by the brain.

Facts live in memory.json on this PC only; secrets (passwords, PINs, card numbers ...) are refused by
Memory.add_fact. Facts are recalled into every brain / answer prompt (see Memory.profile).
"""
from __future__ import annotations

import re

_NAME_STOP = {"not", "going", "trying", "here", "back", "done", "sorry", "fine", "good", "ok", "okay",
              "just", "so", "very", "really", "a", "an", "the", "in", "at", "on", "from", "busy",
              "tired", "working", "looking", "using", "asking", "feeling", "sure", "ready", "thinking"}

_EXPLICIT_REMEMBER = re.compile(r"^\s*(?:please\s+)?(?:remember|note|keep in mind|don't forget)\s+(?:that\s+)?(.+)$", re.I)
_FORGET_ALL = re.compile(r"^\s*(?:please\s+)?forget\s+(?:everything|all)(?:\s+(?:about|you know about)\s+me)?\s*[.!]?\s*$", re.I)
_FORGET = re.compile(r"^\s*(?:please\s+)?forget\s+(?:that\s+|about\s+)?(.+?)\s*[.!]?\s*$", re.I)
_ABOUT_ME = re.compile(r"\b(what do you (?:know|remember) about me|what have you learn(?:ed|t) about me|"
                       r"tell me (?:what you know )?about (?:me|myself)|do you remember me|who am i)\b", re.I)
_MY_NAME_Q = re.compile(r"^\s*(?:do you know |do you remember )?what(?:'s| is) my name\??\s*$|"
                        r"^\s*do you (?:know|remember) my name\??\s*$", re.I)

_IMPLICIT = [
    ("name", re.compile(r"\b(?:my name is|my name's|call me)\s+([A-Z][\w'-]{1,30}(?:\s+[A-Z][\w'-]{1,30})?)", re.I),
     "The user's name is {0}."),
    ("name", re.compile(r"^\s*(?i:(?:hi|hello|hey)[,!\s]*)?(?i:i am|i'm)\s+([A-Z][\w'-]{1,30})\s*[.!]?\s*$"),
     "The user's name is {0}."),
    ("location", re.compile(r"\bi (?:live|stay) in\s+([\w ,.'-]{2,50})", re.I), "The user lives in {0}."),
    ("location", re.compile(r"\bi(?:'m| am) from\s+([\w ,.'-]{2,50})", re.I), "The user is from {0}."),
    ("job", re.compile(r"\bi work (?:at|for)\s+([\w &,.'-]{2,60})", re.I), "The user works at {0}."),
    ("job", re.compile(r"\bi work as (?:a |an )?([\w &,.'-]{2,60})", re.I), "The user works as {0}."),
    ("job", re.compile(r"\bi(?:'m| am) (?:a|an)\s+(student|developer|engineer|designer|teacher|doctor|founder|"
                       r"programmer|researcher|freelancer|manager|writer|artist|[\w-]+ (?:developer|engineer|student))\b",
                       re.I), "The user is a {0}."),
    ("", re.compile(r"\bi (?:really )?(like|love|enjoy|prefer|hate|dislike|don't like)\s+([\w &,.'+#-]{2,80})", re.I),
     "The user {0} {1}."),
    ("", re.compile(r"\bmy fav(?:ou?rite)? ([\w ]{2,30}?) is\s+([\w &,.'+#-]{2,60})", re.I),
     "The user's favourite {0} is {1}."),
    ("birthday", re.compile(r"\bmy birthday is\s+(?:on\s+)?([\w ,'-]{3,30})", re.I), "The user's birthday is {0}."),
    ("", re.compile(r"\bi(?:'m| am) (working on|building|learning)\s+([\w &,.'+#-]{2,80})", re.I),
     "The user is {0} {1}."),
    ("", re.compile(r"\bmy ((?:cat|dog|pet|wife|husband|partner|son|daughter|brother|sister|mom|mother|dad|"
                    r"father|friend|boss|company|team|laptop|pc|car|startup))(?:'s name is| is called| is named)"
                    r"\s+([A-Z][\w'-]{1,30})", re.I),
     "The user's {0} is called {1}."),
]

_REQUEST = re.compile(r"\b(can you|could you|would you|please|open|play|search|find|show|tell me|look up|"
                      r"research|start|launch|make|write|create|send|set|turn|remind|check|help me|what|"
                      r"who|when|where|why|how|which)\b", re.I)


def is_statement(text: str) -> bool:
    """A plain statement about the user (no question, no request) -- nothing to act on."""
    t = (text or "").strip()
    return bool(t) and "?" not in t and not _REQUEST.search(t)


def _clean(v: str) -> str:
    v = re.split(r"\b(?:and|but|so|because|which|please|can you|could you)\b|[.!?;]", v, 1)[0]
    return v.strip(" ,.'\"")


def learn(text: str) -> list[tuple[str, str]]:
    """Facts (key, text) stated in an utterance. Conservative: only clear first-person statements."""
    out = []
    t = (text or "").strip()
    if not t or t.endswith("?"):
        return out
    # "I like X and I prefer Y" -> two clauses, each learned on its own
    clauses = [c for c in re.split(r"(?i)[,;.]?\s+(?:and|but|also)\s+(?=(?:i|i'm|my)\b)|[.;!]\s+", t) if c.strip()]
    if len(clauses) > 1:
        seen = []
        for c in clauses:
            for f in learn(c):
                if f not in seen:
                    seen.append(f)
        return seen
    for key, rx, fmt in _IMPLICIT:
        for m in rx.finditer(t):
            parts = [_clean(g) for g in m.groups()]
            if not all(parts):
                continue
            if key == "name":
                first = parts[0].split()[0].lower()
                if first in _NAME_STOP or len(parts[0]) < 2:
                    continue
                parts = [" ".join(w[:1].upper() + w[1:] for w in parts[0].split())]
            if len(parts) == 2 and fmt.startswith("The user {0}"):
                verb = {"don't like": "doesn't like", "like": "likes", "love": "loves", "enjoy": "enjoys",
                        "prefer": "prefers", "hate": "hates", "dislike": "dislikes"}[parts[0].lower()]
                parts = [verb, parts[1]]
            out.append((key, fmt.format(*parts)))
    return out


def explicit(text: str, memory) -> str | None:
    """Handle remember / forget / what-do-you-know commands. Returns the reply, or None."""
    t = (text or "").strip()
    if memory is None or not t:
        return None
    if _FORGET_ALL.match(t):
        memory.forget_all()
        return "Done. I've forgotten everything I knew about you, and our past conversation."
    m = _EXPLICIT_REMEMBER.match(t)
    if m:
        what = m.group(1).strip().rstrip(".")
        learned = learn(what if re.match(r"(?i)(i|my)\b", what) else "")
        if learned:
            for key, fact in learned:
                memory.add_fact(fact, "told", key)
        elif memory.add_fact(_third_person(what), "told") is None:
            return "I won't store that - it looks like a password or other secret."
        return "Got it, I'll remember that."
    m = _FORGET.match(t)
    if m and not re.match(r"(?i)forget it\b", t):
        gone = memory.forget(m.group(1))
        if gone:
            return "Forgotten: " + "; ".join(_second_person(g) for g in gone) + "."
        return "I didn't have anything stored about that."
    if _MY_NAME_Q.match(t):
        name = memory.fact("name")
        if name:
            return f"You're {name.replace(chr(39) + 's name is', '').replace('The user', '').strip(' .')}."
        return "You haven't told me your name yet. What should I call you?"
    if _ABOUT_ME.search(t):
        facts = memory.facts()
        if not facts:
            return ("I don't know much about you yet. Tell me things like your name, what you work on, or "
                    "what you like, and I'll remember them.")
        lines = [f["text"].replace("The user's", "Your").replace("The user", "You") for f in facts[-15:]]
        lines = [re.sub(r"^You (\w+)s\b", r"You \1", ln) if not ln.startswith(("Your", "You are", "You is"))
                 else ln.replace("You is", "You are") for ln in lines]
        return "Here's what I know about you: " + " ".join(lines)
    return None


def _second_person(fact: str) -> str:
    s = fact.rstrip(".").replace("The user's", "your").replace("The user is", "you are")
    return re.sub(r"^The user (\w+?)s\b", r"you \1", s).replace("The user", "you")


def _third_person(s: str) -> str:
    s = s.strip().rstrip(".")
    s = re.sub(r"(?i)^i am\b|^i'm\b", "The user is", s)
    s = re.sub(r"(?i)^my\b", "The user's", s)
    s = re.sub(r"(?i)^i\b", "The user", s)
    return s[:1].upper() + s[1:] + "."
