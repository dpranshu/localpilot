# LocalPilot

> **Quickstart — one command sets up everything**
> ```bash
> python install.py            # Python deps + Ollama + text model (idempotent)
> # python install.py --vision --startup   # also pull UI-TARS + add auto-start
> ```
> Then launch:
> ```bash
> python localpilot_app.py     # desktop control panel  (or LocalPilot.exe)
> ```
> Try: `type Hello into notepad` — watch the animated AI character + its cursor work.

## Install

`install.py` does the whole setup and skips whatever's already done:

1. `pip install -r requirements.txt`
2. installs **Ollama** if missing (winget, else a silent download from ollama.com)
3. starts the Ollama server (`localhost:11434`)
4. pulls the text model (add `--vision` for UI-TARS-1.5 too)
5. verifies everything is reachable

```bash
python install.py                      # text brain only
python install.py --vision             # + UI-TARS vision model (large)
python install.py --vision --startup   # + auto-start the tray at login
```
One-click: double-click **`install.bat`** (pass `--vision` as an argument if you want it).

A **local, open-model Windows desktop agent**. It looks at whatever window you're in
via **UI Automation (UIA)**, hands a **numbered list of on-screen controls** to a
**local LLM** (served by **Ollama**), and executes the one action the model picks —
clicking, typing, hotkeys, scrolling — using UIA patterns. No cloud, no vision model,
no pixel-coordinate guessing. Runs on a modest GPU (tuned for an RTX 3050).

This is the same "screen-reading action agent" idea as commercial phone agents, but:
- **Open weights** you choose (Qwen2.5-7B / 3B, Llama-3.1-8B, …)
- **Runs entirely on your PC** — the model never leaves `localhost`
- **Accessibility-tree grounding** (set-of-marks), which is fast and reliable on weak GPUs

---

## How it works

```
 PERCEIVE   walk UIA tree of the focused window → numbered actionable controls
            [1] Button "Save"   [2] Edit "Address" value="google.com"  ...
    │
 THINK      POST {goal + controls} → Ollama (localhost:11434) → ONE JSON action
            {"action":"type","id":2,"text":"hello"}
    │
 ACT        execute via UIA pattern (Invoke / SetValue / Toggle) or fallback click
    │
 LOOP       re-perceive, up to max_steps, stop on {"action":"done"}
```

## Install

1. **Python deps** (already installed if you ran setup):
   ```bash
   pip install -r requirements.txt
   ```
2. **Ollama** — install from https://ollama.com/download (or `winget install Ollama.Ollama`),
   then pull a model:
   ```bash
   ollama pull qwen2.5:7b-instruct-q4_K_M     # ~4.7 GB, fits 6-8 GB VRAM
   # or, if that's slow on your GPU:
   ollama pull qwen2.5:3b-instruct            # ~2 GB
   ```
   Ollama runs a server at `http://localhost:11434` automatically.

## Use

```bash
# 1) Verify the model server is reachable:
python run.py --check

# 2) See exactly what the agent perceives (no model, no actions) -- great for debugging:
python run.py --probe
#    (switch to the window you want within 3 seconds)

# 3) Dry run -- the model plans and logs actions but DOES NOT execute them:
python run.py --dry-run "in notepad, type Hello World and save the file"

# 4) For real:
python run.py "in notepad, type Hello World"

# 5) Interactive -- fire task after task; the model stays warm between tasks:
python run.py -i
python run.py -i --backend hybrid        # interactive, with auto-switch

# Options:
python run.py --model qwen2.5:3b-instruct --steps 20 --confirm "your goal"
```

- `--confirm` pauses for your Enter before every action (recommended while testing).
- **Emergency stop:** hold **F12** between steps, or (failsafe) slam the mouse into a screen corner.

### Flags

| Flag | Effect |
|---|---|
| `--dry-run` | Model plans + logs actions but does **not** execute them |
| `--confirm` | Pause for Enter before each action |
| `--no-overlay` | Turn off the live on-screen viewer |
| `--screenshots` | Save a PNG of the screen each step (next to the log) |
| `--backend uia\|vision` | `uia` = text tree (default, tested); `vision` = UI-TARS (experimental) |
| `--model <tag>` | Override the Ollama model |
| `--steps <n>` | Override the step cap |
| `--check` / `--probe` | Verify model / print what the agent sees, then exit |

## Single .exe (no Python needed to run)

Build a one-file Windows executable of the desktop app:

```bash
pip install pyinstaller
python -m PyInstaller --onefile --noconsole --name LocalPilot --icon localpilot.ico \
  --collect-submodules pystray --collect-submodules comtypes --collect-submodules uiautomation \
  --hidden-import PIL._tkinter_finder --hidden-import pystray._win32 localpilot_app.py
```
The result is **`dist/LocalPilot.exe`** -- double-click to open the control panel; no Python
install required on the target machine.

> **Windows Smart App Control / App Control policies may block it.** The exe is *unsigned*, and
> with Smart App Control in *Enforce* mode Windows refuses to run unsigned executables
> ("An Application Control policy has blocked this file"). Options: sign the exe with a trusted
> code-signing certificate, or just use the Python launcher, which is unaffected because
> `python.exe` is signed -- double-click **`start_app.bat`** (control panel) or
> **`start_tray.bat`** (tray). Don't disable Smart App Control just for this. **Ollama is still required separately** (it's the model
server, multi-GB; the exe bundles only the agent client). Start Ollama and pull a model first.

## Desktop control panel

A window to run and control everything -- no terminal:

```bash
python localpilot_app.py          # or: python -m localpilot.gui
```

- A **live orb** in the header that animates with the agent's state (idle / thinking / acting / done).
- **Task** field (with a placeholder example) + **Run** / **Stop** (Stop aborts after the current
  step; **Esc** also stops).
- **Backend** (uia/vision/hybrid), **Model**, **Max steps**.
- Toggles: **Confirm each step · Agent cursor · Overlay · Dry run · Real cursor**.
- A **live log** streams every step/action; a **color-coded status** line (green done / red error);
  **Check Ollama** / **Clear log** / **Quit**.

When you hit Run, the panel **minimizes itself** so it isn't the window the agent acts on --
focus your target during the 3s countdown. (The tray's "Open control panel" opens this too.)

## System-tray launcher

Run tasks from a tray icon by the clock -- no terminal needed:

```bash
python -m localpilot.tray          # or double-click start_tray.bat (no console window)
```

Right-click (or left-click) the icon:
- **Run task...** -- type a task; it runs with a 3s countdown so you can focus your target window.
- **Backend** -- switch uia / vision / hybrid (radio).
- **Confirm each step / Show agent cursor / Show overlay** -- toggles.
- **Probe (what I see)** -- balloon with the focused window's control count.
- **Check model** -- verify Ollama.
- **Quit**.

Tasks run on a background thread (the tray stays responsive; one at a time), and balloon
notifications report start/finish. Needs `pystray` (in `requirements.txt`).

### Auto-start with Windows

```bash
python install_startup.py            # tray launches at every login
python install_startup.py --uninstall
```
Creates `LocalPilot.lnk` in your per-user Startup folder
(`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`) pointing to
`pythonw -m localpilot.tray` with the project as working directory and a generated icon.
Per-user only -- no admin needed, and it touches only that one shortcut. Remove it with
`--uninstall` (or just delete the `.lnk`).

## Live viewer -- the dot-sphere face

While a task runs, the agent's face is a **halftone sphere of white dots on a fully transparent
background** (`localpilot/dotsphere.py`). Dots sit on latitude rows that follow the curvature of the
ball, and each dot's size comes from the lighting -- big, almost-merged dots toward the upper right,
shrinking to nothing toward the lower left and the rim -- which is what gives it the printed-halftone
look. It **spins continuously while the agent works**, and the state changes the motion:

| State | Motion |
|---|---|
| idle | slow spin |
| thinking | faster spin + a gentle breathing ripple through the dots |
| acting | fast spin + a strong ripple |
| done | calm |
| stopped | frozen and dimmed |

Because white dots would vanish on a white page, each dot gets a faint soft shadow behind it.
Around the face the HUD also draws a **status line**, **outlined speech text** (no box behind it), a
**glowing box** around the control it is about to touch, a dashed **pointer line**, and the agent's own
**cursor** (see below).

### Drag it, right-click it

- **Drag** the ball anywhere; its position is remembered between runs.
- **Right-click** it for a menu:
  - **Stop task** -- stops the running task after the current step (greyed out when idle)
  - **Open LocalPilot app** -- brings the control panel to the front (launches it if it isn't running)
  - **Reset position**

Interacting with it **never steals focus**, so the window the agent is working in stays active.
`F12` still emergency-stops.

### How the transparency and mouse handling work

`localpilot/hud.py` uses two Win32 *layered windows with per-pixel alpha* (`UpdateLayeredWindow`,
premultiplied BGRA), rendered every frame with PIL as RGBA -- so there is no black backdrop, no box
and no halo over any content:

1. **Main HUD** (full-screen) is **click-through** (`WS_EX_TRANSPARENT`): all clicks -- including the
   agent's own -- pass straight through the status text, highlight box and cursor.
2. **Orb window** (small) is *not* click-through, but layered windows are hit-tested by alpha, so only
   the ball itself (which carries an invisible alpha=1 disc, so the gaps between dots count) receives
   the mouse; its transparent corners pass clicks through. It is `WS_EX_NOACTIVATE` and answers
   `WM_MOUSEACTIVATE` with `MA_NOACTIVATE`.

Both windows carry a sentinel title so perception ignores them. If the layered windows can't start, it
falls back to the older tkinter colour-key overlay automatically. Size: `orb_size` in `config.py`
(default 150 px). Disable the whole overlay with `--no-overlay`.

> Tip: if the agent needs to click something directly *under* the ball, drag the ball away first --
> the ball catches clicks that land on it.

### The agent's own cursor

The agent has its **own visible cursor** — a glowing arrow pointer that **glides to each target**
before the action, shows an **expanding click ripple** when it clicks, and **chevrons** when it
scrolls. You watch its pointer work the screen, distinct from your real mouse. It's drawn on the
click-through overlay (it never fights your mouse). Controls:

- `--no-cursor` — hide it.
- `--real-cursor` — for UIA actions, also **physically move + click the OS cursor** at the target
  (most visibly "the agent is doing it"); default off, since UIA `Invoke()` is more reliable.
  The vision backend already drives the real cursor (pyautogui) by nature.
- `cursor_move_time` (0.35s) — pause so you can see the cursor travel before it acts.

Add `--screenshots` to also save `logs/run_<ts>_shots/step_NNN.png` for later review.

## Hybrid backend -- auto-switch (recommended)

```bash
python run.py --backend hybrid --confirm "your task"
```

Hybrid uses the **fast UIA text brain by default** and **auto-switches to UI-TARS vision per
step** when the accessibility tree can't do the job. Two triggers:

1. **Structural** — the focused window exposes fewer than `hybrid_min_content` real (non-chrome)
   controls → vision this step. (Rare: most apps, *including Chromium/Electron like Brave,
   Chrome, ChatGPT, Claude*, expose rich trees and stay on the fast path. The bare desktop or a
   pure-canvas window is the classic thin case.)
2. **Escalation** — the text model keeps stalling (picks `wait`/`scroll`, or references a control
   that isn't there) for `hybrid_escalate_after` steps → switch to vision, and stay on vision for
   `hybrid_vision_sticky` steps (anti-thrash) before retrying text. This catches a **canvas or
   custom-rendered area inside an otherwise-rich window**, which a structural count alone misses.

When vision runs, the screenshot is **cropped to the focused window** (`vision_crop_to_window`)
so the model isn't distracted by other windows and runs faster. Each step's `mode` and `reason`
are logged. Knobs: `hybrid_min_content` (3), `hybrid_escalate_after` (2), `hybrid_vision_sticky` (2).

**Tested:** rich tree (Notepad, 17 controls) → stays on UIA and finishes in 2 steps; forced-thin
→ auto-switches to vision and acts via coordinates; crop-to-window verified to keep vision on the
target window. The dispatch logic and coordinate mapping (with crop origin) are unit-tested.

## Vision backend -- UI-TARS-1.5

The default `uia` backend reads the accessibility tree (fast, reliable, fits your GPU). The
`vision` backend instead sends a **screenshot** to **UI-TARS-1.5-7B** and executes the
coordinate actions it returns. Use it for dense apps (Chrome/Electron/canvas/games) where the
UIA tree is thin.

**It is wired to the real UI-TARS-1.5 spec** (`bytedance/UI-TARS`, Apache-2.0):
- the official `COMPUTER_USE` system prompt + action space,
- the `<point>x y</point>` coordinate format (plus `(x,y)` / `[x1,y1,...]` fallbacks),
- the full action set: `click`, `left_double`, `right_single`, `drag`, `type` (with `\n`=submit),
  `scroll`, `hotkey`, `wait`, `finished`,
- screenshot **resize + coordinate scale-back** to real screen pixels.

The parser and coordinate mapping are **unit-tested** (all 10 action types, both coordinate
conventions). 

**Validated on an RTX 3050 (8 GB):** `hf.co/Mungert/UI-TARS-1.5-7B-GGUF:Q4_K_M` loads with its
vision projector in Ollama, and a real run **clicked the Notepad text area and typed "HELLO"**
(read back from the document to confirm). Coordinate convention measured as **image-space**
(the default): on a Close-button target the mapped click landed **34 px** from the real button.
**Tuned:** with `keep_alive` keeping the model warm in VRAM, steps run at **~17 s each** (not the
~53 s of a cold model). A terse-thought instruction makes UI-TARS emit `finished()` when the task
is done (verified: it stopped itself after typing, instead of running to the cap), and a
no-progress detector stops it if it repeats the same action. Still use the `uia` backend for
latency-sensitive work and `vision` for dense/canvas apps.

Vision tuning knobs (in `config.py`):

| Knob | Default | Effect |
|---|---|---|
| `vision_max_side` | 1092 | screenshot long edge; lower = faster, less precise |
| `vision_num_predict` | 160 | cap tokens/step (guards against long ramblings) |
| `vision_terse` | True | one-sentence Thought + stop-when-done instruction |
| `vision_keep_alive` | 15m | keep model warm in VRAM between steps/runs |
| `vision_max_steps` | 14 | step cap for the vision backend |
| `vision_stop_on_repeat` | 2 | stop after N identical no-progress actions |

### Setup

```bash
# Pull a UI-TARS-1.5 GGUF (the default tag in config.py):
ollama pull hf.co/Mungert/UI-TARS-1.5-7B-GGUF:Q4_K_M
```
If Ollama's HF pull does **not** load the vision projector (mmproj), run UI-TARS with llama.cpp's
`llama-server --mmproj <projector>.gguf` (which exposes an OpenAI-compatible endpoint) and point
`ollama_url` at it — the client speaks the same `/api/chat` / vision-image protocol.

### Use

```bash
# See the model's raw reply + the mapped screen action, WITHOUT acting (best first step):
python run.py --backend vision --vision-probe "click the address bar"

# Dry run (plans + maps, no execution):
python run.py --backend vision --dry-run "open a new tab"

# For real, with the live character and confirm:
python run.py --backend vision --confirm "search for cats on the page"
```

- `--coord-space image` (default) treats `<point>` as pixels in the image we sent and scales to
  screen; `--coord-space norm1000` treats them as 0-1000 normalized. If clicks land off-target,
  switch this. Use `--vision-probe` to see raw vs mapped coords and pick the right one.
- `--model <tag>` overrides the vision model; set the default in `config.py` (`vision_model`).

## Memory: your history and personal context

LocalPilot remembers. Every task you run is recorded on this PC (`%APPDATA%\LocalPilot\memory.json`):
the goal, whether it succeeded, how long it took and the steps it took. When you start a new task,
the agent is given a short **personal context**:

- **your own notes** -- set them with **My context…** in the panel (e.g. *"My browser is Brave. I like
  Punjabi music."*);
- the **most similar past tasks** -- what worked, step by step (a recipe to reuse), and what didn't
  finish (so it doesn't repeat the same mistake). Long flailing runs are compacted (`type x (x4)`).

Open **History…** in the panel to see every task (newest first, OK / -- stopped / X failed), click one
to see its steps, **Run again** (or double-click) to repeat it, **Delete** one, or **Clear all**. Turn
memory off with the **Use my history** checkbox. Passwords, card/PIN fields and long digit runs are
never stored. Nothing leaves the PC.

### How it knows a task actually succeeded

Memory is only useful if "success" is true, so a run only counts as successful when:

1. **Goal check** -- whenever one of its actions changes the window, a second, strict model call asks
   *"is the whole goal accomplished on this screen?"* (search results do not count as "playing a
   video"). It ends the task as soon as the answer is yes, instead of flailing on.
2. **On-screen evidence** -- a "done" (from the goal check *or* the model itself) is only accepted if
   the goal's specific words (names, apps, sites) are actually visible -- typo-tolerant, so
   *"wikipidia … networth"* still matches *"Wikipedia … Net worth"*. A "done" without evidence is
   sent back once, then the task is reported as unfinished rather than falsely successful.
3. **Focus pin** -- if another program grabs the foreground without the agent opening or clicking
   into it, the agent never acts there; it switches back to its own window (and stops if that keeps
   happening).

> Measured on the same two Brave/YouTube tasks: before these fixes the first task flailed for 10
> steps and failed (and once typed into the wrong app); after, it finished in **3 steps**, and the
> similar follow-up task -- now with that recipe in its context -- finished in **2 steps**.

### Everyday-task battery (real screen, verified by checking the actual app state)

| Task | Round 1 | Round 4 (after fixes) |
|---|---|---|
| Open Calculator and calculate 25 times 4 | ✗ typed into a button | ✓ display shows 100 |
| Open Notepad and type a note | ✓ but typed into an old tab | ✓ new blank tab, 2 steps |
| Open Brave and go to wikipedia.org | ✓ 3 steps | ✓ 2 steps |
| Open Settings and go to the Bluetooth page | ✗ dragged back by focus pin | ✓ |
| Open File Explorer and go to Downloads | ✗ | ✓ 2 steps |

What changed to get there: "Open X…" goals open X first (known apps/folders only); the agent only
follows focus to a window its own action created, never to an already-open app; documents with
text are appended to, not wiped; apps without a text box (Calculator) receive keystrokes; result
displays are visible to the model; a URL is always submitted; settings pages open directly
(`ms-settings:bluetooth`); simple math goals are checked exactly in code.

## Safety

### The harness: it asks before anything consequential

Every action passes through a permission gate (`localpilot/harness.py`) before it runs. If the
action would **pay / buy / check out, send, delete / remove / uninstall, publish / post, sign out**,
or **type into a password / card / PIN field** (or type a card-like number), LocalPilot pauses and
shows a dialog:

| Button | Effect |
|---|---|
| **Yes** | allow this one action |
| **No** | skip it -- the agent is told it was refused and must find another way or finish |
| **Cancel** | stop the task |

While the dialog is up, the overlay still highlights the exact button it wants to press. If no
dialog can be shown, the task stops (fail-safe). Dry runs never prompt. Every decision is logged
(`"event": "harness"`). Turn it off with the panel's **Ask before risky actions** checkbox or
`--no-harness` (not recommended).

It matches the **control's name** (text brain) or the **model's own description of its target**
(vision brain), whole words only, and deliberately skips words that also name everyday buttons
(Notepad's *Format* menu, *Reset*, *Share*), so it doesn't nag. Add your own rules in `config.py`:
`harness_extra_sensitive=("archive",)` asks first, `harness_block=("amazon",)` is never allowed.

The prompt also tells the model that on-screen text is **untrusted data**, not instructions -- the
harness is the backstop if a web page tries to talk it into something.

> Tested end-to-end on a local test shop: the agent chose **Place order**, the dialog appeared;
> **Cancel** left the page untouched, **Yes** placed the order -- and when the model then tried to
> click **Place order a second time**, the harness asked again, and **No** prevented a duplicate order.

### Other guards

- Hard `max_steps` cap (default 40) prevents runaway loops.
- `--dry-run` and `--confirm` let you watch its reasoning before trusting it.
- It only acts on the **focused window's** controls each step.
- Everything is logged to `logs/run_<timestamp>.jsonl` (every perception, decision, result).

## Files

| File | Role |
|---|---|
| `localpilot/perception.py` | UIA tree → numbered `Element` list (set-of-marks) |
| `localpilot/llm.py` | Ollama client, forces single-JSON replies |
| `localpilot/prompts.py` | System prompt + action schema |
| `localpilot/actions.py` | Executes actions via UIA patterns / pyautogui |
| `localpilot/agent.py` | Perceive→think→act loop, kill-switch, logging |
| `localpilot/config.py` | All tunables (model, caps, timings, safety) |
| `run.py` | CLI entry point |

## Tuning for your hardware (RTX 3050, 24 GB RAM)

- Start with `qwen2.5:7b-instruct-q4_K_M`. If per-step latency is painful, switch to
  `qwen2.5:3b-instruct` (`--model`), which is noticeably snappier with a small quality drop.
- Lower `max_elements` in `config.py` if prompts get long on dense apps.

## Known limits

- **Electron/Chrome** windows sometimes expose a thin UIA tree. If `--probe` shows few
  controls there, enable renderer accessibility (Chrome: launch with
  `--force-renderer-accessibility`), or this is where adding a small **vision** model later
  would help. Native Win32/WinUI apps (Notepad, Explorer, Settings, Office) work well.
