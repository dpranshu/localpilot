"""Web layer: right search box, no double typing, honest 'done'."""
import unittest
from unittest.mock import MagicMock, patch

from localpilot import actions, web
from localpilot.config import Config


class ParseTests(unittest.TestCase):
    def check(self, goal, browser, site, query, url_part=None, rest=""):
        p = web.parse(goal)
        self.assertIsNotNone(p, goal)
        self.assertEqual((p["browser"], p["site"], p["query"], p["rest"]), (browser, site, query, rest), goal)
        if url_part:
            self.assertIn(url_part, p["url"] or p["home"], goal)

    def test_site_search_goes_to_the_site_not_the_address_bar(self):
        self.check("Open Brave and search YouTube for navaan sandhu", "brave", "youtube",
                   "navaan sandhu", "youtube.com/results?search_query=navaan+sandhu")
        self.check("Open Brave, go to youtube.com and search for lofi music", "brave", "youtube",
                   "lofi music", "youtube.com/results")
        self.check("search lofi beats on youtube in brave", "brave", "youtube", "lofi beats")
        self.check("Search Amazon for wireless headphones", None, "amazon", "wireless headphones", "amazon.com/s?k=")
        self.check("search amazon.in for shoes", None, "amazon.in", "shoes", "amazon.in/s?k=shoes")

    def test_web_search_uses_the_browsers_engine(self):
        self.check("Open Brave and search the web for weather in Delhi", "brave", None,
                   "weather in Delhi", "search.brave.com")
        self.check("search the web for pizza using chrome", "chrome", None, "pizza", "google.com/search")
        self.check("open chrome and search for pizza near me", "chrome", None, "pizza near me")

    def test_unknown_site_uses_its_own_search_box(self):
        p = web.parse("go to example.org and search for test")
        self.assertEqual((p["url"], p["home"], p["query"]), (None, "https://example.org", "test"))

    def test_follow_up_after_search_is_kept(self):
        self.check("search cats on youtube and play the first video", None, "youtube", "cats",
                   rest="play the first video")

    def test_not_web_goals(self):
        for g in ("Open Notepad and type hello", "open brave and go to wikipedia.org",
                  "search for my files in documents", "open calculator"):
            self.assertIsNone(web.parse(g), g)

    def test_results_detection(self):
        self.assertTrue(web.query_in_title("lofi music", "(5) lofi music - YouTube - Brave"))
        self.assertFalse(web.query_in_title("lofi music", "YouTube - Brave"))
        with patch.object(web, "page_url", return_value="github.com/search?q=uiautomation&type=repositories"):
            self.assertTrue(web.results_open("uiautomation", "Repository search results - Brave", 1))
        with patch.object(web, "page_url", return_value="github.com/login"):
            self.assertFalse(web.results_open("uiautomation", "Sign in - Brave", 1))


class NoDoubleTypingTests(unittest.TestCase):
    def run_type(self, current_value, role="Edit"):
        ctrl = MagicMock()
        ctrl.HasKeyboardFocus = True
        ctrl.GetValuePattern.return_value.Value = current_value
        el = MagicMock(id=5, role=role, enabled=True, control=ctrl, value=current_value)
        calls = []
        with patch.object(actions, "_find", return_value=el), \
             patch.object(actions.inputs, "typewrite", side_effect=lambda t, interval=0: calls.append(("type", t))), \
             patch.object(actions.inputs, "hotkey", side_effect=lambda *k: calls.append(("hotkey", k))), \
             patch.object(actions.inputs, "press", side_effect=lambda k: calls.append(("press", k))):
            res = actions.execute(Config(), {"action": "type", "id": 5, "text": "lofi music\n"}, [el])
        return res, calls

    def test_text_already_there_only_presses_enter(self):
        res, calls = self.run_type("lofi music")             # autocomplete already filled it
        self.assertTrue(res.ok)
        self.assertEqual(calls, [("press", "enter")])

    def test_replaces_never_appends(self):
        res, calls = self.run_type("lofi")                   # partial / old text in the box
        self.assertEqual(calls, [("hotkey", ("ctrl", "a")), ("type", "lofi music"), ("press", "enter")])

    def test_hotkey_without_keys_for_address_bar(self):
        with patch.object(actions.inputs, "hotkey") as hk:
            res = actions.execute(Config(), {"action": "hotkey", "say": "Focus address bar"}, [])
        self.assertTrue(res.ok); hk.assert_called_with("ctrl", "l")


if __name__ == "__main__":
    unittest.main()
