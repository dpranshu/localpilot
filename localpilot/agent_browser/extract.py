"""Main-content extraction + metadata (title, site, author, publication date)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

DATE_JS = r"""
() => {
  const pick = [];
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const walk = (o) => { if (!o || typeof o !== 'object') return;
        if (Array.isArray(o)) return o.forEach(walk);
        for (const k of ['datePublished', 'dateCreated', 'uploadDate']) if (o[k]) pick.push(['jsonld:' + k, String(o[k])]);
        if (o.dateModified) pick.push(['jsonld:dateModified', String(o.dateModified)]);
        if (o['@graph']) walk(o['@graph']); };
      walk(JSON.parse(s.textContent));
    } catch (e) {}
  }
  for (const n of ['article:published_time', 'og:published_time', 'datePublished', 'pubdate', 'publish-date',
                   'date', 'dc.date', 'DC.date.issued', 'sailthru.date', 'parsely-pub-date', 'article:modified_time'])
    for (const m of document.querySelectorAll(`meta[property="${n}"],meta[name="${n}"],meta[itemprop="${n}"]`))
      if (m.content) pick.push(['meta:' + n, m.content]);
  const t = document.querySelector('article time[datetime], main time[datetime], time[datetime]');
  if (t) pick.push(['time', t.getAttribute('datetime')]);
  return pick.slice(0, 12);
}
"""

_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


@dataclass
class PageContent:
    url: str
    final_url: str
    title: str = ""
    site: str = ""
    author: str = ""
    published: str = ""            # YYYY-MM-DD when known
    published_source: str = ""     # where the date came from (jsonld / meta / time / trafilatura)
    modified: str = ""
    retrieved_at: str = ""
    text: str = ""                 # cleaned main content
    language: str = ""
    description: str = ""
    links: list = field(default_factory=list)
    removed: dict = field(default_factory=dict)        # what cleaning removed, by kind
    removed_samples: list = field(default_factory=list)
    hidden_by_filters: int = 0
    sponsored_results: list = field(default_factory=list)
    affiliate: list = field(default_factory=list)
    injection: list = field(default_factory=list)       # instruction-like text found (NOT obeyed)
    blocked_requests: int = 0
    raw_html: str = ""             # kept only when keep_raw=True
    untrusted: bool = True         # always: web content is data, never instructions
    error: str = ""


def _norm_date(s: str) -> str:
    m = _ISO.search(s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def pick_dates(candidates: list) -> tuple[str, str, str]:
    """(published, source, modified) from page metadata candidates."""
    pub = src = mod = ""
    for kind, val in candidates:
        d = _norm_date(val)
        if not d:
            continue
        if "modified" in kind:
            mod = mod or d
        elif not pub:
            pub, src = d, kind
    return pub, src, mod


def main_text(html: str, url: str) -> dict:
    """trafilatura extraction (local, Apache-2.0). Returns text + metadata dict."""
    import trafilatura
    doc = trafilatura.bare_extraction(html, url=url, with_metadata=True, include_comments=False,
                                      include_tables=True, include_links=False, favor_precision=True)
    d = (doc if isinstance(doc, dict) else (doc.as_dict() if hasattr(doc, "as_dict") else vars(doc))) if doc else {}
    if len(d.get("text") or "") < 1000 and len(html) > 50_000:
        # precision mode found little on a big page (e.g. a rendered front page): try recall mode
        doc2 = trafilatura.bare_extraction(html, url=url, with_metadata=True, include_comments=False,
                                           include_tables=True, include_links=False, favor_recall=True)
        d2 = (doc2 if isinstance(doc2, dict) else (doc2.as_dict() if hasattr(doc2, "as_dict") else vars(doc2))) if doc2 else {}
        if len(d2.get("text") or "") > len(d.get("text") or ""):
            d = d2
    if not d:
        return {}
    return {k: d.get(k) for k in ("title", "author", "date", "sitename", "text", "language", "description")}
