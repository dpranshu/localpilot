"""Resource profiles for LocalPilot's research browser (Vercel agent-browser + Chrome for Testing
headless shell) -- measurable differences, enforced in the backend.

Measured on this PC (5 real pages, 20 Nov 2026): idle ~41 MB, peak ~150-160 MB, 5 processes,
median page read ~1.3 s, ~55 ms per preview screenshot. memory_mb is a HARD limit (Windows Job
Object: commit memory of the agent-browser daemon and every browser process it starts).
Eco also disables images (launch flag). Chromium starts only for a task and closes when idle.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    http_first: bool          # (unused: retired HTTP-first reader; kept for saved-settings compatibility)
    browser_fallback: bool    # (unused: there is one browser backend, agent-browser)
    max_tabs: int
    max_pages: int            # navigations per research task
    block_images: bool        # skip images in the agent browser (text research doesn't need them)
    recycle_every: int        # fresh browser context after this many pages (memory creep)
    idle_close_s: int         # close Chromium after this long without activity
    memory_mb: int            # hard memory budget for all browser processes
    task_timeout_s: int       # research task wall-clock limit
    preview_fps: float        # live preview frames per second (only while visible)

    def as_dict(self):
        return asdict(self)


PROFILES = {
    "eco": Profile("eco", True, True, max_tabs=1, max_pages=6, block_images=True, recycle_every=5,
                   idle_close_s=60, memory_mb=700, task_timeout_s=90, preview_fps=1.0),
    "balanced": Profile("balanced", True, True, max_tabs=2, max_pages=15, block_images=False, recycle_every=8,
                        idle_close_s=180, memory_mb=1100, task_timeout_s=180, preview_fps=1.0),
    "performance": Profile("performance", True, True, max_tabs=3, max_pages=25, block_images=False,
                           recycle_every=12, idle_close_s=600, memory_mb=1700, task_timeout_s=300, preview_fps=2.0),
}


def get(name: str) -> Profile:
    return PROFILES.get((name or "balanced").lower(), PROFILES["balanced"])
