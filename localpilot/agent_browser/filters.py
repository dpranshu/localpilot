"""Ad/tracker filter engine: a documented SUBSET of Adblock Plus / EasyList syntax.

SUPPORTED (network rules)
  ||host^...      host anchor (domain and its subdomains)     |...  / ...|   start / end anchor
  *  wildcard     ^  separator (anything but a letter, digit, _ - . %)    @@  exception (allow)
  /regex/         plain regular expressions (compiled lazily; invalid ones are skipped)
  $options        third-party / 3p, ~third-party / first-party / 1p,
                  resource types: script image stylesheet(css) xmlhttprequest(xhr) subdocument(frame)
                  media font websocket ping object other document  (and their ~negations),
                  domain=a.com|~b.com, important, match-case (ignored: matching is case-insensitive)
SUPPORTED (element hiding)
  ##selector  /  a.com,b.com##selector  /  #@#selector exceptions -- plain CSS selectors only
NOT SUPPORTED (rule skipped and counted in .stats["unsupported"])
  $redirect $removeparam $csp $rewrite $replace $header $permissions $method $to/$from
  $denyallow $popup $generichide/$elemhide, scriptlets (##+js), procedural/extended CSS
  (#?#, #$#, :has(), :-abp-*, :style(), :matches-*, :upward, :xpath ...), HTML filters (##^)

Third-party detection uses an approximate "registrable domain" (last two labels, or three for
common two-level suffixes such as co.uk). There is no full Public Suffix List; this is noted as a
known limitation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

TYPES = {"script", "image", "stylesheet", "xmlhttprequest", "subdocument", "media", "font",
         "websocket", "ping", "object", "other", "document"}
_TYPE_ALIAS = {"css": "stylesheet", "xhr": "xmlhttprequest", "frame": "subdocument",
               "object-subrequest": "object"}
_IGNORED_OPTS = {"match-case", "important", "all"}          # "all" ~ every type: treat as no type limit
_UNSUPPORTED_OPTS = {"redirect", "redirect-rule", "removeparam", "csp", "rewrite", "replace", "header",
                     "permissions", "method", "to", "from", "denyallow", "popup", "popunder",
                     "generichide", "ghide", "elemhide", "ehide", "specifichide", "shide", "badfilter",
                     "empty", "mp4", "inline-script", "inline-font", "cname", "urltransform", "strict1p",
                     "strict3p", "sitekey", "genericblock", "rewrite", "webrtc", "uritransform"}
_BAD_CSS = re.compile(r":(has|has-text|matches-|xpath|upward|remove|style|watch-attr|min-text|"
                      r"-abp-|contains|others|if|if-not|nth-ancestor|matches-css|matches-path)", re.I)
_TWO_LEVEL = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.in", "net.in",
              "org.in", "co.jp", "ne.jp", "or.jp", "com.br", "com.cn", "com.mx", "co.nz", "co.za",
              "com.tr", "com.sg", "com.hk", "co.kr", "com.ar", "com.my", "com.pk", "co.id", "com.vn"}
_TOKEN = re.compile(r"[a-z0-9%]{3,}")


def registrable(host: str) -> str:
    """Approximate eTLD+1 (no full Public Suffix List)."""
    parts = (host or "").lower().strip(".").split(".")
    if len(parts) <= 2:
        return ".".join(parts)
    if ".".join(parts[-2:]) in _TWO_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


@dataclass
class NetRule:
    raw: str
    exception: bool = False
    important: bool = False
    regex: "re.Pattern | None" = None
    pattern_src: str = ""
    host: str = ""                  # for ||host^-only rules (fast path)
    host_only: bool = False         # rule is exactly ||host^ (+options)
    party: int = 0                  # 0 any, 1 third-party only, -1 first-party only
    types_in: frozenset = frozenset()
    types_out: frozenset = frozenset()
    domains_in: tuple = ()
    domains_out: tuple = ()

    def matches(self, url_l: str, host: str, rtype: str, page_host: str) -> bool:
        if self.types_in and rtype not in self.types_in:
            return False
        if rtype in self.types_out:
            return False
        if not self.types_in and rtype == "document" and not self.host_only:
            return False             # generic rules never block a whole top-level page
        if self.party:
            third = registrable(host) != registrable(page_host) if page_host else True
            if (self.party == 1) != third:
                return False
        if self.domains_in and not any(_host_matches(page_host, d) for d in self.domains_in):
            return False
        if any(_host_matches(page_host, d) for d in self.domains_out):
            return False
        if self.host_only:
            return _host_matches(host, self.host)
        if self.regex is None:
            try:
                self.regex = re.compile(self.pattern_src, re.I)
            except re.error:
                self.regex = re.compile(r"(?!x)x")          # never matches
        return self.regex.search(url_l) is not None


def _pattern_to_regex(p: str) -> str:
    if len(p) > 2 and p.startswith("/") and p.endswith("/"):
        return p[1:-1]
    out, i = [], 0
    if p.startswith("||"):
        out.append(r"^[a-z][a-z0-9+.-]*://(?:[^/?#]*\.)?"); i = 2
    elif p.startswith("|"):
        out.append("^"); i = 1
    end_anchor = p.endswith("|") and len(p) > i
    body = p[i:len(p) - 1] if end_anchor else p[i:]
    for ch in body:
        if ch == "*":
            out.append(".*")
        elif ch == "^":
            out.append(r"(?:[^\w\-.%]|$)")
        else:
            out.append(re.escape(ch))
    if end_anchor:
        out.append("$")
    return "".join(out)


class FilterEngine:
    def __init__(self):
        self.block_hosts: dict[str, list[NetRule]] = {}
        self.allow_hosts: dict[str, list[NetRule]] = {}
        self.block_tok: dict[str, list[NetRule]] = {}
        self.allow_tok: dict[str, list[NetRule]] = {}
        self.block_generic: list[NetRule] = []
        self.allow_generic: list[NetRule] = []
        self.hide_generic: set[str] = set()
        self.hide_specific: dict[str, set[str]] = {}
        self.hide_except: dict[str, set[str]] = {}       # domain ('' = all) -> selectors
        self.stats = {"lines": 0, "network": 0, "exceptions": 0, "cosmetic": 0,
                      "unsupported": 0, "comments": 0, "invalid": 0}

    # ------------------------------------------------------------------ parsing
    def add_list(self, text: str):
        for line in text.splitlines():
            self.add_rule(line)
        return self

    def add_rule(self, line: str):
        line = line.strip()
        self.stats["lines"] += 1
        if not line or line.startswith(("!", "[")):
            self.stats["comments"] += 1
            return
        for marker in ("#?#", "#$#", "#%#", "#@$#", "#@?#", "##+js", "#@#+js", "##^", "$$"):
            if marker in line:
                self.stats["unsupported"] += 1
                return
        m = re.match(r"^([^#]*)(#@?#)(.+)$", line)
        if m and not line.startswith(("|", "/", "@@")) or (m and "##" in line and "$" not in m.group(1)):
            self._add_cosmetic(m.group(1), m.group(2) == "#@#", m.group(3))
            return
        self._add_network(line)

    def _add_cosmetic(self, domains: str, exception: bool, selector: str):
        if _BAD_CSS.search(selector) or selector.startswith("+js"):
            self.stats["unsupported"] += 1
            return
        self.stats["cosmetic"] += 1
        doms = [d.strip().lower() for d in domains.split(",") if d.strip()]
        if exception:
            for d in doms or [""]:
                self.hide_except.setdefault(d.lstrip("~"), set()).add(selector)
            return
        if not doms:
            self.hide_generic.add(selector)
            return
        for d in doms:
            if not d.startswith("~"):
                self.hide_specific.setdefault(d, set()).add(selector)

    def _add_network(self, line: str):
        rule = NetRule(raw=line)
        if line.startswith("@@"):
            rule.exception, line = True, line[2:]
        pattern, opts = line, ""
        if "$" in line and not (line.startswith("/") and line.rfind("$") < line.rfind("/")):
            pattern, opts = line.rsplit("$", 1)
        types_in, types_out = set(), set()
        for o in [x.strip().lower() for x in opts.split(",") if x.strip()]:
            neg = o.startswith("~")
            name = o[1:] if neg else o
            name, _, val = name.partition("=")
            name = _TYPE_ALIAS.get(name, name)
            if name in ("third-party", "3p"):
                rule.party = -1 if neg else 1
            elif name in ("first-party", "1p"):
                rule.party = 1 if neg else -1
            elif name in TYPES:
                (types_out if neg else types_in).add(name)
            elif name == "domain":
                ins, outs = [], []
                for d in val.split("|"):
                    d = d.strip().lower()
                    if d:
                        (outs if d.startswith("~") else ins).append(d.lstrip("~"))
                rule.domains_in, rule.domains_out = tuple(ins), tuple(outs)
            elif name == "important":
                rule.important = True
            elif name in _IGNORED_OPTS:
                continue
            else:                                        # unknown or unsupported: skip whole rule
                self.stats["unsupported"] += 1
                return
        rule.types_in, rule.types_out = frozenset(types_in), frozenset(types_out)
        if not pattern or pattern in ("*", "|", "||"):
            if not (rule.domains_in or rule.types_in):
                self.stats["invalid"] += 1
                return
            pattern = "*"
        hm = re.fullmatch(r"\|\|([a-z0-9.\-_]+)\^?", pattern, re.I)
        if hm and not rule.exception or (hm and rule.exception):
            rule.host, rule.host_only = hm.group(1).lower(), True
        else:
            rule.pattern_src = _pattern_to_regex(pattern.lower())
        self.stats["exceptions" if rule.exception else "network"] += 1
        if rule.host_only:
            (self.allow_hosts if rule.exception else self.block_hosts).setdefault(rule.host, []).append(rule)
            return
        tok = self._best_token(pattern.lower())
        if tok:
            (self.allow_tok if rule.exception else self.block_tok).setdefault(tok, []).append(rule)
        else:
            (self.allow_generic if rule.exception else self.block_generic).append(rule)

    @staticmethod
    def _best_token(p: str) -> str:
        if p.startswith("/") and p.endswith("/"):
            return ""
        best = ""
        for m in _TOKEN.finditer(p):
            s, e = m.span()
            if (s > 0 and p[s - 1] == "*") or (e < len(p) and p[e] == "*"):
                continue                                  # token may be partial next to a wildcard
            if len(m.group()) > len(best):
                best = m.group()
        return best

    # ------------------------------------------------------------------ matching
    def _candidates(self, url_l: str, host: str, hosts_idx, tok_idx, generic):
        labels = host.split(".")
        for i in range(len(labels)):
            yield from hosts_idx.get(".".join(labels[i:]), ())
        for t in set(_TOKEN.findall(url_l)):
            yield from tok_idx.get(t, ())
        yield from generic

    def match(self, url: str, rtype: str = "other", page_url: str = "") -> tuple[bool, str]:
        """(blocked?, matching rule). rtype uses ABP names (see TYPES)."""
        url_l = url.lower()
        host = (urlsplit(url_l).hostname or "")
        page_host = (urlsplit(page_url.lower()).hostname or "") if page_url else host
        blocker = None
        for r in self._candidates(url_l, host, self.block_hosts, self.block_tok, self.block_generic):
            if r.matches(url_l, host, rtype, page_host):
                blocker = r
                if r.important:
                    return True, r.raw
                break
        if blocker is None:
            return False, ""
        for r in self._candidates(url_l, host, self.allow_hosts, self.allow_tok, self.allow_generic):
            if r.matches(url_l, host, rtype, page_host):
                return False, r.raw
        return True, blocker.raw

    def hide_selectors(self, page_host: str) -> list[str]:
        """Element-hiding selectors for a page (generic + site-specific, minus exceptions)."""
        page_host = (page_host or "").lower()
        sels = set(self.hide_generic)
        labels = page_host.split(".")
        for i in range(len(labels)):
            sels |= self.hide_specific.get(".".join(labels[i:]), set())
        exc = set(self.hide_except.get("", set()))
        for i in range(len(labels)):
            exc |= self.hide_except.get(".".join(labels[i:]), set())
        return sorted(sels - exc)


# Playwright resource_type -> ABP type
def abp_type(resource_type: str, is_main_frame_nav: bool) -> str:
    return {"document": "document" if is_main_frame_nav else "subdocument",
            "stylesheet": "stylesheet", "image": "image", "media": "media", "font": "font",
            "script": "script", "xhr": "xmlhttprequest", "fetch": "xmlhttprequest",
            "websocket": "websocket", "eventsource": "other", "manifest": "other",
            "texttrack": "media", "ping": "ping", "beacon": "ping"}.get(resource_type, "other")
