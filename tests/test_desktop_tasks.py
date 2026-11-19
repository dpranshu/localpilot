import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from localpilot import actions, agent, llm, vision
from localpilot.config import Config
from localpilot.notepad import NewNotepadText


def element(role, number):
    control = Mock()
    control.GetRuntimeId.return_value = [number]
    return SimpleNamespace(id=number, role=role, control=control, enabled=True)


class NotepadTests(unittest.TestCase):
    def setUp(self):
        self.task = NewNotepadText.from_goal(
            "Open a new blank Notepad document and type Hello. Stop when Hello is visible.")
        self.cfg = Config()
        self.tab = element("TabItem", 1)
        self.tab.control.GetSelectionItemPattern.return_value.IsSelected = True
        self.add = element("Button", 2)
        self.add.control.AutomationId = "AddButton"
        self.editor = element("Document", 3)
        self.value = self.editor.control.GetValuePattern.return_value
        self.value.IsReadOnly = False
        self.value.Value = "Existing draft"
        self.controls = [self.tab, self.add, self.editor]

    def step(self):
        return self.task.step("Draft - Notepad", self.controls, self.cfg, lambda cfg: None)

    def new_tab(self):
        self.step()
        self.tab.control.GetSelectionItemPattern.return_value.IsSelected = False
        new = element("TabItem", 4)
        new.control.GetSelectionItemPattern.return_value.IsSelected = True
        self.controls.append(new)
        self.value.Value = ""

    def test_restored_tab_never_written(self):
        self.step()
        with self.assertRaisesRegex(RuntimeError, "new Notepad tab"):
            self.step()
        self.value.SetValue.assert_not_called()

    def test_one_write_verified_next_step(self):
        self.new_tab()
        self.step()
        self.value.Value = "Hello"
        self.assertTrue(self.step()[1].done)
        self.value.SetValue.assert_called_once_with("Hello")

    def test_mismatch_fails(self):
        self.new_tab()
        self.step()
        with self.assertRaisesRegex(RuntimeError, "readback"):
            self.step()

    def test_nonempty_new_tab(self):
        self.new_tab()
        self.value.Value = "User is editing"
        with self.assertRaisesRegex(RuntimeError, "already contains"):
            self.step()
        self.value.SetValue.assert_not_called()

    def test_missing_new_button(self):
        self.controls.remove(self.add)
        with self.assertRaisesRegex(RuntimeError, "new-tab button"):
            self.step()

    def test_read_only(self):
        self.new_tab()
        self.value.IsReadOnly = True
        with self.assertRaisesRegex(RuntimeError, "writable"):
            self.step()

    def test_focus_loss(self):
        self.new_tab()
        with self.assertRaisesRegex(RuntimeError, "lost focus"):
            self.task.step("Another app", [], self.cfg, lambda cfg: None)
        self.value.SetValue.assert_not_called()

    def test_ambiguous_editor(self):
        self.new_tab()
        self.controls.append(element("Edit", 5))
        with self.assertRaisesRegex(RuntimeError, "unique"):
            self.step()

    def test_changed_selected_tab(self):
        self.new_tab()
        self.step()
        self.controls[-1].control.GetRuntimeId.return_value = [10]
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.step()

    def test_other_requests_use_general_agent(self):
        for goal in ("Open Notepad and edit my draft", "Open a new Notepad tab and type Hello and save it",
                     "Open a new Notepad tab and type Hello. Stop when World is visible."):
            with self.subTest(goal=goal):
                self.assertIsNone(NewNotepadText.from_goal(goal))

    def test_quoted_content(self):
        task = NewNotepadText.from_goal('Create a new tab in Notepad and write "Hello, दुनिया!"')
        self.assertEqual(task.text, "Hello, दुनिया!")


class ExecutionTests(unittest.TestCase):
    def type_in(self, el):
        return actions.execute(Config(), {"action":"type", "id":el.id,"text":"Hello"}, [el])

    def test_failed_typing_reports_failure(self):
        el = element("Edit", 1)
        el.control.GetValuePattern.return_value = None
        el.control.SendKeys.side_effect = RuntimeError("editor unavailable")
        self.assertFalse(self.type_in(el).ok)

    def test_lost_focus_blocks_fallback_keys(self):
        el = element("Edit", 1)
        el.control.GetValuePattern.return_value = None
        el.control.HasKeyboardFocus = False
        self.assertFalse(self.type_in(el).ok)
        el.control.SendKeys.assert_not_called()

    def test_disabled_target(self):
        el = element("Edit", 1)
        el.enabled = False
        self.assertFalse(self.type_in(el).ok)

    def test_cannot_type_into_menu(self):
        el = element("MenuItem", 1)
        el.control.GetValuePattern.return_value = None
        self.assertFalse(self.type_in(el).ok)

    def test_missing_id(self):
        self.assertFalse(actions.execute(Config(), {"action":"type","id":42}, []).ok)

    def test_dry_run_no_launch(self):
        with patch.object(actions, "_launch_app") as launch:
            self.assertTrue(actions.execute(Config(dry_run=True), {"action":"open_app","name":"Notepad"}, []).ok)
            launch.assert_not_called()

    def test_failed_app_launch(self):
        with patch.object(actions._sp,"Popen",side_effect=OSError("unavailable")):
            self.assertFalse(actions.execute(Config(), {"action":"open_app","name":"notepad"}, []).ok)

    def test_unavailable_model(self):
        with patch.object(agent, "Logger"), patch.object(llm, "ping", return_value=(False, "unavailable")):
            self.assertIsInstance(agent._run_impl("a general task",Config()), agent.UnfinishedRun)

    def test_step_limit(self):
        with patch.object(agent, "Logger"), patch.object(llm, "ping", return_value=(True, "test")), patch("time.sleep"):
            self.assertIsInstance(agent._run_impl("a general task",Config(max_steps=0,show_overlay=False)), agent.UnfinishedRun)

    def test_vision_dry_run(self):
        with patch("localpilot.inputs.click") as click:
            result = vision.execute_vision(Config(dry_run=True), {"action":"click","xy":(1,2)})
            self.assertIn("DRY-RUN", result)
            click.assert_not_called()
