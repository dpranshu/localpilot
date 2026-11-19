import unittest
from contextlib import ExitStack
from threading import Event
from unittest.mock import patch

from localpilot import agent, vision
from localpilot.config import Config


class StopTests(unittest.TestCase):
    def test_pending_action_is_discarded(self):
        for backend in ("uia", "hybrid", "vision"):
            for timing in ("startup", "decision", "cursor"):
                with self.subTest(backend=backend, timing=timing), ExitStack() as stack:
                    event = Event()
                    cfg = Config(backend=backend, stop_event=event, show_overlay=False,
                                 show_cursor=True, save_screenshots=False, max_steps=1)
                    module = agent.llm if backend == "uia" else vision
                    def decide(*args):
                        if timing == "decision":
                            event.set()
                        action = {"action": "click", "xy": (5, 5)}
                        return action if backend == "uia" else (action, "", (100, 100))
                    def delay(seconds):
                        if timing == "startup" and seconds == 3:
                            event.set()
                        if timing == "cursor" and seconds == cfg.cursor_move_time:
                            event.set()
                    def cursor(*args):
                        if timing == "cursor":
                            event.set()
                    def mock(obj, name, **kwargs):
                        return stack.enter_context(patch.object(obj, name, **kwargs))
                    mock(agent, "Logger")
                    mock(agent, "_key_down", return_value=False)
                    mock(agent.time, "sleep", side_effect=delay)
                    mock(agent, "_drive_cursor", side_effect=cursor)
                    mock(agent, "perceive", return_value=("Window", [], ""))
                    mock(agent.llm, "ping", return_value=(True, "OK"))
                    mock(vision, "ping_vision", return_value=(True, "OK"))
                    decision = mock(module, "decide" if backend == "uia" else "decide_vision",
                                    side_effect=decide)
                    text_action = mock(agent.actions, "execute")
                    vision_action = mock(vision, "execute_vision")
                    self.assertEqual(agent._run_impl("click", cfg), "stopped by user")
                    text_action.assert_not_called()
                    vision_action.assert_not_called()
                    self.assertEqual(decision.call_count, int(timing != "startup"))
