"""Shared types for LocalPilot's browser: errors, limits and counters.

(Moved out of the retired Playwright engine so the agent-browser engine stands on its own.)
"""
from __future__ import annotations

from dataclasses import dataclass, field


class BrowserStopped(Exception):
    """The user stopped the task. Final: nothing resumes it."""


class BrowserError(Exception):
    """A browser operation failed or was refused by a safety check."""


@dataclass
class Limits:
    nav_timeout_ms: int = 20000
    op_timeout_s: float = 45.0
    max_pages: int = 25            # navigations per task
    max_tabs: int = 3
    max_text_chars: int = 200_000


@dataclass
class Stats:
    requests: int = 0
    blocked_ads: int = 0           # ad / tracker filter
    blocked_unsafe: int = 0        # URL guard / threat list / private network
    blocked_forms: int = 0         # refused non-link clicks (forms can't be submitted)
    blocked_lean: int = 0
    blocked_downloads: int = 0
    popups_closed: int = 0
    pages_visited: int = 0
    crashes: int = 0
    last_blocked: list = field(default_factory=list)

    def note(self, kind: str, url: str, why: str):
        self.last_blocked = (self.last_blocked + [(kind, url[:120], why[:80])])[-30:]
