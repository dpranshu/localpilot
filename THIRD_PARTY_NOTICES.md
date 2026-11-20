# Third-party software in LocalPilot

LocalPilot itself is © its author. It uses the components below, each under its own license.
Versions and licenses were read from the installed package metadata (Windows build).

## Python libraries shipped with the app

| Package | Version | License | Project |
|---|---|---|---|
| uiautomation | 2.0.29 | Apache-2.0 | https://github.com/yinkaisheng/Python-UIAutomation-for-Windows |
| comtypes | 1.5.0 | MIT | https://github.com/enthought/comtypes |
| requests | 2.34.2 | Apache-2.0 | https://requests.readthedocs.io |
| urllib3 | 2.8.0 | MIT | https://github.com/urllib3/urllib3 |
| charset-normalizer | 3.5.2 | MIT | https://github.com/jawah/charset_normalizer |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| certifi | 2026.10.23 | MPL-2.0 | https://github.com/certifi/python-certifi |
| Pillow | 13.0.0 | MIT-CMU (HPND) | https://python-pillow.org |
| pystray | 0.19.5 | **LGPL-3.0** | https://github.com/moses-palmer/pystray |
| six | 1.17.0 | MIT | https://github.com/benjaminp/six |
| faster-whisper | 1.2.1 | MIT | https://github.com/SYSTRAN/faster-whisper (includes the Silero VAD model, MIT) |
| ctranslate2 | 4.9.0 | MIT | https://github.com/OpenNMT/CTranslate2 |
| onnxruntime | 1.32.0 | MIT | https://github.com/microsoft/onnxruntime |
| tokenizers | 0.23.3 | Apache-2.0 | https://github.com/huggingface/tokenizers |
| huggingface-hub | 2.4.0 | Apache-2.0 | https://github.com/huggingface/huggingface_hub |
| av (PyAV) | 19.0.2 | BSD-3-Clause; bundles **FFmpeg (LGPL-2.1)** | https://github.com/PyAV-Org/PyAV |
| sounddevice | 0.5.6 | MIT (bundles PortAudio, MIT) | https://github.com/spatialaudio/python-sounddevice |

**LGPL note (pystray, system-tray icon only):** LGPL-3.0 lets you ship pystray in a commercial
closed-source product, provided you (1) include this notice and the LGPL-3.0 / GPL-3.0 license texts,
(2) state that pystray is used and where to get its source, and (3) let the user replace pystray with
a modified version. A **one-folder** build (pystray kept as a separate, replaceable package) satisfies
(3) more clearly than a one-file `.exe`. Alternatively, the tray can be rewritten on the Windows API to
drop the LGPL component entirely.

**Removed for licensing:** PyAutoGUI was replaced by LocalPilot's own `localpilot/inputs.py` (Windows
SendInput) because it pulls in `pymsgbox` and `mouseinfo`, which declare GPL-3.0+. No GPL code is
loaded by the app (checked at runtime).

**LGPL note (FFmpeg inside PyAV):** shipped as separate DLLs in the one-folder build, so it can
be replaced, which satisfies the LGPL in the same way as pystray.

**Natural voice: Kokoro, shipped as a separate GPL program.** LocalPilot's default voice is
Kokoro (model Apache-2.0, kokoro-onnx MIT). Kokoro pronounces words with espeak-ng and phonemizer,
which are GPL-3.0. So it is built as its **own program**, `voice\KokoroVoice.exe` (GPL-3.0, see
`voice\LICENSE.txt`), which LocalPilot runs and talks to over a pipe. LocalPilot itself contains
no GPL code. If that folder is removed, LocalPilot falls back to the built-in Windows voice (Zira).
*Have a lawyer confirm this "separate program" arrangement before selling.*

**Not shipped: Piper (piper-tts) is GPL-3.0** (it bundles espeak-ng). LocalPilot speaks with
Windows' built-in voices by default. A user who installs Piper themselves can pick the "piper"
voice; LocalPilot then runs it as a *separate process* (`localpilot/piper_worker.py`, the only
GPL-licensed file, excluded from the packaged build). Kokoro is no alternative here, because it also
relies on espeak-ng.

## Research browser (installed at first use, not bundled)

| Component | License | Notes |
|---|---|---|
| agent-browser 0.43.0 (Vercel Labs) | Apache-2.0 | https://github.com/vercel-labs/agent-browser - pinned by SHA-256, run as a separate program |
| websockets 15 | BSD-3-Clause | https://github.com/python-websockets/websockets - client for agent-browser's live stream |
| Chrome for Testing 158 headless shell (Google) | Chromium license (BSD-3-Clause + third-party notices) | downloaded from Google's Chrome for Testing service, MD5-checked |
| EasyList / EasyPrivacy | GPL-3.0+ / CC BY-SA 3.0+ | downloaded at runtime, not bundled |
| Phishing.Database | MIT | downloaded at runtime |
| trafilatura | Apache-2.0 | page main-text extraction |
| lxml | BSD-3-Clause | results-page parsing |

## Runtime components installed separately (not bundled)

| Component | License | Notes |
|---|---|---|
| Python 3.13 | PSF License | the interpreter (bundled only in the `.exe` build) |
| Ollama | MIT | model server; installed on first run by the app (winget or the official installer), not bundled |
| Qwen2.5-7B-Instruct (text model) | Apache-2.0 | verified from `ollama show --license` |
| Whisper base.en (speech recognition) | MIT | downloaded on first voice use into `%APPDATA%\LocalPilot\models` |
| UI-TARS-1.5-7B (vision model, optional) | Apache-2.0 | confirmed on the Hugging Face model cards of both `ByteDance-Seed/UI-TARS-1.5-7B` and the GGUF build used, `Mungert/UI-TARS-1.5-7B-GGUF` (checked 2026-11-19) |

## Build tools (not shipped as code)

| Tool | License | Notes |
|---|---|---|
| PyInstaller | GPL-2.0-or-later **with a bootloader exception** | the exception allows distributing apps built with it under any license |
