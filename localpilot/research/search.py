"""Search results from real results pages opened in the agent-browser (no search API keys).

Engines: Bing web (default) and Bing News (time-sensitive questions). DuckDuckGo and Brave Search
were tested headless on 20 Nov 2026: DuckDuckGo answered with a CAPTCHA and Brave rendered its
results only through scripts -- neither is used. A CAPTCHA is NEVER solved or bypassed: it is
reported, and the research continues without that engine (or stops and says so).
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote_plus, urlsplit

BING_WEB = "https://www.bing.com/search?q={q}&setlang=en"
BING_NEWS = "https://www.bing.com/news/search?q={q}&qft=interval%3d%227%22&setlang=en"   # past 7 days

_CAPTCHA = re.compile(r"(captcha|are you a robot|not a robot|unusual traffic|automated queries|"
                      r"confirm this search was made by a human|verify you are human|select all (squares|images)|"
                      r"solve the challenge|one last step|just a moment\.\.\.|access (temporarily )?(suspended|denied)|"
                      r"403 - forbidden)", re.I)
WIKI_SEARCH = "https://en.wikipedia.org/w/index.php?search={q}&fulltext=1&ns0=1"
# well-known aggregators / re-hosts: still usable, but ranked below primary sources
_AGGREGATORS = ("msn.com", "yahoo.com", "flipboard.com", "newsbreak.com", "ground.news", "pinterest.",
                "quora.com", "medium.com", "linkedin.com", "facebook.com", "x.com", "twitter.com",
                "reddit.com", "youtube.com", "tiktok.com", "instagram.com")
_OFFICIAL_HINTS = ("docs.", "developer.", "developers.", "blog.", "/blog", "/docs", "/news", "/press",
                   "github.com", "huggingface.co", "arxiv.org", "readthedocs", ".gov", "about.")


@dataclass
class Result:
    title: str
    url: str
    snippet: str = ""
    engine: str = "bing"
    rank: int = 0
    source: str = ""            # news outlet (Bing News)
    age: str = ""               # "13 hours ago" (Bing News)
    sponsored: bool = False
    score: float = 0.0
    why: list = field(default_factory=list)

    @property
    def domain(self) -> str:
        return (urlsplit(self.url).hostname or "").lower().removeprefix("www.")


def search_url(query: str, news: bool = False) -> str:
    return (BING_NEWS if news else BING_WEB).format(q=quote_plus(query))


def is_captcha(title: str, text: str) -> bool:
    return bool(_CAPTCHA.search(f"{title} {text[:3000]}"))


def _unwrap_bing(href: str) -> str:
    """Bing wraps result links in /ck/a?...&u=a1<base64url>; return the real destination."""
    if "bing.com/ck/a" not in href:
        return href
    u = parse_qs(urlsplit(href).query).get("u", [""])[0]
    if u.startswith("a1"):
        s = u[2:] + "=" * (-len(u[2:]) % 4)
        try:
            return base64.urlsafe_b64decode(s).decode("utf-8", "replace")
        except Exception:
            return href
    return href


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def parse_bing(html: str) -> list[Result]:
    import lxml.html
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    out = []
    for li in doc.xpath('//li[contains(concat(" ", normalize-space(@class), " "), " b_algo ")]'):
        a = li.xpath('.//h2//a[@href]')
        if not a:
            continue
        url = _unwrap_bing(a[0].get("href", ""))
        if not url.startswith(("http://", "https://")):
            continue
        p = li.xpath('.//p')
        out.append(Result(_clean(a[0].text_content())[:200], url, _clean(p[0].text_content())[:400] if p else "",
                          "bing", len(out) + 1))
    for ad in doc.xpath('//li[contains(@class, "b_ad")]//a[@href]'):      # sponsored: marked, excluded
        out.append(Result(_clean(ad.text_content())[:200], _unwrap_bing(ad.get("href", "")), "", "bing",
                          len(out) + 1, sponsored=True))
    return out


def parse_bing_news(html: str) -> list[Result]:
    import lxml.html
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    out = []
    for c in doc.xpath('//*[contains(concat(" ", normalize-space(@class), " "), " news-card ")]'):
        a = c.xpath('.//a[contains(@class,"title")]') or c.xpath('.//a[@href]')
        url = c.get("url") or (a[0].get("href") if a else "")
        if not url or not url.startswith(("http://", "https://")):
            continue
        snip = c.xpath('.//*[contains(@class,"snippet")]')
        when = c.xpath('.//span[@aria-label]/@aria-label')
        out.append(Result(_clean(a[0].text_content() if a else c.get("data-title", ""))[:200], url,
                          _clean(snip[0].text_content())[:400] if snip else "", "bing-news", len(out) + 1,
                          source=c.get("data-author") or "", age=(when[0] if when else "")))
    return out


GITHUB_SEARCH = "https://github.com/search?q={q}&type=repositories&s=stars&o=desc"
_GH_RESERVED = {"search", "features", "topics", "login", "signup", "orgs", "settings", "marketplace", "sponsors",
                "about", "pricing", "enterprise", "team", "customer-stories", "resources", "solutions",
                "collections", "trending", "explore", "notifications", "new", "site", "security", "readme"}
_GENERIC = {"repository", "repo", "repositories", "github", "project", "projects", "official", "open", "source",
            "open-source", "library", "framework", "tool", "documentation", "docs", "research", "latest",
            "using", "their", "two", "the", "this", "that", "please", "tell", "me", "about"}


def project_terms(question: str) -> tuple[str, list[str]]:
    """'Research the Vercel agent-browser repository' -> ('agent-browser', ['vercel']):
    the likely repo NAME (hyphenated / longest distinctive word) and owner hints."""
    words = [w for w in _terms(question) if w not in _GENERIC]
    if not words:
        return "", []
    name = next((w for w in words if "-" in w or "_" in w), max(words, key=len))
    return name, [w for w in words if w != name]


def github_url(name: str) -> str:
    return GITHUB_SEARCH.format(q=quote_plus(name))


def parse_github(html: str, name: str = "", hints: list | None = None) -> list[Result]:
    """Repository links from GitHub's repository search (sorted by stars), best match first:
    exact repo name, then owner matching a hint (e.g. 'vercel' -> 'vercel-labs')."""
    import lxml.html
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    seen, out = [], []
    for a in doc.xpath('//a[@href]'):
        h = a.get("href") or ""
        if re.fullmatch(r"/[\w.-]+/[\w.-]+", h) and h.split("/")[1].lower() not in _GH_RESERVED and h not in seen:
            seen.append(h)
    hints = [x.lower() for x in (hints or [])]
    for i, h in enumerate(seen[:20]):
        owner, repo = h.strip("/").split("/")
        r = Result(f"{owner}/{repo} - GitHub", "https://github.com" + h, "", "github", i + 1)
        r.score = 20 - i
        if name and repo.lower() == name.lower():
            r.score += 12; r.why.append("repository name matches")
        if any(x in owner.lower() for x in hints):
            r.score += 10; r.why.append("owner matches")
        out.append(r)
    out.sort(key=lambda r: -r.score)
    return out


def wiki_url(query: str) -> str:
    return WIKI_SEARCH.format(q=quote_plus(query))


def parse_wikipedia(html: str) -> list[Result]:
    import lxml.html
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    out = []
    for li in doc.xpath('//ul[contains(@class,"mw-search-results")]/li'):
        a = li.xpath('.//div[contains(@class,"mw-search-result-heading")]//a[@href]')
        if not a:
            continue
        snip = li.xpath('.//div[contains(@class,"searchresult")]')
        out.append(Result(_clean(a[0].text_content())[:200], "https://en.wikipedia.org" + a[0].get("href"),
                          _clean(snip[0].text_content())[:400] if snip else "", "wikipedia", len(out) + 1))
    return out


def relevant(r: Result, question: str) -> bool:
    """A result must mention at least one key term of the question (title, snippet or address).
    Stops research from reading off-topic pages (a real case: a film series for a GitHub repo)."""
    keys = [w for w in _terms(question) if w not in _GENERIC and len(w) > 2]
    if not keys:
        return True
    hay = f"{r.title} {r.snippet} {r.url}".lower().replace("%20", " ")
    return any(k in hay or k.replace("-", " ") in hay or k.replace("-", "") in hay.replace("-", "") for k in keys)


def _terms(q: str) -> list[str]:
    stop = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "is", "what", "who", "how", "did",
            "does", "latest", "current", "today", "news", "about", "research", "compare", "vs", "with"}
    words = [w.strip(".-") for w in re.findall(r"[a-z0-9][a-z0-9.\-]+", q.lower())]
    return [w for w in words if w and w not in stop and len(w) > 1]


def rank(results: list[Result], query: str, per_domain: int = 1) -> list[Result]:
    """Primary sources first; sponsored dropped; one result per domain (diversity)."""
    terms = _terms(query)
    seen, ranked = {}, []
    for r in results:
        if r.sponsored:
            continue
        d, u = r.domain, r.url.lower()
        s = 10.0 - r.rank * 0.5
        if any(t in d.replace("-", "") for t in terms):
            s += 4; r.why.append("site matches the topic")
        if any(h in u for h in _OFFICIAL_HINTS):
            s += 2; r.why.append("documentation / official-looking path")
        if any(d.endswith(x) or x in d for x in _AGGREGATORS):
            s -= 4; r.why.append("aggregator / social")
        if any(t in (r.title + " " + r.snippet).lower() for t in terms):
            s += 1
        r.score = s
        ranked.append(r)
    ranked.sort(key=lambda r: -r.score)
    out = []
    for r in ranked:
        if seen.get(r.domain, 0) >= per_domain:
            continue
        seen[r.domain] = seen.get(r.domain, 0) + 1
        out.append(r)
    return out
