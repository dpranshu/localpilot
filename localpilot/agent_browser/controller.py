"""Agent Browser controller: the one owner of LocalPilot's research browser.

* The ONLY browser backend is Vercel agent-browser (abengine.AgentBrowserEngine) inside LocalPilot's
  security envelope. There is no fallback engine and no HTTP side-channel: if agent-browser can't
  run, browsing fails clearly and nothing invents an answer.
* Chromium starts only when a task needs it and closes after the profile's idle time.
* Pause / Resume / Stop are the LocalPilot session's own events (one source of truth). A stopped
  task ends; nothing restarts it.
* The UI only reads snapshot() and calls these methods; all enforcement happens here and below.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import profiles as _profiles

STATES = ("Idle", "Starting", "Searching", "Reading", "Verifying", "Writing", "Paused", "Blocked",
          "Completed", "Stopped", "Error")


@dataclass
class Source:
    n: int
    url: str
    title: str
    site: str
    published: str
    published_source: str
    retrieved_at: str
    method: str                  # always "agent-browser"
    chars: int
    flags: list = field(default_factory=list)      # sponsored / affiliate / injection notes
    excerpt: str = ""


class BrowserController:
    def __init__(self, stop_event: threading.Event, hold_event: threading.Event, profile: str = "balanced",
                 allow_hosts=(), host_map=None, threats=None, on_change=lambda: None, **_old_options):
        self.stop_event, self.hold_event = stop_event, hold_event
        self.foreign_clients: list[dict] = []           # port-guard breaches (shown in Security)
        self._grant = None                              # Take Control authorization (see grant_control)
        self.profile = _profiles.get(profile)
        self.allow_hosts, self.host_map = frozenset(allow_hosts), host_map or {}
        self.on_change = on_change
        if threats is None:
            from .threatintel import ThreatIntel
            threats = ThreatIntel().load()          # cached list only; never downloads at startup
        self.threats = threats
        self._browser = None
        self._lock = threading.RLock()
        self._op = threading.Lock()                 # one browser operation at a time
        self._task_thread: threading.Thread | None = None
        self._last_activity = time.time()
        self.state, self.task, self.url, self.detail, self.action = "Idle", "", "", "", ""
        self.sources: list[Source] = []
        self.events: list[dict] = []
        self.timeline: list[dict] = []
        self.visited: list[str] = []
        self.taking_control = None                  # not supported (would need a second, visible browser)
        self.active = False                         # a research task is using the browser
        self._closed = False
        # started LAST: the watcher reads the attributes above
        self._idle_thread = threading.Thread(target=self._idle_watch, daemon=True, name="LocalPilot-BrowserIdle")
        self._idle_thread.start()

    # ------------------------------------------------------------------ state
    def _set(self, state=None, detail=None, url=None, action=None):
        with self._lock:
            if state:
                self.state = state
            if detail is not None:
                self.detail = detail
            if action is not None:
                self.action = action
            if url is not None:
                self.url = url
                if url and (not self.visited or self.visited[-1] != url):
                    self.visited = (self.visited + [url])[-80:]
            self._last_activity = time.time()
            self.timeline = (self.timeline + [{"at": time.strftime("%H:%M:%S"), "state": self.state,
                                               "detail": self.detail, "url": self.url}])[-300:]
        try:
            self.on_change()
        except Exception:
            pass

    def _event(self, kind, url, why):
        with self._lock:
            self.events = (self.events + [{"at": time.strftime("%H:%M:%S"), "kind": kind, "url": url[:160],
                                           "why": why[:160]}])[-200:]

    def start_task(self, task: str):
        """A research task begins: reset what the panel shows for it."""
        with self._lock:
            self.task, self.sources, self.active = task, [], True
            self.bing_blocked = False
        self._set("Starting", "Getting the research browser ready", "")

    def end_task(self, state: str, detail: str):
        with self._lock:
            self.active = False
        self._set(state, detail)

    @property
    def busy(self) -> bool:
        return self.active or (self._task_thread is not None and self._task_thread.is_alive())

    @property
    def browser_running(self) -> bool:
        b = self._browser
        return bool(b and b.alive())

    def snapshot(self) -> dict:
        """Everything the UI shows -- read from real state."""
        b = self._browser
        bsum = b.summary() if self.browser_running else {}
        with self._lock:
            evs = list(self.events)
            if b is not None:
                evs += [{"at": "", "kind": k, "url": u, "why": w} for k, u, w in b.stats.last_blocked]
            return {"state": "Paused" if (self.hold_event.is_set() and self.busy) else self.state,
                    "task": self.task, "url": self.url, "domain": urlsplit(self.url).hostname or "",
                    "detail": self.detail, "action": self.action, "sources": list(self.sources),
                    "events": evs[-200:], "timeline": list(self.timeline), "visited": list(self.visited),
                    "busy": self.busy, "browser_running": self.browser_running, "profile": self.profile.name,
                    "engine": "agent-browser",
                    "ads_blocked": bsum.get("ads_trackers_blocked", 0),
                    "downloads_blocked": bsum.get("downloads_blocked", 0),
                    "security_blocked": bsum.get("unsafe_blocked", 0) + bsum.get("forms_blocked", 0),
                    "memory": bsum.get("memory", {}), "threats": self.threats.status(),
                    "taking_control": False}

    # ------------------------------------------------------------------ lifecycle
    def _ensure_browser(self):
        if not self.browser_running:
            from .abengine import AgentBrowserEngine
            self._set("Starting", "Starting the research browser (agent-browser)")
            self._browser = AgentBrowserEngine(profile=self.profile, stop_event=self.stop_event,
                                               hold_event=self.hold_event, allow_hosts=self.allow_hosts,
                                               host_map=self.host_map, threats=self.threats).start()
            self._browser.on_breach = self._on_breach
        return self._browser

    def _on_breach(self, b: dict):
        """A foreign local process connected to the research browser (see portguard)."""
        self.foreign_clients.append(b)
        self._grant = None
        self._event("foreign client", f"127.0.0.1:{b['port']}",
                    f"{b['name']} (pid {b['pid']}) connected to the research browser - task paused, "
                    "browser discarded. Say 'continue' to go on with a fresh browser, or 'stop'.")
        self._set(detail=f"Paused: another program ({b['name']}) tried to connect to the browser")

    def close_browser(self):
        b, self._browser = self._browser, None
        if b is not None:
            b.close()

    def _idle_watch(self):
        while not self._closed:
            time.sleep(1.0)
            try:
                self._idle_check()
            except Exception:
                pass                                   # never let the watcher die

    def _idle_check(self):
        if (self.browser_running and not self.busy
                and time.time() - self._last_activity > self.profile.idle_close_s):
            self.close_browser()
            self._set(detail="Browser closed after being idle")

    def apply_settings(self, v: dict):
        """Profile + user overrides -> the limits the backend enforces."""
        from dataclasses import replace
        base = _profiles.get(v.get("profile", "balanced"))
        self.profile = replace(base, max_tabs=int(v.get("max_tabs", base.max_tabs)),
                               memory_mb=int(v.get("memory_mb", base.memory_mb)),
                               max_pages=int(v.get("max_pages", base.max_pages)),
                               task_timeout_s=int(v.get("task_timeout_s", base.task_timeout_s)),
                               preview_fps=float(v.get("preview_fps", base.preview_fps)),
                               idle_close_s=base.idle_close_s if v.get("auto_close_idle", True) else 10**9)
        if self.browser_running and not self.busy:
            self.close_browser()                    # the next start uses the new budget

    def set_profile(self, name: str):
        self.profile = _profiles.get(name)
        if self.browser_running and not self.busy:
            self.close_browser()

    def shutdown(self):
        self._closed = True
        self.close_browser()

    def clear_session_data(self):
        """Close the browser: its temporary profile, cookies and cache go with it."""
        self.close_browser()
        with self._lock:
            self.visited, self.timeline = [], []

    # ------------------------------------------------------------------ research operations
    BING_GAP_S = 4.0            # polite spacing between Bing searches

    def search(self, query: str, news: bool = False, github: bool = False, wiki: bool = False):
        """Open a real results page in the agent-browser and parse it. Returns (results, captcha?).
        github=True: GitHub's repository search; wiki=True: Wikipedia's search. If Bing has shown a
        CAPTCHA in this task it is NOT asked again (CAPTCHAs are never solved or worked around):
        web searches go to Wikipedia instead."""
        from ..research import search as _s
        if not github and not wiki and getattr(self, "bing_blocked", False):
            wiki = True
        if github:
            name, hints = _s.project_terms(query)
            url = _s.github_url(name or query)
        elif wiki:
            url = _s.wiki_url(query)
        else:
            gap = self.BING_GAP_S - (time.time() - getattr(self, "_last_bing", 0.0))
            if gap > 0:
                self._set(None, "Waiting a moment between searches (polite rate)")
                end = time.time() + gap
                while time.time() < end and not self.stop_event.is_set():
                    time.sleep(0.1)
            self._last_bing = time.time()
            url = _s.search_url(query, news)
        where = "GitHub" if github else "Wikipedia" if wiki else ("news" if news else "the web")
        self._set("Searching", f"Searching {where} for: {query[:80]}", url,
                  action=f"open {urlsplit(url).hostname}")
        with self._op:
            b = self._ensure_browser()
            r = b.goto(url)
            html = str(b._cli("get", "html", "html", timeout=30).get("html") or "")
            # the challenge text can sit deep in the HTML: check what the page actually SAYS
            text = str(b._cli("get", "text", "body", timeout=30).get("text") or "")[:4000]
            parse = (_s.parse_wikipedia if wiki else (lambda h: _s.parse_github(h, name, hints)) if github
                     else _s.parse_bing_news if news else _s.parse_bing)
            if not parse(html) and not _s.is_captcha(r.get("title", ""), text):
                b._cli("wait", "1500", timeout=10)                 # results may still be rendering
                html = str(b._cli("get", "html", "html", timeout=30).get("html") or "")
                text = str(b._cli("get", "text", "body", timeout=30).get("text") or "")[:4000]
        self._set(None, None, r["url"])
        if _s.is_captcha(r.get("title", ""), text):
            self._event("captcha", r["url"], "search engine asked for a CAPTCHA -- not solved")
            if not github and not wiki:
                self.bing_blocked = True
            return [], True
        if wiki:
            return _s.parse_wikipedia(html), False
        if github:
            return _s.parse_github(html, name, hints), False
        return (_s.parse_bing_news(html) if news else _s.parse_bing(html)), False

    def read(self, url: str, why: str = ""):
        """Open a page in the agent-browser, extract its main content, record it as a source."""
        from . import classify
        self._set("Reading", why or f"Reading {urlsplit(url).hostname}", url, action=f"open {url[:80]}")
        with self._op:
            pc = self._ensure_browser().read(url)
        self._set("Verifying", "Checking for ads, affiliate links and injected instructions", pc.final_url,
                  action="extract main content")
        inj = classify.injection_signals(pc.text or "")
        flags = [f"instruction-like text (ignored): {x[:60]}" for x in inj[:2]]
        for x in inj[:2]:
            self._event("prompt injection", pc.final_url, x)
        with self._lock:
            src = Source(len(self.sources) + 1, pc.final_url, pc.title or "", pc.site or "",
                         pc.published or "", pc.published_source or "", pc.retrieved_at or "",
                         "agent-browser", len(pc.text or ""), flags, (pc.text or "")[:280])
            self.sources.append(src)
        self._set(None, f"{len(self.sources)} sources collected", pc.final_url)
        return pc, src

    def scroll(self, pages: float = 1.0):
        with self._op:
            self._ensure_browser().scroll(pages)
        self._set(None, None, None, action="scroll down")

    # ------------------------------------------------------------------ user actions (UI)
    def navigate(self, url: str):
        """User typed an address: the agent browser goes there (same protections)."""
        if self.busy:
            raise RuntimeError("pause or stop the current task first")
        if "://" not in url:
            url = "https://" + url
        self.stop_event.clear()
        self._set("Reading", "Opening the address you entered", url, action=f"open {url[:80]}")
        try:
            with self._op:
                r = self._ensure_browser().goto(url)
            self._set("Idle", r.get("title", ""), r["url"])
        except Exception as e:
            self._set("Blocked" if "blocked" in str(e) else "Error", str(e)[:140], url)
            self._event("blocked" if "blocked" in str(e) else "error", url, str(e))

    def history(self, step: int):
        if self.busy or not self.browser_running:
            return
        try:
            with self._op:
                r = self._browser.history(step)
            self._set(None, r.get("title", ""), r["url"])
        except Exception as e:
            self._event("error", self.url, str(e))

    # ------------------------------------------------------------------ live viewers (PiP / expanded)
    def viewer_attach(self):
        """A viewer window opened: the official stream is wanted (turned on when the browser runs)."""
        with self._lock:
            self._viewers = getattr(self, "_viewers", 0) + 1

    def viewer_detach(self):
        """A viewer closed: when the last one goes, the stream is turned OFF (no idle input channel)."""
        with self._lock:
            self._viewers = max(0, getattr(self, "_viewers", 0) - 1)
            last = self._viewers == 0
        b = self._browser
        if last and b is not None and self.browser_running and b.stream_wanted:
            try:
                b.set_stream(False)
            except Exception:
                pass

    def stream_endpoint(self) -> str | None:
        """ws:// URL of the SAME agent-browser session the agent drives, or None (no browser yet /
        no viewer). Turns the stream on lazily, so a viewer opened before browsing still connects."""
        b = self._browser
        if b is None or not self.browser_running or getattr(self, "_viewers", 0) <= 0                 or getattr(b, "breach", None):
            return None
        port = b.stream_port()
        if port is None:
            try:
                port = b.set_stream(True)
            except Exception:
                return None
        return f"ws://127.0.0.1:{port}" if port else None

    # ------------------------------------------------------------------ Take Control authorization
    def grant_control(self) -> str | None:
        """Issue a one-time input grant -- ONLY while no agent action can run (task paused, or no
        task). The viewer calls this after the user explicitly confirmed. Returns the token or None."""
        b = self._browser
        if b is None or not self.browser_running or getattr(b, "breach", None):
            return None
        if self.busy and not self.hold_event.is_set():
            return None
        import secrets
        self._grant = secrets.token_hex(16)
        self._event("take control", self.url, "user took control (agent paused); input enabled")
        return self._grant

    def revoke_control(self):
        if self._grant is not None:
            self._event("take control", self.url, "control returned; input disabled (agent stays paused)")
        self._grant = None

    def input_allowed(self, token: str | None) -> bool:
        """True only for the CURRENT grant, while the agent still can't act and the browser is clean."""
        import hmac
        g, b = self._grant, self._browser
        if not (g and token and hmac.compare_digest(g, token)):
            return False
        if b is None or not self.browser_running or getattr(b, "breach", None):
            return False
        return not (self.busy and not self.hold_event.is_set())

    def stream_lost(self):
        """The viewer couldn't reach the stream (dropped / stale port): re-enable it on the next call."""
        b = self._browser
        if b is not None:
            b._stream_port = None

    def preview_frame(self) -> bytes | None:
        """Real JPEG of the agent-browser's current page, or None (no browser / busy / paused).
        Never fabricated, never queued behind agent work."""
        b = self._browser
        if b is None or not self.browser_running:
            return None
        # between the engine's individual commands (open, get html, ...) -- so the view follows the
        # agent page by page; try_screenshot itself never waits behind or delays agent work
        return b.try_screenshot()

    def take_control(self, *_):
        """Not supported: it would need a second, visible browser (the task stays as it is)."""
        self._event("take control", self.url, "not available: LocalPilot has one isolated, headless browser")
        return False

    def return_control(self):
        return None


class _Stopped(Exception):
    pass
