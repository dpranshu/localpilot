"""The brain: local answers, direct commands, plan clean-up (no model needed for these)."""
import unittest
from unittest.mock import patch

from localpilot import brain, assistant
from localpilot.config import Config


class LocalAnswerTests(unittest.TestCase):
    def test_maths_and_time_without_the_model(self):
        with patch.object(brain.requests, "post", side_effect=AssertionError("model called")):
            self.assertEqual(brain.think(Config(), "what's 18% of 2400")["reply"], "That's 432.")
            p = brain.think(Config(), "what time is it")
            self.assertEqual(p["intent"], "answer"); self.assertTrue(p["reply"].startswith("It's"))

    def test_direct_commands_skip_thinking(self):
        with patch.object(brain.requests, "post", side_effect=AssertionError("model called")):
            for u in ("Search YouTube for lofi beats", "Open Settings and go to the Display page",
                      "Open File Explorer and go to the Downloads folder", "open calculator"):
                p = brain.think(Config(), u)
                self.assertEqual((p["intent"], p["steps"], p["source"]), ("do", [u], "direct"), u)


class FixStepsTests(unittest.TestCase):
    def test_merges_notepad_steps_and_drops_unrequested_save(self):
        steps, _ = brain._fix_steps(["Open Notepad", "Type: Buy milk", "Save it as C:\\notes\\b.txt"],
                                    "ok", "write a quick note to buy milk")
        self.assertEqual(steps, ["Open Notepad and type: Buy milk"])

    def test_requested_save_is_merged(self):
        steps, _ = brain._fix_steps(["Open Notepad", "Type: hi", "Save it as C:\\x\\a.txt"],
                                    "ok", "write hi and save it as C:\\x\\a.txt")
        self.assertEqual(steps, ["Open Notepad, type hi, and save it as C:\\x\\a.txt"])

    def test_control_chars_removed(self):
        steps, _ = brain._fix_steps(["Search YouTube for la\x08fi"], "ok", "lofi")
        self.assertEqual(steps, ["Search YouTube for lafi"])

    def test_redundant_site_open_dropped(self):
        steps, _ = brain._fix_steps(["Open YouTube.com", "Search YouTube for Python tutorials"], "ok")
        self.assertEqual(steps, ["Search YouTube for Python tutorials"])

    def test_missing_app_uses_web_version(self):
        with patch("localpilot.apps.find", return_value=None):
            steps, reply = brain._fix_steps(["Open Spotify"], "Opening Spotify.")
        self.assertEqual(steps, ["Open open.spotify.com"])
        self.assertIn("Spotify isn't installed", reply)

    def test_how_to_question_is_answered_not_typed(self):
        with patch.object(brain, "answer", return_value="Boil water, add tea."):
            plan = {"intent": "do", "reply": "Sure", "thought": "",
                    "steps": ["Open Notepad and type: Boil water"]}
            out = brain._clean(plan, "how do I make tea")
            if out.pop("needs_answer", False):
                out["reply"] = brain.answer(Config(), "how do I make tea")
        self.assertEqual((out["intent"], out["steps"]), ("answer", []))


class AssistantTests(unittest.TestCase):
    def test_answer_opens_nothing(self):
        with patch.object(assistant.brain, "think", return_value={
                "intent": "answer", "thought": "", "reply": "J.K. Rowling.", "steps": []}), \
             patch.object(assistant, "run_agent", side_effect=AssertionError("acted")):
            self.assertEqual(assistant.handle("who wrote Harry Potter", Config()), "J.K. Rowling.")

    def test_stops_at_first_failed_step_and_says_why(self):
        from localpilot.agent import UnfinishedRun
        finals = iter(["done: ok", UnfinishedRun("stopped: no search box")])
        with patch.object(assistant.brain, "think", return_value={
                "intent": "do", "thought": "", "reply": "On it.", "steps": ["A step", "B step", "C step"]}), \
             patch.object(assistant, "run_agent", side_effect=lambda s, c: next(finals)):
            msg = assistant.handle("x", Config())
        self.assertIn("1 of 3", msg); self.assertIn("no search box", msg)


if __name__ == "__main__":
    unittest.main()
