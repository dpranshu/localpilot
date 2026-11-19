import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from localpilot import agent, vision
from localpilot.actions import ActionResult
from localpilot.config import Config
from localpilot.harness import ALLOW, SKIP, STOP, Harness, classify


def el(i, name, role="Button"):
    return SimpleNamespace(id=i, name=name, role=role, value="", enabled=True,
                           rect=(10, 10, 60, 30), control=Mock())


def click(i, say=""):
    return {"action": "click", "id": i, "say": say}


class ClassifierTests(unittest.TestCase):
    def assertLevel(self, action, elements, level):
        got = classify(action, elements)[0]
        self.assertEqual(got, level, f"{action} on {[e.name for e in elements or []]} -> {got}")

    def test_sensitive_buttons_need_confirmation(self):
        for name in ("Pay", "Pay now", "Buy now", "Place your order", "Proceed to checkout",
                     "Send", "Delete", "Delete permanently", "Remove from cart",
                     "Empty Recycle Bin", "Publish", "Post", "Sign out", "Subscribe",
                     "Uninstall", "Confirm purchase"):
            with self.subTest(name=name):
                self.assertLevel(click(1), [el(1, name)], "confirm")

    def test_everyday_buttons_stay_quiet(self):
        # Notepad's Format menu, Reset, Share sheets etc. must NOT nag the user
        for name in ("File", "Format", "Edit", "Reset zoom", "Share", "Settings",
                     "Search", "Videos", "Help", "PayPal", "Sender info", "Postcode",
                     "Back", "Comments", "Show more"):
            with self.subTest(name=name):
                self.assertLevel(click(1), [el(1, name)], "safe")

    def test_credentials_and_card_numbers(self):
        self.assertLevel({"action": "type", "id": 1, "text": "hunter2"},
                         [el(1, "Password", "Edit")], "confirm")
        self.assertLevel({"action": "type", "id": 1, "text": "x"},
                         [el(1, "Card number", "Edit")], "confirm")
        self.assertLevel({"action": "type_text", "text": "4111 1111 1111 1111"}, [], "confirm")
        self.assertLevel({"action": "type", "id": 1, "text": "hello world"},
                         [el(1, "Text editor", "Document")], "safe")
        self.assertLevel({"action": "type_text", "text": "call me at 555 1234"}, [], "safe")

    def test_vision_uses_model_thought(self):
        self.assertLevel({"action": "click", "xy": (5, 5),
                          "thought": "I will click the Pay now button to finish."}, None, "confirm")
        self.assertLevel({"action": "click", "xy": (5, 5),
                          "thought": "Click the search box."}, None, "safe")

    def test_non_risky_kinds_are_safe(self):
        for a in ({"action": "open_app", "name": "Brave"}, {"action": "hotkey", "keys": "ctrl+l"},
                  {"action": "scroll", "dir": "down"}, {"action": "wait"}, {"action": "done"}):
            self.assertLevel(a, [], "safe")


class GateTests(unittest.TestCase):
    def gate(self, answer=None, **cfg):
        calls = []
        def cb(desc):
            calls.append(desc)
            return answer
        h = Harness(Config(harness_confirm_callback=cb, **cfg))
        return h, calls

    def test_answers_map_to_decisions(self):
        for answer, expected in ((ALLOW, ALLOW), (SKIP, SKIP), (STOP, STOP),
                                 (True, ALLOW), (False, STOP), (None, STOP), ("??", STOP)):
            with self.subTest(answer=answer):
                h, calls = self.gate(answer)
                decision, note = h.review(click(1, "pay for the order"), [el(1, "Pay now")])
                self.assertEqual(decision, expected)
                self.assertIn("payment", note)
                self.assertEqual(len(calls), 1)
                self.assertIn("pay for the order", calls[0])

    def test_safe_action_never_asks(self):
        h, calls = self.gate(STOP)
        self.assertEqual(h.review(click(1), [el(1, "File")]), (ALLOW, ""))
        self.assertEqual(calls, [])

    def test_callback_error_fails_safe(self):
        def boom(desc):
            raise RuntimeError("dialog crashed")
        h = Harness(Config(harness_confirm_callback=boom))
        self.assertEqual(h.review(click(1), [el(1, "Delete")])[0], STOP)

    def test_disabled_and_dry_run_do_not_ask(self):
        h, calls = self.gate(STOP, harness_enabled=False)
        self.assertEqual(h.review(click(1), [el(1, "Delete")])[0], ALLOW)
        h, calls = self.gate(STOP, dry_run=True)
        decision, note = h.review(click(1), [el(1, "Delete")])
        self.assertEqual(decision, ALLOW)
        self.assertIn("dry run", note)
        self.assertEqual(calls, [])

    def test_user_rules(self):
        h, calls = self.gate(ALLOW, harness_block=("amazon",))
        decision, note = h.review(click(1), [el(1, "Open Amazon")])
        self.assertEqual(decision, SKIP)
        self.assertIn("blocked", note)
        self.assertEqual(calls, [])               # hard block never prompts
        h, calls = self.gate(SKIP, harness_extra_sensitive=("archive",))
        self.assertEqual(h.review(click(1), [el(1, "Archive")])[0], SKIP)
        self.assertEqual(len(calls), 1)


class LoopTests(unittest.TestCase):
    """The gate inside the real agent loops: nothing executes without permission."""

    def run_loop(self, backend, answer, decisions):
        with ExitStack() as stack:
            def mock(obj, name, **kw):
                return stack.enter_context(patch.object(obj, name, **kw))
            cfg = Config(backend=backend, show_overlay=False, max_steps=3,
                         harness_confirm_callback=lambda desc: answer)
            mock(agent, "Logger")
            mock(agent, "_key_down", return_value=False)
            mock(agent.time, "sleep")
            mock(vision.time, "sleep")
            mock(agent, "_drive_cursor")
            mock(agent, "perceive", return_value=("Shop", [el(1, "Place order")], "[1] Button"))
            mock(agent.llm, "ping", return_value=(True, "OK"))
            mock(vision, "ping_vision", return_value=(True, "OK"))
            decide = mock(agent.llm, "decide", side_effect=decisions)
            vdecide = mock(vision, "decide_vision",
                           side_effect=[(d, "", (100, 100)) for d in decisions])
            execute = mock(agent.actions, "execute", side_effect=lambda c, a, e: (
                ActionResult(True, "done", done=True) if a["action"] == "done"
                else ActionResult(True, "clicked")))
            vexecute = mock(vision, "execute_vision", return_value="clicked")
            final = agent._run_impl("buy the thing", cfg)
            return final, (vexecute if backend == "vision" else execute), \
                (vdecide if backend == "vision" else decide)

    def test_text_brain(self):
        final, execute, _ = self.run_loop("uia", STOP, [click(1, "place the order")])
        self.assertTrue(final.startswith("stopped: you declined"), final)
        execute.assert_not_called()

        final, execute, _ = self.run_loop("uia", ALLOW,
                                          [click(1), {"action": "done", "say": "ordered"}])
        self.assertEqual(execute.call_count, 2)     # the click + the done
        self.assertEqual(execute.call_args_list[0].args[1]["id"], 1)

        final, execute, decide = self.run_loop(
            "uia", SKIP, [click(1), {"action": "done", "say": "gave up"}])
        # the click was skipped; only 'done' reached the executor
        self.assertEqual([c.args[1]["action"] for c in execute.call_args_list], ["done"])
        history = decide.call_args_list[1].args[4]
        self.assertTrue(any("SKIPPED" in h for h in history), history)

    def test_vision_brain(self):
        pay = {"action": "click", "xy": (5, 5), "thought": "Click the Pay now button."}
        final, vexecute, _ = self.run_loop("vision", STOP, [pay])
        self.assertTrue(final.startswith("stopped: you declined"), final)
        vexecute.assert_not_called()

        final, vexecute, _ = self.run_loop("vision", ALLOW, [pay, {"action": "done", "say": "ok"}])
        self.assertEqual(vexecute.call_count, 1)


if __name__ == "__main__":
    unittest.main()
