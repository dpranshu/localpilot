"""Research browser: threat intelligence and the Tk interface (compact panel, live view, settings).
(Lifecycle tests moved to test_vercel_lifecycle.py.) Local test site only; no real ads, no personal accounts."""
import array
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from localpilot.agent_browser import jobs, profiles, threatintel, uisettings
from localpilot.agent_browser.proxy import GuardProxy
from localpilot.agent_browser.threatintel import ThreatIntel, deceptive_reasons
from tests.agent_browser_site import ALLOW, LEGIT, Site

HOSTMAP = {h: "127.0.0.1" for h in ALLOW}


def fake_threats(domains=("evil-phish.test",)):
    ti = ThreatIntel(directory=tempfile.mkdtemp())
    ti._sets["phishing"] = array.array("Q", sorted(threatintel._h(d) for d in domains))
    ti._meta["phishing"] = {"label": "test list", "entries": len(domains), "updated": time.time()}
    return ti


def chrome_pids():
    return [p for p, n in jobs.descendants() if any(k in n.lower() for k in ("chrome", "headless", "msedge"))]


# ============================================================ threat intelligence (no browser)
class ThreatIntelTests(unittest.TestCase):
    def test_listed_and_parent_domains_are_known_malicious(self):
        ti = fake_threats()
        self.assertEqual(ti.verdict("https://evil-phish.test/login")[0], "known-malicious")
        self.assertEqual(ti.verdict("https://a.b.evil-phish.test/")[0], "known-malicious")

    def test_never_says_safe(self):
        ti = fake_threats()
        for u in ("https://www.wikipedia.org/", "https://example.com/"):
            self.assertEqual(ti.verdict(u)[0], "unknown")

    def test_deceptive_urls(self):
        for u in ("https://paypal.com.account-verify.top/login", "https://xn--pple-43d.com/",
                  "http://user@evil.example/", "https://login.microsoft.secure-update.verify.example.net/"):
            self.assertTrue(deceptive_reasons(u), u)
        for u in ("https://www.paypal.com/signin", "https://docs.python.org/3/", "https://github.com/x/y"):
            self.assertFalse(deceptive_reasons(u), u)

    def test_unavailable_and_stale_are_reported(self):
        empty = ThreatIntel(directory=tempfile.mkdtemp()).load()
        st = empty.status()
        self.assertEqual(st["phishing"], "unavailable"); self.assertIn("UNAVAILABLE", st["warning"])
        self.assertEqual(st["malware"], "unavailable")
        old = fake_threats(); old._meta["phishing"]["updated"] = time.time() - 5 * 86400
        self.assertEqual(old.status()["phishing"], "stale")

    def test_update_failure_keeps_last_good_copy(self):
        d = tempfile.mkdtemp()
        ti = ThreatIntel(directory=d)
        with open(os.path.join(d, "phishing.bin"), "wb") as f:
            f.write(array.array("Q", [1, 2, 3]).tobytes())
        os.utime(os.path.join(d, "phishing.bin"), (0, 0))              # make it old -> update attempted
        with patch.object(threatintel.requests, "get", side_effect=OSError("offline")):
            r = ti.update()
        self.assertIn("last good copy", r["phishing"])
        self.assertEqual(len(ThreatIntel(directory=d).load()._sets["phishing"]), 3)

    def test_tiny_list_is_refused(self):
        class R:
            text = "a.com\nb.com"
            def raise_for_status(self): pass
        ti = ThreatIntel(directory=tempfile.mkdtemp())
        with patch.object(threatintel.requests, "get", return_value=R()):
            self.assertIn("suspiciously small", ti.update(force=True)["phishing"])

    def test_proxy_refuses_listed_hosts(self):
        px = GuardProxy(threats=fake_threats())
        try:
            ip, why = px.decide("evil-phish.test", 443)
            self.assertIsNone(ip); self.assertIn("known-malicious", why)
        finally:
            px.stop()


# ============================================================ HTTP-first reader (local site)
# ============================================================ interface
class FakeController:
    def __init__(self):
        self.hold_event, self.stop_event = threading.Event(), threading.Event()
        self.frames = 0
        self.threats = fake_threats()
        self.state = {"state": "Idle", "task": "", "url": "", "domain": "", "detail": "", "sources": [],
                      "events": [], "timeline": [], "visited": [], "busy": False, "browser_running": False,
                      "profile": "balanced", "ads_blocked": 0, "downloads_blocked": 0, "security_blocked": 0,
                      "memory": {}, "threats": self.threats.status(), "taking_control": False}
        self.taking_control = None

    def snapshot(self):
        return dict(self.state)

    def preview_frame(self):
        self.frames += 1
        from PIL import Image
        import io
        buf = io.BytesIO(); Image.new("RGB", (64, 48), (90, 90, 90)).save(buf, "JPEG")
        return buf.getvalue()

    def close_browser(self):
        pass

    def navigate(self, url):
        self.navigated = url


class FakeSession:
    def __init__(self, c):
        self.c, self.calls = c, []

    def interrupt(self, t="stop"):
        self.calls.append("stop")

    def hold(self):
        self.calls.append("hold"); self.c.hold_event.set()

    def resume(self):
        self.calls.append("resume"); self.c.hold_event.clear()


class UITests(unittest.TestCase):
    def setUp(self):
        from localpilot.agent_browser import ui
        self.ui = ui
        self.root = tk.Tk(); self.root.geometry("600x500")
        self.c = FakeController(); self.s = FakeSession(self.c)
        self.ws = ui.Workspace(self.root, self.c, self.s, ui.DARK, {"preview_fps": 2.0}, lambda u: None)
        self.panel = ui.CompactPanel(self.root, self.c, self.s, ui.DARK, self.ws.toggle, lambda: None)
        self.panel.pack(fill="x")
        self.root.update()

    def tearDown(self):
        self.ws.close(); self.root.destroy()

    def pump(self, secs):
        end = time.time() + secs
        while time.time() < end:
            self.root.update(); time.sleep(0.02)

    def test_compact_states_controls_and_warning(self):
        p = self.panel
        p.refresh(self.c.snapshot(), self.ui.DARK); self.root.update()
        self.assertEqual(p.state.cget("text"), "Idle")
        self.assertFalse(p.btn_stop.winfo_ismapped())                 # Stop shown only when running
        self.c.state.update(state="Reading", task="Read the Orion survey", url="https://news.test/a",
                            domain="news.test", busy=True, security_blocked=2, ads_blocked=7)
        p.refresh(self.c.snapshot(), self.ui.DARK); self.root.update()
        self.assertEqual(p.state.cget("text"), "Reading")
        self.assertTrue(p.btn_stop.winfo_ismapped())
        self.assertIn("news.test", p.info.cget("text")); self.assertIn("2 unsafe blocked", p.info.cget("text"))
        p.btn_stop.invoke(); p.btn_pause.invoke()
        self.assertEqual(self.s.calls, ["stop", "hold"])
        self.c.state["threats"] = ThreatIntel(directory=tempfile.mkdtemp()).status()   # list unavailable
        p.refresh(self.c.snapshot(), self.ui.DARK); self.root.update()
        self.assertTrue(p.warn.winfo_ismapped()); self.assertIn("UNAVAILABLE", p.warn.cget("text"))

    def test_preview_only_while_visible_and_never_without_a_browser(self):
        self.ws.open(); self.pump(1.0)
        self.assertEqual(self.c.frames, 0, "captured frames although no browser is running")
        self.c.state["browser_running"] = True
        self.pump(1.6)
        self.assertGreaterEqual(self.c.frames, 2)
        self.assertGreaterEqual(self.ws.frames_shown, 1)
        self.ws.win.iconify(); self.root.update(); n = self.c.frames
        self.pump(1.5)
        self.assertEqual(self.c.frames, n, "kept capturing while minimized")
        self.ws.close(); n = self.c.frames; self.pump(1.0)
        self.assertEqual(self.c.frames, n, "kept capturing after collapse")
        self.assertIsNone(self.ws._photo)                               # frame dropped from memory

    def test_theme_switch_recolours_everything(self):
        self.ui.apply_theme(self.root, self.ui.LIGHT, self.ui.DARK); self.root.update()
        self.assertEqual(self.panel.card.inner.cget("bg"), self.ui.LIGHT["card"])
        self.assertEqual(self.panel.task.cget("fg"), self.ui.LIGHT["fg"])
        self.ui.apply_theme(self.root, self.ui.DARK, self.ui.LIGHT); self.root.update()
        self.assertEqual(self.panel.card.inner.cget("bg"), self.ui.DARK["card"])

    def test_resize_and_keyboard(self):
        self.ws.open(); self.root.update()
        for g in ("760x480", "1400x900", "720x460"):
            self.ws.win.geometry(g); self.pump(0.2)
        self.ws.win.focus_force(); self.pump(0.2)            # key events go to the focused window
        self.ws.win.event_generate("<Escape>"); self.pump(0.2)
        self.assertIn("stop", self.s.calls)
        self.ws.win.focus_force(); self.ws.win.event_generate("<Control-l>"); self.pump(0.2)

    def test_settings_validate_and_persist(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "ab.json")
        dlg = self.ui.SettingsDialog(self.root, {}, self.ui.DARK, lambda v: None, self.c)
        dlg.vars["max_tabs"].set("9"); dlg.vars["memory_mb"].set("abc"); dlg.vars["theme"].set("light")
        v = dlg.values()
        self.assertEqual((v["max_tabs"], v["memory_mb"], v["theme"]), (3, 1100, "light"))
        dlg.win.destroy()
        self.assertTrue(uisettings.save(v, path))
        self.assertEqual(uisettings.load(path)["theme"], "light")
        with open(path, "w") as f:
            f.write('{"profile": "turbo", "memory_mb": 99999, "theme": 5}')
        bad = uisettings.load(path)
        self.assertEqual((bad["profile"], bad["memory_mb"], bad["theme"]), ("balanced", 1100, "dark"))


if __name__ == "__main__":
    unittest.main()
