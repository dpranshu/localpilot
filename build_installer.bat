@echo off
rem Builds dist\LocalPilot\ (one-folder app) and dist\LocalPilot-Setup-<ver>.exe
cd /d "%~dp0"
for /f %%v in ('python -c "import re;print(re.search(r'__version__ = \"(.+?)\"',open('localpilot/__init__.py').read()).group(1))"') do set VER=%%v
for /f %%p in ('python -c "import pystray,os;print(os.path.dirname(pystray.__file__))"') do set PYSTRAY=%%p
python -m PyInstaller --noconfirm --onedir --noconsole --name LocalPilot --icon localpilot.ico ^
  --add-data "localpilot.ico;." --add-data "THIRD_PARTY_NOTICES.md;." --add-data "%PYSTRAY%;pystray" ^
  --exclude-module pystray --hidden-import six --collect-submodules comtypes --collect-submodules uiautomation ^
  --hidden-import PIL._tkinter_finder --exclude-module pyautogui --exclude-module pymsgbox --exclude-module mouseinfo ^
  localpilot_app.py || exit /b 1
set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
"%ISCC%" /DAppVersion=%VER% installer\LocalPilot.iss || exit /b 1
echo Done: dist\LocalPilot-Setup-%VER%.exe
