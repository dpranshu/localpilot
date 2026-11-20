"""Security boundary of the research browser's local endpoints -- real agent-browser, local test site.

agent-browser 0.43 exposes unauthenticated loopback ports (daemon commands, Chrome DevTools, and while
a viewer is open the stream server with input + an /api/command relay). LocalPilot's boundary:
  1. a default-deny daemon ACTION POLICY (protocol names) -- enforced for every daemon client;
  2. a PORT GUARD -- any other local process connecting to those ports pauses the task and discards
     the browser (detect + contain; the first packets of a foreign client are NOT prevented);
  3. the viewer is in-process (no listening socket); input needs a controller GRANT, issued only after
     explicit confirmation while the agent can't act.
Foreign clients here are SEPARATE processes, like a real one would be."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
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


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def foreign(code: str) -> subprocess.Popen:
    """Another local program (not LocalPilot, not in its job)."""
    return subprocess.Popen([sys.executable, "-I", "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=0x08000000)


def daemon_cmd(port: int, cmd: dict) -> dict:
    s = socket.create_connection(("127.0.0.1", port), 5)
    s.sendall((json.dumps(cmd) + "\n").encode())
    s.settimeout(10)
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.decode().splitlines()[-1])


def listening_ports(pids) -> set:
    from localpilot.agent_browser.portguard import LISTEN, tcp_table
    return {lp for st, lp, _, _, pid in tcp_table() if st == LISTEN and pid in pids}


@unittest.skipUnless(HAVE_AB and HAVE_WS, "agent-browser / Chrome for Testing / websockets not installed")
class StreamSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = Site()

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()

    def setUp(self):
        from localpilot.agent_browser.controller import BrowserController
        self.hold, self.stop = threading.Event(), threading.Event()
        self.c = BrowserController(self.stop, self.hold, "balanced", allow_hosts=ALLOW, host_map=HOSTMAP,
                                   threats=FakeThreats())
        self.c.start_task("security test")
        self.c.read(self.site.url("wiki.test", "/clickme"))
        self.b = self.c._browser

    def tearDown(self):
        self.c.end_task("Completed", "")
        self.c.shutdown()

    # ---------------------------------------------------------------- 1. policy (any daemon client)
    def test_01_daemon_policy_denies_everything_localpilot_does_not_use(self):
        dport = int(open(self.b._daemon_port_file()).read())
        for action in ("evaluate", "fill", "type", "press", "upload", "download", "cookies_get",
                       "state_save", "storage_get", "network_route", "addscript", "set_content"):
            r = daemon_cmd(dport, {"id": action, "action": action, "script": "1+1", "selector": "a",
                                   "text": "x", "value": "x"})
            self.assertFalse(r.get("success"), f"{action} must be denied: {r}")
            self.assertIn("policy", str(r.get("error", "")).lower(), action)
        r = daemon_cmd(dport, {"id": "u", "action": "url"})               # what LocalPilot needs still works
        self.assertTrue(r.get("success"), r)

    def test_02_stream_command_relay_denies_evaluate(self):
        self.c.viewer_attach()
        try:
            port = int(self.c.stream_endpoint().rsplit(":", 1)[1])
            body = json.dumps({"action": "evaluate", "script": "document.cookie"}).encode()
            s = socket.create_connection(("127.0.0.1", port), 5)
            s.sendall(b"POST /api/command HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: http://127.0.0.1:%d\r\n"
                      b"Content-Length: %d\r\n\r\n" % (port, port, len(body)) + body)
            s.settimeout(10); resp = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                resp += chunk
            s.close()
            self.assertIn(b"denied by policy", resp)
        finally:
            self.c.viewer_detach()

    # ---------------------------------------------------------------- 2. port guard
    def _assert_contained(self, p, label):
        try:
            self.assertTrue(wait_for(lambda: self.c.foreign_clients, 5), f"{label}: foreign client not detected")
            self.assertTrue(self.hold.is_set(), f"{label}: task must be paused")
            self.assertTrue(wait_for(lambda: not self.c.browser_running, 10), f"{label}: browser not discarded")
            b = self.c.foreign_clients[0]
            from localpilot.agent_browser.jobs import descendants
            # a venv's python.exe is a launcher: the real interpreter is its child
            self.assertIn(b["pid"], {p.pid} | {d for d, _ in descendants(p.pid)})
            self.assertTrue(any(e["kind"] == "foreign client" for e in self.c.snapshot()["events"]))
            self.assertIsNone(self.c.stream_endpoint())
        finally:
            p.kill(); p.wait()

    def test_03_foreign_stream_client_is_detected_and_contained(self):
        self.c.viewer_attach()
        try:
            port = int(self.c.stream_endpoint().rsplit(":", 1)[1])
            p = foreign(f"import socket,time; s=socket.create_connection(('127.0.0.1',{port})); time.sleep(20)")
            self._assert_contained(p, "stream")
            # the port is gone with the browser: a second attempt can't connect at all
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), 1).close()
        finally:
            self.c.viewer_detach()

    def test_04_foreign_daemon_client_is_detected_and_contained(self):
        dport = int(open(self.b._daemon_port_file()).read())
        p = foreign(f"import socket,time; s=socket.create_connection(('127.0.0.1',{dport})); time.sleep(20)")
        self._assert_contained(p, "daemon")

    def test_05_foreign_devtools_client_is_detected_and_contained(self):
        ours = self.b._our_pids()
        dport = int(open(self.b._daemon_port_file()).read())
        cdp = sorted(listening_ports(ours) - {dport} - {self.b._stream_port})
        self.assertTrue(cdp, "Chrome's DevTools port should be listening")
        p = foreign(f"import socket,time; s=socket.create_connection(('127.0.0.1',{cdp[0]})); time.sleep(20)")
        self._assert_contained(p, "devtools")

    def test_06_injected_click_cannot_follow_detection(self):
        """Honest measurement: a foreign client that connects and keeps clicking. Its first events may
        land (no prevention), but after detection the browser is gone and nothing more can happen."""
        self.c.viewer_attach()
        try:
            port = int(self.c.stream_endpoint().rsplit(":", 1)[1])
            code = ("import asyncio,json,websockets,time\n"
                    "async def m():\n"
                    f"  async with websockets.connect('ws://127.0.0.1:{port}') as w:\n"
                    "    for i in range(200):\n"
                    "      for t in ('mousePressed','mouseReleased'):\n"
                    "        await w.send(json.dumps({'type':'input_mouse','eventType':t,'x':50,'y':50,"
                    "'button':'left','clickCount':1}))\n"
                    "      await asyncio.sleep(0.05)\n"
                    "try: asyncio.run(m())\nexcept Exception as e: print('cut off', type(e).__name__)")
            n0 = len(self.site.got("wiki.test", "/injected"))
            p = foreign(code)
            self._assert_contained(p, "injector")
            n1 = len(self.site.got("wiki.test", "/injected"))
            time.sleep(2)
            self.assertEqual(len(self.site.got("wiki.test", "/injected")), n1, "nothing lands after containment")
            print(f"\n   [measured] injected navigations before containment: {n1 - n0}")
        finally:
            self.c.viewer_detach()

    def test_07_own_traffic_never_trips_the_guard(self):
        self.c.viewer_attach()
        try:
            from localpilot.agent_browser.viewer import StreamClient
            cl = StreamClient(self.c).start()
            self.assertTrue(wait_for(lambda: cl.connected, 15))
            for _ in range(3):
                self.c.read(self.site.url("wiki.test", "/orion"))
            self.b._since_recycle = self.b.profile.recycle_every          # recycle: new daemon + Chrome
            self.c.read(self.site.url("news.test", "/article"))
            self.assertTrue(wait_for(lambda: cl.connected and "news.test" in cl.url, 15))
            cl.close()
            self.assertEqual(self.c.foreign_clients, [])
            self.assertGreater(self.b._guard.checks, 5)
        finally:
            self.c.viewer_detach()

    # ---------------------------------------------------------------- 3. input authorization
    def test_08_grant_rules(self):
        self.assertIsNone(self.c.grant_control(), "no grant while the agent can act")
        self.assertFalse(self.c.input_allowed(None))
        self.hold.set()
        tok = self.c.grant_control()
        self.assertTrue(tok and self.c.input_allowed(tok))
        self.assertFalse(self.c.input_allowed("0" * 32), "forged token")
        self.hold.clear()
        self.assertFalse(self.c.input_allowed(tok), "agent running again -> input off")
        self.hold.set()
        self.c.revoke_control()
        self.assertFalse(self.c.input_allowed(tok), "revoked")
        tok = self.c.grant_control()
        self.b._on_foreign_client({"at": "", "pid": 1, "name": "test", "port": 1})
        self.assertFalse(self.c.input_allowed(tok), "a foreign client revokes control")

    # ---------------------------------------------------------------- 4. cleanup
    def test_09_cleanup(self):
        ours = self.b._our_pids()
        self.c.viewer_attach(); self.c.stream_endpoint(); self.c.viewer_detach()
        with self.b._lock:
            st = self.b._run_raw(["stream", "status"]).get("data") or {}
        self.assertFalse(st.get("enabled"), "stream off when no viewer is open")
        session, guard = self.b.session, self.b._guard
        self.c.close_browser()
        self.assertTrue(wait_for(lambda: not listening_ports(ours), 10), "a browser port is still listening")
        self.assertTrue(guard._stop.is_set())
        home = os.path.join(os.path.expanduser("~"), ".agent-browser")
        self.assertEqual([f for f in os.listdir(home) if f.startswith(session)], [])


if __name__ == "__main__":
    unittest.main()
