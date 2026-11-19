import json
import os
import tempfile
import unittest
from unittest.mock import patch

from localpilot import agent, memory
from localpilot.agent import UnfinishedRun
from localpilot.config import Config
from localpilot.memory import Memory, outcome_of, summarize_event
from localpilot.prompts import build_user_message


class Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "sub", "memory.json")
        self.mem = Memory(self.path)

    def tearDown(self):
        self.dir.cleanup()


class StoreTests(Tmp):
    def test_record_persist_order_delete_clear(self):
        a = self.mem.add_run("open brave", "done: ok", ["open_app Brave"])
        self.mem.add_run("type hello in notepad", UnfinishedRun("max steps reached"), [])
        again = Memory(self.path)                       # survives a restart
        runs = again.runs()
        self.assertEqual([r["goal"] for r in runs], ["type hello in notepad", "open brave"])
        self.assertEqual([r["outcome"] for r in runs], ["failed", "success"])
        again.delete(a["id"])
        self.assertEqual(len(again.runs()), 1)
        again.clear()
        self.assertEqual(again.runs(), [])

    def test_cap_and_preferences(self):
        with patch.object(memory, "MAX_RUNS", 5):
            for i in range(9):
                self.mem.add_run(f"task {i}", "done", [])
        self.assertEqual([r["goal"] for r in self.mem.runs()][0], "task 8")
        self.assertEqual(len(self.mem.runs()), 5)
        self.mem.set_preferences("  My browser is Brave.  ")
        self.assertEqual(Memory(self.path).preferences(), "My browser is Brave.")

    def test_corrupt_file_is_kept_aside_not_fatal(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(self.mem.runs(), [])
        self.mem.add_run("x", "done", [])
        self.assertEqual(len(self.mem.runs()), 1)
        self.assertTrue(any(n.startswith("memory.json.corrupt") for n in os.listdir(os.path.dirname(self.path))))

    def test_secrets_never_stored(self):
        self.mem.add_run("pay", "done: card 4111 1111 1111 1111 used", ["type_text 4111111111111111"])
        with open(self.path, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("4111", raw)

    def test_outcomes(self):
        self.assertEqual(outcome_of("done: typed it"), "success")
        self.assertEqual(outcome_of(UnfinishedRun("stopped by user")), "stopped")
        self.assertEqual(outcome_of(UnfinishedRun("stopped: you declined -- pay")), "stopped")
        self.assertEqual(outcome_of(UnfinishedRun("stopped: repeated the same action")), "failed")


class RecallTests(Tmp):
    def test_similar_tasks_recalled_unrelated_ignored(self):
        self.mem.add_run("Open Brave and play Navaan Sandhu on YouTube", "done: playing",
                         ["open_app Brave", "hotkey ctrl+l", "type_text youtube.com/results?search_query=navaan sandhu",
                          "click Kath Lagda"])
        self.mem.add_run("type hello into notepad", "done", ["type hello"])
        rel = self.mem.relevant("play Navaan Sandhu songs on youtube in brave")
        self.assertEqual([r["goal"] for r in rel], ["Open Brave and play Navaan Sandhu on YouTube"])
        self.assertEqual(self.mem.relevant("check the weather in settings"), [])

    def test_context_has_notes_recipe_and_failures(self):
        self.mem.set_preferences("My browser is Brave.")
        self.mem.add_run("play arijit singh on youtube", "done", ["open_app Brave", "click video"])
        self.mem.add_run("play arijit singh on youtube", UnfinishedRun("stopped: repeated the same action"),
                         ["hotkey ctrl+l", "hotkey ctrl+l"])
        ctx = self.mem.context_for("play arijit singh songs on youtube")
        self.assertIn("My browser is Brave.", ctx)
        self.assertIn("SUCCEEDED in 2 steps: open_app Brave -> click video", ctx)
        self.assertIn("did NOT finish", ctx)
        self.assertEqual(Memory(os.path.join(self.dir.name, "empty.json")).context_for("anything"), "")


class EventTests(unittest.TestCase):
    def test_summaries(self):
        self.assertEqual(summarize_event({"event": "action", "action": {"action": "open_app", "name": "Brave"}}),
                         "open_app Brave")
        self.assertEqual(summarize_event({"event": "action", "action": {"action": "hotkey", "keys": "ctrl+l"}}),
                         "hotkey ctrl+l")
        self.assertIsNone(summarize_event({"event": "action", "action": {"action": "done"}}))
        self.assertEqual(summarize_event({"event": "action", "action": {
            "action": "type_text", "text": "4111 1111 1111 1111"}}), "type_text [hidden]")
        self.assertIn("skipped by user", summarize_event({"event": "harness", "decision": "skip", "note": "pay"}))
        self.assertIsNone(summarize_event({"event": "step"}))

    def test_prompt_includes_context_and_history_len(self):
        msg = build_user_message("g", "W", "[1] Button", [f"s{i}" for i in range(10)],
                                 context="About the user: likes Brave", history_len=3)
        self.assertIn("PERSONAL CONTEXT", msg)
        self.assertIn("likes Brave", msg)
        self.assertIn("s9", msg); self.assertNotIn("s6", msg)
        self.assertNotIn("PERSONAL CONTEXT", build_user_message("g", "W", "", []))


class RunRecordingTests(Tmp):
    def fake_run(self, final=None, raise_exc=None):
        seen = {}
        def impl(goal, cfg):
            seen["context"] = cfg.user_context
            log = cfg.log_callback or (lambda rec: None)       # like Logger: optional
            log({"event": "action", "action": {"action": "open_app", "name": "Brave"}})
            log({"event": "harness", "decision": "allow", "note": "type into the field 'Password'"})
            log({"event": "action", "action": {"action": "type", "id": 3, "text": "hunter2"}})
            if raise_exc:
                raise raise_exc
            return final
        return impl, seen

    def test_run_injects_context_records_steps_and_forwards_events(self):
        self.mem.add_run("open brave and log in", "done", ["open_app Brave"])
        forwarded = []
        cfg = Config(memory_path=self.path, log_callback=forwarded.append)
        impl, seen = self.fake_run("done: logged in")
        with patch.object(agent, "_run_impl", side_effect=impl):
            self.assertEqual(agent.run("open brave and log in please", cfg), "done: logged in")
        self.assertIn("open brave and log in", seen["context"])
        newest = Memory(self.path).runs()[0]
        self.assertEqual(newest["steps"], ["open_app Brave", "type [hidden]"])   # password hidden
        with open(self.path, encoding="utf-8") as f:
            self.assertNotIn("hunter2", f.read())
        self.assertEqual(len(forwarded), 3)                     # GUI still gets every event
        self.assertEqual(cfg.user_context, "")                  # caller's config untouched

    def test_crash_is_still_recorded_and_memory_can_be_off(self):
        impl, _ = self.fake_run(raise_exc=RuntimeError("boom"))
        with patch.object(agent, "_run_impl", side_effect=impl), self.assertRaises(RuntimeError):
            agent.run("crashy task", Config(memory_path=self.path))
        self.assertEqual(Memory(self.path).runs()[0]["outcome"], "failed")
        impl, seen = self.fake_run("done")
        with patch.object(agent, "_run_impl", side_effect=impl):
            agent.run("crashy task", Config(memory_path=self.path, use_memory=False))
        self.assertEqual(seen["context"], "")
        self.assertEqual(len(Memory(self.path).runs()), 1)       # nothing new recorded


class GuiHistoryTests(Tmp):
    def test_history_window_lists_and_reruns(self):
        from localpilot.gui import App
        self.mem.add_run("open brave", "done", ["open_app Brave"])
        self.mem.add_run("type hi in notepad", UnfinishedRun("stopped by user"), [])
        app = App()
        app.memory = Memory(self.path)
        try:
            app.open_history()
            items = app.hist_list.get(0, "end")
            self.assertEqual(len(items), 2)
            self.assertTrue(items[0].startswith("--") and "type hi in notepad" in items[0])
            self.assertIn("2 tasks, 1 succeeded", app.hist_count.cget("text"))
            app.hist_list.selection_set(1); app._show_run()
            self.assertIn("open_app Brave", app.hist_detail.get("1.0", "end"))
            with patch.object(app, "on_run") as on_run:
                app.run_again()
            on_run.assert_called_once()
            self.assertEqual(app.task_text(), "open brave")
        finally:
            app.root.destroy()


if __name__ == "__main__":
    unittest.main()


class FixTests(unittest.TestCase):
    def test_compact_collapses_flailing(self):
        from localpilot.memory import compact
        s = compact(["open_app Brave", "hotkey ctrl+l", "type a", "type b", "type c",
                     "type_text x", "type_text y", "hotkey ctrl+l"], 3)
        self.assertEqual(s, "open_app Brave -> hotkey ctrl+l -> type a (x3) -> ... (2 more)")

    def test_type_into_button_fails_cleanly_not_crash(self):
        from types import SimpleNamespace
        from localpilot import actions
        class ButtonLike:                     # like uiautomation ButtonControl: no GetValuePattern
            pass
        el = SimpleNamespace(id=4, role="Button", name="Go", enabled=True, rect=(0, 0, 1, 1),
                             control=ButtonLike())
        with patch.object(actions, "pyautogui") as pg:              # Button: keys go to the app
            res = actions.execute(Config(), {"action": "type", "id": 4, "text": "25*4="}, [el])
        self.assertTrue(res.ok)
        pg.write.assert_called_once_with("25*4=", interval=0.03)
        menu = SimpleNamespace(**{**el.__dict__, "role": "Menu"})  # menus: never stray keys
        with patch.object(actions, "pyautogui") as pg:
            res = actions.execute(Config(), {"action": "type", "id": 4, "text": "x"}, [menu])
        self.assertFalse(res.ok)
        pg.write.assert_not_called()

    def test_goal_check_ends_run_only_when_met(self):
        from contextlib import ExitStack
        from localpilot.actions import ActionResult
        titles = iter(["Start", "YouTube results", "YouTube results", "YouTube results"])
        for met in (True, False):
            with self.subTest(met=met), ExitStack() as st:
                def m(o, n, **k):
                    return st.enter_context(patch.object(o, n, **k))
                titles = iter(["Start", "YouTube results", "YouTube results", "YouTube results"])
                m(agent, "Logger"); m(agent, "_key_down", return_value=False)
                m(agent.time, "sleep"); m(agent, "_drive_cursor")
                m(agent, "perceive", side_effect=lambda cfg: (next(titles), [], ""))
                m(agent.llm, "ping", return_value=(True, "OK"))
                m(agent.llm, "decide", return_value={"action": "hotkey", "keys": "enter"})
                chk = m(agent.llm, "check_goal", return_value=(met, "results are shown"))
                ex = m(agent.actions, "execute", return_value=ActionResult(True, "ok"))
                final = agent._run_impl("search youtube", Config(show_overlay=False, max_steps=4))
                if met:
                    self.assertEqual(final, "done: results are shown")
                    self.assertEqual(ex.call_count, 1)        # stopped right after success
                else:
                    self.assertIsInstance(final, UnfinishedRun)
                chk.assert_called_once()                       # only when the title changed


class GoalGuardTests(unittest.TestCase):
    """Real false positives seen live on 2026-11-19 must be rejected; real successes kept."""
    def test_cases(self):
        from localpilot.llm import goal_terms_on_screen as g
        karan = "(5) KARAN AUJLA MEGA MASHUP - Ultimate Mega Mashup - YouTube - Brave"
        self.assertFalse(g("Open Brave and search YouTube for Navaan Sandhu", karan, "[1] Button \"Like\""))
        self.assertFalse(g("search youtube for navaan sandhu songs",
                           "navaan sandhu songs - Brave Search - Brave", ""))
        self.assertTrue(g("Open Brave and search YouTube for Navaan Sandhu",
                          "navaan sandhu - YouTube - Brave", ""))
        self.assertTrue(g("Play the first Navaan Sandhu video on YouTube",
                          "Kath Lagda - Navaan Sandhu - YouTube - Brave", ""))
        self.assertTrue(g("Type hello into Notepad", "*hello - Notepad", '[1] Document value="hello"'))
        self.assertFalse(g("Type hello into Notepad", "Untitled - Notepad", '[1] Document value=""'))

    def test_model_yes_is_ignored_when_guard_fails(self):
        from localpilot import llm
        with patch.object(llm.requests, "post") as post:
            done, why = llm.check_goal(Config(), "search youtube for navaan sandhu",
                                       "KARAN AUJLA - YouTube", "")
        self.assertFalse(done)
        post.assert_not_called()                 # no model call wasted either


class DoneAndFocusTests(unittest.TestCase):
    def test_missing_terms_rules(self):
        from localpilot.llm import goal_missing_terms as miss
        self.assertEqual(miss("buy the thing", "Order placed - Shop", ""), [])            # verbs/vague ignored
        self.assertEqual(miss("close notepad", "Desktop", ""), [])                        # removal goals skipped
        self.assertEqual(miss("search youtube for navaan sandhu", "Save As", ""),
                         ["navaan", "sandhu", "youtube"])

    def test_model_done_rejected_when_goal_not_visible(self):
        from contextlib import ExitStack
        from localpilot.actions import ActionResult
        with ExitStack() as st:
            def m(o, n, **k):
                return st.enter_context(patch.object(o, n, **k))
            m(agent, "Logger"); m(agent, "_key_down", return_value=False)
            m(agent.time, "sleep"); m(agent, "_drive_cursor")
            m(agent, "_fg_window", return_value=(1, 100))
            m(agent, "perceive", return_value=("Save As", [], "[1] Button \"Save\""))
            m(agent.llm, "ping", return_value=(True, "OK"))
            decide = m(agent.llm, "decide", return_value={"action": "done", "say": "searched"})
            m(agent.actions, "execute", return_value=ActionResult(True, "done: searched", done=True))
            final = agent._run_impl("search youtube for navaan sandhu", Config(show_overlay=False, max_steps=5))
        self.assertIsInstance(final, UnfinishedRun)
        self.assertIn("not visible on screen", final)
        self.assertEqual(decide.call_count, 2)          # one retry with feedback, then honest stop
        self.assertTrue(any("does not show" in h for h in decide.call_args_list[1].args[4]))

    def test_focus_drift_never_acts_in_other_app(self):
        from contextlib import ExitStack
        from localpilot.actions import ActionResult
        fg = iter([(1, 100), (2, 200), (2, 200), (2, 200), (2, 200)])   # brave(100) then claude(200)
        with ExitStack() as st:
            def m(o, n, **k):
                return st.enter_context(patch.object(o, n, **k))
            m(agent, "Logger"); m(agent, "_key_down", return_value=False)
            m(agent.time, "sleep"); m(agent, "_drive_cursor")
            m(agent, "_fg_window", side_effect=lambda: next(fg))
            m(agent, "_top_windows", return_value={1, 2})     # claude(2) already existed
            restore = m(agent, "_restore_focus", return_value=True)
            m(agent, "perceive", return_value=("Window", [], ""))
            m(agent.llm, "ping", return_value=(True, "OK"))
            m(agent.llm, "check_goal", return_value=(False, ""))
            m(agent.llm, "decide", return_value={"action": "type_text", "text": "hi"})
            ex = m(agent.actions, "execute", return_value=ActionResult(True, "typed"))
            final = agent._run_impl("type hi", Config(show_overlay=False, max_steps=5))
        self.assertEqual(ex.call_count, 1)              # only the step in the original app ran
        self.assertGreaterEqual(restore.call_count, 1)  # it tried to switch back
        self.assertIn("focus left the target app", final)

    def test_typed_text_never_looks_cut_off(self):
        from localpilot.actions import _shown
        url = "https://www.youtube.com/results?search_query=Navaan Sandhu\n"
        s = _shown(url)
        self.assertIn("search_query=Navaan Sandhu'", s)
        self.assertIn("all 58 chars typed", s)


class ClickNameTests(unittest.TestCase):
    def test_click_history_uses_button_name(self):
        rec = {"event": "action", "target": "Search",
               "action": {"action": "click", "id": 38, "say": "Clicking Search to proceed"}}
        self.assertEqual(summarize_event(rec), "click Search")


class TypoToleranceTests(unittest.TestCase):
    def test_user_typos_and_joined_words(self):
        from localpilot.llm import goal_missing_terms as miss
        goal = "open brave search in wikipidia elon musk and find there networth"   # real user goal
        page = ("Elon Musk - Wikipedia - Brave", '[1] Text "Net worth US$ 849.7 billion"')
        self.assertEqual(miss(goal, *page), [])
        self.assertNotEqual(miss(goal, "Jeff Bezos - Wikipedia - Brave", '[1] Text "Net worth"'), [])
        # still strict on the real false positive
        self.assertNotEqual(miss("search youtube for navaan sandhu",
                                 "KARAN AUJLA MEGA MASHUP - YouTube - Brave", ""), [])


class NewWindowTests(unittest.TestCase):
    def test_window_created_by_our_action_is_followed(self):
        from contextlib import ExitStack
        from localpilot.actions import ActionResult
        fg = iter([(1, 100), (3, 300), (3, 300)])          # win+e creates Explorer (hwnd 3)
        with ExitStack() as st:
            def m(o, n, **k):
                return st.enter_context(patch.object(o, n, **k))
            m(agent, "Logger"); m(agent, "_key_down", return_value=False)
            m(agent.time, "sleep"); m(agent, "_drive_cursor")
            m(agent, "_fg_window", side_effect=lambda: next(fg))
            m(agent, "_top_windows", return_value={1, 2})     # hwnd 3 did not exist before
            restore = m(agent, "_restore_focus", return_value=True)
            m(agent, "perceive", return_value=("Window", [], ""))
            m(agent.llm, "ping", return_value=(True, "OK"))
            m(agent.llm, "check_goal", return_value=(False, ""))
            m(agent.llm, "decide", side_effect=[{"action": "hotkey", "keys": "win+e"},
                                                {"action": "hotkey", "keys": "ctrl+l"},
                                                {"action": "done", "say": "ok"}])
            ex = m(agent.actions, "execute", side_effect=[ActionResult(True, "k"), ActionResult(True, "k"),
                                                          ActionResult(True, "done", done=True)])
            agent._run_impl("go somewhere", Config(show_overlay=False, max_steps=3, verify_goal=False))
        self.assertEqual(ex.call_count, 3)              # kept working in the new window
        restore.assert_not_called()

    def test_goal_parsing_and_auto_open(self):
        from localpilot.actions import app_from_goal
        self.assertEqual(app_from_goal("Open Calculator and calculate 25 times 4"), "Calculator")
        self.assertEqual(app_from_goal("Open File Explorer and go to the Downloads folder"), "File Explorer")
        self.assertIsNone(app_from_goal("open my resume.docx and fix typos"))
        self.assertIsNone(app_from_goal("search youtube for x"))


class Round3FixTests(unittest.TestCase):
    def test_guard_accepts_real_round3_successes(self):
        from localpilot.llm import goal_missing_terms as miss
        self.assertEqual(miss("Open File Explorer and go to the Downloads folder",
                              "Downloads - File Explorer", ""), [])
        self.assertEqual(miss("Open Calculator and calculate 25 times 4",
                              "Calculator", '[1] Text "Display is 100"'), [])
        self.assertNotEqual(miss("Open File Explorer and go to the Downloads folder",
                                 "Home - File Explorer", ""), [])

    def test_result_display_is_perceived(self):
        from localpilot.perception import _is_actionable
        class C:
            ControlTypeName = "TextControl"; Name = "Display is 100"; AutomationId = "CalculatorResults"
            def GetInvokePattern(self): return None
            def GetSelectionItemPattern(self): return None
        self.assertTrue(_is_actionable(C()))
        C.AutomationId = "SomeLabel"
        self.assertFalse(_is_actionable(C()))


class MathCheckTests(unittest.TestCase):
    def test_math_answer_and_exact_check(self):
        from localpilot import llm
        self.assertEqual(llm.math_answer("Open Calculator and calculate 25 times 4"), "100")
        self.assertEqual(llm.math_answer("what is 7 divided by 2"), "3.5")
        self.assertEqual(llm.math_answer("1200 plus 34"), "1234")
        self.assertIsNone(llm.math_answer("open brave"))
        cfg = Config()
        with patch.object(llm.requests, "post") as post:
            self.assertTrue(llm.check_goal(cfg, "Open Calculator and calculate 25 times 4",
                                           "Calculator", '[3] Text "Display is 100"')[0])
            self.assertFalse(llm.check_goal(cfg, "Open Calculator and calculate 25 times 4",
                                            "Calculator", '[3] Text "Display is 1,700"')[0])
            self.assertTrue(llm.check_goal(cfg, "calculate 1200 plus 34",
                                           "Calculator", '[3] Text "Display is 1,234"')[0])
        post.assert_not_called()                     # exact, no model call
