"""agent-browser (Vercel Labs, Apache-2.0) as LocalPilot's browser engine -- inside LocalPilot's
own security envelope, because on its own (verified on this PC) agent-browser opened file:// pages,
saved page-triggered downloads, let its `read` command bypass the proxy, and runs an input-capable
live stream by default.

The envelope:
  * pinned version, SHA-256 verified, installed in %LOCALAPPDATA%/LocalPilot/bin
  * ALL browser traffic through LocalPilot's GuardProxy (private-network / DNS-rebinding / redirect
    protection, phishing list, host-level ad & tracker blocking) incl. loopback (<-loopback>)
  * URL gate before every navigation: http/https only, threat verdict (known-malicious/suspicious)
  * action policy: read, eval, upload, download, state and network commands DENIED
  * downloads land in a private quarantine folder and are deleted at once (counted, never opened)
  * the live WebSocket stream is disabled; preview = on-demand screenshots (temp file, deleted at once)
  * its detached daemon + Chromium are put in a Windows Job Object: hard memory budget, and
    kill-on-close so a LocalPilot crash leaves no browser behind
  * pause (no timeout) / stop (final) checked before every command; stop also kills in-flight work
Same public interface as browser.AgentBrowser, so the controller and UI don't change.
"""
from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid

import requests

import re

from . import extract, profiles as _profiles, urlguard
from .common import BrowserError, BrowserStopped, Limits, Stats
from .proxy import GuardProxy

VERSION = "0.43.0"
SHA256 = "ebcef5ebea93c7ccf0fe3f8c18eee908ae9174dfef8e52a3dda6f24a59f630dc"
URL = f"https://github.com/vercel-labs/agent-browser/releases/download/v{VERSION}/agent-browser-win32-x64.exe"
BASE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "LocalPilot")
BIN = os.path.join(BASE, "bin", f"agent-browser-{VERSION}.exe")
POLICY = os.path.join(BASE, "agent-browser-policy.json")
# The daemon's own action policy, enforced for EVERY client of the daemon (LocalPilot's CLI calls,
# the stream server's /api/command relay, and any other local process that finds the daemon port).
# It matches agent-browser's PROTOCOL action names ("evaluate", not the CLI's "eval"): the earlier
# deny-list named CLI commands and therefore denied nothing. Default DENY; only what LocalPilot uses.
POLICY_ALLOW = ["launch", "navigate", "url", "title", "tab_list", "innerhtml", "gettext", "snapshot",
                "tab_new", "tab_switch", "tab_close", "click", "scroll", "back", "forward", "reload",
                "screenshot", "wait", "close",
                "stream_enable", "stream_disable", "stream_status"]
# LocalPilot calls ONLY these agent-browser commands, and only these sub-commands. Everything else
# (eval, read, type, fill, press, keyboard, find, upload, download, network, cookies, storage, state,
# auth, plugin, mcp, chat, dashboard, connect, ...) is refused here -- the action policy alone is not
# enough: verified that agent-browser 0.43 does not deny `eval` despite a policy that lists it.
# No typing/filling/key commands exist at all, and clicks are limited to links (see click()), so a
# form can never be filled or submitted and nothing can be bought.
ALLOWED_COMMANDS = {
    "open": None,                                   # URL is gated in _run_raw
    "get": {"url", "title", "html", "text"},
    "snapshot": None,
    "click": None,                                  # @ref of a LINK from the latest snapshot only
    "scroll": {"down", "up"},
    "tab": {"new", "list", "close"},                # plus t<n> (validated below)
    "back": None, "forward": None, "reload": None,
    "screenshot": None,
    # "stream enable" is NOT allowed here: only set_stream() turns the official live stream on, and only
    # while a LocalPilot viewer is open (its input channel has no auth; the viewer is view-only unless
    # the user explicitly takes control)
    "stream": {"disable", "status"},
    "close": None,
    "wait": None,
}
_REF = re.compile(r"^@e\d{1,5}$")
_TAB = re.compile(r"^t\d{1,2}$")
_LINK_LINE = re.compile(r'^\s*-\s*link\b.*\[ref=(e\d+)\]')


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_installed(progress=lambda m: None) -> str:
    """Pinned agent-browser binary, verified by SHA-256 (re-downloaded if missing or altered)."""
    if os.path.exists(BIN) and _sha(BIN) == SHA256:
        return BIN
    os.makedirs(os.path.dirname(BIN), exist_ok=True)
    progress(f"Downloading agent-browser {VERSION}...")
    tmp = BIN + ".part"
    with requests.get(URL, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    if _sha(tmp) != SHA256:
        os.remove(tmp)
        raise BrowserError("agent-browser download failed its SHA-256 check -- not installed")
    os.replace(tmp, BIN)
    return BIN


CFT_DIR = os.path.join(os.path.expanduser("~"), ".agent-browser", "browsers")


CFT_VERSION = "158.0.8127.36"          # the Chrome for Testing build agent-browser 0.43 installs


def browser_executable(headless: bool = True) -> str | None:
    """Chrome for Testing, in agent-browser's own browsers folder -- never the user's Chrome / Edge /
    Brave and never Playwright's copy. Prefers the official CfT *headless shell* (measured on this PC:
    idle 41 MB vs 207 MB, peak ~150 MB vs ~500 MB, 5 vs 14-17 processes, same speed and the same
    extracted text), else the full Chrome for Testing that `agent-browser install` provides."""
    shell = glob.glob(os.path.join(CFT_DIR, "headless-shell-*", "chrome-headless-shell-win64", "chrome-headless-shell.exe"))
    if shell:
        return sorted(shell)[-1]
    found = glob.glob(os.path.join(CFT_DIR, "chrome-*", "chrome-win64", "chrome.exe"))
    return sorted(found)[-1] if found else None


def _download_headless_shell(progress) -> str | None:
    """Official CfT headless shell: resumable download (flaky networks), checked against Google's
    published MD5 before it is unpacked. None if it can't be fetched."""
    import base64
    import zipfile
    url = (f"https://storage.googleapis.com/chrome-for-testing-public/{CFT_VERSION}/win64/"
           "chrome-headless-shell-win64.zip")
    tmp = os.path.join(BASE, "tmp", f"headless-shell-{CFT_VERSION}.zip.part")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    try:
        head = requests.head(url, timeout=30)
        want = next((p.split("=", 1)[1] for p in head.headers.get("x-goog-hash", "").split(",")
                     if p.strip().startswith("md5=")), "")
        total = int(head.headers.get("content-length") or 0)
        for _ in range(8):                                    # resume after dropped connections
            have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            if total and have >= total:
                break
            try:
                with requests.get(url, headers={"Range": f"bytes={have}-"} if have else {}, stream=True,
                                  timeout=60) as r:
                    r.raise_for_status()
                    with open(tmp, "ab" if have else "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
                            have += len(chunk)
                            if total:
                                progress(f"Downloading the research browser... {have * 100 // total}%")
            except requests.RequestException:
                time.sleep(2)
        md5 = hashlib.md5(open(tmp, "rb").read()).digest()
        if not want or base64.b64encode(md5).decode() != want.strip():
            os.remove(tmp)
            return None
        dest = os.path.join(CFT_DIR, f"headless-shell-{CFT_VERSION}")
        zipfile.ZipFile(tmp).extractall(dest)
        os.remove(tmp)
        return browser_executable()
    except Exception:
        return None


def ensure_chrome(progress=lambda m: None) -> str:
    """Chrome for Testing for agent-browser: installed copy, else the headless shell (resumable,
    hash-checked), else `agent-browser install` (full Chrome for Testing)."""
    exe = browser_executable()
    if exe:
        return exe
    exe = _download_headless_shell(progress)
    if exe:
        return exe
    progress("Installing agent-browser's Chrome (one time, ~200 MB)...")
    r = subprocess.run([ensure_installed(progress), "install"], capture_output=True, text=True,
                       timeout=1800, creationflags=0x08000000, env=_clean_env())
    exe = browser_executable()
    if not exe:
        raise BrowserError("agent-browser's Chrome is not installed (download failed: "
                           f"{(r.stderr or r.stdout or '').strip()[-160:]})")
    return exe


def _clean_env() -> dict:
    """The parent environment WITHOUT anything that could redirect agent-browser: no inherited
    AGENT_BROWSER_* settings (e.g. a --profile pointing at the user's real Chrome profile, a CDP
    endpoint, an auto-connect flag) and no system proxies that would bypass the guard."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("AGENT_BROWSER_")}
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
              "all_proxy", "no_proxy"):
        env.pop(k, None)
    return env


def _write_policy():
    os.makedirs(os.path.dirname(POLICY), exist_ok=True)
    with open(POLICY, "w", encoding="utf-8") as f:
        json.dump({"default": "deny", "allow": POLICY_ALLOW}, f)


class AgentBrowserEngine:
    """Drop-in for browser.AgentBrowser, driven through the agent-browser CLI."""
    engine = "agent-browser"

    def __init__(self, headless: bool = True, lean: bool = True, filters=None, allow_hosts=(),
                 stop_event: threading.Event | None = None, hold_event: threading.Event | None = None,
                 limits: Limits | None = None, host_map: dict | None = None, profile=None, threats=None,
                 use_job: bool = True, keep_raw: bool = False, **_ignored):
        self.headless = True               # always: the user watches through the in-app preview
        self.allow_hosts = frozenset(h.lower() for h in allow_hosts)
        self.host_map = host_map or {}
        if filters is None:
            from . import filterlists
            filters, _ = filterlists.load()
        self.filters, self.threats = filters, threats
        self.stop_event = stop_event or threading.Event()
        self.hold_event = hold_event or threading.Event()
        self.profile = _profiles.get(profile) if isinstance(profile, (str, type(None))) else profile
        self.limits = limits or Limits(max_pages=self.profile.max_pages, max_tabs=self.profile.max_tabs)
        self.use_job, self.keep_raw = use_job, keep_raw
        self.stats = Stats()
        self.status = "stopped"
        self.recycles = 0
        self._since_recycle = 0
        self._tabs = 1
        self._lock = threading.Lock()
        self._proxy = self._job = None
        self._daemon_pid = None
        self._alive = False
        self._closing = False
        self.session = f"localpilot-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._link_refs: set = set()       # refs of LINKS in the latest snapshot (clickable)
        self.stream_wanted = False         # official live stream: only while a viewer is open
        self._stream_port = None
        self.quarantine = os.path.join(BASE, "quarantine", self.session)
        self.shotdir = os.path.join(BASE, "tmp", self.session)

    # ----------------------------------------------------------------- lifecycle
    def alive(self) -> bool:
        return self._alive

    def start(self):
        if self._alive:
            return self
        self.exe = ensure_installed()
        self.chrome = ensure_chrome()
        _sweep_stale_sessions()
        _write_policy()
        os.makedirs(self.quarantine, exist_ok=True)
        os.makedirs(self.shotdir, exist_ok=True)
        self._proxy = GuardProxy(self.allow_hosts, self.host_map, threats=self.threats, filters=self.filters,
                                 on_block=self._proxy_blocked)
        self.env = dict(_clean_env(),
                        AGENT_BROWSER_PROXY=f"http://127.0.0.1:{self._proxy.port}",
                        AGENT_BROWSER_PROXY_USERNAME=self._proxy.user,
                        AGENT_BROWSER_PROXY_PASSWORD=self._proxy.password,
                        AGENT_BROWSER_PROXY_BYPASS="<-loopback>",          # loopback also via the guard
                        AGENT_BROWSER_ACTION_POLICY=POLICY,
                        AGENT_BROWSER_CONTENT_BOUNDARIES="1",
                        AGENT_BROWSER_NO_VERSION_CHECK="1",                 # no direct outbound check
                        AGENT_BROWSER_ALLOW_FILE_ACCESS="0")
        if self.profile.block_images:                       # Eco: pages load without images
            self.env["AGENT_BROWSER_ARGS"] = "--blink-settings=imagesEnabled=false"
        self._alive = True
        self._closing = False
        threading.Thread(target=self._quarantine_watch, daemon=True, name="LocalPilot-ABQuarantine").start()
        threading.Thread(target=self._stop_watch, daemon=True, name="LocalPilot-ABStop").start()
        self._before_ab = {p for p, n in _procs() if "agent-browser" in n.lower()}
        if self.use_job:
            from .jobs import BrowserJob
            try:
                self._job = BrowserJob(self.profile.memory_mb)
            except OSError:
                self._job = None
        # nobody but LocalPilot may connect to the daemon / Chrome DevTools / stream ports (see portguard)
        from .portguard import PortGuard
        self.breach = None
        self._guard = PortGuard(self._our_pids, self._on_foreign_client).start()
        # internal setup, not agent actions: not held back by a pause (a stop still applies)
        if self.stop_event.is_set():
            raise BrowserStopped("stopped by user")
        with self._lock:
            self._run_raw(["open", "about:blank"], timeout=60)    # starts the daemon + browser
            self._run_raw(["stream", "disable"])                  # no input-capable WebSocket
        self._adopt()
        self.status = "ready"
        return self

    def _our_pids(self) -> set[int]:
        """Processes allowed to talk to the browser's local ports: those in our job (daemon, Chrome,
        LocalPilot's own CLI calls); without a job, LocalPilot's descendants."""
        if self._job is not None:
            return self._job.pids()
        from .jobs import descendants
        return {p for p, _ in descendants()}

    def _on_foreign_client(self, b: dict):
        """Another local process connected to the daemon, Chrome's DevTools port or the stream. Its
        first packets can't be stopped -- contain it: pause the task (it never resumes by itself),
        discard this browser (kills every port it could use) and report it."""
        self.breach = b
        self.hold_event.set()
        self.stats.blocked_unsafe += 1
        self.stats.note("foreign client", f"127.0.0.1:{b['port']}",
                        f"{b['name']} (pid {b['pid']}) - paused, browser discarded")
        cb = getattr(self, "on_breach", None)
        if cb:
            try:
                cb(b)
            except Exception:
                pass
        threading.Thread(target=self.close, daemon=True, name="LocalPilot-ABDiscard").start()

    def _daemon_port_file(self) -> str:
        return os.path.join(os.path.expanduser("~"), ".agent-browser", self.session + ".port")

    def _daemon_up(self) -> bool:
        try:
            port = int(open(self._daemon_port_file()).read().strip())
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return True
        except (OSError, ValueError):
            return False

    def _spawn_daemon(self):
        """Start agent-browser's daemon OURSELVES with a hidden console (CREATE_NO_WINDOW), in our job.
        Upstream starts it DETACHED (no console) and launches Chrome without CREATE_NO_WINDOW; Chrome for
        Testing's headless shell is a console program, so Windows then opens a NEW, visible console
        window for it on the user's desktop. Started from here, Chrome inherits the hidden console.
        The CLI calls that follow find this daemon ready and use it (same session, same options)."""
        env = dict(self.env, AGENT_BROWSER_DAEMON="1", AGENT_BROWSER_SESSION=self.session,
                   AGENT_BROWSER_EXECUTABLE_PATH=self.chrome, AGENT_BROWSER_DOWNLOAD_PATH=self.quarantine,
                   AGENT_BROWSER_IDLE_TIMEOUT_MS=str(max(60, self.profile.idle_close_s) * 1000),
                   AGENT_BROWSER_HIDE_SCROLLBARS="0")
        job = self._job if self.use_job else None
        p = subprocess.Popen([self.exe], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env,
                             creationflags=0x08000000 | 0x00000200 | (0x00000004 if job else 0))
        if job:
            job.adopt_handle(int(p._handle))
            _resume(int(p._handle))
        port_file = self._daemon_port_file()
        end = time.time() + 15
        while time.time() < end and p.poll() is None:
            try:
                if not self._daemon_up():
                    raise OSError
                # the bookkeeping the CLI writes after starting a daemon itself; without it the CLI
                # replaces this daemon (it only compares "debug", which needs a restart)
                with open(port_file[:-5] + ".config", "w") as f:
                    json.dump({"debug": False, "action_policy": POLICY, "confirm_actions": None,
                               "idle_timeout": f"{max(60, self.profile.idle_close_s)}s",
                               "default_timeout": None, "no_auto_dialog": False}, f)
                return
            except (OSError, ValueError):
                time.sleep(0.05)
        # not ready: the CLI starts its own daemon as before (works, but may show the console)

    def close(self):
        self._closing = True
        g = getattr(self, "_guard", None)
        if g is not None:
            g.stop()
        try:
            if self._alive:
                self._run_raw(["close"], timeout=20)
        except Exception:
            pass
        self._alive = False
        if self._job is not None:
            self._job.close()                                     # kills anything left (daemon, Chrome)
            self._job = None
        if self._proxy is not None:
            self._proxy.stop()
        for d in (self.shotdir, self.quarantine):
            shutil.rmtree(d, ignore_errors=True)
        # agent-browser's own per-session bookkeeping (.pid/.port/.target/...): leave nothing behind
        for f in glob.glob(os.path.join(os.path.expanduser("~"), ".agent-browser", self.session + ".*")):
            try:
                os.remove(f)
            except OSError:
                pass
        self.status = "stopped"

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.close()

    def _adopt(self):
        """Put agent-browser's detached daemon (+ the Chromium it launched) into our job."""
        if not self.use_job:
            return
        from .jobs import BrowserJob, descendants
        try:
            if self._job is None:
                self._job = BrowserJob(self.profile.memory_mb)
            new_ab = [p for p, n in _procs() if "agent-browser" in n.lower() and p not in self._before_ab]
            for pid in new_ab:
                kids = descendants(pid)
                if any(("chrome" in n.lower() or "msedge" in n.lower() or "headless" in n.lower()) for _, n in kids):
                    self._daemon_pid = pid
                    self._job.adopt_pids([pid] + [k for k, _ in kids])
                    break
        except OSError:
            self._job = None

    # ----------------------------------------------------------------- official live stream
    def set_stream(self, on: bool) -> int | None:
        """Turn agent-browser's official viewport stream on/off for THIS session. Returns the
        loopback port when on. Internal setup, not an agent action: not held back by a pause."""
        self.stream_wanted = bool(on)
        if not self._alive:
            return None
        with self._lock:
            self._run_raw(["stream", "enable" if on else "disable"], _viewer=True)
            d = self._run_raw(["stream", "status"]).get("data") or {}
        self._stream_port = d.get("port") if (on and d.get("enabled")) else None
        return self._stream_port

    def stream_port(self) -> int | None:
        return self._stream_port if (self._alive and self.stream_wanted) else None

    def _reapply_stream(self):
        """After the daemon restarted (recycling), put the stream back in the state LocalPilot wants:
        a fresh daemon must never keep an unrequested input-capable stream."""
        try:
            self.set_stream(self.stream_wanted)
        except BrowserError:
            self._stream_port = None

    def memory(self) -> dict:
        if not self._job:
            return {"current_mb": None, "peak_mb": None, "budget_mb": self.profile.memory_mb, "enforced": False}
        cur, peak = self._job.memory_mb_used()
        return {"current_mb": round(cur), "peak_mb": round(peak), "budget_mb": self.profile.memory_mb,
                "enforced": bool(self._job.members)}

    # ----------------------------------------------------------------- safety plumbing
    def _proxy_blocked(self, host, why):
        if "ad/tracker" in why:
            self.stats.blocked_ads += 1
        else:
            self.stats.blocked_unsafe += 1
        self.stats.note("ad/tracker" if "ad/tracker" in why else "unsafe (network)", host, why)

    def _quarantine_watch(self):
        seen = set()
        while self._alive:
            try:
                for name in os.listdir(self.quarantine):
                    path = os.path.join(self.quarantine, name)
                    stem = name.split(".crdownload")[0]
                    if stem not in seen:
                        seen.add(stem)
                        self.stats.blocked_downloads += 1
                        self.stats.note("download", name, "downloads are blocked: deleted unopened")
                    try:
                        os.remove(path) if os.path.isfile(path) else shutil.rmtree(path, ignore_errors=True)
                    except OSError:
                        pass
            except OSError:
                pass
            time.sleep(0.25)

    def _stop_watch(self):
        while self._alive:
            if self.stop_event.wait(0.2):
                if self._proxy is not None:
                    self._proxy.stopped.set(); self._proxy.drop_all()   # in-flight loads die at once
                return

    def _checkpoint(self):
        while self.hold_event.is_set() and not self.stop_event.is_set():   # paused: no timeout
            if self._closing:
                raise BrowserStopped("browser closed")
            self.status = "paused"
            time.sleep(0.05)
        if self.stop_event.is_set():
            self.status = "stopped by user"
            raise BrowserStopped("stopped by user")

    @staticmethod
    def check_command(args) -> None:
        """Raise unless this exact command + sub-command is on LocalPilot's allow-list."""
        if not args or args[0] not in ALLOWED_COMMANDS:
            raise BrowserError(f"command '{args[0] if args else ''}' is not allowed by LocalPilot")
        sub = ALLOWED_COMMANDS[args[0]]
        a1 = args[1] if len(args) > 1 else ""
        if args[0] == "tab" and a1 and not (a1 in sub or _TAB.match(a1)):
            raise BrowserError(f"'tab {a1}' is not allowed")
        if args[0] == "tab" and a1 == "close" and len(args) > 2 and not _TAB.match(args[2]):
            raise BrowserError("tab close needs a tab id")
        if args[0] in ("get", "scroll", "stream") and a1 not in sub:
            raise BrowserError(f"'{args[0]} {a1}' is not allowed")
        if args[0] == "click" and not _REF.match(a1):
            raise BrowserError("only element refs (@eN) can be clicked")
        if any(str(a).startswith("--") and str(a) not in ("--screenshot-format", "--screenshot-quality")
               for a in args[1:]):
            raise BrowserError("extra flags are not allowed")

    def _run_raw(self, args, timeout=45.0, _viewer=False) -> dict:
        if not (_viewer and list(args) == ["stream", "enable"]):
            self.check_command(args)
        if args[0] == "open" and args[1] != "about:blank":
            self._gate(args[1])                  # nothing opens without passing the URL gate
        if args[0] != "close" and not self._daemon_up():
            self._spawn_daemon()                 # start, recycle, crash recovery: always a hidden console
        cmd = [self.exe, "--session", self.session, "--json", "--executable-path", self.chrome,
               "--download-path", self.quarantine,
               "--idle-timeout", f"{max(60, self.profile.idle_close_s)}s"] + list(args)
        job = self._job if self.use_job else None
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env,
                             creationflags=0x08000000 | (0x00000004 if job else 0),   # CREATE_SUSPENDED
                             encoding="utf-8", errors="replace")
        if job:
            # in the job BEFORE it runs: the daemon and Chromium it starts inherit the memory budget
            # and kill-on-close (a detached daemon can't escape: our job doesn't allow breakaway)
            job.adopt_handle(int(p._handle))
            _resume(int(p._handle))
        end = time.time() + timeout
        while True:                    # read WHILE waiting: a big reply must never fill the pipe
            try:
                so, se = p.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if self.stop_event.is_set() and args[:1] != ["close"]:
                    p.kill(); p.communicate(); raise BrowserStopped("stopped by user")
                if time.time() > end:
                    p.kill(); p.communicate(); raise BrowserError(f"agent-browser timed out on '{args[0]}'")
        out = (so or se or "").strip()
        try:
            return json.loads(out.splitlines()[-1]) if out else {}
        except (ValueError, IndexError):
            return {"success": False, "error": out[:300]}

    def _cli(self, *args, timeout=45.0) -> dict:
        self._checkpoint()
        with self._lock:
            r = self._run_raw(list(args), timeout)
        if not r.get("success", False):
            err = str(r.get("error") or "unknown error")
            if "policy" in err.lower() or "denied" in err.lower():
                raise BrowserError(f"blocked by action policy: {err[:200]}")
            raise BrowserError(err[:300])
        data = r.get("data")
        return data if isinstance(data, dict) else {"value": data}

    def _gate(self, url: str):
        if self.threats is not None:
            verdict, reasons = self.threats.verdict(url)
            if verdict in ("known-malicious", "suspicious"):
                self.stats.blocked_unsafe += 1; self.stats.note(verdict, url, "; ".join(reasons))
                raise BrowserError(f"blocked ({verdict}): {'; '.join(reasons)}")
        ok, why = urlguard.check(url, self.allow_hosts)
        if not ok:
            self.stats.blocked_unsafe += 1; self.stats.note("unsafe", url, why)
            raise BrowserError(f"blocked: {why}")

    # ----------------------------------------------------------------- operations
    def _url_title(self) -> tuple[str, str]:
        u = self._cli("get", "url"); t = self._cli("get", "title")
        return str(u.get("url") or u.get("value") or ""), str(t.get("title") or t.get("value") or "")

    def goto(self, url: str) -> dict:
        self._gate(url)
        if self.stats.pages_visited >= self.limits.max_pages:
            raise BrowserError(f"page limit reached ({self.limits.max_pages} per task)")
        if self._since_recycle >= self.profile.recycle_every:      # fresh browser (memory creep)
            with self._lock:
                self._run_raw(["close"], timeout=20)
                self._run_raw(["open", "about:blank"], timeout=60)    # new daemon...
            self._since_recycle, self.recycles = 0, self.recycles + 1
            self._reapply_stream()                                    # ...with LocalPilot's stream state
        self.status = f"loading {url[:60]}"
        from .filters import registrable as _reg
        from urllib.parse import urlsplit as _us
        self._proxy.page_site = _reg(_us(url).hostname or "")
        try:
            self._cli("open", url, timeout=self.limits.nav_timeout_ms / 1000 + 10)
        except BrowserError as e:
            self.status = "ready"
            if "ERR_ABORTED" in str(e) and self.stats.blocked_downloads:
                raise BrowserError("download blocked: downloads are disabled")
            # agent-browser 0.43: "the command reached the daemon but no response came back" (seen on
            # pages whose trackers the guard blocks). The navigation usually DID happen: check where
            # the tab really is; accept only the requested site (the URL gate below still applies).
            if "no response came back" in str(e) or "EOF while parsing" in str(e):
                try:
                    now = self._url_title()[0]
                except BrowserError:
                    now = ""
                if not _same_site(now, url):
                    raise BrowserError(f"could not load {url}: {str(e)[:160]}")
            else:
                raise BrowserError(f"could not load {url}: {str(e)[:160]}")
        finally:
            self._adopt()
        final, title = self._url_title()
        from .filters import registrable
        from urllib.parse import urlsplit
        self._proxy.page_site = registrable(urlsplit(final).hostname or "")   # for third-party rules
        try:
            self._gate(final)                                    # where the redirects ended up
        except BrowserError:
            self._cli("open", "about:blank")
            raise
        self.stats.pages_visited += 1
        self._since_recycle += 1
        self._close_popups()
        self.status = "ready"
        return {"url": final, "title": title, "status": None}

    def _close_popups(self):
        """Tabs a PAGE opened by itself (window.open popups) are closed at once; only tabs LocalPilot
        opened with new_tab() stay. (Their traffic went through the guard proxy like everything else.)"""
        try:
            tabs = self._cli("tab").get("tabs") or []
        except BrowserError:
            return
        extra = [t for t in tabs if not t.get("active")][max(0, self._tabs - 1):]
        for t in extra:
            tid = str(t.get("tabId") or "")
            if _TAB.match(tid):
                try:
                    self._cli("tab", "close", tid)
                    self.stats.popups_closed += 1
                    self.stats.note("popup", str(t.get("url") or "")[:120], "opened by the page: closed")
                except BrowserError:
                    pass

    def read(self, url: str | None = None) -> extract.PageContent:
        from .httpreader import page_from_html
        if url:
            self.goto(url)
        final, title = self._url_title()
        try:
            html = self._cli("get", "html", "html", timeout=30)
            html = str(html.get("html") or html.get("value") or "")
        except BrowserError as e:
            # agent-browser 0.43 fails on very large replies ("Invalid response: EOF while parsing"):
            # fall back to the page's visible text (no metadata, but the evidence is the same page)
            if "EOF" not in str(e) and "Invalid response" not in str(e):
                raise
            body = self._cli("get", "text", "body", timeout=30)
            text = str(body.get("text") or body.get("value") or "")
            html = "<body>" + "".join(f"<p>{_esc(p)}</p>" for p in text.splitlines() if p.strip()) + "</body>"
        pc = page_from_html(url or final, final, f"<html>{html}</html>", None, "agent-browser")
        if len(pc.text or "") < 200 and not getattr(self, "_rereading", False):
            # script-rendered pages may still be drawing: give them one more moment, read once more
            self._cli("wait", "1500", timeout=10)
            self._rereading = True
            try:
                again = self.read(None)
                if len(again.text or "") > len(pc.text or ""):
                    return again
            finally:
                self._rereading = False
        pc.title = pc.title or title
        pc.blocked_requests = self.stats.blocked_ads
        pc.raw_html = html if self.keep_raw else ""
        pc.retrieved_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        return pc

    def snapshot(self, interactive: bool = True) -> str:
        """Accessibility snapshot with @refs (wrapped as untrusted page content by agent-browser).
        Remembers which refs are LINKS: only those can be clicked."""
        d = self._cli("snapshot", "-i") if interactive else self._cli("snapshot")
        text = str(d.get("snapshot") or d.get("value") or "")
        self._link_refs = {m.group(1) for m in map(_LINK_LINE.match, text.splitlines()) if m}
        return text

    def click(self, ref: str) -> dict:
        """Click a LINK from the latest snapshot. Buttons, inputs, checkboxes etc. are refused, so
        nothing can be submitted, typed, bought or uploaded. Where we land is gated like any URL."""
        ref = ref if ref.startswith("@") else "@" + ref
        if ref[1:] not in self._link_refs:
            self.stats.blocked_forms += 1
            self.stats.note("click refused", ref, "not a link in the latest snapshot")
            raise BrowserError(f"{ref} is not a link on this page -- only links can be clicked")
        if self.stats.pages_visited >= self.limits.max_pages:
            raise BrowserError(f"page limit reached ({self.limits.max_pages} per task)")
        self._cli("click", ref)
        self._link_refs = set()                     # refs are stale after navigation
        self._close_popups()                        # target=_blank links / popups never stay open
        final, title = self._url_title()
        try:
            self._gate(final)
        except BrowserError:
            self._cli("open", "about:blank")
            raise
        self.stats.pages_visited += 1
        return {"url": final, "title": title}

    SEARCH_ENGINES = {
        "duckduckgo": "https://html.duckduckgo.com/html/?q={q}",
        "bing": "https://www.bing.com/search?q={q}",
        "brave": "https://search.brave.com/search?q={q}",
    }

    def search(self, query: str, engine: str = "duckduckgo") -> dict:
        """Open a search engine's results page for query (a URL -- nothing is typed)."""
        from urllib.parse import quote_plus
        url = self.SEARCH_ENGINES.get(engine, self.SEARCH_ENGINES["duckduckgo"]).format(q=quote_plus(query))
        return self.goto(url)

    def scroll(self, pages: float = 1.0):
        self._cli("scroll", "down", str(int(900 * pages)))

    def new_tab(self, url: str | None = None) -> int:
        if self._tabs >= self.limits.max_tabs:
            raise BrowserError(f"tab limit reached ({self.limits.max_tabs})")
        if url:
            self._gate(url)
        self._cli("tab", "new", *( [url] if url else [] ))
        self._tabs += 1
        return self._tabs - 1

    def tabs(self) -> list:
        d = self._cli("tab")
        return d.get("tabs") or d.get("value") or []

    def switch_tab(self, i: int):
        self._cli("tab", f"t{int(i) + 1}")
        self._link_refs = set()

    def close_tab(self, i: int):
        if self._tabs > 1:
            self._cli("tab", "close", f"t{int(i) + 1}"); self._tabs -= 1
            self._link_refs = set()

    def history(self, step: int) -> dict:
        self._cli("back" if step < 0 else "forward" if step > 0 else "reload")
        self._link_refs = set()
        final, title = self._url_title()
        try:
            self._gate(final)                       # back/forward can land anywhere the tab has been
        except BrowserError:
            self._cli("open", "about:blank")
            raise
        return {"url": final, "title": title}

    def current(self) -> dict:
        final, title = self._url_title()
        return {"url": final, "title": title}

    def screenshot(self) -> bytes:
        path = os.path.join(self.shotdir, f"f{uuid.uuid4().hex[:8]}.jpg")
        try:
            self._cli("screenshot", path, "--screenshot-format", "jpeg", "--screenshot-quality", "55", timeout=10)
            with open(path, "rb") as f:
                return f.read()
        finally:
            try:
                os.remove(path)                                  # on disk only for milliseconds
            except OSError:
                pass

    def try_screenshot(self, timeout: float = 3.0) -> bytes | None:
        """Preview frame only when idle -- never waits behind (or delays) agent work."""
        if not self._alive or self.stop_event.is_set() or self.hold_event.is_set() or self._lock.locked():
            return None
        try:
            return self.screenshot()
        except Exception:
            return None

    def summary(self) -> dict:
        s = self.stats
        return {"status": self.status, "engine": self.engine, "pages_visited": s.pages_visited,
                "ads_trackers_blocked": s.blocked_ads, "unsafe_blocked": s.blocked_unsafe,
                "downloads_blocked": s.blocked_downloads, "forms_blocked": s.blocked_forms,
                "popups_closed": s.popups_closed, "crashes_recovered": s.crashes, "recycles": self.recycles,
                "profile": self.profile.name, "memory": self.memory(), "daemon_pid": self._daemon_pid}


def _same_site(a: str, b: str) -> bool:
    from urllib.parse import urlsplit
    from .filters import registrable
    ha, hb = (urlsplit(a).hostname or ""), (urlsplit(b).hostname or "")
    return bool(ha) and registrable(ha) == registrable(hb)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _sweep_stale_sessions():
    """agent-browser's per-session files (.port/.pid/.config/...) left by LocalPilot runs that were
    killed: remove those whose LocalPilot process is gone (never another program's sessions)."""
    import re as _re
    folder = os.path.join(os.path.expanduser("~"), ".agent-browser")
    live = {p for p, _ in _procs()}
    for f in glob.glob(os.path.join(folder, "localpilot-*-*.*")):
        m = _re.match(r"localpilot-(\d+)-[0-9a-f]{8}\.", os.path.basename(f))
        if m and int(m.group(1)) not in live:
            try:
                os.remove(f)
            except OSError:
                pass


def _resume(handle: int):
    import ctypes
    ctypes.WinDLL("ntdll").NtResumeProcess(ctypes.c_void_p(handle))


def _procs():
    from .jobs import _all_processes
    return _all_processes()
