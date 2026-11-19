"""The agent loop: perceive -> think -> act, with a kill-switch and logging."""
from __future__ import annotations

from dataclasses import replace
from threading import Event

import ctypes
import json
import os
import sys
import re as _re
import time
from datetime import datetime

# Windows consoles default to cp1252; model text may contain smart quotes / emoji.
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

from . import llm, actions
from .harness import Harness, SKIP, STOP, target_name
from .notepad import NewNotepadText
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


class UnfinishedRun(str):
    """An unsuccessful run result that remains printable for existing callers."""


class _RunStopped(Exception):
    pass


def _check_stop(cfg):
    if cfg.stop_event.is_set():
        raise _RunStopped("stopped by user")
    if _key_down(cfg.kill_key_vk):
        raise _RunStopped("aborted by user (F12)")


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
    """Run a task. Safe to call from any thread (GUI / tray worker threads included).

    UI Automation needs COM initialised on the calling thread; without it every
    perception call fails with "CoInitialize has not been called" and the agent never
    sees the screen. The initializer is entered here so every caller is covered.
    """
    import uiautomation as _auto
    cfg = cfg or DEFAULT
    mem, steps, t0 = None, [], time.time()
    if cfg.use_memory:
        try:
            from .memory import Memory, summarize_event
            mem = Memory(cfg.memory_path or None)
            context = mem.context_for(goal)
            hide_next = [False]
            user_cb = cfg.log_callback

            def record(rec):                       # collect steps, then pass events on
                try:
                    if rec.get("event") == "harness" and "field" in str(rec.get("note", "")):
                        hide_next[0] = True
                    line = summarize_event(rec)
                    if line:
                        if hide_next[0] and rec.get("event") == "action":
                            line, hide_next[0] = line.split(" ")[0] + " [hidden]", False
                        steps.append(line)
                except Exception:
                    pass
                if user_cb:
                    user_cb(rec)
            cfg = replace(cfg, user_context=context, log_callback=record)
            if context:
                print("[memory] using personal context:\n  " + context.replace("\n", "\n  "))
        except Exception as e:
            print(f"(memory unavailable: {e})")
            mem = None
    final = None
    try:
        with _auto.UIAutomationInitializerInThread():
            final = _run_impl(goal, cfg)
        return final
    finally:
        if mem is not None:
            try:
                mem.add_run(goal, final if final is not None else UnfinishedRun("error: crashed"),
                            steps, cfg.backend, time.time() - t0)
            except Exception as e:
                print(f"(could not save history: {e})")


def _run_impl(goal: str, cfg: Config | None = None) -> str:
    cfg = cfg or DEFAULT

    if cfg.stop_event is None:
        cfg = replace(cfg, stop_event=Event())

    if cfg.backend == "vision":
        from .vision import run_vision
        return run_vision(goal, cfg)

    if actions._HAS_PYAUTOGUI and cfg.pyautogui_failsafe:
        actions.pyautogui.FAILSAFE = True  # slam mouse to a corner to abort

    logger = Logger(cfg)
    logger.log({"event": "start", "goal": goal, "model": cfg.model})

    notepad = NewNotepadText.from_goal(goal)
    if notepad is None:
        up, msg = llm.ping(cfg)
        print(msg)
        logger.log({"event": "ping", "ok": up, "message": msg})
        if not up:
            final = UnfinishedRun(f"Aborted: {msg}")
            logger.log({"event": "end", "final": final})
            return final

    # Live viewer
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
    final = UnfinishedRun("max steps reached")
    harness = Harness(cfg)              # permission gate in front of every action

    # Hybrid state
    vision_mod = None
    if cfg.backend == "hybrid":
        from . import vision as vision_mod  # noqa
    text_stall = 0
    vision_sticky = 0
    addr_armed = False          # True right after ctrl+l -> auto-Enter on next type
    last_sig = None
    prev_title = None           # goal check runs when the screen (window title) changes
    prev_rendered = None
    last_kind = ""
    acted = False
    work_hwnd = work_pid = None # the app the agent is working in (focus pin)
    windows_before = None       # top-level windows that existed before our last action
    just_opened = ""            # app name our last action asked to open
    auto_app = actions.app_from_goal(goal) if notepad is None else None
    drift = 0
    done_rejects = 0
    repeat = 0

    try:
        for step in range(1, cfg.max_steps + 1):
            _check_stop(cfg)

            try:
                title, elements, rendered = perceive(cfg)
            except Exception as e:
                logger.log({"event": "error", "message": f"perceive failed: {e}"})
                time.sleep(cfg.settle_seconds)
                continue

            if notepad is not None:
                logger.log({"event": "step", "step": step, "title": title, "mode": "uia"})
                if cfg.dry_run:
                    final = "Dry run: open a new Notepad tab, type the text, and verify it"
                    break
                if cfg.confirm_each_step:
                    input("Proceed with the next Notepad step? Press Enter to allow...")
                try:
                    action, result = notepad.step(title, elements, cfg, _check_stop)
                except _RunStopped:
                    raise
                except Exception as exc:
                    final = UnfinishedRun(f"stopped: {exc}")
                    overlay.stopped(final)
                    break
                logger.log({"event": "action", "step": step, "action": action,
                            "result": str(result), "mode": "uia"})
                if result.done:
                    final = result.detail
                    overlay.done(final)
                    break
                time.sleep(cfg.settle_seconds)
                continue

            # Focus pin: if another program took the foreground without us opening or
            # clicking into it, never act there -- switch back to our window instead.
            fg_hwnd, fg_pid = _fg_window()

            # "Open X ..." goals: open X first, deterministically (known apps only), so the
            # model never starts by typing into whatever window happens to be in front.
            if auto_app and step == 1 and not cfg.dry_run:
                windows_before, just_opened = _top_windows(), auto_app
                res = actions._launch_app(auto_app)
                work_hwnd, work_pid = fg_hwnd, fg_pid
                logger.log({"event": "action", "step": step, "result": str(res), "mode": "uia",
                            "action": {"action": "open_app", "name": auto_app, "say": "auto"}})
                history.append(f"step {step}: open_app {auto_app} -> {res}")
                acted = acted or res.ok
                time.sleep(max(cfg.settle_seconds, 1.5))
                continue

            # Focus pin: follow focus ONLY to a window our last action created, or to the
            # app it asked to open. An already-open app (an IDE, chat, browser) that comes
            # to the front is never adopted -- a real run once typed into Android Studio.
            legit_switch = (windows_before is not None and fg_hwnd not in windows_before) or                            _title_matches(title, just_opened)
            if work_pid is not None and fg_pid != work_pid and not legit_switch:
                drift += 1
                logger.log({"event": "focus_drift", "step": step, "title": title, "count": drift})
                if drift > 2 or not _restore_focus(work_hwnd):
                    final = UnfinishedRun(f"stopped: focus left the target app (now '{title[:40]}')")
                    overlay.stopped(final); break
                history.append(f"step {step}: focus jumped to '{title[:40]}', which is not your "
                               "target app; LocalPilot switched back. Continue in the target app.")
                time.sleep(cfg.settle_seconds)
                continue
            drift = 0

            # Goal check: after one of OUR actions changed the screen, ask whether the goal
            # is already met -- stops the agent from flailing after it has succeeded.
            changed = (title != prev_title) or (
                last_kind in ("type", "type_text") and rendered != prev_rendered)
            if (cfg.verify_goal and acted and prev_title is not None and changed
                    and not cfg.dry_run):
                met, why = llm.check_goal(cfg, goal, title, rendered)
                logger.log({"event": "goal_check", "step": step, "title": title,
                            "done": met, "why": why})
                if met:
                    final = f"done: {why or 'goal reached'}"
                    overlay.done(final[:60]); time.sleep(1.0); break
            prev_title, prev_rendered = title, rendered

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
                _check_stop(cfg)
                xy = vaction.get("xy")
                vlabel = f"[vision] {vaction.get('action')}" + (f" {xy}" if xy else "")
                if xy:
                    overlay.act((xy[0] - 28, xy[1] - 20, xy[0] + 28, xy[1] + 20),
                                vlabel, vaction.get("thought", "")[:60] or vlabel)
                else:
                    overlay.think(f"[vision] {vlabel}", vaction.get("thought", "")[:40])
                if cfg.confirm_each_step:
                    input(f"  about to run (vision) {vaction.get('action')} {xy or ''} -- Enter...")
                _check_stop(cfg)
                if vaction.get("action") == "done":
                    missing = (llm.goal_missing_terms(goal, title, rendered)
                               if cfg.verify_goal and not cfg.dry_run else [])
                    if missing and done_rejects < 1:
                        done_rejects += 1
                        history.append(f"step {step}: you answered done, but the screen does not "
                                       f"show {', '.join(missing)} yet. Keep going.")
                        time.sleep(cfg.settle_seconds); continue
                    if missing:
                        final = UnfinishedRun("stopped: the model said done, but "
                                              f"{', '.join(missing)} is not visible on screen")
                        overlay.stopped(final); break
                    final = vaction.get("say", "done")
                    overlay.done(final[:60]); time.sleep(1.2); break
                gate = _apply_harness(harness, vaction, None, step, logger, overlay, history)
                _check_stop(cfg)
                if gate == STOP:
                    final = UnfinishedRun(f"stopped: you declined -- {history[-1]}")
                    overlay.stopped(final); break
                if gate == SKIP:
                    text_stall += 1
                    time.sleep(cfg.settle_seconds); overlay.clear(); continue
                _drive_cursor(cfg, overlay, xy, vaction.get("action"), vaction.get("dir"))
                _check_stop(cfg)
                windows_before, just_opened = _top_windows(), ""
                try:
                    res = vision_mod.execute_vision(cfg, vaction)
                except Exception as e:
                    res = f"action error: {type(e).__name__}: {e}"
                logger.log({"event": "action", "step": step, "result": res, "mode": "vision"})
                history.append(f"step {step}: [vision] {vaction.get('action')} {xy or ''} -> {res}")
                acted = True
                if not str(res).startswith("action error"):
                    work_hwnd, work_pid = fg_hwnd, fg_pid
            else:
                try:
                    action = llm.decide(cfg, goal, title, rendered, history)
                except llm.LLMError as e:
                    logger.log({"event": "error", "message": str(e)})
                    history.append(f"step {step}: model error, retrying")
                    text_stall += 1
                    time.sleep(cfg.settle_seconds)
                    continue
                _check_stop(cfg)
                akind = action.get("action")
                # Auto-submit: typing right after ctrl+l (address bar) -> append Enter,
                # so navigation works even if the model forgets the trailing newline.
                t = str(action.get("text", ""))
                looks_url = bool(_re.fullmatch(
                    r"(https?://|www\.)\S+|[\w-]+(\.[\w-]+)*\.(com|org|net|io|in|edu|gov|co|dev|ai|app)(/\S*)?",
                    t.strip(), _re.IGNORECASE))
                if (addr_armed or looks_url) and akind in ("type", "type_text") \
                        and t.strip() and not t.endswith("\n"):
                    action["text"] = t + "\n"          # a URL is always meant to be opened
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
                    final = UnfinishedRun("stopped: repeated the same action with no progress")
                    logger.log({"event": "stop", "step": step, "reason": "repeat"})
                    overlay.done("no further progress"); time.sleep(1.0); break

                label = _action_label(action)
                say = str(action.get("say", "")) or label
                rect = _rect_for_action(action, elements)
                overlay.act(rect, label, say)
                if cfg.confirm_each_step:
                    input(f"  about to run {action} -- press Enter to allow, Ctrl+C to stop...")
                gate = _apply_harness(harness, action, elements, step, logger, overlay, history)
                _check_stop(cfg)
                if gate == STOP:
                    final = UnfinishedRun(f"stopped: you declined -- {history[-1]}")
                    overlay.stopped(final); break
                if gate == SKIP:
                    if cfg.backend == "hybrid":
                        text_stall += 1
                    time.sleep(cfg.settle_seconds); overlay.clear(); continue
                center = (((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
                          if rect else None)
                _check_stop(cfg)
                _drive_cursor(cfg, overlay, center, action.get("action"), action.get("dir"))
                _check_stop(cfg)
                windows_before = _top_windows()
                just_opened = str(action.get("name", "")) if akind == "open_app" else ""
                try:
                    result = actions.execute(cfg, action, elements)
                except Exception as e:             # one bad action must not kill the task
                    result = actions.ActionResult(False, f"action error: {type(e).__name__}: {e}")
                logger.log({"event": "action", "step": step, "action": action,
                            "target": target_name(action, elements),
                            "result": str(result), "mode": "uia"})
                history.append(f"step {step}: {action.get('action')} "
                               f"{action.get('id', action.get('keys',''))} -> {result}")
                acted = acted or result.ok
                last_kind = akind
                if cfg.backend == "hybrid":
                    text_stall = 0 if result.ok and _text_is_progress(action, rect) else text_stall + 1
                if result.ok and akind not in ("wait", "done"):
                    work_hwnd, work_pid = fg_hwnd, fg_pid
                if result.done:
                    missing = (llm.goal_missing_terms(goal, title, rendered)
                               if cfg.verify_goal and not cfg.dry_run else [])
                    if missing:
                        done_rejects += 1
                        logger.log({"event": "done_rejected", "step": step, "missing": missing})
                        if done_rejects >= 2:
                            final = UnfinishedRun("stopped: the model said done, but "
                                                  f"{', '.join(missing)} is not visible on screen")
                            overlay.stopped(final); break
                        history.append(f"step {step}: you answered done, but the screen does not "
                                       f"show {', '.join(missing)} yet. Keep going; answer done only "
                                       "when the goal is visible.")
                        time.sleep(cfg.settle_seconds); overlay.clear(); continue
                    final = result.detail
                    overlay.done(result.detail[:60]); time.sleep(1.4); break

            time.sleep(cfg.settle_seconds)
            overlay.clear()
        else:
            overlay.stopped(final)
    except _RunStopped as exc:
        final = UnfinishedRun(str(exc))
        logger.log({"event": "abort", "step": step})
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


def _fg_window():
    """(hwnd, pid) of the current foreground window."""
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.GetForegroundWindow.restype = wintypes.HWND
    h = u.GetForegroundWindow()
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(h, ctypes.byref(pid))
    return h, pid.value


def _top_windows() -> set:
    """Handles of all visible top-level windows right now."""
    from ctypes import wintypes
    u = ctypes.windll.user32
    found = set()
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        if u.IsWindowVisible(h):
            found.add(h)
        return True
    u.EnumWindows(proc(cb), 0)
    return found


def _title_matches(title: str, app: str) -> bool:
    """Does this window look like the app we asked to open? ('file explorer' ~ 'Home - File Explorer')"""
    import re as _re
    if not app:
        return False
    words = [w for w in _re.findall(r"[a-z]+", app.lower()) if len(w) >= 4 and w != "settings"]
    t = (title or "").lower()
    if "settings" in app.lower() and "settings" in t:
        return True
    return bool(words) and any(w in t for w in words)


def _restore_focus(hwnd) -> bool:
    """Bring our working window back to the front. True if it is in front afterwards."""
    if not hwnd:
        return False
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.GetForegroundWindow.restype = wintypes.HWND
    if not u.IsWindow(hwnd):
        return False
    try:
        cur = u.GetForegroundWindow()
        t1 = u.GetWindowThreadProcessId(cur, None)
        t2 = k.GetCurrentThreadId()
        u.AttachThreadInput(t1, t2, True)
        u.ShowWindow(hwnd, 9)                      # SW_RESTORE if minimized
        u.SetForegroundWindow(hwnd)
        u.BringWindowToTop(hwnd)
        u.AttachThreadInput(t1, t2, False)
        time.sleep(0.3)
        return u.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _apply_harness(harness, action, elements, step, logger, overlay, history) -> str:
    """Run the permission gate; log it and tell the model. Returns allow/skip/stop."""
    decision, note = harness.review(action, elements)
    if not note:
        return decision
    logger.log({"event": "harness", "step": step, "decision": decision,
                "note": note, "action": action})
    if decision == SKIP:
        history.append(f"step {step}: SKIPPED -- the user did not allow this ({note}). "
                       "Do not try it again; find another way, or answer done.")
        overlay.status("skipped: not allowed")
    elif decision == STOP:
        history.append(note)
    return decision


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
