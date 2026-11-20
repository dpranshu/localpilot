"""Guard proxy: every connection the Agent Browser makes goes through here.

Why: Playwright's request router does not see redirect hops, and Chromium resolves DNS itself, so
checking URLs in the router alone allows redirect-to-localhost and DNS-rebinding bypasses. This
proxy closes both: it resolves each host ONCE, requires every address to be public, and connects to
exactly the checked IP (pinning). Redirects, sub-resources, WebSockets and service-worker traffic
are just more connections through it.

* Listens on 127.0.0.1 only, random port, Basic proxy-auth with a per-session secret (other local
  programs cannot use it). Chromium is launched with --proxy-bypass-list=<-loopback> so even
  loopback URLs come through here (and are refused).
* Ports: 80 / 443 / 8080 / 8443 for public hosts; explicitly allowed hosts (tests) may use any.
* stop(): refuses new connections and closes every open one at once -> in-flight loads die.
"""
from __future__ import annotations

import base64
import ipaddress
import secrets
import select
import socket
import socketserver
import threading

from . import urlguard

PUBLIC_PORTS = {80, 443, 8080, 8443}


class GuardProxy:
    def __init__(self, allow_hosts=frozenset(), host_map=None, on_block=lambda host, why: None, threats=None,
                 filters=None):
        self.threats = threats                      # ThreatIntel: known-malicious hosts refused
        self.filters = filters                      # FilterEngine: ad/tracker HOSTS refused (||host^ rules)
        self.ads_blocked = 0
        self.page_site = ""                         # registrable domain of the current page (engine sets it)
        self.allow_hosts = frozenset(h.lower() for h in allow_hosts)
        self.host_map = {k.lower(): v for k, v in (host_map or {}).items()}
        self.on_block = on_block
        self.user, self.password = "lp", secrets.token_urlsafe(18)
        self._auth = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        self._socks: set = set()
        self._lock = threading.Lock()
        self.stopped = threading.Event()
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                proxy._handle(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        self._srv = Server(("127.0.0.1", 0), Handler)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True, name="LocalPilot-GuardProxy").start()

    # ---------------------------------------------------------------- decisions
    def _allowed(self, host: str) -> bool:
        return host in self.allow_hosts or any(
            a.startswith("*.") and (host == a[2:] or host.endswith(a[1:])) for a in self.allow_hosts)

    def decide(self, host: str, port: int) -> tuple[str | None, str]:
        """(ip to connect to, reason). ip None = refused."""
        host = (host or "").lower().strip("[]").rstrip(".")
        if self.stopped.is_set():
            return None, "stopped"
        if self.threats is not None:
            listed = self.threats.lookup(host)
            if listed:
                return None, f"known-malicious site ({listed} list)"
        if self.filters is not None and self._ad_host(host):
            self.ads_blocked += 1
            return None, "ad/tracker host (EasyList)"
        if self._allowed(host):
            return self.host_map.get(host) or self._first_ip(host), "explicitly allowed"
        if port not in PUBLIC_PORTS:
            return None, f"port {port} is not allowed"
        ok, why = urlguard.check(f"http://{host}/", frozenset())
        if not ok:
            return None, why
        addrs = urlguard._resolve(host) if not _is_ip(host) else [host]
        public = [a for a in addrs if urlguard.is_public_ip(a)]
        if not public or len(public) != len(addrs):
            return None, f"'{host}' resolves to a non-public address"
        return public[0], "ok"                       # pinned: we connect to the IP we checked

    def _ad_host(self, host: str) -> bool:
        """Host-level rules only (a proxy sees host names, not HTTPS paths): plain ||host^ rules with
        no type/party/domain options, unless an exception rule allows the host."""
        labels = host.split(".")
        suffixes = [".".join(labels[i:]) for i in range(len(labels))]
        from .filters import registrable
        third = bool(self.page_site) and registrable(host) != self.page_site

        def plain(r):
            if not r.host_only or r.types_in or r.types_out or r.domains_in or r.domains_out:
                return False
            return r.party == 0 or (r.party == 1 and third) or (r.party == -1 and not third and self.page_site)
        if any(plain(r) for sfx in suffixes for r in self.filters.allow_hosts.get(sfx, ())):
            return False
        return any(plain(r) for sfx in suffixes for r in self.filters.block_hosts.get(sfx, ()))

    @staticmethod
    def _first_ip(host):
        try:
            return socket.getaddrinfo(host, None)[0][4][0]
        except OSError:
            return None

    # ---------------------------------------------------------------- plumbing
    def _track(self, *socks):
        with self._lock:
            self._socks.update(socks)

    def _untrack(self, *socks):
        with self._lock:
            for s in socks:
                self._socks.discard(s)
                try:
                    s.close()
                except OSError:
                    pass

    _EXT_TYPE = {".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
                 ".svg": "image", ".ico": "image", ".js": "script", ".mjs": "script", ".css": "stylesheet",
                 ".woff": "font", ".woff2": "font", ".ttf": "font", ".mp4": "media", ".webm": "media", ".mp3": "media"}

    def _url_blocked(self, url: str, hdrs: dict):
        """Full EasyList network-rule match for a plain-HTTP request (type from Accept / extension)."""
        import os.path
        from urllib.parse import urlsplit
        accept = hdrs.get("accept", "")
        ext = os.path.splitext(urlsplit(url).path)[1].lower()
        if "text/html" in accept:
            rtype = "document" if not self.page_site else "subdocument"
        elif accept.startswith("image/") or self._EXT_TYPE.get(ext) == "image":
            rtype = "image"
        else:
            rtype = self._EXT_TYPE.get(ext, "other")
        page = f"http://{self.page_site}/" if self.page_site else url
        try:
            blocked, rule = self.filters.match(url, rtype, page)
        except Exception:
            return False, None
        if blocked:
            try:
                self.on_block(urlsplit(url).hostname or url, f"ad/tracker (EasyList: {str(rule)[:60]})")
            except Exception:
                pass
        return blocked, rule

    def _handle(self, client: socket.socket):
        self._track(client)
        upstream = None
        try:
            client.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(65536)
                if not chunk:
                    return
                head += chunk
                if len(head) > 65536:
                    return
            header, _, rest = head.partition(b"\r\n\r\n")
            lines = header.decode("latin-1").split("\r\n")
            method, target, version = (lines[0].split(" ") + ["", ""])[:3]
            hdrs = {}
            for ln in lines[1:]:
                k, _, v = ln.partition(":")
                hdrs[k.strip().lower()] = v.strip()
            if hdrs.get("proxy-authorization") != self._auth:
                client.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                               b"Proxy-Authenticate: Basic realm=\"LocalPilot\"\r\nContent-Length: 0\r\n\r\n")
                return
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                port = int(port or 443)
            else:
                from urllib.parse import urlsplit
                u = urlsplit(target)
                if u.scheme not in ("http", "ws"):
                    return self._refuse(client, target, f"scheme '{u.scheme}' not allowed")
                host, port = u.hostname or "", u.port or 80
            ip, why = self.decide(host, port)
            if ip is None:
                return self._refuse(client, host, why)
            if method.upper() != "CONNECT":
                # plain HTTP: the proxy sees the WHOLE request, so it can do more than host checks
                if method.upper() not in ("GET", "HEAD", "OPTIONS"):
                    return self._refuse(client, target, f"{method.upper()} request refused (no form submissions)")
                if self.filters is not None:
                    hit, rule = self._url_blocked(target, hdrs)
                    if hit:
                        return self._refuse(client, target, f"ad/tracker (EasyList: {str(rule)[:60]})")
            upstream = socket.create_connection((ip, port), timeout=15)
            self._track(upstream)
            if method.upper() == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
            else:
                from urllib.parse import urlsplit
                u = urlsplit(target)
                path = (u.path or "/") + (("?" + u.query) if u.query else "")
                out = [f"{method} {path} {version}"]
                for ln in lines[1:]:
                    k = ln.split(":", 1)[0].strip().lower()
                    if k in ("proxy-authorization", "proxy-connection", "connection", "keep-alive"):
                        continue
                    out.append(ln)
                out.append("Connection: close")       # one request per connection: no host mixing
                upstream.sendall(("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + rest)
            self._relay(client, upstream)
        except (OSError, ValueError):
            pass
        finally:
            self._untrack(client, *( [upstream] if upstream else []))

    def _refuse(self, client, host, why):
        self.on_block(host, why)
        try:
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass

    def _relay(self, a: socket.socket, b: socket.socket):
        socks = [a, b]
        while not self.stopped.is_set():
            r, _, x = select.select(socks, [], socks, 0.5)
            if x:
                return
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)

    def drop_all(self):
        """Kill every open connection now (used by stop)."""
        with self._lock:
            socks = list(self._socks)
        self._untrack(*socks)

    def stop(self):
        self.stopped.set()
        self.drop_all()
        try:
            self._srv.shutdown(); self._srv.server_close()
        except OSError:
            pass


def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False
