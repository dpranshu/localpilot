@echo off
rem Launch the LocalPilot system-tray icon with no console window.
cd /d "%~dp0"
start "" pythonw -m localpilot.tray
