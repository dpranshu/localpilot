"""Phishing / malware threat intelligence for the Agent Browser (free, no API keys).

SOURCES (verified 2026-11-19)
  * Phishing.Database (github.com/Phishing-Database/Phishing.Database) -- MIT licence, maintained,
    "phishing-domains-ACTIVE.txt" (~390k domains), downloaded directly, no key.
  * Malware URLs: abuse.ch URLhaus public downloads now answer "410 Gone" (an Auth-Key is required)
    and its commercial use may need a Spamhaus licence -> NOT used. Malware intelligence is reported
    as UNAVAILABLE rather than silently assumed.

VERDICTS: "known-malicious" (block) | "suspicious" (deceptive-URL heuristics) | "unknown".
There is deliberately NO "safe" verdict: absence from a list proves nothing.

Storage: sorted 8-byte BLAKE2b hashes of domains (array('Q'), ~3 MB for 390k) + binary search.
Updated every 12 h when used; the last good copy is kept; older than 3 days = "stale" (warning).
"""
from __future__ import annotations

import array
import bisect
import hashlib
import os
import re
import threading
import time
from urllib.parse import urlsplit

import requests

DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "LocalPilot", "threats")
FEEDS = {
    "phishing": ("https://raw.githubusercontent.com/Phishing-Database/Phishing.Database/master/"
                 "phishing-domains-ACTIVE.txt", "Phishing.Database (MIT)"),
}
REFRESH = 12 * 3600
STALE = 3 * 24 * 3600


def _h(domain: str) -> int:
    return int.from_bytes(hashlib.blake2b(domain.encode("utf-8"), digest_size=8).digest(), "little")


class ThreatIntel:
    def __init__(self, directory: str = DIR):
        self.dir = directory
        self._sets: dict[str, array.array] = {}
        self._meta: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ data
    def _bin(self, name):
        return os.path.join(self.dir, name + ".bin")

    def load(self) -> "ThreatIntel":
        """Load cached lists (fast). Never downloads."""
        for name, (url, label) in FEEDS.items():
            p = self._bin(name)
            if os.path.exists(p):
                a = array.array("Q")
                with open(p, "rb") as f:
                    a.frombytes(f.read())
                with self._lock:
                    self._sets[name] = a
                    self._meta[name] = {"label": label, "entries": len(a), "updated": os.path.getmtime(p)}
        return self

    def update(self, force: bool = False, progress=lambda m: None) -> dict:
        """Download feeds that are missing or older than 12 h. Keeps the last good copy on failure."""
        os.makedirs(self.dir, exist_ok=True)
        out = {}
        for name, (url, label) in FEEDS.items():
            p = self._bin(name)
            if not force and os.path.exists(p) and time.time() - os.path.getmtime(p) < REFRESH:
                out[name] = "fresh"; continue
            try:
                progress(f"Updating {label}...")
                r = requests.get(url, timeout=60, headers={"User-Agent": "LocalPilot"})
                r.raise_for_status()
                doms = {ln.strip().lower().rstrip(".") for ln in r.text.splitlines()
                        if ln.strip() and not ln.startswith("#") and "." in ln}
                if len(doms) < 1000:
                    raise ValueError(f"only {len(doms)} entries -- refusing a suspiciously small list")
                a = array.array("Q", sorted(_h(d) for d in doms))
                tmp = p + ".part"
                with open(tmp, "wb") as f:
                    f.write(a.tobytes())
                os.replace(tmp, p)
                with self._lock:
                    self._sets[name] = a
                    self._meta[name] = {"label": label, "entries": len(a), "updated": time.time()}
                out[name] = f"updated ({len(a)} entries)"
            except Exception as e:
                out[name] = f"failed: {str(e)[:80]}" + (" - using last good copy" if os.path.exists(p) else "")
        return out

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        """What protection is actually active right now (shown in the UI)."""
        now = time.time()
        feeds = {}
        for name, (url, label) in FEEDS.items():
            m = self._meta.get(name)
            if not m:
                feeds[name] = {"label": label, "state": "unavailable"}
            else:
                age = now - m["updated"]
                feeds[name] = {"label": label, "state": "stale" if age > STALE else "active",
                               "entries": m["entries"], "age_hours": round(age / 3600, 1)}
        phishing = feeds["phishing"]["state"]
        return {"phishing": phishing, "malware": "unavailable", "feeds": feeds,
                "warning": None if phishing == "active" else
                ("Phishing protection is OUT OF DATE" if phishing == "stale" else
                 "Phishing protection is UNAVAILABLE (list not downloaded)"),
                "note": "Malware URL intelligence: no keyless free source verified (URLhaus now needs a key)."}

    # ------------------------------------------------------------------ verdicts
    def lookup(self, host: str) -> str | None:
        """Feed name if host or any parent domain is listed, else None."""
        labels = (host or "").lower().strip(".").split(".")
        cands = [".".join(labels[i:]) for i in range(len(labels) - 1)]
        with self._lock:
            sets = dict(self._sets)
        for name, a in sets.items():
            for d in cands:
                x = _h(d)
                i = bisect.bisect_left(a, x)
                if i < len(a) and a[i] == x:
                    return name
        return None

    def verdict(self, url: str) -> tuple[str, list[str]]:
        """('known-malicious' | 'suspicious' | 'unknown', reasons). Never 'safe'."""
        host = (urlsplit(url).hostname or "").lower()
        hit = self.lookup(host)
        if hit:
            return "known-malicious", [f"listed by {FEEDS[hit][1]}"]
        reasons = deceptive_reasons(url)
        return ("suspicious", reasons) if reasons else ("unknown", [])


# ---------------------------------------------------------------------- heuristics
_BRANDS = ("paypal", "apple", "microsoft", "office365", "outlook", "google", "gmail", "amazon", "netflix",
           "facebook", "instagram", "whatsapp", "binance", "coinbase", "metamask", "chase", "wellsfargo",
           "bankofamerica", "hdfc", "icici", "sbi", "paytm", "phonepe", "steam", "github", "dropbox", "docusign")
_LOGIN_WORDS = re.compile(r"(login|log-in|signin|sign-in|verify|account|secure|update|wallet|unlock|"
                          r"suspend|password|billing|confirm)", re.I)


def deceptive_reasons(url: str) -> list[str]:
    """Signs of a deceptive URL. Heuristic: catches common tricks, misses many, can misfire."""
    from .filters import registrable
    try:
        p = urlsplit(url)
    except ValueError:
        return ["malformed URL"]
    host = (p.hostname or "").lower()
    reg = registrable(host)
    out = []
    if "xn--" in host:
        out.append("internationalised (punycode) domain - possible look-alike")
    if "@" in (p.netloc or ""):
        out.append("'@' in address hides the real destination")
    labels = host.split(".")
    for b in _BRANDS:
        brand_elsewhere = any(b in lab for lab in labels[:-2]) or (b in reg.split(".")[0] and reg.split(".")[0] != b)
        if brand_elsewhere and not reg.startswith(b + "."):
            out.append(f"mentions '{b}' but the site is '{reg}'")
            break
    if host.count(".") >= 4 and _LOGIN_WORDS.search(host):
        out.append("many subdomains with account/login words")
    if re.fullmatch(r"[\d.]+", host):
        out.append("raw IP address instead of a domain")
    if len(host) > 60:
        out.append("unusually long host name")
    return out
