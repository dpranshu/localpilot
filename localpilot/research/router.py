"""Does this request need live web research, and in which mode?

Order in the session: control (stop/continue) -> inspect (my PC) -> RESEARCH -> brain.
Two layers:
  1. rules (instant): explicit research words, or a question about something time-sensitive
  2. Qwen (brain.think) decides the rest: its "research" intent sends a question here too
Requests to DO something on the PC ("search YouTube for X", "open github.com") are not research:
they open the user's normal flow.
"""
from __future__ import annotations

import re

_EXPLICIT = re.compile(r"^\s*(please\s+)?((do|perform|run|conduct|start)\s+(a\s+|an\s+)?)?(deep\s+research|research|look\s+up|find\s+out|"
                       r"fact[\s-]?check|verify|investigate|search\s+(the\s+web|online|the\s+internet)\s+(for|about))\b",
                       re.I)
_DEEP = re.compile(r"\b(deep\s+research|in[\s-]?depth|comprehensive|thorough(ly)?|detailed\s+report|"
                   r"full\s+report|write\s+a\s+report|deep\s+dive)\b", re.I)
_COMPARE = re.compile(r"\b(compare|comparison|versus|vs\.?|pros\s+and\s+cons|difference\s+between|"
                      r"which\s+is\s+better|official\s+documentation|docs)\b", re.I)
_LIVE = re.compile(r"\b(today|tonight|yesterday|this\s+(week|month|morning|year)|right\s+now|currently|"
                   r"current|latest|newest|recent(ly)?|breaking|news|headlines|happened|happening|"
                   r"announced?|announcement|released?|launch(ed)?|price|prices|stock|exchange\s+rate|"
                   r"score|weather|forecast|who\s+won|election|update[sd]?)\b", re.I)
_ABOUT_PROJECT = re.compile(r"\b(github\s+(project|repo|repository)|repository|repo\b|open[\s-]?source\s+project)\b", re.I)
_QUESTIONISH = re.compile(r"^\s*(what|who|when|where|which|how|why|is|are|did|does|do|has|have|can|tell\s+me|"
                          r"give\s+me|show\s+me|any)\b|\?\s*$", re.I)
# the user wants their PC to DO something (open / play / search on a site) -> not research
_PC_ACTION = re.compile(r"^\s*(please\s+)?(open|launch|start|play|go\s+to|visit|navigate|type|click|close|"
                        r"search\s+(youtube|amazon|flipkart|google\s+maps|spotify)\b)", re.I)
# about THIS computer -> inspect, not research
_MY_PC = re.compile(r"\b(my|this)\s+(pc|computer|laptop|screen|desktop|files?|folder)\b", re.I)


def wants_research(text: str) -> bool:
    t = (text or "").strip()
    if not t or _MY_PC.search(t):
        return False
    if _EXPLICIT.search(t):
        return True
    if _PC_ACTION.search(t):
        return False
    if _ABOUT_PROJECT.search(t) and _QUESTIONISH.search(t):
        return True
    if re.match(r"^\s*(please\s+)?compare\b", t, re.I) or (_COMPARE.search(t) and _QUESTIONISH.search(t)):
        return True
    return bool(_LIVE.search(t) and _QUESTIONISH.search(t))


def mode_for(text: str, default: str = "quick") -> str:
    t = text or ""
    if _DEEP.search(t):
        return "deep"
    if _COMPARE.search(t) or re.match(r"^\s*(please\s+)?research\b", t, re.I) or _ABOUT_PROJECT.search(t):
        return "research"
    return default if default in ("quick", "research", "deep") else "quick"


def clean_question(text: str) -> str:
    """'Research the latest Qwen model.' -> 'the latest Qwen model' kept as the question itself."""
    t = re.sub(r"^\s*(please\s+)?((do|perform|run|conduct|start)\s+(a\s+|an\s+)?)?(deep\s+research(\s+(on|into|about))?|research)\s+",
               "", text or "", flags=re.I)
    return t.strip() or (text or "").strip()
