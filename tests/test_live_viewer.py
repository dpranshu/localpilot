"""Live viewer (PiP / expanded) on agent-browser's OFFICIAL stream -- real browser, local test site.

The stream is on only while a viewer is open; frames come from the SAME session the agent drives;
view-only sends no input; Take Control pauses first; recovery after a dropped stream and a browser
recycle; PiP <-> expanded switching, Pause/Resume and Stop through the real windows."""
import os
import threading
import time
import tkinter as tk
import unittest

from localpilot.agent_browser import abengine

HAVE_AB = os.path.exists(abengine.BIN) and bool(abengine.browser_executable())
try:
    import websockets  # noqa: F401
    HAVE_WS = True
except ImportError:
    HAVE_WS = False
if HAVE_AB:
    from tests.agent_browser_site import ALLOW, Site
HOSTMAP = {h: "127.0.0.1" for h in (ALLOW if HAVE_AB else ())}


class FakeThreats:
    def verdict(self, url): return "unknown", []
    def lookup(self, host): return None
    def status(self): return {"phishing": "active", "malware": "unavailable", "warning": "", "feeds": {}}


class FakeSession:
    """Records what the viewer asks LocalPilot's session to do (the real Session's API)."""
    def __init__(self, hold):
        self.hold_event, self.calls, self.busy = hold, [], True
    def hold(self):
        self.calls.append("hold"); self.hold_event.set()
    def resume(self):
        self.calls.append("resume"); self.hold_event.clear()
    def interrupt(self, text="stop"):
        self.calls.append("stop")


def wait_for(cond, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


@unittest.skipUnless(HAVE_AB and HAVE_WS, "agent-browser / Chrome for Testing / websockets not installed")
class LiveViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from localpilot.agent_browser.controller import BrowserController
        cls.site = Site()
        cls.hold, cls.stop = threading.Event(), threading.Event()
        cls.c = BrowserController(cls.stop, cls.hold, "balanced", allow_hosts=ALLOW, host_map=HOSTMAP,
                                  threats=FakeThreats())
        cls.c.start_task("viewer test")
        cls.c.read(cls.site.url("wiki.test", "/orion"))

    @classmethod
    def tearDownClass(cls):
        cls.c.end_task("Completed", "")
        cls.c.shutdown()
        cls.site.stop()

    def stream_enabled(self):
        b = self.c._browser
        with b._lock:
            return bool((b._run_raw(["stream", "status"]).get("data") or {}).get("enabled"))

    def test_0_no_visible_console_window(self):
        """The headless shell is a console program: it must never pop a console on the user's desktop
        (at start or after a recycle)."""
        import ctypes
        import ctypes.wintypes as W
        u = ctypes.windll.user32

        def visible():
            out = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)
            def cb(h, _):
                if u.IsWindowVisible(h):
                    b = ctypes.create_unicode_buffer(512); u.GetWindowTextW(h, b, 512)
                    if ".agent-browser" in b.value or "chrome-headless-shell" in b.value:
                        out.append(b.value)
                return True
            u.EnumWindows(cb, 0)
            return out
        time.sleep(1.0)
        self.assertEqual(visible(), [])
        b = self.c._browser
        b._since_recycle = b.profile.recycle_every
        self.c.read(self.site.url("wiki.test", "/orion")); time.sleep(1.5)
        self.assertEqual(visible(), [], "a recycle must not show a console either")

    def test_1_stream_only_while_viewer_open_and_same_session(self):
        from localpilot.agent_browser.viewer import StreamClient
        self.assertFalse(self.stream_enabled(), "stream must be off with no viewer")
        self.assertIsNone(self.c.stream_endpoint())
        cl = StreamClient(self.c, max_fps=5).start()
        try:
            self.assertTrue(wait_for(lambda: cl.connected and cl.frames_received > 0), cl.last_error)
            self.assertTrue(self.stream_enabled())
            self.assertIn("wiki.test", cl.url or "".join(t.get("url", "") for t in cl.tabs))
            data, _, meta = cl.latest_frame()
            self.assertEqual(data[:2], b"\xff\xd8", "frame is a real JPEG")
            # the agent navigates: the SAME stream shows it, with the command in the activity feed
            self.c.read(self.site.url("news.test", "/article"))
            self.assertTrue(wait_for(lambda: "news.test" in (cl.url or "")))
            self.assertTrue(any(r["action"] for r in cl.activity))
        finally:
            cl.close()
        self.assertTrue(wait_for(lambda: not self.stream_enabled(), 5), "stream must be off after close")

    def test_2_view_only_sends_nothing_until_control(self):
        from localpilot.agent_browser.viewer import StreamClient
        cl = StreamClient(self.c).start()
        try:
            self.assertTrue(wait_for(lambda: cl.connected))
            click = {"type": "input_mouse", "eventType": "mousePressed", "x": 5, "y": 5, "button": "left"}
            self.assertFalse(cl.send_input(click), "view-only must not send input")
            self.assertFalse(cl.send_input({"type": "something_else"}))
            cl.set_control("forged-token")
            self.assertFalse(cl.send_input({"type": "input_mouse", "eventType": "mouseMoved", "x": 5, "y": 5}),
                             "a token the controller didn't issue is refused")
            self.hold.set()                                  # agent paused -> a grant can be issued
            cl.set_control(self.c.grant_control())
            self.assertTrue(cl.send_input({"type": "input_mouse", "eventType": "mouseMoved", "x": 5, "y": 5}))
            self.assertFalse(cl.send_input({"type": "config", "maxFps": 60}), "only input messages")
            self.c.revoke_control()
            self.assertFalse(cl.send_input({"type": "input_mouse", "eventType": "mouseMoved", "x": 5, "y": 5}))
            self.hold.clear()
        finally:
            cl.close()

    def test_3_recovers_after_stream_drop_and_browser_recycle(self):
        from localpilot.agent_browser.viewer import StreamClient
        cl = StreamClient(self.c, max_fps=5).start()
        try:
            self.assertTrue(wait_for(lambda: cl.connected and cl.frames_received > 0))
            b = self.c._browser
            with b._lock:                                    # stream dies under the viewer
                b._run_raw(["stream", "disable"])
            self.assertTrue(wait_for(lambda: cl.reconnects >= 1))
            n = cl.frames_received
            self.c.read(self.site.url("wiki.test", "/orion"))
            self.assertTrue(wait_for(lambda: cl.connected and cl.frames_received > n), cl.last_error)
            # a recycle (fresh Chromium) re-applies the stream and the viewer follows
            b._since_recycle = b.profile.recycle_every
            r0, n = b.recycles, cl.frames_received
            self.c.read(self.site.url("news.test", "/article"))
            self.assertEqual(b.recycles, r0 + 1)
            self.assertTrue(wait_for(lambda: cl.connected and cl.frames_received > n and "news.test" in cl.url),
                            cl.last_error)
        finally:
            cl.close()

    def test_4_windows_pip_expand_pause_stop_take_control(self):
        from localpilot.agent_browser import ui as abui
        from localpilot.agent_browser.viewer import LiveViewer
        root = tk.Tk(); root.withdraw()
        pal = abui.palette("dark"); abui.apply_theme(root, pal, abui.DARK)
        s = FakeSession(self.hold)
        v = LiveViewer(root, self.c, s, pal, {"pip_fps": 5, "expanded_fps": 5})

        def pump(sec):
            end = time.time() + sec
            while time.time() < end:
                root.update(); time.sleep(0.03)
        try:
            v.show_pip(); pump(0.5)
            self.assertEqual(v.mode, "pip")
            pump_until = time.time() + 20
            while time.time() < pump_until and v.pip.view.frames_drawn == 0:
                pump(0.2)
            self.assertGreater(v.pip.view.frames_drawn, 0, "PiP shows the live page")
            self.assertIn("LIVE", v.pip.live.cget("text"))
            v.pip._pause_resume(); pump(0.3)
            self.assertEqual(s.calls[-1], "hold")
            v.pip._pause_resume(); pump(0.3)
            self.assertEqual(s.calls[-1], "resume")
            v.pip._toggle_top(); self.assertFalse(bool(v.pip.win.attributes("-topmost"))); v.pip._toggle_top()
            v.pip._expand(); pump(0.5)
            self.assertEqual(v.mode, "expanded")
            self.assertIsNone(v.pip.client, "PiP's stream client closed when expanding")
            end = time.time() + 20
            while time.time() < end and v.big.view.frames_drawn == 0:
                pump(0.2)
            self.assertGreater(v.big.view.frames_drawn, 0)
            # Take Control: agent paused FIRST, input allowed only then; giving back keeps it paused
            s.calls.clear(); self.hold.clear()
            v.big.confirm = lambda *a: False                 # user declines: paused, still view-only
            v.big._toggle_control()
            self.assertEqual(s.calls, ["hold"]); self.assertFalse(v.big.controlling)
            self.assertFalse(v.big.client.controlling)
            v.big.confirm = lambda *a: True                  # user explicitly authorizes
            v.big._toggle_control()
            self.assertTrue(v.big.controlling); self.assertTrue(v.big.client.controlling)
            v.big._toggle_control()
            self.assertFalse(v.big.client.controlling); self.assertTrue(self.hold.is_set(), "stays paused")
            self.hold.clear()
            v.big._collapse(); pump(0.4)
            self.assertEqual(v.mode, "pip")
            v.pip.win.focus_force(); pump(0.3)
            v.pip.win.event_generate("<Escape>"); pump(0.2)
            self.assertEqual(s.calls[-1], "stop")
            v.close(); pump(0.3)
            self.assertEqual(v.mode, "closed")
            self.assertTrue(wait_for(lambda: not self.stream_enabled(), 5))
        finally:
            v.close(); root.destroy()


if __name__ == "__main__":
    unittest.main()
