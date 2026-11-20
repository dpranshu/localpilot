"""Filter lists for the Agent Browser.

EasyList and EasyPrivacy are dual-licensed GPL-3.0-or-later / CC BY-SA 3.0+ (easylist.to/pages/
licence.html). LocalPilot does NOT bundle them: they are downloaded on first use from easylist.to
into %APPDATA%/LocalPilot/filters, refreshed weekly, and attributed below. Until they are
downloaded (or offline) a small built-in list of LocalPilot's own rules is used.
"""
from __future__ import annotations

import os
import time

import requests

from .filters import FilterEngine

DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "LocalPilot", "filters")
LISTS = {
    "easylist": "https://easylist.to/easylist/easylist.txt",
    "easyprivacy": "https://easylist.to/easylist/easyprivacy.txt",
}
MAX_AGE = 7 * 24 * 3600
ATTRIBUTION = ("Ad and tracker blocking uses EasyList and EasyPrivacy by The EasyList authors "
               "(https://easylist.to/), licensed GPL-3.0-or-later or CC BY-SA 3.0. Lists are "
               "downloaded from easylist.to and are not modified.")

# LocalPilot's own minimal fallback (written for LocalPilot; not derived from EasyList).
BUILTIN = """
! LocalPilot built-in fallback: major ad / tracking networks only
||doubleclick.net^
||googlesyndication.com^
||googleadservices.com^
||adservice.google.com^
||google-analytics.com^
||googletagmanager.com^
||amazon-adsystem.com^
||adnxs.com^
||criteo.com^
||criteo.net^
||taboola.com^
||outbrain.com^
||scorecardresearch.com^
||quantserve.com^
||rubiconproject.com^
||pubmatic.com^
||openx.net^
||casalemedia.com^
||moatads.com^
||hotjar.com^
||facebook.net^$third-party
||connect.facebook.net^$third-party
||ads-twitter.com^
||analytics.tiktok.com^
||bat.bing.com^
||clarity.ms^
||adsrvr.org^
||media.net^
||zedo.com^
||yieldmo.com^
##.adsbygoogle
##[id^="google_ads_iframe"]
##[data-ad-slot]
"""


def _path(name: str) -> str:
    return os.path.join(DIR, name + ".txt")


def update(progress=lambda msg: None, force: bool = False) -> dict:
    """Download lists that are missing or older than a week. Never raises."""
    os.makedirs(DIR, exist_ok=True)
    out = {}
    for name, url in LISTS.items():
        p = _path(name)
        fresh = os.path.exists(p) and time.time() - os.path.getmtime(p) < MAX_AGE
        if fresh and not force:
            out[name] = "cached"
            continue
        try:
            progress(f"Updating {name}...")
            r = requests.get(url, timeout=30, headers={"User-Agent": "LocalPilot"})
            r.raise_for_status()
            if "[Adblock" not in r.text[:200] and "!" not in r.text[:50]:
                raise ValueError("unexpected content")
            tmp = p + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(r.text)
            os.replace(tmp, p)
            out[name] = "updated"
        except Exception as e:
            out[name] = f"failed: {str(e)[:80]}" + (" (using cached copy)" if os.path.exists(p) else "")
    return out


def load(use_downloaded: bool = True, extra_rules: str = "") -> tuple[FilterEngine, list[str]]:
    """Build the engine: built-in rules + downloaded lists (if present) + user exceptions."""
    eng = FilterEngine().add_list(BUILTIN)
    used = ["builtin"]
    if use_downloaded:
        for name in LISTS:
            p = _path(name)
            if os.path.exists(p):
                with open(p, encoding="utf-8", errors="replace") as f:
                    eng.add_list(f.read())
                used.append(name)
    if extra_rules:
        eng.add_list(extra_rules)
        used.append("user")
    return eng, used
