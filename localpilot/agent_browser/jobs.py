"""Windows Job Object for the Agent Browser's processes (no extra dependencies).

* JOB_OBJECT_LIMIT_JOB_MEMORY: a hard, OS-enforced commit-memory budget for ALL browser processes
  together. Exceeding it makes allocations fail (the tab crashes; the browser recovers).
* JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: when LocalPilot exits -- even if it crashes -- Windows kills
  every process in the job. No orphaned Chromium processes.
Processes are found by walking LocalPilot's own process tree (Toolhelp snapshot), so nothing else
on the PC is ever touched.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class EXT_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class MEM_USAGE(ctypes.Structure):
    _fields_ = [("JobMemory", ctypes.c_ulonglong), ("PeakJobMemoryUsed", ctypes.c_ulonglong)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


k32.CreateJobObjectW.restype = wt.HANDLE
k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
k32.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]

LIMIT_JOB_MEMORY = 0x200
LIMIT_KILL_ON_JOB_CLOSE = 0x2000
LIMIT_BREAKAWAY_OK = 0x800
LIMIT_SILENT_BREAKAWAY_OK = 0x1000


def _all_processes() -> list[tuple[int, str]]:
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    out = []
    e = PROCESSENTRY32W(); e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    if k32.Process32FirstW(snap, ctypes.byref(e)):
        while True:
            out.append((e.th32ProcessID, e.szExeFile))
            if not k32.Process32NextW(snap, ctypes.byref(e)):
                break
    k32.CloseHandle(snap)
    return out


def descendants(root_pid: int | None = None) -> list[tuple[int, str]]:
    """(pid, exe name) of every process descended from root (default: this process)."""
    root_pid = root_pid or os.getpid()
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    parent = {}
    e = PROCESSENTRY32W(); e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    if k32.Process32FirstW(snap, ctypes.byref(e)):
        while True:
            parent[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
            if not k32.Process32NextW(snap, ctypes.byref(e)):
                break
    k32.CloseHandle(snap)
    out, frontier = [], {root_pid}
    while frontier:
        nxt = {pid for pid, (pp, _) in parent.items() if pp in frontier and pid not in frontier and pid != root_pid}
        nxt -= {p for p, _ in out}
        out += [(p, parent[p][1]) for p in nxt]
        frontier = nxt
    return out


class BrowserJob:
    def __init__(self, memory_mb: int):
        self.memory_mb = memory_mb
        self.handle = k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = EXT_LIMIT()
        info.BasicLimitInformation.LimitFlags = LIMIT_JOB_MEMORY | LIMIT_KILL_ON_JOB_CLOSE
        info.JobMemoryLimit = memory_mb * 2**20
        if not k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        self.members: set[int] = set()

    def adopt(self, names=("chrome", "headless", "msedge", "node")) -> int:
        """Put browser-related descendants of this process into the job. Returns how many were added."""
        added = 0
        for pid, exe in descendants():
            if pid in self.members or not any(n in exe.lower() for n in names):
                continue
            h = k32.OpenProcess(0x0100 | 0x0001 | 0x1000, False, pid)   # SET_QUOTA | TERMINATE | QUERY_LIMITED
            if h:
                if k32.AssignProcessToJobObject(self.handle, h):
                    self.members.add(pid); added += 1
                k32.CloseHandle(h)
        return added

    def adopt_handle(self, handle: int) -> bool:
        ok = bool(k32.AssignProcessToJobObject(self.handle, wt.HANDLE(handle)))
        if ok:
            self.members.add(-handle)
        return ok

    def adopt_pids(self, pids) -> int:
        """Put specific processes (e.g. a detached daemon and its browser) into the job."""
        added = 0
        for pid in pids:
            if pid in self.members:
                continue
            h = k32.OpenProcess(0x0100 | 0x0001 | 0x1000, False, pid)
            if h:
                if k32.AssignProcessToJobObject(self.handle, h):
                    self.members.add(pid); added += 1
                k32.CloseHandle(h)
        return added

    def memory_mb_used(self) -> tuple[float, float]:
        """(current, peak) committed memory of the whole job, MB."""
        m = MEM_USAGE()
        if k32.QueryInformationJobObject(self.handle, 28, ctypes.byref(m), ctypes.sizeof(m), None):
            return m.JobMemory / 2**20, m.PeakJobMemoryUsed / 2**20
        info = EXT_LIMIT()
        k32.QueryInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info), None)
        return 0.0, info.PeakJobMemoryUsed / 2**20

    def pids(self) -> set[int]:
        """Process ids currently in the job (the OS's own list: daemon, Chrome, CLI calls)."""
        if not self.handle:
            return set()
        n = 1024

        class LIST(ctypes.Structure):
            _fields_ = [("assigned", wt.DWORD), ("in_list", wt.DWORD), ("ids", ctypes.c_size_t * n)]
        lst = LIST()
        if not k32.QueryInformationJobObject(self.handle, 3, ctypes.byref(lst), ctypes.sizeof(lst), None):
            return set()
        return {int(lst.ids[i]) for i in range(min(lst.in_list, n))}

    def close(self):
        """Closing the handle kills every process still in the job (KILL_ON_JOB_CLOSE)."""
        if self.handle:
            k32.CloseHandle(self.handle)
            self.handle = None
