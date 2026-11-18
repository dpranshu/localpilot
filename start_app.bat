@echo off
rem Launch the LocalPilot desktop control panel with no console window.
rem Uses the signed Python interpreter, so it works even where Windows Smart App
rem Control blocks the unsigned LocalPilot.exe.
cd /d "%~dp0"
start "" pythonw localpilot_app.py
