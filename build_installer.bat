@echo off
rem Builds dist\LocalPilot\ (one-folder app) and dist\LocalPilot-Setup-<ver>.exe
cd /d "%~dp0"
for /f "tokens=2 delims==" %%v in ('findstr /b __version__ localpilot\__init__.py') do set VER=%%v
set VER=%VER: =%
set VER=%VER:"=%
for /f %%p in ('python -c "import pystray,os;print(os.path.dirname(pystray.__file__))"') do set PYSTRAY=%%p
python -m PyInstaller --noconfirm --onedir --noconsole --name LocalPilot --icon localpilot.ico ^
  --add-data "localpilot.ico;." --add-data "THIRD_PARTY_NOTICES.md;." --add-data "%PYSTRAY%;pystray" ^
  --exclude-module pystray --hidden-import six --collect-submodules comtypes --collect-submodules uiautomation ^
  --hidden-import PIL._tkinter_finder --exclude-module pyautogui --exclude-module pymsgbox --exclude-module mouseinfo --exclude-module piper --exclude-module localpilot.piper_worker --exclude-module kokoro_onnx --exclude-module phonemizer --exclude-module espeakng_loader --exclude-module localpilot.kokoro_worker ^
  --collect-all faster_whisper --collect-submodules ctranslate2 --collect-data onnxruntime --collect-all sounddevice ^
  localpilot_app.py || exit /b 1
rem --- the natural voice runs as its OWN program (GPL-3.0: Kokoro uses espeak-ng), shipped alongside
python -m PyInstaller --noconfirm --onedir --noconsole --name KokoroVoice --distpath build\voice_dist ^
  --collect-all kokoro_onnx --collect-all espeakng_loader --collect-all phonemizer ^
  localpilot\kokoro_worker.py || exit /b 1
if exist dist\LocalPilot\voice rmdir /s /q dist\LocalPilot\voice
xcopy /e /i /q build\voice_dist\KokoroVoice dist\LocalPilot\voice >nul
copy /y localpilot\kokoro_worker.py dist\LocalPilot\voice\kokoro_worker.py >nul
copy /y installer\VOICE_LICENSE.txt dist\LocalPilot\voice\LICENSE.txt >nul
set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
"%ISCC%" /DAppVersion=%VER% installer\LocalPilot.iss || exit /b 1
echo Done: dist\LocalPilot-Setup-%VER%.exe
