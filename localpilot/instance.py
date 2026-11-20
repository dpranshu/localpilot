"""One LocalPilot per Windows user session.

Starting LocalPilot again while it already runs (e.g. in the background at login) does not start a
second assistant, voice listener and orb: the new process asks the running one to show its window and
exits. Uses a named mutex + a named auto-reset event (per user session: the "Local\\" namespace).
"""
from __future__ import annotations

import ctypes
import threading

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.restype = ctypes.c_void_p
_k32.CreateEventW.restype = ctypes.c_void_p
_k32.OpenEventW.restype = ctypes.c_void_p
_k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
_k32.SetEvent.argtypes = [ctypes.c_void_p]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]

ERROR_ALREADY_EXISTS = 183
MUTEX = "Local\\LocalPilot.Instance"
SHOW = "Local\\LocalPilot.Show"
_mutex = None
_event = None


def claim(background: bool = False, mutex: str = MUTEX, show: str = SHOW) -> bool:
    """True if this process is THE LocalPilot. Otherwise asks the running one to show itself
    (unless this start was a background/login start) and returns False."""
    global _mutex, _event
    h = _k32.CreateMutexW(None, False, mutex)
    if h and ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _k32.CloseHandle(h)
        if not background:
            ev = _k32.OpenEventW(0x0002, False, show)            # EVENT_MODIFY_STATE
            if ev:
                _k32.SetEvent(ev)
                _k32.CloseHandle(ev)
        return False
    _mutex = h
    _event = _k32.CreateEventW(None, False, False, show)        # auto-reset
    return True


def on_show(callback):
    """Call `callback` whenever another start of LocalPilot asks this one to show itself."""
    ev = _event

    def wait():
        while ev:
            if _k32.WaitForSingleObject(ev, 0xFFFFFFFF) == 0:
                try:
                    callback()
                except Exception:
                    pass
    if ev:
        threading.Thread(target=wait, daemon=True, name="LocalPilot-Instance").start()


def release():
    global _mutex, _event
    for h in (_event, _mutex):
        if h:
            _k32.CloseHandle(h)
    _mutex = _event = None
