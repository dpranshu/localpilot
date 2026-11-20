"""LocalPilot desktop app entry point.

Run:  python localpilot_app.py          (control panel)
      python localpilot_app.py --background   (runs hidden with the orb; used at Windows login)
      python localpilot_app.py --tray   (same as --background; the old tray launcher is retired)
Same flags work on the packaged LocalPilot.exe.
"""
import sys

if __name__ == "__main__":
    from localpilot.gui import main
    main(background=bool({"--tray", "--background"} & set(sys.argv[1:])))
