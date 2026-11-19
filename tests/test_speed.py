"""Fast paths: installed-app lookup, open-only goals, observer, model URL."""
import time
import unittest
from unittest.mock import patch

from localpilot import actions, agent, apps
from localpilot.config import Config

APPS = [{"name": n, "id": n.replace(" ", ".")} for n in (
    "Calculator", "Google Chrome", "Brave", "Visual Studio Code", "Photos", "Settings",
    "Uninstall Brave", "Git FAQs (Frequently Asked Questions)", "Git CMD", "Spotify")]


class AppIndexTests(unittest.TestCase):
    def test_find(self):
        cases = {"calculator": "Calculator", "Calculater": "Calculator", "chrome": "Google Chrome",
                 "vs code": "Visual Studio Code", "the photos app": "Photos", "brave": "Brave",
                 "spotify": "Spotify", "git faqs": None, "xyzzy": None, "": None}
        for q, want in cases.items():
            got = apps.find(q, APPS)
            self.assertEqual(got and got["name"], want, q)

    def test_never_picks_uninstaller(self):
        self.assertEqual(apps.find("uninstall brave", APPS), None)


class OpenOnlyTests(unittest.TestCase):
    def test_is_only_open(self):
        yes = ["open calculator", "Open Brave.", "please launch the photos app", "start Spotify"]
        no = ["open brave and play music", "open notepad then type hi", "open chrome to youtube",
              "open settings for bluetooth", "type hello", "search cats"]
        for g in yes:
            self.assertTrue(actions.is_only_open(g), g)
        for g in no:
            self.assertFalse(actions.is_only_open(g), g)

    def test_open_only_goal_never_calls_the_model(self):
        with patch.object(actions, "app_from_goal", return_value="Calculator"), \
             patch.object(actions, "_launch_app", return_value=actions.ActionResult(True, "launched")), \
             patch.object(agent, "_wait_for_app", return_value=True), \
             patch.object(agent, "perceive", return_value=("x", [], "")), \
             patch.object(agent.llm, "ping", return_value=(True, "ok")), \
             patch.object(agent.llm, "decide", side_effect=AssertionError("model called")), \
             patch.object(agent.llm, "warm") as warm:
            final = agent._run_impl("open calculator", Config(show_overlay=False, max_steps=3))
        self.assertTrue(final.startswith("done: opened"), final)
        warm.assert_not_called()


class BrowserFastPathTests(unittest.TestCase):
    def test_url_and_visit_goals(self):
        self.assertEqual(actions.url_from_goal("Open Brave and go to wikipedia.org"), "https://wikipedia.org")
        self.assertEqual(actions.url_from_goal("open notepad and type hi"), None)
        self.assertTrue(actions.is_open_and_visit("Open Brave and go to wikipedia.org"))
        self.assertTrue(actions.is_open_and_visit("open chrome and visit https://news.ycombinator.com/"))
        self.assertFalse(actions.is_open_and_visit("open brave and go to wikipedia.org and search cats"))
        self.assertEqual(agent._site_word("https://www.wikipedia.org/wiki/X"), "wikipedia")
        self.assertTrue(agent._BROWSER_TITLE.search("YouTube - Brave"))
        self.assertFalse(agent._BROWSER_TITLE.search("Untitled - Notepad"))


class ObserverTests(unittest.TestCase):
    def test_settle_returns_early_when_screen_changes(self):
        seq = iter([("w", "a")] + [("w", "b")] * 50)
        with patch.object(agent, "_ui_signature", side_effect=lambda: next(seq)):
            t = time.time(); agent._settle(Config(settle_seconds=2.0), ("w", "a"))
        self.assertLess(time.time() - t, 1.0)

    def test_settle_caps_at_settle_seconds(self):
        with patch.object(agent, "_ui_signature", return_value=("same",)):
            t = time.time(); agent._settle(Config(settle_seconds=0.5), ("same",))
        self.assertLess(time.time() - t, 0.8)


class UrlTests(unittest.TestCase):
    def test_localhost_becomes_ipv4(self):     # 'localhost' costs ~2 s per call on Windows
        self.assertEqual(Config().ollama_url, "http://127.0.0.1:11434")
        self.assertEqual(Config(ollama_url="http://localhost:1").ollama_url, "http://127.0.0.1:1")


if __name__ == "__main__":
    unittest.main()
