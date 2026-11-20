"""Security & behaviour tests for LocalPilot's ONLY browser backend: Vercel agent-browser inside
LocalPilot's envelope. Real agent-browser + Chrome for Testing against a LOCAL test site (the guard
proxy maps the *.test names to it). These are the Phase 1 tests, ported, plus agent-browser-specific
checks. Skipped if agent-browser or its Chrome isn't installed."""
import glob
import json
import os
import subprocess
import threading
import time
import unittest

from localpilot.agent_browser import abengine, filterlists
from localpilot.agent_browser.common import BrowserError, BrowserStopped, Limits

HAVE_AB = os.path.exists(abengine.BIN) and bool(abengine.browser_executable())
if HAVE_AB:
    from tests.agent_browser_site import ALLOW, FILTERS, LEGIT, Site


def _engine_filters():
    eng, _ = filterlists.load(use_downloaded=True, extra_rules=FILTERS)
    return eng


def _chrome_procs():
    import psutil
    out = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if "chrome" in (p.info["name"] or "").lower() and any(
                    "chrome-for-testing" in c.lower() or ".agent-browser" in c.lower() for c in (p.info["cmdline"] or [])):
                out.append(p)
        except Exception:
            pass
    return out


@unittest.skipUnless(HAVE_AB, "agent-browser / Chrome for Testing not installed")
class VercelBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = Site()
        cls.filters = _engine_filters()

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()

    def browser(self, **kw):
        kw.setdefault("filters", self.filters)
        return abengine.AgentBrowserEngine(allow_hosts=ALLOW, host_map={h: "127.0.0.1" for h in ALLOW}, **kw)

    def link_ref(self, b, text):
        for line in b.snapshot().splitlines():
            if line.strip().startswith("- link") and text in line:
                return line.split("[ref=")[1].split("]")[0]
        self.fail(f"no link '{text}' in snapshot")

    # 1. startup / shutdown / cleanup: nothing left behind
    def test_01_start_stop_and_cleanup(self):
        b = self.browser().start()
        self.assertEqual(b.status, "ready")
        b.goto(self.site.url("wiki.test", "/orion"))
        session = b.session
        b.close()
        self.assertEqual(b.status, "stopped")
        time.sleep(1.5)
        self.assertEqual(glob.glob(os.path.join(os.path.expanduser("~"), ".agent-browser", session + ".*")), [],
                         "agent-browser session files left behind")
        self.assertFalse(os.path.exists(b.quarantine) or os.path.exists(b.shotdir))

    # 2-3. ads + trackers never reach the network; legitimate 3rd-party kept
    def test_02_ads_and_trackers_never_reach_the_network(self):
        with self.browser() as b:
            b.read(self.site.url("news.test", "/article"))
            s = b.summary()
        for host in ("securepubads.g.doubleclick.net", "ads.adnet.test", "track.spy.test", "www.google-analytics.com"):
            self.assertEqual(self.site.got(host), [], f"{host} request leaked")
        self.assertEqual(self.site.got(path_part="/banner-ad/"), [], "banner image leaked")
        self.assertTrue(self.site.got("cdn.libs.test", "/chart.js"), "a legitimate 3rd-party script was blocked")
        self.assertGreaterEqual(s["ads_trackers_blocked"], 4)

    # 4. sponsored search results are identified (and excluded from evidence)
    def test_03_sponsored_results_identified(self):
        with self.browser() as b:
            pc = b.read(self.site.url("search.test", "/serp"))
        titles = sorted(r["title"] for r in pc.sponsored_results)
        self.assertEqual(titles, ["CheapPhone X official store", "MegaVPN - Fastest VPN 2026"])

    # 5-6. main content clean and complete, with metadata
    def test_04_main_content_clean_and_complete(self):
        with self.browser() as b:
            pc = b.read(self.site.url("news.test", "/article"))
        for sentence in LEGIT:
            self.assertIn(sentence, pc.text, "legitimate sentence lost")
        for junk in ("CheapPhone", "MegaCorp VPN", "AdSense block", "Flash sale", "newsletter"):
            self.assertNotIn(junk, pc.text, f"non-evidence kept: {junk}")
        self.assertEqual(pc.published, "2026-11-10")
        self.assertTrue(pc.affiliate, "affiliate link/disclosure not detected")

    # 7. JavaScript-rendered pages
    def test_05_javascript_rendered_content(self):
        with self.browser() as b:
            pc = b.read(self.site.url("wiki.test", "/spa"))
        self.assertIn("loaded by a script after the page opened", pc.text)

    # 8. navigation: link clicks (links only), tabs, scrolling, back/forward
    def test_06_navigation_click_tabs(self):
        with self.browser() as b:
            b.goto(self.site.url("search.test", "/serp"))
            r = b.click(self.link_ref(b, "Orion telescope - Encyclopedia"))
            self.assertIn("wiki.test", r["url"])
            b.scroll(1)
            self.assertIn("search.test", b.history(-1)["url"])
            self.assertIn("wiki.test", b.history(+1)["url"])
            i = b.new_tab(self.site.url("agency.test", "/orion"))
            self.assertEqual(len(b.tabs()), 2)
            b.switch_tab(0); b.close_tab(i)
            self.assertEqual(len(b.tabs()), 1)
            with self.assertRaises(BrowserError):
                for _ in range(5):
                    b.new_tab()

    # 9. citation fields
    def test_07_citation_fields(self):
        with self.browser() as b:
            pc = b.read(self.site.url("news.test", "/article"))
        self.assertTrue(pc.final_url.endswith("/article") and pc.title.startswith("Orion survey"))
        self.assertTrue(pc.site and pc.retrieved_at.endswith("+00:00"))

    # 11. unsafe targets: schemes, private network, metadata IP, redirects, links, forms, popups
    def test_08_unsafe_targets_blocked(self):
        with self.browser() as b:
            for bad in ("file:///C:/Windows/win.ini", f"http://127.0.0.1:{self.site.port}/secret",
                        "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/", "chrome://settings",
                        "javascript:alert(1)", "data:text/html,<h1>x</h1>"):
                with self.assertRaises(BrowserError, msg=bad):
                    b.goto(bad)
            with self.assertRaises(BrowserError):             # redirect into the local network
                b.goto(self.site.url("news.test", "/redirect-private"))
            self.assertIn("wiki.test", b.goto(self.site.url("news.test", "/redirect-ok"))["url"])
            b.goto(self.site.url("news.test", "/links"))
            for text in ("local file", "router"):
                try:
                    b.click(self.link_ref(b, text))
                except BrowserError:
                    pass
                b.goto(self.site.url("news.test", "/links"))
            b.goto(self.site.url("shop.test", "/form"))
            b.snapshot()
            with self.assertRaises(BrowserError):              # a button is not a link: refused
                b.click("@e1")
            b.goto(self.site.url("news.test", "/popup")); time.sleep(1)
            b.goto(self.site.url("wiki.test", "/orion"))
            s = b.summary()
        self.assertEqual(self.site.got(path_part="/secret"), [], "private-network target was fetched")
        self.assertEqual([h for h in self.site.hits if h[0] == "POST"], [], "a form was submitted")
        self.assertGreaterEqual(s["forms_blocked"], 1)
        self.assertGreaterEqual(s["popups_closed"], 1, "a page-opened popup was left open")

    def test_09_service_worker_cannot_bypass_blocking(self):
        with self.browser() as b:
            b.goto(self.site.url("news.test", "/sw")); time.sleep(1.5)
        self.assertEqual(self.site.got("ads.adnet.test"), [], "ad request leaked via page or service worker")

    def test_10_downloads_are_not_saved(self):
        with self.browser() as b:
            with self.assertRaises(BrowserError):
                b.goto(self.site.url("news.test", "/download"))
            time.sleep(1.0)
            q = b.quarantine
            left = [f for f in os.listdir(q)] if os.path.exists(q) else []
            s = b.summary()
        self.assertEqual(left, [], "a downloaded file was kept")
        self.assertGreaterEqual(s["downloads_blocked"], 1)
        home = os.path.expanduser("~")
        self.assertFalse(glob.glob(os.path.join(home, "Downloads", "setup*.exe")), "a file reached Downloads")

    # 12. prompt injection: flagged as data; the page has no bridge into LocalPilot
    def test_11_prompt_injection_is_flagged_not_obeyed(self):
        with self.browser() as b:
            pc = b.read(self.site.url("news.test", "/article"))
            # test-only raw eval (LocalPilot itself can never run eval: it is not on the allow-list)
            out = subprocess.run([b.exe, "--session", b.session, "--json", "eval",
                                  "Object.keys(window).filter(k => /localpilot|playwright|binding|agent/i.test(k))"],
                                 capture_output=True, text=True, env=b.env, timeout=30).stdout
        self.assertTrue(pc.injection, "malicious instruction not flagged")
        exposed = (json.loads(out.strip().splitlines()[-1]).get("data") or {}) if out.strip() else {}
        self.assertFalse(any("localpilot" in str(x).lower() for x in json.dumps(exposed).split(",")),
                         f"page can see a LocalPilot bridge: {exposed}")
        with self.assertRaises(BrowserError):
            abengine.AgentBrowserEngine.check_command(["eval", "1"])

    # 13. pause has no timeout; stop is final and interrupts a load
    def test_12_pause_has_no_timeout_and_stop_is_final(self):
        stop, hold = threading.Event(), threading.Event()
        b = self.browser(stop_event=stop, hold_event=hold).start()
        try:
            before = len(self.site.got("agency.test"))
            hold.set()
            res = {}
            t = threading.Thread(target=lambda: res.setdefault("r", b.goto(self.site.url("agency.test", "/orion"))),
                                 daemon=True)
            t.start(); t.join(1.5)
            self.assertTrue(t.is_alive(), "paused browser acted on its own")
            self.assertEqual(len(self.site.got("agency.test")), before, "request sent while paused")
            hold.clear(); t.join(20)
            self.assertIn("url", res["r"])
            err, t0 = {}, time.time()

            def slow():
                try:
                    b.goto(self.site.url("wiki.test", "/slow"))
                except Exception as e:
                    err["e"] = e
            t2 = threading.Thread(target=slow, daemon=True); t2.start(); time.sleep(0.8)
            stop.set(); t2.join(10)
            self.assertLess(time.time() - t0, 4.0, "stop did not interrupt the load")
            self.assertIsInstance(err.get("e"), (BrowserStopped, BrowserError))
            with self.assertRaises(BrowserStopped):
                b.goto(self.site.url("wiki.test", "/orion"))
        finally:
            hold.clear(); stop.set(); b.close()

    def test_12b_closing_a_paused_browser_releases_waiters(self):
        hold = threading.Event(); hold.set()
        b = self.browser(hold_event=hold).start()
        err = {}

        def waiter():
            try:
                b.goto(self.site.url("wiki.test", "/orion"))
            except Exception as e:
                err["e"] = e
        t = threading.Thread(target=waiter, daemon=True); t.start(); time.sleep(0.5)
        b.close(); t.join(5)
        self.assertFalse(t.is_alive(), "a paused caller was left hanging after close")
        self.assertIsInstance(err.get("e"), BrowserStopped)

    def test_14_page_limit(self):
        with self.browser(limits=Limits(max_pages=2)) as b:
            b.goto(self.site.url("wiki.test", "/orion")); b.goto(self.site.url("agency.test", "/orion"))
            with self.assertRaises(BrowserError):
                b.goto(self.site.url("news.test", "/article"))

    # DNS rebinding: a public-looking name resolving to a private address is refused
    def test_15_dns_rebinding_refused(self):
        with self.browser() as b:
            with self.assertRaises(BrowserError):
                b.goto(f"http://127.0.0.1.nip.io:{self.site.port}/secret")
        self.assertEqual(self.site.got(path_part="/secret"), [])

    def test_16_proxy_rejects_strangers(self):
        import socket as _s
        with self.browser() as b:
            c = _s.create_connection(("127.0.0.1", b._proxy.port), timeout=5)
            c.sendall(("GET http://wiki.test:%d/orion HTTP/1.1\r\nHost: wiki.test\r\n\r\n" % self.site.port).encode())
            reply = c.recv(200); c.close()
        self.assertIn(b"407", reply)

    def test_18_fake_login_form_cannot_submit(self):
        with self.browser() as b:
            b.goto(self.site.url("shop.test", "/form"))
            snap = b.snapshot()
            refs = [l.split("[ref=")[1].split("]")[0] for l in snap.splitlines() if "[ref=" in l]
            for r in refs:                                     # try every element on the page
                try:
                    b.click(r)
                except BrowserError:
                    pass
        self.assertEqual([h for h in self.site.hits if h[0] == "POST"], [], "a form was submitted")

    def test_19_chromium_sandbox_on_and_isolated_profile(self):
        with self.browser() as b:
            b.goto(self.site.url("wiki.test", "/orion"))
            cmds = [" ".join(p.cmdline()) for p in _chrome_procs()]
        self.assertTrue(cmds, "no Chrome for Testing process found")
        self.assertFalse(any("--no-sandbox" in c for c in cmds), "Chromium launched without its sandbox")
        self.assertTrue(any("--type=renderer" in c for c in cmds))
        personal = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data").lower()
        for c in cmds:
            self.assertNotIn(personal, c.lower(), "the user's personal Chrome profile was used")
            self.assertNotIn("brave", c.lower())
            self.assertNotIn("msedge", c.lower())

    # ---------------- agent-browser specific ----------------
    def test_20_inherited_settings_cannot_redirect_the_browser(self):
        os.environ["AGENT_BROWSER_PROFILE"] = "Default"
        os.environ["AGENT_BROWSER_AUTO_CONNECT"] = "1"
        try:
            b = self.browser()
            self.assertEqual(b.headless, True)
            with b:
                self.assertNotIn("AGENT_BROWSER_PROFILE", b.env)
                self.assertNotIn("AGENT_BROWSER_AUTO_CONNECT", b.env)
                self.assertEqual(b.env["AGENT_BROWSER_ALLOW_FILE_ACCESS"], "0")
        finally:
            os.environ.pop("AGENT_BROWSER_PROFILE", None); os.environ.pop("AGENT_BROWSER_AUTO_CONNECT", None)

    def test_21_command_allow_list(self):
        bad = [["eval", "1"], ["type", "@e1", "x"], ["fill", "@e1", "x"], ["press", "Enter"], ["find", "text", "a", "click"],
               ["upload", "@e1", "C:/x"], ["download", "@e1", "C:/x"], ["cookies"], ["storage", "local"],
               ["network", "requests"], ["auth", "login", "x"], ["connect", "9222"], ["dashboard"], ["chat", "hi"],
               ["stream", "enable"], ["get", "cookies"], ["click", "#go"], ["tab", "../x"], ["open", "x", "--profile", "Default"]]
        for args in bad:
            with self.assertRaises(BrowserError, msg=args):
                abengine.AgentBrowserEngine.check_command(args)
        for ok in (["open", "https://a.com"], ["get", "url"], ["snapshot", "-i"], ["click", "@e3"], ["tab", "new"],
                   ["tab", "close", "t2"], ["scroll", "down", "900"], ["stream", "disable"]):
            abengine.AgentBrowserEngine.check_command(ok)

    def test_22_live_stream_is_off(self):
        with self.browser() as b:
            st = b._run_raw(["stream", "status"])
        data = st.get("data") or {}
        self.assertFalse(data.get("enabled"), f"input-capable live stream is on: {data}")

    def test_23_screenshot_is_real_and_in_memory_only(self):
        with self.browser() as b:
            b.goto(self.site.url("wiki.test", "/orion"))
            jpg = b.screenshot()
            left = os.listdir(b.shotdir)
        self.assertTrue(jpg.startswith(b"\xff\xd8\xff") and len(jpg) > 2000, "not a real JPEG")
        self.assertEqual(left, [], "screenshot file left on disk")


if __name__ == "__main__":
    unittest.main()
