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

**LGPL note (pystray, system-tray icon only):** LGPL-3.0 lets you ship pystray in a commercial
closed-source product, provided you (1) include this notice and the LGPL-3.0 / GPL-3.0 license texts,
(2) state that pystray is used and where to get its source, and (3) let the user replace pystray with
a modified version. A **one-folder** build (pystray kept as a separate, replaceable package) satisfies
(3) more clearly than a one-file `.exe`. Alternatively, the tray can be rewritten on the Windows API to
drop the LGPL component entirely.

**Removed for licensing:** PyAutoGUI was replaced by LocalPilot's own `localpilot/inputs.py` (Windows
SendInput) because it pulls in `pymsgbox` and `mouseinfo`, which declare GPL-3.0+. No GPL code is
loaded by the app (checked at runtime).

## Runtime components installed separately (not bundled)

| Component | License | Notes |
|---|---|---|
| Python 3.13 | PSF License | the interpreter (bundled only in the `.exe` build) |
| Ollama | MIT | model server; installed on first run by the app (winget or the official installer), not bundled |
| Qwen2.5-7B-Instruct (text model) | Apache-2.0 | verified from `ollama show --license` |
| UI-TARS-1.5-7B (vision model, optional) | Apache-2.0 | confirmed on the Hugging Face model cards of both `ByteDance-Seed/UI-TARS-1.5-7B` and the GGUF build used, `Mungert/UI-TARS-1.5-7B-GGUF` (checked 2026-11-19) |

## Build tools (not shipped as code)

| Tool | License | Notes |
|---|---|---|
| PyInstaller | GPL-2.0-or-later **with a bootloader exception** | the exception allows distributing apps built with it under any license |
