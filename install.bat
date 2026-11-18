@echo off
rem One-click LocalPilot setup: Python deps + Ollama + model.
rem   install.bat            text model only
rem   install.bat --vision   also pull the UI-TARS vision model (large)
cd /d "%~dp0"
python install.py %*
echo.
pause
