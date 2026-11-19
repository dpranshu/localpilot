"""Fixes from the 15-task battery: settings pages, in-app search, folders, percent maths,
typing check that reads the editor, save path, first-result follow-ups, honest 'not installed'."""
import unittest
from unittest.mock import patch

from localpilot import actions, agent, llm, web
from localpilot.config import Config
from localpilot.perception import Element


class ParsersTests(unittest.TestCase):
    def test_settings_pages(self):
        self.assertEqual(actions.settings_page_from_goal("Open Settings and go to the Display page"),
                         "ms-settings:display")
        self.assertEqual(actions.settings_page_from_goal("open sound settings"), "ms-settings:sound")
        self.assertIsNone(actions.settings_page_from_goal("open settings and search for mouse"))

    def test_in_app_search(self):
        self.assertEqual(actions.app_search_from_goal("Open File Explorer, go to Downloads and search for pdf"),
                         ("File Explorer", "Downloads", "pdf"))
        self.assertEqual(actions.app_search_from_goal("Open Settings and search for mouse"),
                         ("Settings", "", "mouse"))
        self.assertIsNone(actions.app_search_from_goal("open brave and search for cats"))

    def test_folders(self):
        self.assertEqual(actions.folder_from_goal("Open File Explorer and go to the Downloads folder"), "downloads")
        self.assertIsNone(actions.folder_from_goal("Open File Explorer, go to Downloads and search for pdf"))

    def test_percent(self):
        self.assertEqual(llm.math_expression("work out 15 percent of 200"), "200*15/100")
        self.assertEqual(llm.math_answer("what is 15% of 200"), "30")

    def test_save_path(self):
        self.assertEqual(actions.save_path_from_goal(r"Open Notepad, type hi, and save it as C:\tmp\a b.txt"),
                         r"C:\tmp\a b.txt")
        self.assertIsNone(actions.save_path_from_goal("save the world"))

    def test_open_bare_domain(self):
        p = web.parse("Open youtube.com")
        self.assertEqual((p["query"], p["url"]), (None, "https://youtube.com"))

    def test_first_result_follow_ups(self):
        for r in ("play the first video", "open the article", "and open the first result"):
            self.assertTrue(web.wants_first_result(r), r)
        self.assertFalse(web.wants_first_result("like the video"))
        self.assertIn("go=Go", web.parse("Search Wikipedia for Mount Everest")["url"])


class TypingCheckTests(unittest.TestCase):
    def doc(self, value):
        return [Element(1, "Document", "Text editor", value, (0, 0, 1, 1), True, None)]

    def test_reads_the_editor_not_the_title(self):
        g = "Open Notepad and type a shopping list: milk, eggs, bread"
        self.assertEqual(llm.typed_items(g), ["milk", "eggs", "bread"])
        self.assertTrue(llm.typed_goal_met(g, self.doc("milk\reggs\rbread")))
        self.assertFalse(llm.typed_goal_met(g, self.doc("milk")))

    def test_save_clause_is_not_part_of_the_text(self):
        g = r"Open Notepad, type hello world, and save it as C:\x\y.txt"
        self.assertEqual(llm.typed_items(g), ["hello world"])

    def test_multiline_value_is_shown_whole(self):
        line = self.doc("milk\reggs\rbread")[0].to_line()
        self.assertIn("milk / eggs / bread", line)


class HonestNotInstalledTests(unittest.TestCase):
    def test_missing_app_stops_at_once(self):
        with patch.object(actions, "app_from_goal", return_value="Paint"), \
             patch.object(actions, "_launch_app", return_value=actions.ActionResult(False, "WinError 2")), \
             patch.object(agent, "perceive", return_value=("x", [], "")), \
             patch.object(agent.web, "parse", return_value=None), \
             patch.object(agent.llm, "ping", return_value=(True, "ok")), \
             patch.object(agent.llm, "decide", side_effect=AssertionError("model called")):
            final = agent._run_impl("Open Paint", Config(show_overlay=False, max_steps=3))
        self.assertIn("not seem to be installed", final)


if __name__ == "__main__":
    unittest.main()
