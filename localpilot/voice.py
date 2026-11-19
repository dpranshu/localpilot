"""Offline voice: speak replies (Windows SAPI) and listen for one command (System.Speech).

Both are built into Windows -- nothing is downloaded and no audio leaves the PC.
"""
from __future__ import annotations

import subprocess
import threading

_voice = None
_lock = threading.Lock()


def speak(text: str, wait: bool = False) -> bool:
    """Say text aloud. Interrupts whatever is being said. Never raises."""
    global _voice
    if not text:
        return False
    try:
        import comtypes.client
        with _lock:
            if _voice is None:
                import comtypes
                comtypes.CoInitialize()
                _voice = comtypes.client.CreateObject("SAPI.SpVoice")
            # 1 = async, 2 = purge what's being said
            _voice.Speak(text, 0 if wait else (1 | 2))
        return True
    except Exception:
        return False


def stop_speaking():
    try:
        if _voice is not None:
            _voice.Speak("", 1 | 2)
    except Exception:
        pass


_LISTEN_PS = r"""
Add-Type -AssemblyName System.Speech
$r = New-Object System.Speech.Recognition.SpeechRecognitionEngine
$r.SetInputToDefaultAudioDevice()
$r.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$r.InitialSilenceTimeout = [TimeSpan]::FromSeconds(6)
$r.EndSilenceTimeout = [TimeSpan]::FromSeconds(0.8)
$r.BabbleTimeout = [TimeSpan]::FromSeconds(4)
$res = $r.Recognize([TimeSpan]::FromSeconds(12))
if ($res) { [Console]::Out.Write($res.Text) }
"""


def listen(timeout: float = 20.0) -> str:
    """Listen on the default microphone for one spoken command; '' if nothing heard."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", _LISTEN_PS],
                             capture_output=True, text=True, timeout=timeout,
                             creationflags=0x08000000)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def available() -> tuple[bool, bool]:
    """(can_speak, can_listen)."""
    try:
        import comtypes.client  # noqa
        can_speak = True
    except Exception:
        can_speak = False
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Add-Type -AssemblyName System.Speech; "
                            "[System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers().Count"],
                           capture_output=True, text=True, timeout=20, creationflags=0x08000000)
        can_listen = (r.stdout or "0").strip() not in ("", "0")
    except Exception:
        can_listen = False
    return can_speak, can_listen
