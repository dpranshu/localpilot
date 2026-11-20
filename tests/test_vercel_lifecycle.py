"""Lifecycle & resource limits of the agent-browser research browser (ported from Phase 1):
hard memory budget, recycling, no orphans after a LocalPilot crash, lazy start, idle shutdown,
and session pause / continue / stop through the controller. Local test site only."""
import os
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from dataclasses import replace

from localpilot.agent_browser import abengine, profiles
from localpilot.agent_browser.common import BrowserError

HAVE_AB = os.path.exists(abengine.BIN) and bool(abengine.browser_executable())
if HAVE_AB:
    from tests.agent_browser_site import ALLOW, Site
HOSTMAP = {h: "127.0.0.1" for h in (ALLOW if HAVE_AB else ())}


def ab_procs():
    import psutil
    out = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            n = (p.info["name"] or "").lower()
            cl = " ".join(p.info["cmdline"] or []).lower()
            if "agent-browser" in n or ("chrome" in n and ".agent-browser" in cl):
                out.append(p.pid)
        except Exception:
            pass
    return set(out)


class FakeThreats:
    def verdict(self, url): return "unknown", []
    def lookup(self, host): return None
    def status(self): return {"phishing": "active", "malware": "unavailable", "warning": "", "feeds": {}}


@unittest.skipUnless(HAVE_AB, "agent-browser / Chrome for Testing not installed")
class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = Site()

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()

    def browser(self, **kw):
        return abengine.AgentBrowserEngine(allow_hosts=ALLOW, host_map=HOSTMAP, **kw)

    def controller(self, stop=None, hold=None):
        from localpilot.agent_browser.controller import BrowserController
        return BrowserController(stop or threading.Event(), hold or threading.Event(), "balanced",
                                 allow_hosts=ALLOW, host_map=HOSTMAP, threats=FakeThreats())

    def test_hard_memory_limit_contains_a_memory_hog(self):
        prof = replace(profiles.get("eco"), memory_mb=600)
        with self.browser(profile=prof) as b:
            try:
                b.goto(self.site.url("wiki.test", "/hog")); time.sleep(2)
            except BrowserError:
                pass
            mem = b.memory()
            self.assertTrue(mem["enforced"], "browser processes are not in the memory-capped job")
            self.assertLessEqual(mem["peak_mb"], 600 + 5, "job exceeded its memory budget")
            pc = b.read(self.site.url("wiki.test", "/orion"))       # still usable afterwards
            self.assertIn("telescope", pc.text)

    def test_session_recycles_after_n_pages(self):
        with self.browser(profile=replace(profiles.get("balanced"), recycle_every=2)) as b:
            for _ in range(4):
                b.goto(self.site.url("wiki.test", "/orion"))
            self.assertGreaterEqual(b.recycles, 1)

    def test_no_orphans_even_if_localpilot_crashes(self):
        """Kill-on-close job: a hard-killed LocalPilot leaves no agent-browser or Chrome behind."""
        before = ab_procs()
        code = textwrap.dedent(f"""
            import sys, os, time; sys.path.insert(0, {os.getcwd()!r})
            from localpilot.agent_browser.abengine import AgentBrowserEngine
            b = AgentBrowserEngine().start()
            b.goto('https://example.com')
            print('started', flush=True); time.sleep(1); os._exit(1)      # simulated crash
        """)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        self.assertIn("started", out.stdout, out.stderr[-400:])
        time.sleep(3)
        self.assertEqual(ab_procs() - before, set(), "browser processes survived LocalPilot's crash")

    def test_controller_is_lazy_and_uses_only_agent_browser(self):
        before = ab_procs()
        c = self.controller()
        try:
            self.assertEqual(ab_procs() - before, set(), "a browser started without a task")
            c.start_task("test")
            c.read(self.site.url("news.test", "/article"))
            c.read(self.site.url("wiki.test", "/spa"))
            c.end_task("Completed", "done")
            snap = c.snapshot()
            self.assertEqual([s.method for s in snap["sources"]], ["agent-browser", "agent-browser"])
            self.assertTrue(any(e["kind"] == "prompt injection" for e in snap["events"]))
            self.assertEqual(snap["engine"], "agent-browser")
        finally:
            c.shutdown()
        time.sleep(1.5)
        self.assertEqual(ab_procs() - before, set(), "browser left running after shutdown")

    def test_idle_browser_closes_itself(self):
        c = self.controller()
        c.profile = replace(c.profile, idle_close_s=2)
        try:
            c.start_task("t"); c.read(self.site.url("wiki.test", "/orion")); c.end_task("Completed", "")
            self.assertTrue(c.browser_running)
            time.sleep(5)
            self.assertFalse(c.browser_running, "idle browser was not closed")
        finally:
            c.shutdown()

    def test_session_pause_continue_and_final_stop(self):
        from localpilot.config import Config
        from localpilot.session import Session
        cfg = Config(use_memory=False)
        sess = Session(cfg, None, lambda k, d: None)
        c = self.controller(cfg.stop_event, cfg.hold_event)
        urls = [self.site.url("wiki.test", "/slow")] + [self.site.url("agency.test", "/orion")] * 4
        done = {}

        def task():                                    # the session's task: browse page by page
            c.start_task("pause test")
            try:
                for u in urls:
                    if cfg.stop_event.is_set():
                        break
                    try:
                        c.read(u)
                    except Exception as e:
                        done["err"] = e
                        break
            finally:
                c.end_task("Stopped" if cfg.stop_event.is_set() else "Completed", "")
        sess._worker = threading.Thread(target=task, daemon=True)
        sess._task = "pause test"
        try:
            sess._worker.start(); time.sleep(0.5)
            sess.hold()                                 # Pause button / voice barge-in
            self.assertTrue(sess.paused)
            n = len(self.site.got("agency.test"))
            time.sleep(8)                               # past the slow page: must stay paused
            self.assertEqual(len(self.site.got("agency.test")), n, "acted while paused")
            self.assertEqual(sess.submit("what time is it", "voice"), "paused")    # unrelated: stays paused
            self.assertEqual(sess.submit("continue", "voice"), "resumed")
            time.sleep(1.5)
            sess.interrupt("stop")                      # final
            sess._worker.join(20)
            m = len(self.site.got("agency.test")); time.sleep(2)
            self.assertEqual(len(self.site.got("agency.test")), m, "a stopped task kept going")
            self.assertFalse(c.busy)
            self.assertEqual(sess.submit("continue", "voice"), "nothing")          # stopped stays stopped
        finally:
            c.shutdown()


if __name__ == "__main__":
    unittest.main()
