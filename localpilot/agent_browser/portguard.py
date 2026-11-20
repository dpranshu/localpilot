"""Port guard: nobody but LocalPilot may talk to the research browser's local ports.

agent-browser 0.43 exposes three UNAUTHENTICATED loopback TCP endpoints while it runs (verified):
  * the daemon's command port (what the CLI uses on Windows),
  * Chrome's DevTools port (--remote-debugging-port=0; full control of the browser),
  * while a viewer is open, the stream server (WebSocket input_mouse/keyboard/touch is dispatched
    straight to Chrome, and HTTP /api/command relays commands to the daemon).
None of them can be bound privately or given a token through a supported option. This guard watches
the OS TCP table every ~100 ms: a connection to any port listened on by a process in LocalPilot's
job object, from a process that is neither LocalPilot itself nor in that job, is a breach and is
reported to `on_breach` (the engine pauses the task and discards the browser).

It DETECTS and CONTAINS; it does not prevent the first packets of a foreign client.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import threading
import time

_iphlp = ctypes.WinDLL("iphlpapi")
LISTEN, SYN_SENT, SYN_RCVD, ESTABLISHED = 2, 3, 4, 5
_LOOPBACK = 0x0100007F          # 127.0.0.1 as stored in the table (network byte order, read little-endian)


class _ROW(ctypes.Structure):
    _fields_ = [("state", wt.DWORD), ("laddr", wt.DWORD), ("lport", wt.DWORD),
                ("raddr", wt.DWORD), ("rport", wt.DWORD), ("pid", wt.DWORD)]


def _port(v: int) -> int:
    return ((v & 0xFF) << 8) | ((v >> 8) & 0xFF)


def tcp_table() -> list[tuple[int, int, int, int, int]]:
    """IPv4 TCP rows: (state, local_port, remote_addr, remote_port, owning_pid)."""
    size = wt.DWORD(0)
    _iphlp.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 5, 0)   # AF_INET, OWNER_PID_ALL
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = wt.DWORD(len(buf))
        if _iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 5, 0) == 0:
            n = ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0]
            rows = ctypes.cast(ctypes.addressof(buf) + 4, ctypes.POINTER(_ROW * n)).contents
            return [(r.state, _port(r.lport), r.raddr, _port(r.rport), r.pid) for r in rows]
    return []


def process_name(pid: int) -> str:
    try:
        from .jobs import _all_processes
        return next((n for p, n in _all_processes() if p == pid), "?")
    except Exception:
        return "?"


def foreign_clients(rows, ours: set[int]) -> list[tuple[int, int]]:
    """(pid, port) of loopback connections to OUR listening ports from processes that aren't ours."""
    guarded = {lport for st, lport, _, _, pid in rows if st == LISTEN and pid in ours}
    out = []
    for st, lport, raddr, rport, pid in rows:
        if st in (SYN_SENT, SYN_RCVD, ESTABLISHED) and raddr == _LOOPBACK and rport in guarded \
                and pid and pid not in ours:
            out.append((pid, rport))
    return out


class PortGuard:
    def __init__(self, our_pids, on_breach, interval: float = 0.1):
        """our_pids(): set of pids that may connect (LocalPilot + its job). on_breach(pid, name, port)."""
        self.our_pids, self.on_breach, self.interval = our_pids, on_breach, interval
        self._stop = threading.Event()
        self.checks = 0
        self.breaches: list[dict] = []
        self._seen: set[tuple[int, int]] = set()
        self._t = threading.Thread(target=self._run, daemon=True, name="LocalPilot-PortGuard")

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._stop.set()

    def check_once(self):
        ours = set(self.our_pids()) | {os.getpid()}
        hits = foreign_clients(tcp_table(), ours)
        self.checks += 1
        for pid, port in hits:
            if (pid, port) in self._seen:
                continue
            self._seen.add((pid, port))
            b = {"at": time.strftime("%H:%M:%S"), "pid": pid, "name": process_name(pid), "port": port}
            self.breaches.append(b)
            try:
                self.on_breach(b)
            except Exception:
                pass
        return hits

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.check_once()
            except Exception:
                pass
