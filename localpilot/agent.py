"""The agent loop: perceive -> think -> act, with a kill-switch and logging."""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from datetime import datetime

# Windows consoles default to cp1252; model text may contain smart quotes / emoji.
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

from . import llm, actions
from .perception import perceive
from .overlay import Overlay, NullOverlay
from .config import Config, DEFAULT


def _rect_for_action(action: dict, elements):
    """Return the screen rect of the control an action targets, or None."""
    eid = action.get("id")
    try:
        eid = int(eid)
    except Exception:
        return None
    for e in elements:
        if e.id == eid:
            return e.rect
    return None


def _key_down(vk: int) -> bool:
    # GetAsyncKeyState high bit set => key currently pressed.
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def _stop_requested(cfg) -> bool:
    ev = getattr(cfg, "stop_event", None)
    try:
        return ev is not None and ev.is_set()
    except Exception:
        return False


_CHROME_NAMES = {"Minimize", "Maximize", "Restore", "Close", "System", "Application"}


def _count_content(elements) -> int:
    """Count actionable controls that are real content, not window chrome."""
    n = 0
    for e in elements:
        if e.name in _CHROME_NAMES and e.role in ("Button", "TitleBar"):
            continue
        n += 1
    return n


def _text_is_progress(action: dict, rect) -> bool:
    """Did the text model pick a concrete, executable action on a real target?"""
    k = action.get("action")
    if k in ("click", "type"):
        return rect is not None          # must reference a real on-screen element
    if k == "hotkey":
        return bool(action.get("keys"))
    if k in ("open_app", "type_text"):
        return True                      # these always do something concrete
    # scroll / wait / unknown are treated as non-progress (possible stall)
    return False


class Logger:
    def __init__(self, cfg: Config):
        os.makedirs(cfg.log_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(cfg.log_dir, f"run_{stamp}.jsonl")
        self.verbose = cfg.verbose
        self.callback = getattr(cfg, "log_callback", None)

    def log(self, record: dict):
        record["ts"] = datetime.now().isoformat(timespec="seconds")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.callback:
            try:
                self.callback(record)
            except Exception:
                pass
        if self.verbose:
            k = record.get("event")
            if k == "step":
                print(f"\n--- step {record['step']} --- window: {record.get('title','')}")
            elif k == "action":
                if "action" in record:
                    print(f"  model: {record['action']}")
                print(f"  result: {record.get('result','')}")
            elif k == "decide":
                print(f"  model: {record.get('action')}")
            elif k == "error":
                print(f"  ! {record['message']}")


def run(goal: str, cfg: Config | None = None) -> str:
    cfg = cfg or DEFAULT

    if cfg.backend == "vision":
        from .vision import run_vision
        return run_vision(goal, cfg)

    if actions._HAS_PYAUTOGUI and cfg.pyautogui_failsafe:
        actions.pyautogui.FAILSAFE = True  # slam mouse to a corner to abort

    logger = Logger(cfg)
    logger.log({"event": "start", "goal": goal, "model": cfg.model})

    up, msg = llm.ping(cfg)
    print(msg)
    logger.log({"event": "ping", "ok": up, "message": msg})
    if not up:
        return f"Aborted: {msg}"

    # Live viewer
    # one stop event per run (so the orb's right-click "Stop task" works from the CLI too)
    if getattr(cfg, "stop_event", None) is None:
        import threading
        cfg.stop_event = threading.Event()
    cfg.stop_event.clear()

    overlay = Overlay(cfg) if cfg.show_overlay else NullOverlay()
    if cfg.show_overlay and not overlay.ok:
        print("(overlay unavailable; continuing without it)")
        overlay = NullOverlay()

    # Per-step screenshot dir
    shot_dir = None
    if cfg.save_screenshots:
        shot_dir = os.path.splitext(logger.path)[0] + "_shots"
        os.makedirs(shot_dir, exist_ok=True)

    print(f"\nGOAL: {goal}")
    print(f"Hold F12 to emergency-stop. Starting in 3s -- click the target window now...")
    time.sleep(3)

    history: list[str] = []
    final = "max steps reached"

    # Hybrid state
    vision_mod = None
    if cfg.backend == "hybrid":
        from . import vision as vision_mod  # noqa
    text_stall = 0
    vision_sticky = 0
    addr_armed = False          # True right after ctrl+l -> auto-Enter on next type
    last_sig = None
    repeat = 0

    try:
        for step in range(1, cfg.max_steps + 1):
            if _key_down(cfg.kill_key_vk) or _stop_requested(cfg):
                final = "aborted by user (F12)" if _key_down(cfg.kill_key_vk) else "stopped by user"
                logger.log({"event": "abort", "step": step})
                break

            try:
                title, elements, rendered = perceive(cfg)
            except Exception as e:
                logger.log({"event": "error", "message": f"perceive failed: {e}"})
                time.sleep(cfg.settle_seconds)
                continue

            content = _count_content(elements)

            # ---- decide which brain to use this step ----
            mode = "uia"
            reason = ""
            if cfg.backend == "hybrid":
                if vision_sticky > 0:
                    mode, reason = "vision", "sticky"
                    vision_sticky -= 1
                elif content < cfg.hybrid_min_content:
                    mode, reason = "vision", f"thin tree ({content} controls)"
                elif text_stall >= cfg.hybrid_escalate_after:
                    mode, reason = "vision", "text stalled"
                    vision_sticky = cfg.hybrid_vision_sticky
                    text_stall = 0

            overlay.think(f"[{mode}] step {step}/{cfg.max_steps}  (F12 to stop)", title[:40])
            logger.log({"event": "step", "step": step, "title": title,
                        "num_elements": len(elements), "content": content,
                        "mode": mode, "reason": reason})
            if shot_dir:
                _save_shot(shot_dir, step)

            if mode == "vision":
                try:
                    vaction, raw, screen = vision_mod.decide_vision(cfg, goal, history)
                except Exception as e:
                    logger.log({"event": "error", "message": f"vision: {e}"})
                    time.sleep(cfg.settle_seconds)
                    continue
                xy = vaction.get("xy")
                vlabel = f"[vision] {vaction.get('action')}" + (f" {xy}" if xy else "")
                if xy:
                    overlay.act((xy[0] - 28, xy[1] - 20, xy[0] + 28, xy[1] + 20),
                                vlabel, vaction.get("thought", "")[:60] or vlabel)
                else:
                    overlay.think(f"[vision] {vlabel}", vaction.get("thought", "")[:40])
                if cfg.confirm_each_step:
                    input(f"  about to run (vision) {vaction.get('action')} {xy or ''} -- Enter...")
                if vaction.get("action") == "done":
                    final = vaction.get("say", "done")
                    overlay.done(final[:60]); time.sleep(1.2); break
                _drive_cursor(cfg, overlay, xy, vaction.get("action"), vaction.get("dir"))
                res = vision_mod.execute_vision(cfg, vaction)
                logger.log({"event": "action", "step": step, "result": res, "mode": "vision"})
                history.append(f"step {step}: [vision] {vaction.get('action')} {xy or ''} -> {res}")
            else:
                try:
                    action = llm.decide(cfg, goal, title, rendered, history)
                except llm.LLMError as e:
                    logger.log({"event": "error", "message": str(e)})
                    history.append(f"step {step}: model error, retrying")
                    text_stall += 1
                    time.sleep(cfg.settle_seconds)
                    continue
                akind = action.get("action")
                # Auto-submit: typing right after ctrl+l (address bar) -> append Enter,
                # so navigation works even if the model forgets the trailing newline.
                if addr_armed and akind in ("type", "type_text"):
                    t = str(action.get("text", ""))
                    if not t.endswith("\n"):
                        action["text"] = t + "\n"
                addr_armed = (akind == "hotkey"
                              and "ctrl" in str(action.get("keys", "")).lower()
                              and str(action.get("keys", "")).lower().replace(" ", "").endswith("+l"))

                # Repeat / no-progress detection (stop a stuck loop; escalate in hybrid)
                sig = (akind, str(action.get("keys", "")), action.get("id"),
                       str(action.get("text", ""))[:20], action.get("name"))
                repeat = repeat + 1 if sig == last_sig else 0
                last_sig = sig
                if repeat >= 2:
                    if cfg.backend == "hybrid":
                        logger.log({"event": "escalate", "step": step, "reason": "text repeat"})
                        vision_sticky = cfg.hybrid_vision_sticky
                        text_stall = 0; repeat = 0
                        time.sleep(cfg.settle_seconds); continue
                    final = "stopped: repeated the same action with no progress"
                    logger.log({"event": "stop", "step": step, "reason": "repeat"})
                    overlay.done("no further progress"); time.sleep(1.0); break

                label = _action_label(action)
                say = str(action.get("say", "")) or label
                rect = _rect_for_action(action, elements)
                overlay.act(rect, label, say)
                if cfg.confirm_each_step:
                    input(f"  about to run {action} -- press Enter to allow, Ctrl+C to stop...")
                center = (((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
                          if rect else None)
                _drive_cursor(cfg, overlay, center, action.get("action"), action.get("dir"))
                result = actions.execute(cfg, action, elements)
                logger.log({"event": "action", "step": step,
                            "action": action, "result": str(result), "mode": "uia"})
                history.append(f"step {step}: {action.get('action')} "
                               f"{action.get('id', action.get('keys',''))} -> {result}")
                if cfg.backend == "hybrid":
                    text_stall = 0 if _text_is_progress(action, rect) else text_stall + 1
                if result.done:
                    final = result.detail
                    overlay.done(result.detail[:60]); time.sleep(1.4); break

            time.sleep(cfg.settle_seconds)
            overlay.clear()
        else:
            overlay.done("finished")
    finally:
        if final.startswith("aborted"):
            try:
                overlay.stopped(final)
                time.sleep(1.0)
            except Exception:
                pass
        overlay.close()

    logger.log({"event": "end", "final": final})
    print(f"\n=== FINISHED: {final} ===")
    print(f"Log: {logger.path}")
    return final


def _action_label(action: dict) -> str:
    k = action.get("action", "?")
    if k == "click":
        return f"click [{action.get('id')}]"
    if k == "type":
        return f"type [{action.get('id')}] <- {str(action.get('text',''))[:24]}"
    if k == "type_text":
        return f"type_text <- {str(action.get('text',''))[:24]}"
    if k == "open_app":
        return f"open_app {action.get('name','')}"
    if k == "hotkey":
        return f"hotkey {action.get('keys','')}"
    if k == "scroll":
        return f"scroll {action.get('dir','')}"
    return k


def _drive_cursor(cfg, overlay, xy, action_kind, direction=None):
    """Glide the agent's visible cursor to the target, then show click/scroll FX.
    With cfg.real_cursor, also physically move the OS cursor so the action is
    visibly performed by a real pointer."""
    if not cfg.show_cursor or not xy:
        return
    overlay.cursor_move(xy[0], xy[1])
    if cfg.real_cursor and actions._HAS_PYAUTOGUI:
        try:
            actions.pyautogui.moveTo(xy[0], xy[1], duration=cfg.cursor_move_time)
        except Exception:
            time.sleep(cfg.cursor_move_time)
    else:
        time.sleep(cfg.cursor_move_time)   # let the on-screen cursor visibly travel
    if action_kind in ("click", "double", "right", "type"):
        overlay.click_burst()
    elif action_kind == "scroll":
        overlay.scroll_hint(direction or "down")


def _save_shot(shot_dir, step):
    try:
        import pyautogui
        pyautogui.screenshot().save(os.path.join(shot_dir, f"step_{step:03d}.png"))
    except Exception:
        pass
