"""Web layer: understand "search X on <site>" goals and do them the reliable way.

The model used to mix up two different search boxes:
  * the browser's address bar  -> searches the browser's engine (Brave Search, Google, Bing)
  * the website's own search   -> YouTube / Wikipedia / Amazon / GitHub results
and typed into the wrong one, or typed again on top of autocomplete. Here we parse the goal
into (browser, site, query) and open the right results page directly -- no typing, so no
autocomplete trouble. Unknown sites get their own on-page search box filled instead.
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus

# site word -> (home URL, search URL with {q}, word that appears in the results-page title)
SITES = {
    "youtube": ("https://www.youtube.com", "https://www.youtube.com/results?search_query={q}", "youtube"),
    "google": ("https://www.google.com", "https://www.google.com/search?q={q}", "google"),
    "wikipedia": ("https://en.wikipedia.org", "https://en.wikipedia.org/w/index.php?search={q}&go=Go", "wikipedia"),
    "github": ("https://github.com", "https://github.com/search?q={q}&type=repositories", "github"),
    "amazon": ("https://www.amazon.com", "https://www.amazon.com/s?k={q}", "amazon"),
    "amazon.in": ("https://www.amazon.in", "https://www.amazon.in/s?k={q}", "amazon"),
    "flipkart": ("https://www.flipkart.com", "https://www.flipkart.com/search?q={q}", "flipkart"),
    "reddit": ("https://www.reddit.com", "https://www.reddit.com/search/?q={q}", "reddit"),
    "stackoverflow": ("https://stackoverflow.com", "https://stackoverflow.com/search?q={q}", "stack overflow"),
    "bing": ("https://www.bing.com", "https://www.bing.com/search?q={q}", "bing"),
    "duckduckgo": ("https://duckduckgo.com", "https://duckduckgo.com/?q={q}", "duckduckgo"),
    "brave search": ("https://search.brave.com", "https://search.brave.com/search?q={q}", "brave search"),
    "google maps": ("https://www.google.com/maps", "https://www.google.com/maps/search/{q}", "google maps"),
    "maps": ("https://www.google.com/maps", "https://www.google.com/maps/search/{q}", "google maps"),
    "spotify": ("https://open.spotify.com", "https://open.spotify.com/search/{q}", "spotify"),
    "twitter": ("https://x.com", "https://x.com/search?q={q}", "x"),
    "x": ("https://x.com", "https://x.com/search?q={q}", "x"),
    "ebay": ("https://www.ebay.com", "https://www.ebay.com/sch/i.html?_nkw={q}", "ebay"),
    "imdb": ("https://www.imdb.com", "https://www.imdb.com/find/?q={q}", "imdb"),
    "pinterest": ("https://www.pinterest.com", "https://www.pinterest.com/search/pins/?q={q}", "pinterest"),
    "quora": ("https://www.quora.com", "https://www.quora.com/search?q={q}", "quora"),
    "linkedin": ("https://www.linkedin.com", "https://www.linkedin.com/search/results/all/?keywords={q}", "linkedin"),
    "netflix": ("https://www.netflix.com", "https://www.netflix.com/search?q={q}", "netflix"),
}
ALIAS = {"yt": "youtube", "you tube": "youtube", "wiki": "wikipedia", "stack overflow": "stackoverflow",
         "ddg": "duckduckgo", "google map": "google maps", "amazon india": "amazon.in"}
# the browser's own engine when the user just says "search the web"
ENGINE = {"brave": "https://search.brave.com/search?q={q}", "chrome": "https://www.google.com/search?q={q}",
          "edge": "https://www.bing.com/search?q={q}", "firefox": "https://www.google.com/search?q={q}"}
BROWSERS = ("brave", "chrome", "edge", "firefox")
_WEB = {"the web", "web", "internet", "the internet", "online"}

_DOMAIN = r"[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|io|in|edu|gov|co|dev|ai|app|me|tv)(?:/\S*)?"
_BROWSER_RE = r"(?:the\s+)?(?:brave|chrome|google chrome|edge|microsoft edge|firefox)(?:\s+browser)?"
_OPEN = rf"(?:(?:open|launch|start|use)\s+{_BROWSER_RE}\s*(?:,|and|then)?\s*)?"
_END = r"(?=\s*(?:,|\.|!|$|\s+(?:and|then)\s+))"


def _site_key(word: str) -> str | None:
    w = re.sub(r"^(the|my)\s+|\s+(website|site|app)$", "", (word or "").lower().strip()).strip()
    w = ALIAS.get(w, w)
    if w in SITES:
        return w
    host = re.sub(r"^https?://", "", w).split("/")[0]
    host = re.sub(r"^(www|m)\.", "", host)
    if host == "amazon.in":
        return "amazon.in"
    base = host.split(".")[0]
    base = ALIAS.get(base, base)
    if base in SITES and (host == base or host.endswith((".com", ".org"))):
        return base
    return None


def default_browser() -> str | None:
    """The user's default browser (brave / chrome / edge / firefox), from Windows settings."""
    import winreg
    base = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https"
    prog = ""
    # newer Windows 11 keeps the real choice in UserChoiceLatest; UserChoice can be stale
    for sub in (r"\UserChoiceLatest\ProgId", r"\UserChoiceLatest", r"\UserChoice"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + sub) as k:
                prog = str(winreg.QueryValueEx(k, "ProgId")[0]).lower()
            if prog:
                break
        except OSError:
            continue
    for key, name in (("brave", "brave"), ("chrome", "chrome"), ("msedge", "edge"),
                      ("firefox", "firefox")):
        if key in prog:
            return name
    return None


def browser_in(goal: str) -> str | None:
    g = (goal or "").lower()
    for b in BROWSERS:
        if re.search(rf"\b{b}\b", g):
            return b
    return None


def parse(goal: str) -> dict | None:
    """{'browser', 'site', 'query', 'url', 'title_word', 'rest', 'home'} or None.
    'site' None means: search with the browser's own engine. 'url' None + 'home' set means:
    open that (unknown) site and use its own search box."""
    g = " ".join((goal or "").split())
    # "... on youtube in brave": the browser is not part of the site or the query
    g = re.sub(r"\s+(?:in|on|using|with)\s+" + _BROWSER_RE + r"\s*[.!]?$", "", g, flags=re.I)
    pats = [
        # search youtube for X / search on youtube for X
        rf"^{_OPEN}search\s+(?:on\s+|in\s+)?(?P<site>{_DOMAIN}|[\w.' ]+?)\s+for\s+(?P<q>.+?){_END}",
        # go to youtube(.com) and search (for) X
        rf"^{_OPEN}(?:go\s+to|open|visit|navigate\s+to)\s+(?!{_BROWSER_RE}\b)(?P<site>{_DOMAIN}|[\w.' ]+?)\s*(?:,|and|then)?\s+"
        rf"(?:and\s+)?search\s+(?:for\s+)?(?P<q>.+?){_END}",
        # search (for) X on/in youtube
        rf"^{_OPEN}(?:search|look\s+up|find)\s+(?:for\s+)?(?P<q>.+?)\s+(?:on|in|at)\s+(?P<site>{_DOMAIN}|[\w.' ]+?){_END}",
        # play X on youtube / spotify (search first; the model clicks a result)
        rf"^{_OPEN}play\s+(?P<q>.+?)\s+(?:on|in)\s+(?P<site>youtube|yt|spotify){_END}",
        # google X
        rf"^{_OPEN}(?P<site>google)\s+(?!for\b)(?P<q>.+?){_END}",
        # search (the web) for X / search X
        rf"^{_OPEN}search\s+(?:the\s+web\s+|the\s+internet\s+|online\s+|web\s+)?(?:for\s+)?(?P<q>.+?){_END}",
    ]
    m = re.fullmatch(rf"(?:please\s+)?(?:open|go\s+to|visit|load)\s+(?:the\s+)?(?P<d>{_DOMAIN})\s*[.!]?", g, re.I)
    if m:                       # "open youtube.com": just the site, in the user's browser
        d = m.group("d")
        url = d if d.lower().startswith("http") else "https://" + d
        return {"browser": browser_in(goal), "query": None, "rest": "", "site": d, "url": url,
                "home": url, "title_word": None}
    for p in pats:
        m = re.match(p, g, re.IGNORECASE)
        if not m:
            continue
        q = re.sub(rf"\s+(?:in|on|using|with)\s+{_BROWSER_RE}$", "", m.group("q"), flags=re.I)
        q = q.strip(" \"'")
        site_word = (m.groupdict().get("site") or "").strip()
        if not q or re.fullmatch(_DOMAIN, q, re.I):
            continue
        rest = g[m.end():].strip(" ,.!")
        rest = re.sub(r"^(and|then)\s+", "", rest, flags=re.I)
        play = g.lower().find("play ") >= 0 and "play" in p
        browser = browser_in(goal)
        if site_word.lower() in _WEB or not site_word:
            key = None
        else:
            key = _site_key(site_word)
            if key is None and not re.fullmatch(_DOMAIN, site_word, re.I):
                # "search cats in my notes" etc. -- not a website we know: not a web goal
                if browser is None:
                    return None
                key = None if site_word.lower() in _WEB else "?"
        info = {"browser": browser, "query": q, "rest": ("play the first result" if play and not rest else rest)}
        if key and key != "?":
            home, search, word = SITES[key]
            info.update(site=key, url=search.format(q=quote_plus(q)), home=home, title_word=word)
        elif key == "?" or (site_word and re.fullmatch(_DOMAIN, site_word, re.I)):
            dom = site_word if site_word.lower().startswith("http") else "https://" + site_word
            info.update(site=site_word, url=None, home=dom, title_word=None)
        else:
            eng = ENGINE.get(browser or "brave", ENGINE["brave"])
            info.update(site=None, url=eng.format(q=quote_plus(q)), home=None, title_word=None)
        return info
    return None


# results-page link ids (Chromium exposes the HTML id as the UIA AutomationId)
FIRST_RESULT_ID = {"youtube": "video-title"}
_FIRST = re.compile(r"^(?:and\s+)?(?:play|open|click|watch|show|go\s+to)\s+(?:on\s+)?(?:the\s+)?"
                    r"(?:(?:first|top|1st)\s*(?:one|video|result|song|link|article|page)?|"
                    r"(?:article|page|video|wikipedia\s+article))\s*[.!]?$", re.I)


def wants_first_result(rest: str) -> bool:
    return bool(_FIRST.match((rest or "").strip()))


def click_first_result(site: str, hwnd) -> bool:
    """Click the first result link on a results page (real mouse click at its centre)."""
    aid = FIRST_RESULT_ID.get(site or "")
    if not aid:
        return False
    try:
        import uiautomation as auto
        from . import inputs
        win = auto.ControlFromHandle(hwnd)
        wr = win.BoundingRectangle
        for c, _ in auto.WalkControl(win, maxDepth=60):
            if c.ControlTypeName == "HyperlinkControl" and c.AutomationId == aid:
                r = c.BoundingRectangle
                if r.width() > 0 and r.height() > 0 and wr.top < r.top < wr.bottom:
                    inputs.click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
                    return True
    except Exception:
        return False
    return False


def query_in_title(query: str, title: str, need: float = 0.6) -> bool:
    """Results pages put the search words in the title ('lofi music - YouTube')."""
    words = [w for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(w) > 1]
    t = (title or "").lower()
    if not words:
        return False
    return sum(w in t for w in words) / len(words) >= need


def find_search_box(window):
    """The page's own search field (Edit/ComboBox named or id'd 'search'), or None."""
    try:
        import uiautomation as auto
        best = None
        for c, _ in auto.WalkControl(window, maxDepth=40):
            if c.ControlTypeName not in ("EditControl", "ComboBoxControl"):
                continue
            label = f"{c.Name} {c.AutomationId} {getattr(c, 'HelpText', '')}".lower()
            if "address" in label and "search" in label:      # the browser's own omnibox
                continue
            if "search" in label or "query" in label or label.strip() in ("q",):
                r = c.BoundingRectangle
                if r.width() > 0 and r.height() > 0:
                    return c
                best = best or c
        return best
    except Exception:
        return None


def page_url(hwnd) -> str:
    """The URL in the browser's address bar for window hwnd ('' if not found)."""
    try:
        import uiautomation as auto
        win = auto.ControlFromHandle(hwnd)
        for c, _ in auto.WalkControl(win, maxDepth=14):
            if c.ControlTypeName == "EditControl" and "address" in (c.Name or "").lower():
                try:
                    return c.GetValuePattern().Value or ""
                except Exception:
                    return ""
    except Exception:
        pass
    return ""


def results_open(query: str, title: str, hwnd=None) -> bool:
    """Search results for query are showing: words in the title, or in the page URL
    (some sites, e.g. GitHub, title results pages generically)."""
    if query_in_title(query, title):
        return True
    if hwnd is None:
        return False
    from urllib.parse import unquote_plus
    url = unquote_plus(page_url(hwnd)).lower()
    return bool(url) and query_in_title(query, url, need=1.0)
