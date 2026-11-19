"""Vision backend for UI-TARS-1.5 (GUI grounding by screenshot).

Instead of the UIA text tree, this sends a SCREENSHOT to a UI-TARS-1.5 vision
model (served by Ollama, or any OpenAI/Ollama-compatible vision endpoint) and
executes the coordinate actions it returns.

Faithful to the official UI-TARS-1.5 "computer use" prompt and action space
(bytedance/UI-TARS codes/ui_tars/prompt.py, Apache-2.0), which uses the
`<point>x y</point>` coordinate format:

    Thought: ...
    Action: click(point='<point>512 377</point>')

Coordinate handling: we resize the screenshot to `vision_max_side` on its long
edge before sending, remember both the screen size and the sent-image size, then
map the model's point back to real screen pixels. The convention (absolute image
pixels vs 0-1000 normalized) is set by `cfg.vision_coord_space` because model
builds differ; the test harness prints raw + mapped coords so you can confirm it.

Status of validation is documented in the README. The UIA text backend remains
the tested default; this path depends on a vision-capable local model (heavy for
an 8 GB GPU) and on the server actually loading the vision projector (mmproj).
"""
from __future__ import annotations

import base64
import io
import re
import time

import requests

# ---- Official UI-TARS-1.5 computer-use prompt (reconstructed verbatim) ----
COMPUTER_USE_PROMPT = """You are a GUI agent. You are given a task and your action \
history, with screenshots. You need to perform the next action to complete the task.

## Output Format
```
Thought: ...
Action: ...
```

## Action Space
click(point='<point>x1 y1</point>')
left_double(point='<point>x1 y1</point>')
right_single(point='<point>x1 y1</point>')
drag(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')
hotkey(key='ctrl c') # Split keys with a space and use lowercase. Also, do not use more than 3 keys in one hotkey action.
type(content='xxx') # Use escape characters \\', \\", and \\n in content part to ensure we can parse the content in normal python string format. If you want to submit your input, use \\n at the end of content.
scroll(point='<point>x1 y1</point>', direction='down or up or right or left') # Show more information on the `direction` side.
wait() # Sleep for 5s and take a screenshot to check for any changes.
finished(content='xxx') # Use escape characters \\', \\", and \\n in content part to ensure we can parse the content in normal python string format.

## Note
- Use {language} in `Thought` part.
- Write a small plan and finally summarize your next action (with its target element) in one sentence in `Thought` part.

## User Instruction
{instruction}"""

_ACTION_RE = re.compile(r"Action:\s*(.+?)(?:\n\s*(?:Thought:|Observation:)|\Z)", re.S | re.I)
_CALL_RE = re.compile(r"(\w+)\s*\((.*)\)", re.S)
_POINT_RE = re.compile(r"<point>\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*</point>")
_POINT_FALLBACKS = [
    re.compile(r"\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)"),   # (x, y)
    re.compile(r"\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)"),         # [x, y, ...]
]
_CONTENT_RE = re.compile(r"content\s*=\s*'((?:[^'\\]|\\.)*)'", re.S)
_DIR_RE = re.compile(r"direction\s*=\s*'(\w+)'")
_KEY_RE = re.compile(r"key\s*=\s*'([^']*)'")


# ---------------- screenshot + encoding ----------------
def capture_screen():
    from PIL import ImageGrab
    return ImageGrab.grab()  # full virtual screen, PIL RGB


def resize_for_model(img, max_side: int):
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        sent = img.resize((max(1, round(w * scale)), max(1, round(h * scale))))
    else:
        sent = img
    return sent, (w, h), sent.size


def _encode(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------- parsing ----------------
def _points(arg_str: str):
    """Return list of (x, y) floats found in an argument string."""
    pts = [(float(a), float(b)) for a, b in _POINT_RE.findall(arg_str)]
    if pts:
        return pts
    for rx in _POINT_FALLBACKS:
        m = rx.findall(arg_str)
        if m:
            return [(float(a), float(b)) for a, b in m]
    return []


def parse_action(text: str) -> dict:
    """Parse UI-TARS output into a raw action dict (coords still in model space)."""
    thought = ""
    mt = re.search(r"Thought:\s*(.+?)(?:\n\s*Action:|\Z)", text, re.S | re.I)
    if mt:
        thought = mt.group(1).strip()
    ma = _ACTION_RE.search(text)
    body = ma.group(1).strip() if ma else text.strip()
    mc = _CALL_RE.search(body)
    if not mc:
        return {"action": "wait", "say": f"unparsed: {body[:60]}", "thought": thought}
    name, args = mc.group(1).lower(), mc.group(2)
    pts = _points(args)
    out = {"thought": thought, "say": thought[:60] or name}
    if name in ("click", "left_single"):
        out.update(action="click", pt=pts[0] if pts else None)
    elif name == "left_double":
        out.update(action="double", pt=pts[0] if pts else None)
    elif name == "right_single":
        out.update(action="right", pt=pts[0] if pts else None)
    elif name == "drag":
        out.update(action="drag", pt=pts[0] if pts else None,
                   pt2=pts[1] if len(pts) > 1 else None)
    elif name == "type":
        c = _CONTENT_RE.search(args)
        out.update(action="type", text=_unescape(c.group(1)) if c else "")
    elif name == "scroll":
        d = _DIR_RE.search(args)
        out.update(action="scroll", pt=pts[0] if pts else None,
                   dir=d.group(1) if d else "down")
    elif name == "hotkey":
        k = _KEY_RE.search(args)
        out.update(action="hotkey", keys=(k.group(1) if k else ""))
    elif name in ("wait",):
        out.update(action="wait")
    elif name in ("finished", "done"):
        c = _CONTENT_RE.search(args)
        out.update(action="done", say=_unescape(c.group(1)) if c else "finished")
    else:
        out.update(action="wait", say=f"unknown action {name}")
    return out


def _unescape(s: str) -> str:
    return s.replace("\\n", "\n").replace("\\'", "'").replace('\\"', '"')


def map_point(cfg, pt, region_wh, sent_wh, origin=(0, 0)):
    """Map a model-space point to absolute screen pixels.

    region_wh = size of the image region we captured (full screen, or the cropped
    window). origin = that region's top-left in screen coords. sent_wh = size we
    actually sent after resizing.
    """
    if pt is None:
        return None
    mx, my = pt
    rw, rh = region_wh
    ox, oy = origin
    if cfg.vision_coord_space == "norm1000":
        return round(ox + mx / 1000.0 * rw), round(oy + my / 1000.0 * rh)
    iw, ih = sent_wh
    return round(ox + mx * rw / iw), round(oy + my * rh / ih)


def map_action_to_screen(cfg, raw, region_wh, sent_wh, origin=(0, 0)) -> dict:
    a = dict(raw)
    if raw.get("pt") is not None:
        a["xy"] = map_point(cfg, raw["pt"], region_wh, sent_wh, origin)
    if raw.get("pt2") is not None:
        a["xy2"] = map_point(cfg, raw["pt2"], region_wh, sent_wh, origin)
    return a


def _foreground_region(screen_size):
    """Return (left, top, right, bottom) of the focused window clamped to screen,
    or None to use the whole screen."""
    try:
        from .perception import get_foreground_window
        _, win = get_foreground_window()
        if win is None:
            return None
        r = win.BoundingRectangle
        sw, sh = screen_size
        l, t = max(0, r.left), max(0, r.top)
        rr, bb = min(sw, r.right), min(sh, r.bottom)
        if rr - l < 80 or bb - t < 80:   # too small / bogus -> full screen
            return None
        return (l, t, rr, bb)
    except Exception:
        return None


# ---------------- model call ----------------
def ping_vision(cfg):
    try:
        r = requests.get(f"{cfg.ollama_url}/api/tags", timeout=5)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
        base = cfg.vision_model.split(":")[0].split("/")[-1].lower()
        have = any(base in n.lower() for n in names)
        return True, f"Ollama up. Vision model {'present' if have else 'NOT pulled'}: {cfg.vision_model}"
    except Exception as e:
        return False, f"Ollama not reachable: {e}"


def decide_vision(cfg, goal: str, history: list[str]):
    """Capture screen, ask the model, return (mapped_action, raw_text, screen_wh)."""
    full = capture_screen()
    screen_wh = full.size
    origin = (0, 0)
    region = full
    if cfg.vision_crop_to_window:
        box = _foreground_region(screen_wh)
        if box:
            region = full.crop(box)
            origin = (box[0], box[1])
    sent, region_wh, sent_wh = resize_for_model(region, cfg.vision_max_side)
    system = COMPUTER_USE_PROMPT.format(language=cfg.language, instruction=goal)
    if cfg.vision_terse:
        system += ("\n- Keep the Thought to ONE short sentence.\n"
                   "- When the task is already accomplished, output finished() immediately.")
    # UI-TARS is Qwen-based and sometimes answers finished(content=...) in Chinese;
    # the Thought language was forced, the answer text was not.
    system += f"\n- Write finished(content='...') in {cfg.language}, never in another language."
    if getattr(cfg, "user_context", ""):
        system += ("\n\n## Personal context (this user's past tasks and notes; the screen wins)\n"
                   + cfg.user_context[:1200])
    msgs = [{"role": "system", "content": system}]
    # brief textual history of prior actions (keeps context small for 8 GB)
    for h in history[-4:]:
        msgs.append({"role": "assistant", "content": h})
    msgs.append({"role": "user", "content": "Here is the current screenshot. Give the next Action.",
                 "images": [_encode(sent)]})
    payload = {"model": cfg.vision_model, "messages": msgs, "stream": False,
               "keep_alive": cfg.vision_keep_alive,
               "options": {"temperature": cfg.temperature,
                           "num_predict": cfg.vision_num_predict}}
    r = requests.post(f"{cfg.ollama_url}/api/chat", json=payload, timeout=cfg.request_timeout)
    r.raise_for_status()
    raw = r.json().get("message", {}).get("content", "")
    parsed = parse_action(raw)
    mapped = map_action_to_screen(cfg, parsed, region_wh, sent_wh, origin)
    return mapped, raw, screen_wh


# ---------------- execution ----------------
def execute_vision(cfg, action: dict) -> str:
    import pyautogui
    k = action.get("action")
    xy = action.get("xy")
    if cfg.dry_run:
        return f"DRY-RUN would {k} {xy if xy else ''} {action.get('text','')}".strip()
    if k == "click" and xy:
        pyautogui.click(*xy); return f"click {xy}"
    if k == "double" and xy:
        pyautogui.doubleClick(*xy); return f"double {xy}"
    if k == "right" and xy:
        pyautogui.rightClick(*xy); return f"right {xy}"
    if k == "drag" and xy and action.get("xy2"):
        pyautogui.moveTo(*xy); pyautogui.dragTo(*action["xy2"], duration=0.4)
        return f"drag {xy}->{action['xy2']}"
    if k == "type":
        txt = action.get("text", "")
        submit = txt.endswith("\n")
        pyautogui.write(txt.rstrip("\n"), interval=0.02)
        if submit:
            pyautogui.press("enter")
        return f"type {txt[:30]!r}{' + enter' if submit else ''}"
    if k == "scroll":
        if xy:
            pyautogui.moveTo(*xy)
        amt = -500 if action.get("dir") in ("down", "right") else 500
        pyautogui.scroll(amt); return f"scroll {action.get('dir')}"
    if k == "hotkey":
        parts = [p for p in action.get("keys", "").split() if p]
        if parts:
            pyautogui.hotkey(*parts)
        return f"hotkey {action.get('keys')}"
    if k == "wait":
        time.sleep(5); return "wait 5s"
    return f"noop {action}"


# ---------------- loop ----------------
def run_vision(goal: str, cfg) -> str:
    from .agent import Logger, UnfinishedRun, _check_stop, _RunStopped, _apply_harness
    from .harness import Harness, SKIP, STOP
    from .overlay import Overlay, NullOverlay

    ok, msg = ping_vision(cfg)
    print(msg)
    if not ok:
        return UnfinishedRun(f"Aborted: {msg}")

    logger = Logger(cfg)
    logger.log({"event": "start", "goal": goal, "model": cfg.vision_model, "backend": "vision"})
    print(f"[vision backend] model={cfg.vision_model}  coord_space={cfg.vision_coord_space}")
    print("Hold F12 to stop. Starting in 3s -- switch to your target window...")
    time.sleep(3)

    overlay = Overlay(cfg) if cfg.show_overlay else NullOverlay()
    if cfg.show_overlay and not overlay.ok:
        overlay = NullOverlay()

    history: list[str] = []
    final = UnfinishedRun("max steps reached")
    harness = Harness(cfg)
    max_steps = cfg.vision_max_steps or cfg.max_steps
    last_sig = None
    repeat = 0
    try:
        for step in range(1, max_steps + 1):
            _check_stop(cfg)
            overlay.think(f"[vision] step {step}/{max_steps}  (F12 to stop)")
            try:
                action, raw, screen_wh = decide_vision(cfg, goal, history)
            except Exception as e:
                logger.log({"event": "error", "message": str(e)})
                time.sleep(cfg.settle_seconds); continue

            _check_stop(cfg)
            xy = action.get("xy")
            label = action.get("action", "?") + (f" {xy}" if xy else "")
            if xy:
                overlay.act((xy[0]-28, xy[1]-20, xy[0]+28, xy[1]+20), label,
                            action.get("thought", "")[:60] or label)
            else:
                overlay.think(f"[vision] {label}", action.get("thought","")[:40])

            logger.log({"event": "decide", "step": step, "raw": raw[:400], "action": action})

            if cfg.confirm_each_step:
                input(f"  about to run {action.get('action')} {xy or ''} -- Enter to allow...")

            _check_stop(cfg)
            if action.get("action") == "done":
                final = action.get("say", "done")
                overlay.done(final[:60]); time.sleep(1.2); break

            # no-progress detection: identical action+coords repeated -> stop early
            sig = (action.get("action"),
                   tuple(round(c / 15) for c in xy) if xy else None,
                   (action.get("text") or "")[:20])
            if sig == last_sig:
                repeat += 1
            else:
                repeat = 0
            last_sig = sig
            if repeat + 1 >= cfg.vision_stop_on_repeat:
                final = UnfinishedRun("stopped: repeated the same action with no progress")
                logger.log({"event": "stop", "reason": "no-progress", "sig": str(sig)})
                overlay.done("no further progress"); time.sleep(1.0); break

            gate = _apply_harness(harness, action, None, step, logger, overlay, history)
            _check_stop(cfg)
            if gate == STOP:
                final = UnfinishedRun(f"stopped: you declined -- {history[-1]}")
                overlay.stopped(final); break
            if gate == SKIP:
                time.sleep(cfg.settle_seconds); overlay.clear(); continue

            if cfg.show_cursor and xy:
                overlay.cursor_move(xy[0], xy[1])
                time.sleep(cfg.cursor_move_time)
                if action.get("action") in ("click", "double", "right", "type"):
                    overlay.click_burst()
                elif action.get("action") == "scroll":
                    overlay.scroll_hint(action.get("dir", "down"))
            _check_stop(cfg)
            try:
                res = execute_vision(cfg, action)
            except Exception as e:
                res = f"action error: {type(e).__name__}: {e}"
            logger.log({"event": "action", "step": step, "result": res})
            history.append(f"Thought: {action.get('thought','')}\nAction: {action.get('action')}")
            time.sleep(cfg.settle_seconds)
            overlay.clear()
        else:
            overlay.stopped(final)
    except _RunStopped as exc:
        final = UnfinishedRun(str(exc))
        logger.log({"event": "abort", "step": step})
    finally:
        if final.startswith("aborted"):
            overlay.stopped(final); time.sleep(0.8)
        overlay.close()

    logger.log({"event": "end", "final": final})
    print(f"\n=== FINISHED (vision): {final} ===\nLog: {logger.path}")
    return final
