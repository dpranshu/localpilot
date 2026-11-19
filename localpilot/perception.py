"""Perception: walk the Windows UI Automation (UIA) tree of the focused window
and turn it into a compact, numbered list of actionable controls.

This is the "set-of-marks" source: every control the agent can act on gets an
integer id. The model never sees pixels or coordinates -- just text like:
    [3] Button "Save"
    [7] Edit "Address" value="google.com"
"""
from __future__ import annotations

import uiautomation as auto

# Control types we consider actionable / worth showing to the model.
ACTIONABLE = {
    "ButtonControl", "EditControl", "DocumentControl", "CheckBoxControl",
    "RadioButtonControl", "ComboBoxControl", "ListItemControl", "MenuItemControl",
    "TabItemControl", "HyperlinkControl", "TreeItemControl", "SplitButtonControl",
    "SliderControl", "MenuControl", "ToolBarControl", "TextControl",
}
# Text controls are noisy; only keep them if they look clickable/interactive.
NOISY_IF_TEXT = {"TextControl", "ToolBarControl", "MenuControl"}


class Element:
    """A single perceived control plus a live handle for acting on it."""

    __slots__ = ("id", "role", "name", "value", "rect", "enabled", "control")

    def __init__(self, id, role, name, value, rect, enabled, control):
        self.id = id
        self.role = role            # short role, e.g. "Button"
        self.name = name
        self.value = value          # text value for edit fields, else ""
        self.rect = rect            # (left, top, right, bottom)
        self.enabled = enabled
        self.control = control      # live uiautomation control (for actions)

    def to_line(self) -> str:
        parts = [f"[{self.id}] {self.role}"]
        if self.name:
            parts.append(f'"{self.name[:80]}"')
        if self.value:
            parts.append(f'value="{self.value[:60]}"')
        if not self.enabled:
            parts.append("(disabled)")
        return " ".join(parts)


def _short_role(control_type_name: str) -> str:
    return control_type_name.replace("Control", "") or control_type_name


def _get_value(control) -> str:
    try:
        vp = control.GetValuePattern()
        if vp:
            return (vp.Value or "").strip()
    except Exception:
        pass
    return ""


def _is_actionable(control) -> bool:
    ctn = control.ControlTypeName
    if ctn not in ACTIONABLE:
        return False
    name = (control.Name or "").strip()
    # Drop empty, nameless noise (toolbars/menus/text with no label).
    if ctn in NOISY_IF_TEXT and not name:
        return False
    if ctn == "TextControl":
        # Result / status displays (Calculator's "Display is 100", progress text) are
        # read-only but the model must SEE them, or it types blindly and never knows
        # it is finished. Keep them; drop other plain labels to keep the list short.
        try:
            aid = (control.AutomationId or "").lower()
        except Exception:
            aid = ""
        if name and any(k in aid for k in ("result", "display", "status", "output", "answer")):
            return True
        try:
            if not control.GetInvokePattern() and not control.GetSelectionItemPattern():
                return False
        except Exception:
            return False
    return True


OVERLAY_SENTINEL = "LocalPilotOverlay"


def get_foreground_window():
    """The window the user is currently in (never our own overlay)."""
    fg = auto.GetForegroundControl()
    # Walk up to the top-level window for a stable title.
    win = fg
    try:
        while win and win.ControlTypeName not in ("WindowControl", "PaneControl"):
            parent = win.GetParentControl()
            if parent is None:
                break
            win = parent
    except Exception:
        pass
    # Safety guard: if the foreground resolved to our own overlay, pick the
    # topmost real window beneath it instead.
    try:
        if win is not None and win.Name == OVERLAY_SENTINEL:
            desktop = auto.GetRootControl()
            for c in desktop.GetChildren():
                if (c.ControlTypeName in ("WindowControl", "PaneControl")
                        and c.Name != OVERLAY_SENTINEL and not c.IsOffscreen):
                    return c, c
    except Exception:
        pass
    return fg, win


def perceive(cfg) -> tuple[str, list[Element], str]:
    """Return (window_title, elements, rendered_text) for the focused window."""
    fg, win = get_foreground_window()
    title = ""
    try:
        title = (win.Name if win else fg.Name) or ""
    except Exception:
        title = ""

    root = win or fg
    if root is None:
        return title, [], "(no focused window found)"

    elements: list[Element] = []
    counter = [0]

    def walk(control, depth):
        if depth > cfg.max_tree_depth or counter[0] >= cfg.max_elements:
            return
        try:
            children = control.GetChildren()
        except Exception:
            children = []
        for child in children:
            if counter[0] >= cfg.max_elements:
                return
            try:
                offscreen = bool(child.IsOffscreen)
            except Exception:
                offscreen = False
            if offscreen and not cfg.include_offscreen:
                # still descend -- a visible child can live under an offscreen pane
                walk(child, depth + 1)
                continue
            try:
                if _is_actionable(child):
                    rect = child.BoundingRectangle
                    r = (rect.left, rect.top, rect.right, rect.bottom)
                    # skip zero-size controls
                    if r[2] - r[0] <= 0 or r[3] - r[1] <= 0:
                        walk(child, depth + 1)
                        continue
                    try:
                        enabled = bool(child.IsEnabled)
                    except Exception:
                        enabled = True
                    counter[0] += 1
                    el = Element(
                        id=counter[0],
                        role=_short_role(child.ControlTypeName),
                        name=(child.Name or "").strip(),
                        value=_get_value(child),
                        rect=r,
                        enabled=enabled,
                        control=child,
                    )
                    elements.append(el)
            except Exception:
                pass
            walk(child, depth + 1)

    walk(root, 0)

    lines = [e.to_line() for e in elements]
    rendered = "\n".join(lines) if lines else "(no actionable controls found)"
    return title, elements, rendered
