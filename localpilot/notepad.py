"""Handle a literal writing request in a verified new Windows Notepad tab."""
import re

from . import actions


# Only take over unambiguous, single-purpose requests. Quoting preserves punctuation.
_REQUEST = re.compile(
    r'(?:open|create|start) (?:a )?new (?:blank )?(?:Notepad (?:document|tab|file)|'
    r'(?:document|tab|file) in Notepad) and (?:type|write) '
    r'(?:"([^"]+)"|([\w-]+))(?:\.|!)?(?:\s+stop when (.+?) is visible\.?)?',
    re.IGNORECASE,
)


class NewNotepadText:
    @classmethod
    def from_goal(cls, goal):
        match = _REQUEST.fullmatch(goal.strip())
        if not match:
            return None
        text = match[1] or match[2]
        if match[3] and match[3].strip('"') != text:
            return None
        return cls(text)

    def __init__(self, text):
        self.text = text
        self.launched = False
        self.old_tabs = None
        self.tab = None
        self.written = False

    def step(self, title, elements, cfg, check_stop):
        """Return (action, result). All edits use scoped UIA patterns."""
        if not title.endswith(" - Notepad"):
            if self.old_tabs is not None:
                raise RuntimeError("Notepad lost focus")
            if not self.launched:
                self.launched = True
                action = {"action": "open_app", "name": "Notepad"}
                check_stop(cfg)
                result = actions.execute(cfg, action, [])
                if not result.ok:
                    raise RuntimeError(result.detail)
                return action, result
            return {"action": "wait"}, actions.ActionResult(True, "Waiting for Notepad")

        tabs = [e.control for e in elements if e.role == "TabItem"]
        if self.old_tabs is None:
            button = next((e.control for e in elements if e.role == "Button"
                           and e.control.AutomationId == "AddButton"), None)
            if not tabs or button is None:
                raise RuntimeError("Cannot identify the Notepad new-tab button")
            self.old_tabs = {tuple(t.GetRuntimeId()) for t in tabs}
            check_stop(cfg)
            button.GetInvokePattern().Invoke()
            return {"action": "click"}, actions.ActionResult(True, "Opened a new Notepad tab")

        selected = [tuple(t.GetRuntimeId()) for t in tabs if t.GetSelectionItemPattern().IsSelected]
        if len(selected) != 1 or selected[0] in self.old_tabs:
            raise RuntimeError("Could not verify that the new Notepad tab is selected")
        if self.tab is not None and self.tab != selected[0]:
            raise RuntimeError("The selected Notepad tab changed")
        self.tab = selected[0]
        editors = [e.control for e in elements if e.role in ("Document", "Edit")]
        if len(editors) != 1:
            raise RuntimeError("Cannot identify a unique Notepad editor")
        value = editors[0].GetValuePattern()
        if not value or value.IsReadOnly:
            raise RuntimeError("Cannot verify a writable Notepad editor")
        if self.written:
            if value.Value != self.text:
                raise RuntimeError("Notepad readback differs from the requested text")
            return {"action": "done"}, actions.ActionResult(True, "Verified text in the new Notepad tab", done=True)
        if value.Value:
            raise RuntimeError("The new Notepad tab already contains text")
        check_stop(cfg)
        value.SetValue(self.text)
        self.written = True
        return {"action": "type"}, actions.ActionResult(True, "Typed into the empty Notepad tab")
