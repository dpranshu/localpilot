"""LocalPilot desktop app entry point.

Run:  python localpilot_app.py          (control panel)
      python localpilot_app.py --tray   (system-tray mode; used at Windows login)
Same flags work on the packaged LocalPilot.exe.
"""
import sys

if __name__ == "__main__":
    if "--tray" in sys.argv[1:]:
        from localpilot.tray import main
    else:
        from localpilot.gui import main
    main()
