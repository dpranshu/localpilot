"""Research-content classification: what on a page is NOT evidence.

Two layers:
  1. CLEAN_JS runs inside the page before extraction. It removes element-hiding matches (EasyList
     cosmetic rules), and labels + removes blocks that look like ads, sponsored/promoted units,
     cookie/consent banners, newsletter/promo overlays, and "recommended" link farms. Every removal
     is counted and a short sample is kept, so the decision can be inspected.
  2. Python checks on the result: sponsored search results, affiliate links/disclosures, and
     prompt-injection-like text (instructions aimed at an AI). Flags are evidence for the reader;
     they never cause any action.

Heuristics, not certainty: a page with no ads is not thereby trustworthy, and a legitimate block
can occasionally be mislabeled. Counts and samples are returned so this can be audited.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

CLEAN_JS = r"""
(args) => {
  const out = {hidden: 0, removed: {}, samples: []};
  const note = (kind, el) => {
    out.removed[kind] = (out.removed[kind] || 0) + 1;
    if (out.samples.length < 12) out.samples.push(kind + ': ' + (el.innerText || '').trim().slice(0, 80));
  };
  // 1. EasyList element hiding (plain CSS selectors, in batches; invalid batches split up)
  const sels = args.selectors || [];
  const tryRemove = (list) => {
    try { document.querySelectorAll(list.join(',')).forEach(e => { e.remove(); out.hidden++; }); return true; }
    catch (e) { return false; }
  };
  for (let i = 0; i < sels.length; i += 300) {
    const batch = sels.slice(i, i + 300);
    if (!tryRemove(batch)) batch.forEach(s => tryRemove([s]));
  }
  // 2. labels / attributes that mark ads and paid placements
  const AD_TOK = /^(ad|ads|advert|adverts|advertisement|advertising|adslot|adunit|adsbygoogle|dfp|gpt-ad|sponsor|sponsored|sponsorship|promoted|promo|taboola|outbrain|native-ad|paid-content|partner-content)$/i;
  const LABEL = /^\s*(ad|ads|advertisement|sponsored|promoted|paid partnership|partner content|sponsored content|paid post|anzeige|publicité)\s*[·:|-]?\s*$/i;
  const tokens = (el) => ((el.id || '') + ' ' + (typeof el.className === 'string' ? el.className : '') + ' ' +
                          (el.getAttribute('data-ad') !== null ? 'ad' : '') + ' ' + (el.getAttribute('aria-label') || ''))
                          .split(/[\s_\-]+/).filter(Boolean);
  const blocks = Array.from(document.querySelectorAll('div,section,aside,li,article,iframe,ins,figure,span'));
  for (const el of blocks) {
    if (!el.isConnected || el === document.body) continue;
    if (el.closest('main article p') && el.tagName === 'SPAN') continue;   // inline text
    const tk = tokens(el);
    const labelled = Array.from(el.children).slice(0, 3).some(c => LABEL.test(c.innerText || ''));
    if (tk.some(t => AD_TOK.test(t)) || labelled || el.matches('[rel~="sponsored"]')) {
      if ((el.innerText || '').length < 4000) { note('ad/sponsored', el); el.remove(); }
    }
  }
  // 3. cookie / consent banners and promo overlays (fixed/sticky, covering, promotional words)
  const PROMO = /(cookie|consent|gdpr|subscribe|newsletter|sign up|join now|limited time|% off|discount|download our app|accept all)/i;
  for (const el of Array.from(document.querySelectorAll('body *'))) {
    if (!el.isConnected) continue;
    const cs = getComputedStyle(el);
    if ((cs.position === 'fixed' || cs.position === 'sticky') && PROMO.test((el.innerText || '').slice(0, 400))) {
      note(/(cookie|consent|gdpr|accept all)/i.test(el.innerText) ? 'cookie-banner' : 'promo-overlay', el);
      el.remove();
    }
  }
  // 4. unrelated recommendation link farms ("Recommended for you", "You may also like")
  for (const h of Array.from(document.querySelectorAll('h2,h3,h4,div,span'))) {
    if (/^\s*(recommended( for you)?|you may also like|more from|around the web|trending now|related stories)\s*$/i.test(h.innerText || '')) {
      const box = h.closest('section,aside,div');
      if (box && box !== document.body && (box.innerText || '').length < 3000) { note('recommendations', box); box.remove(); }
    }
  }
  return out;
}
"""

_SPONSORED_LABEL = re.compile(r"(^|\n|\s)(sponsored|ad|ads|promoted|advertisement)(\s|·|:|$)", re.I)
_AD_CLICK = re.compile(r"(googleadservices\.com|/aclk\?|doubleclick\.net|bing\.com/aclick|"
                       r"duckduckgo\.com/y\.js|ad\.doubleclick|/pagead/|adurl=)", re.I)
_AFF_HOSTS = ("amzn.to", "go.skimresources.com", "click.linksynergy.com", "awin1.com", "shareasale.com",
              "prf.hn", "anrdoezrs.net", "tkqlhce.com", "dpbolvw.net", "jdoqocy.com", "rstyle.me",
              "go.redirectingat.com", "shop-links.co", "howl.me", "fave.co", "bit.ly/aff", "impact.com")
_AFF_PARAMS = {"tag", "affid", "aff_id", "affiliate", "affiliate_id", "irclickid", "clickid", "ref_id", "aff"}
_DISCLOSURE = re.compile(r"(we may earn (a|an) (small )?(commission|affiliate)|affiliate link|"
                         r"commission (if|when) you (buy|purchase)|sponsored by|paid partnership|"
                         r"this (post|article) (is|was) sponsored)", re.I)
_INJECTION = [re.compile(p, re.I) for p in (
    r"ignore (all |any )?(previous|prior|above|earlier) (instructions|prompts|rules)",
    r"disregard (all |any )?(previous|prior|your) (instructions|rules|guidelines)",
    r"(you are|act as) (now )?(an? )?(ai|assistant|chatbot|language model)\b.{0,40}(must|should|will)",
    r"\b(system|developer) prompt\b", r"\bnew instructions\b",
    r"(open|run|launch) (a |the )?(terminal|powershell|command prompt|cmd)",
    r"(run|execute) (the following|this) (command|script|code)",
    r"(delete|remove|wipe|format) (all |the |your )?(files|folders|drive|disk|documents)",
    r"(download|install) (and run|this) ", r"send (me |us )?(your|the user's) (password|api key|token|credentials)",
    r"\bto (the|any) (ai|assistant|agent|llm)\b.{0,60}(reading|summari[sz]ing|processing) this",
)]


def classify_search_results(items: list[dict]) -> list[dict]:
    """items: [{title, url, text}] from a results page -> adds 'sponsored' + 'why'."""
    out = []
    for it in items:
        why = []
        if _SPONSORED_LABEL.search((it.get("label") or "") + "\n" + (it.get("text") or "")[:60]):
            why.append("labelled as ad/sponsored")
        if _AD_CLICK.search(it.get("url") or ""):
            why.append("ad-click redirect link")
        out.append({**it, "sponsored": bool(why), "why": why})
    return out


def affiliate_signals(links: list[str], text: str) -> list[str]:
    ev = []
    for u in links[:500]:
        try:
            p = urlsplit(u)
        except ValueError:
            continue
        host = (p.hostname or "").lower()
        if any(host == h or host.endswith("." + h) or u.startswith("https://" + h) for h in _AFF_HOSTS):
            ev.append(f"affiliate link host: {host}")
        elif set(parse_qs(p.query).keys()) & _AFF_PARAMS and ("amazon." in host or "ebay." in host
                                                              or "flipkart." in host):
            ev.append(f"affiliate tag on {host}")
    m = _DISCLOSURE.search(text or "")
    if m:
        ev.append(f"disclosure: '{m.group(0)}'")
    return sorted(set(ev))[:10]


def injection_signals(text: str) -> list[str]:
    """Instruction-like text aimed at an AI. Reported, never obeyed."""
    hits = []
    for rx in _INJECTION:
        for m in rx.finditer(text or ""):
            s = max(0, m.start() - 40)
            hits.append((text[s:m.end() + 40]).replace("\n", " ").strip())
            break
    return hits[:8]
