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

from . import llm, actions, web
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

    if cfg.mouse_failsafe:
        actions.inputs.FAILSAFE = True  # mouse into the top-left corner aborts

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
    auto_app = actions.app_from_goal(goal) if notepad is None else None
    plan = web.parse(goal) if notepad is None else None      # "search X on <site>" goals
    if plan:
        plan["browser"] = plan["browser"] or (auto_app if auto_app and actions.is_browser(auto_app)
                                              else web.default_browser())
        auto_app = plan["browser"] or "browser"
    only_open = bool(auto_app) and not plan and actions.is_only_open(goal)
    if notepad is None and not only_open:
        llm.warm(cfg)                       # load the model into memory while we get ready
    if auto_app or notepad is not None:
        print("Hold F12 to emergency-stop.")   # we open the app ourselves: nothing to click
    else:
        print("Hold F12 to emergency-stop. Starting in 3s -- click the target window now...")
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
    drift = 0
    before_sig = None
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

            # Web goals ("search X on YouTube", "google X", "go to site and search X"):
            # open the right results page directly -- never the wrong search box, no typing
            # on top of autocomplete. Unknown sites: fill the page's own search box once.
            if plan and step == 1 and not cfg.dry_run:
                windows_before, just_opened = _top_windows(), plan["browser"] or ""
                target = plan["url"] or plan["home"]
                if plan["browser"]:
                    res = actions.launch_browser_at(plan["browser"], target)
                else:
                    try:
                        os.startfile(target)
                        res = actions.ActionResult(True, f"opened {target} in the default browser")
                    except Exception as e:
                        res = actions.ActionResult(False, f"could not open {target}: {e}")
                logger.log({"event": "action", "step": step, "result": str(res), "mode": "uia",
                            "action": {"action": "open_url", "url": target, "say": "web layer"}})
                history.append(f"step {step}: opened {target} -> {res}")
                acted = acted or res.ok
                appeared = res.ok and _wait_for_app(just_opened, windows_before, cfg)
                if appeared:
                    work_hwnd, work_pid = _fg_now()      # work in the NEW browser window
                q = plan["query"]
                if plan["url"]:
                    found = res.ok and _wait_results(q, 12)
                else:
                    if appeared:
                        _wait_ui_ready(cfg)
                    found = appeared and _site_search(q, logger, step)
                if found and not plan["rest"]:
                    final = f"done: search results for '{q}' are open"
                    logger.log({"event": "goal_check", "step": step, "done": True,
                                "why": "the page title shows the search words (web layer)"})
                    overlay.done(final[:60]); time.sleep(0.6); break
                if found:
                    history.append(f"step {step}: the search results for '{q}' are ALREADY open "
                                   f"on screen. Do not search again. Now: {plan['rest']}")
                elif appeared:
                    _wait_ui_ready(cfg)
                continue

            # "Open X ..." goals: open X first, deterministically, so the model never starts
            # by typing into whatever window happens to be in front.
            if auto_app and step == 1 and not cfg.dry_run:
                windows_before, just_opened = _top_windows(), auto_app
                url = actions.url_from_goal(goal) if actions.is_browser(auto_app) else None
                res = (actions.launch_browser_at(auto_app, url) if url
                       else actions._launch_app(auto_app))
                logger.log({"event": "action", "step": step, "result": str(res), "mode": "uia",
                            "action": {"action": "open_app", "name": auto_app, "say": "auto"}})
                history.append(f"step {step}: open_app {auto_app} -> {res}")
                acted = acted or res.ok
                appeared = res.ok and _wait_for_app(auto_app, windows_before, cfg)
                if appeared:
                    # pin to the app we just opened -- NOT the window that was in front before
                    # (that once sent typing back into the user's previous window)
                    work_hwnd, work_pid = _fg_now()
                if only_open and appeared:
                    final = f"done: opened {auto_app}"
                    logger.log({"event": "goal_check", "step": step, "done": True,
                                "why": "the app's window is open (fast path, no model needed)"})
                    overlay.done(final[:60]); time.sleep(0.6); break
                if url and appeared and actions.is_open_and_visit(goal):
                    site = _site_word(url)
                    if _wait_title_has(site, cfg):
                        final = f"done: opened {auto_app} at {url}"
                        logger.log({"event": "goal_check", "step": step, "done": True,
                                    "why": f"browser title shows '{site}' (fast path, no model)"})
                        overlay.done(final[:60]); time.sleep(0.6); break
                if not appeared:
                    time.sleep(cfg.settle_seconds)
                    continue
                _wait_ui_ready(cfg)          # don't let the model look at a half-loaded window
                expr = llm.math_expression(goal) if "calc" in auto_app.lower() else None
                if expr:
                    # Calculator maths: clear, type the expression, read the answer. No model.
                    actions.inputs.press("esc")
                    actions.inputs.write(expr + "=", interval=0.02)
                    history.append(f"step {step}: typed {expr}= into Calculator (auto)")
                    logger.log({"event": "action", "step": step, "mode": "uia",
                                "action": {"action": "type_text", "text": expr + "=", "say": "auto"},
                                "result": f"[OK] typed {expr}= (fast path)"})
                    time.sleep(0.4)
                    try:
                        t2, _, r2 = perceive(cfg)
                        met, why = llm.check_goal(cfg, goal, t2, r2)
                    except Exception:
                        met, why = False, ""
                    if met:
                        final = f"done: {why}"
                        logger.log({"event": "goal_check", "step": step, "done": True, "why": why})
                        overlay.done(final[:60]); time.sleep(0.6); break
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
                if looks_url and akind in ("type", "type_text") and not addr_armed                         and _BROWSER_TITLE.search(title or "") and not cfg.dry_run:
                    try:                              # never type a URL into the page itself
                        actions.inputs.hotkey("ctrl", "l"); time.sleep(0.15)
                        action = {"action": "type_text", "text": action["text"],
                                  "say": action.get("say", "")}
                    except Exception:
                        pass
                if akind in ("type", "type_text") and "=" in t and                         (title or "").strip().lower() == "calculator" and not cfg.dry_run:
                    try:                              # start the sum fresh: no leftover digits
                        actions.inputs.press("esc"); time.sleep(0.05)
                    except Exception:
                        pass
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
                before_sig = _ui_signature()
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
                    if plan and not plan["rest"] and not cfg.dry_run and \
                            not web.results_open(
                            plan["query"], title, ctypes.windll.user32.GetForegroundWindow()):
                        missing = missing or [f"the results for '{plan['query']}'"]
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

            _settle(cfg, before_sig)
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


_BROWSER_TITLE = _re.compile(r"(brave|chrome|edge|firefox)\s*$", _re.IGNORECASE)


def _site_word(url: str) -> str:
    """'https://www.wikipedia.org/x' -> 'wikipedia'."""
    host = _re.sub(r"^https?://", "", url).split("/")[0].lower()
    parts = [p for p in host.split(".") if p not in ("www", "m")]
    return parts[0] if parts else host


def _wait_title_has(word: str, cfg, timeout: float = 10.0) -> bool:
    import ctypes
    end = time.time() + timeout
    while time.time() < end:
        if word and word in _win_title(ctypes.windll.user32.GetForegroundWindow()).lower():
            return True
        time.sleep(0.15)
    return False


def _wait_ui_ready(cfg, timeout: float = 4.0, poll: float = 0.2):
    """Wait until the front window's controls stop appearing (app finished loading)."""
    end, last, same = time.time() + timeout, -1, 0
    while time.time() < end:
        try:
            n = len(perceive(cfg)[1])
        except Exception:
            n = -1
        same = same + 1 if (n == last and n >= 5) else 0
        if same >= 2:
            return
        last = n
        time.sleep(poll)


def _fg_now():
    """(hwnd, pid) of the foreground window right now."""
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return hwnd, pid.value


def _wait_title(pred, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred(_win_title(ctypes.windll.user32.GetForegroundWindow())):
            return True
        time.sleep(0.15)
    return False


def _wait_results(query: str, timeout: float = 10.0) -> bool:
    """Wait until the front window shows results for query (title, or URL every ~1 s)."""
    end, n = time.time() + timeout, 0
    while time.time() < end:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        title = _win_title(hwnd)
        if web.query_in_title(query, title):
            return True
        n += 1
        if n % 6 == 0 and web.results_open(query, title, hwnd):
            return True
        time.sleep(0.15)
    return False


def _site_search(query: str, logger, step) -> bool:
    """Use the open page's OWN search box: replace its text, type once, Enter once."""
    try:
        import uiautomation as auto
        win = auto.ControlFromHandle(ctypes.windll.user32.GetForegroundWindow())
        box = web.find_search_box(win)
        if box is None:
            logger.log({"event": "site_search", "step": step, "found_box": False})
            return False
        try:
            box.Click(simulateMove=False)
        except Exception:
            box.SetFocus()
        time.sleep(0.15)
        actions.inputs.hotkey("ctrl", "a")              # replace, never append
        actions.inputs.write(query, interval=0.02)
        time.sleep(0.25)
        actions.inputs.press("enter")
        logger.log({"event": "site_search", "step": step, "found_box": True,
                    "box": (box.Name or box.AutomationId)[:40]})
        return _wait_results(query, 10)
    except Exception as e:
        logger.log({"event": "site_search", "step": step, "error": str(e)[:120]})
        return False


def _ui_signature():
    """Cheap fingerprint of what the user would see change: window, title, focused control."""
    try:
        import ctypes
        import uiautomation as auto
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        f = auto.GetFocusedControl()
        val = ""
        if f is not None:
            try:
                val = f.GetValuePattern().Value[:80]
            except Exception:
                pass
        return (hwnd, _win_title(hwnd),
                f.Name[:80] if f else "", val)
    except Exception:
        return None


def _win_title(hwnd) -> str:
    import ctypes
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _settle(cfg, before, min_wait: float = 0.2, poll: float = 0.08):
    """Observe: return as soon as the UI changed and held still briefly; never longer than
    cfg.settle_seconds. Replaces a fixed sleep after every action."""
    time.sleep(min_wait)
    if before is None:
        time.sleep(max(0.0, cfg.settle_seconds - min_wait)); return
    deadline = time.time() + max(0.0, cfg.settle_seconds - min_wait)
    last = _ui_signature()
    stable_since = None if last == before else time.time()
    while time.time() < deadline:
        time.sleep(poll)
        cur = _ui_signature()
        if cur != before and cur == last:
            if stable_since and time.time() - stable_since >= 0.15:
                return
            stable_since = stable_since or time.time()
        else:
            stable_since = time.time() if cur != before else None
        last = cur


def _wait_for_app(app: str, windows_before, cfg, timeout: float = 10.0) -> bool:
    """Wait until the app we launched is in front (new window or matching title)."""
    end = time.time() + timeout
    import ctypes
    while time.time() < end:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        title = _win_title(hwnd)
        if (windows_before is not None and hwnd not in windows_before and title) or                 _title_matches(title, app):
            time.sleep(0.25)                 # let the first frame paint
            return True
        time.sleep(0.1)
    return False


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
    if cfg.real_cursor and actions._HAS_INPUT:
        try:
            actions.inputs.moveTo(xy[0], xy[1], duration=cfg.cursor_move_time)
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
        from . import inputs
        inputs.screenshot().save(os.path.join(shot_dir, f"step_{step:03d}.png"))
    except Exception:
        pass
