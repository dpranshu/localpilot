"""Install (or remove) a Windows Startup shortcut so the LocalPilot tray
auto-starts at login.

    python install_startup.py            # install
    python install_startup.py --uninstall

It creates "LocalPilot.lnk" in your per-user Startup folder, pointing to
pythonw.exe -m localpilot.tray (no console window), with the project as the
working directory and a generated icon. Per-user only -- no admin needed, and
it only ever touches that one shortcut file.
"""
from __future__ import annotations

import os
import sys

PROJECT = os.path.dirname(os.path.abspath(__file__))
SHORTCUT_NAME = "LocalPilot.lnk"


def _pythonw() -> str:
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


def _startup_dir() -> str:
    return os.path.join(os.environ["APPDATA"],
                        r"Microsoft\Windows\Start Menu\Programs\Startup")


def _shortcut_path() -> str:
    return os.path.join(_startup_dir(), SHORTCUT_NAME)


def _make_ico() -> str:
    ico = os.path.join(PROJECT, "localpilot.ico")
    try:
        from localpilot.tray import _make_image
        _make_image().save(ico, format="ICO",
                           sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    except Exception:
        return ""
    return ico


def _psq(s: str) -> str:
    """PowerShell single-quoted literal (backslashes are safe; escape quotes)."""
    return "'" + s.replace("'", "''") + "'"


def install() -> str:
    import subprocess
    import tempfile
    lnk = _shortcut_path()
    os.makedirs(_startup_dir(), exist_ok=True)
    ico = _make_ico()
    icon_line = f"$s.IconLocation = {_psq(ico)}\n" if ico else ""
    ps = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_psq(lnk)})\n"
        f"$s.TargetPath = {_psq(_pythonw())}\n"
        f"$s.Arguments = '-m localpilot.tray'\n"
        f"$s.WorkingDirectory = {_psq(PROJECT)}\n"
        f"{icon_line}"
        f"$s.Description = 'LocalPilot system-tray agent launcher'\n"
        f"$s.WindowStyle = 7\n"
        f"$s.Save()\n"
    )
    tf = tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8")
    tf.write(ps)
    tf.close()
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tf.name],
            check=True, capture_output=True, text=True)
    finally:
        try:
            os.remove(tf.name)
        except OSError:
            pass
    return lnk


def uninstall() -> bool:
    lnk = _shortcut_path()
    if os.path.exists(lnk):
        os.remove(lnk)
        return True
    return False


def main():
    if "--uninstall" in sys.argv:
        if uninstall():
            print("Removed Startup shortcut. LocalPilot will no longer auto-start.")
        else:
            print("No Startup shortcut found.")
        return
    lnk = install()
    print("Installed Startup shortcut:")
    print(" ", lnk)
    print(f"  -> {_pythonw()} -m localpilot.tray  (cwd: {PROJECT})")
    print("LocalPilot tray will now start automatically at login.")
    print("To undo:  python install_startup.py --uninstall")


if __name__ == "__main__":
    main()
