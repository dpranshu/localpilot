"""Action execution: turn a model action dict into real UI interaction.

Prefers UIA patterns (Invoke / SetValue) over raw coordinate clicks, falling
back to clicking the control's bounding-rect center when no pattern is exposed.
"""
from __future__ import annotations

import time
import uiautomation as auto

try:
    import pyautogui
    _HAS_PYAUTOGUI = True
except Exception:
    _HAS_PYAUTOGUI = False


class ActionResult:
    def __init__(self, ok: bool, detail: str, done: bool = False):
        self.ok = ok
        self.detail = detail
        self.done = done

    def __str__(self):
        tag = "DONE" if self.done else ("OK" if self.ok else "FAIL")
        return f"[{tag}] {self.detail}"


def _find(elements, eid):
    for e in elements:
        if e.id == eid:
            return e
    return None


import os as _os
import subprocess as _sp

# relative exe paths searched under LOCALAPPDATA / Program Files / Program Files (x86)
_APP_PATHS = {
    "brave": [r"BraveSoftware\Brave-Browser\Application\brave.exe"],
    "chrome": [r"Google\Chrome\Application\chrome.exe"],
    "edge": [r"Microsoft\Edge\Application\msedge.exe"],
    "firefox": [r"Mozilla Firefox\firefox.exe"],
}
_APP_COMMANDS = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe",
    "settings": "ms-settings:", "paint": "mspaint.exe", "wordpad": "write.exe",
}


def _find_exe(rel_paths):
    roots = [_os.environ.get("LOCALAPPDATA", ""), _os.environ.get("PROGRAMFILES", ""),
             _os.environ.get("PROGRAMFILES(X86)", "")]
    for rel in rel_paths:
        for root in roots:
            if not root:
                continue
            p = _os.path.join(root, rel)
            if _os.path.exists(p):
                return p
    return None


def _launch_app(name: str) -> str:
    low = name.lower().strip()
    # 1) known browsers / apps by path
    for key, rels in _APP_PATHS.items():
        if key in low:
            p = _find_exe(rels)
            if p:
                try:
                    _sp.Popen([p]); return f"launched {key} ({p})"
                except Exception as e:
                    return f"launch failed: {e}"
    # 2) simple commands
    for key, cmd in _APP_COMMANDS.items():
        if key == low or key in low:
            try:
                if cmd.endswith(":"):
                    _os.startfile(cmd)  # e.g. ms-settings:
                else:
                    _sp.Popen(cmd)
                return f"launched {key}"
            except Exception as e:
                return f"launch failed: {e}"
    # 3) universal fallback: Start-menu search (what a human does)
    if _HAS_PYAUTOGUI:
        try:
            pyautogui.press("win"); time.sleep(0.7)
            pyautogui.typewrite(name, interval=0.03); time.sleep(1.0)
            pyautogui.press("enter"); time.sleep(0.6)
            return f"start-menu launched '{name}'"
        except Exception as e:
            return f"start-menu launch failed: {e}"
    return f"could not launch '{name}'"


def _click_control(ctrl) -> str:
    # 1) Try InvokePattern (buttons, links, menu items).
    try:
        ip = ctrl.GetInvokePattern()
        if ip:
            ip.Invoke()
            return "invoked"
    except Exception:
        pass
    # 2) Try SelectionItem (list items, tabs, radios).
    try:
        sp = ctrl.GetSelectionItemPattern()
        if sp:
            sp.Select()
            return "selected"
    except Exception:
        pass
    # 3) Try Toggle (checkboxes).
    try:
        tp = ctrl.GetTogglePattern()
        if tp:
            tp.Toggle()
            return "toggled"
    except Exception:
        pass
    # 4) Fallback: physical click at the control center.
    try:
        ctrl.SetFocus()
    except Exception:
        pass
    ctrl.Click(simulateMove=False)
    return "clicked"


def _type_control(ctrl, text: str) -> str:
    try:
        ctrl.SetFocus()
    except Exception:
        pass
    # Prefer ValuePattern.SetValue for clean, instant entry.
    try:
        vp = ctrl.GetValuePattern()
        if vp and not vp.IsReadOnly:
            vp.SetValue(text)
            return "set via ValuePattern"
    except Exception:
        pass
    # Fallback: select-all then type the characters.
    try:
        ctrl.SendKeys("{Ctrl}a", waitTime=0.05)
        ctrl.SendKeys(_escape_sendkeys(text), waitTime=0.02)
        return "typed via SendKeys"
    except Exception as e:
        return f"type failed: {e}"


def _escape_sendkeys(text: str) -> str:
    # uiautomation SendKeys treats {}()+^%~ specially; escape them.
    out = []
    for ch in text:
        if ch in "{}()+^%~[]":
            out.append("{" + ch + "}")
        else:
            out.append(ch)
    return "".join(out)


def execute(cfg, action: dict, elements) -> ActionResult:
    kind = str(action.get("action", "")).lower()
    say = action.get("say", "")

    if kind == "done":
        return ActionResult(True, f"done: {say}", done=True)

    if kind == "wait":
        time.sleep(max(cfg.settle_seconds, 1.0))
        return ActionResult(True, f"waited ({say})")

    if cfg.dry_run:
        return ActionResult(True, f"DRY-RUN would do: {action}")

    if kind == "open_app":
        name = str(action.get("name") or action.get("text") or "").strip()
        if not name:
            return ActionResult(False, "open_app missing 'name'")
        return ActionResult(True, _launch_app(name))

    if kind == "type_text":
        text = str(action.get("text", ""))
        if not _HAS_PYAUTOGUI:
            return ActionResult(False, "type_text needs pyautogui")
        submit = text.endswith("\n")
        pyautogui.write(text.rstrip("\n"), interval=0.02)
        if submit:
            pyautogui.press("enter")
        return ActionResult(True, f"type_text {text[:40]!r}{' + enter' if submit else ''}")

    if kind == "hotkey":
        keys = str(action.get("keys", "")).strip()
        if not keys:
            return ActionResult(False, "hotkey missing 'keys'")
        try:
            if _HAS_PYAUTOGUI:
                parts = [p.strip().lower() for p in keys.replace("-", "+").split("+") if p.strip()]
                pyautogui.hotkey(*parts)
            else:
                auto.SendKeys(_keys_to_uia(keys))
            return ActionResult(True, f"hotkey {keys}")
        except Exception as e:
            return ActionResult(False, f"hotkey {keys} failed: {e}")

    if kind == "scroll":
        direction = str(action.get("dir", "down")).lower()
        try:
            n = 3
            if direction == "up":
                auto.WheelUp(n)
            else:
                auto.WheelDown(n)
            return ActionResult(True, f"scrolled {direction}")
        except Exception as e:
            return ActionResult(False, f"scroll failed: {e}")

    if kind in ("click", "type"):
        eid = action.get("id")
        if not isinstance(eid, int):
            try:
                eid = int(eid)
            except Exception:
                return ActionResult(False, f"{kind} missing valid integer 'id'")
        el = _find(elements, eid)
        if el is None:
            return ActionResult(False, f"no control with id {eid} this step")
        if kind == "click":
            if getattr(cfg, "real_cursor", False) and _HAS_PYAUTOGUI:
                cx = (el.rect[0] + el.rect[2]) // 2
                cy = (el.rect[1] + el.rect[3]) // 2
                pyautogui.click(cx, cy)
                how = f"real-click @({cx},{cy})"
            else:
                how = _click_control(el.control)
            return ActionResult(True, f"click [{eid}] {el.role} '{el.name[:40]}' -> {how}")
        else:
            text = str(action.get("text", ""))
            how = _type_control(el.control, text)
            return ActionResult(True, f"type [{eid}] <- {text[:40]!r} -> {how}")

    return ActionResult(False, f"unknown action: {action}")


def _keys_to_uia(keys: str) -> str:
    mapping = {"ctrl": "{Ctrl}", "control": "{Ctrl}", "alt": "{Alt}", "shift": "{Shift}",
               "win": "{Win}", "enter": "{Enter}", "return": "{Enter}", "tab": "{Tab}",
               "esc": "{Esc}", "escape": "{Esc}", "space": " "}
    parts = [p.strip().lower() for p in keys.replace("-", "+").split("+") if p.strip()]
    out = ""
    for p in parts:
        out += mapping.get(p, p)
    return out
