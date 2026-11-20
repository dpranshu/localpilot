"""Shared HTML -> evidence extraction for pages read by the agent-browser.

page_from_html() turns a page's HTML into clean main text + citation metadata (title, site,
publication date and where it came from), flags sponsored results, affiliate links and
instruction-like text. (The old HTTP-first fetching path was removed: LocalPilot browses only
through Vercel agent-browser.)
"""
from __future__ import annotations

import datetime as _dt
import re
from urllib.parse import urljoin, urlsplit


from . import classify, extract, urlguard

TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_MAGIC = [(b"MZ", "Windows executable"), (b"PK\x03\x04", "ZIP/Office archive"), (b"%PDF", "PDF document"),
          (b"\x7fELF", "Linux executable"), (b"Rar!", "RAR archive"), (b"7z\xbc\xaf", "7-Zip archive"),
          (b"\x1f\x8b", "gzip archive"), (b"\xd0\xcf\x11\xe0", "Office/MSI document"),
          (b"#!", "script"), (b"\xca\xfe\xba\xbe", "Java/Mach-O binary")]
_DATE_PATTERNS = [
    ("jsonld:datePublished", r'"datePublished"\s*:\s*"([^"]+)"'),
    ("meta:article:published_time", r'<meta[^>]+(?:property|name)=["\']article:published_time["\'][^>]+content=["\']([^"\']+)'),
    ("meta:article:published_time", r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']article:published_time'),
    ("meta:date", r'<meta[^>]+(?:name|itemprop)=["\'](?:date|pubdate|publish-date|datePublished|DC\.date\.issued)["\'][^>]+content=["\']([^"\']+)'),
    ("time", r'<time[^>]+datetime=["\']([^"\']+)'),
    ("jsonld:dateModified", r'"dateModified"\s*:\s*"([^"]+)"'),
]
_AD_ATTR = re.compile(r'\b(class|id)=["\'][^"\']*\b(ad|ads|advert|advertisement|adslot|adunit|adsbygoogle|'
                      r'sponsor|sponsored|promoted|taboola|outbrain|native-ad|paid-content)\b', re.I)
_SPA_HINT = re.compile(r'<div[^>]+id=["\'](root|app|__next|__nuxt|svelte)["\']|enable javascript|'
                       r'requires javascript|<noscript>', re.I)


def page_from_html(url: str, final: str, html: str, status=None, method: str = "http",
                   min_chars: int = 400) -> extract.PageContent:
    """One extraction pipeline for every engine: ad-block cleanup, trafilatura text, dates, flags."""
    if True:
        clean = _strip_ad_blocks(html)
        meta = extract.main_text(clean, final) or {}
        cands = []
        for kind, pat in _DATE_PATTERNS:
            for m in re.finditer(pat, html[:400_000], re.I):
                cands.append((kind, m.group(1)))
                break
        pub, src, mod = extract.pick_dates(cands)
        if not pub and meta.get("date"):
            guess = extract._norm_date(str(meta["date"]))
            today = _dt.date.today().isoformat()
            if guess and guess != today:           # an estimate equal to today is not a real date
                pub, src = guess, "estimated (trafilatura)"
        text = meta.get("text") or ""
        links = [urljoin(final, h) for h in re.findall(r'<a[^>]+href=["\']([^"\'#]+)', html[:500_000], re.I)][:800]
        title = meta.get("title") or (re.search(r"<title[^>]*>([^<]{0,300})", html, re.I) or [None, ""])[1]
        pc = extract.PageContent(
            url=url, final_url=final, title=(title or "").strip(),
            site=meta.get("sitename") or (urlsplit(final).hostname or ""), author=meta.get("author") or "",
            published=pub, published_source=src, modified=mod,
            retrieved_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            text=text, language=meta.get("language") or "", description=meta.get("description") or "",
            links=links, affiliate=classify.affiliate_signals(links, text),
            injection=classify.injection_signals(text))
        pc.method = method
        pc.status = status
        pc.sponsored_results = _sponsored_from_html(html, final)
        pc.needs_browser = len(text) < min_chars or (len(text) < 1500 and bool(_SPA_HINT.search(html)))
        return pc


def _sponsored_from_html(html: str, base: str) -> list:
    """Search-result style blocks (link with a heading) that are labelled or routed as ads."""
    try:
        import lxml.html
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    items = []
    for a in doc.xpath("//a[@href][.//h1 or .//h2 or .//h3 or ancestor::h1 or ancestor::h2 or ancestor::h3]")[:40]:
        box = a.getparent()
        while box is not None and box.tag not in ("li", "article", "div"):
            box = box.getparent()
        box = box if box is not None else a
        labels = [ (e.text_content() or "").strip() for e in box.xpath(".//span|.//small|.//div")][:6]
        label = next((t for t in labels if re.match(r"^(sponsored|ad|ads|promoted|advertisement)\b", t, re.I)), "")
        items.append({"title": " ".join(a.text_content().split())[:200], "url": urljoin(base, a.get("href")),
                      "label": label, "text": " ".join(box.text_content().split())[:300]})
    return [r for r in classify.classify_search_results(items) if r["sponsored"]]


def _strip_ad_blocks(html: str) -> str:
    """Remove elements whose class/id marks them as ads/sponsored (before trafilatura)."""
    try:
        import lxml.html
        doc = lxml.html.fromstring(html)
        n = 0
        for el in doc.xpath("//*[@class or @id]"):
            attr = f'class="{el.get("class", "")}" id="{el.get("id", "")}"'
            if _AD_ATTR.search(attr) and el.getparent() is not None and len(el.text_content()) < 4000:
                el.getparent().remove(el); n += 1
        return lxml.html.tostring(doc, encoding="unicode") if n else html
    except Exception:
        return html
