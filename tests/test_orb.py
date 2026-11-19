import queue
import sys
import unittest
from unittest.mock import Mock, patch

from localpilot.orb import AnimatedOrb
from localpilot.overlay import TkOverlay


class OrbMotionTests(unittest.TestCase):
    def test_active_states_float_and_return_to_rest(self):
        for state in ("thinking", "acting"):
            with self.subTest(state=state):
                orb = AnimatedOrb(64, 7, animate=True)
                start = orb._last
                first = orb.render("idle", start)
                lifts = []
                for frame in range(1, 41):
                    active = orb.render(state, start + frame / 20)
                    lifts.append(orb.lift)
                self.assertLess(min(lifts), 0)
                self.assertGreaterEqual(min(lifts), -4)
                self.assertNotEqual(first.tobytes(), active.tobytes())
                for frame in range(41, 81):
                    orb.render("idle", start + frame / 20)
                self.assertEqual(orb.lift, 0)

    def test_idle_sleeps_after_thirty_seconds_and_wakes(self):
        orb = AnimatedOrb(64, 7, animate=False)
        start = orb._last
        idle = orb.render("idle", start)
        self.assertIs(idle, orb.render("idle", start + 29.99))
        sleeping = orb.render("idle", start + 30)
        self.assertNotEqual(idle.tobytes(), sleeping.tobytes())
        thinking = orb.render("thinking", start + 31)
        self.assertNotEqual(sleeping.tobytes(), thinking.tobytes())
        self.assertEqual(orb.lift, 0)

    def test_reduced_motion_and_stopped_reuse_static_frames(self):
        for animate in (False, True):
            orb = AnimatedOrb(150, 12, animate=animate)
            start = orb._last
            orb.render("acting", start + .1)
            stopped = orb.render("stopped", start + .2)
            self.assertIs(stopped, orb.render("stopped", start + 2))
            self.assertEqual(stopped.size, (182, 182))
            self.assertEqual(stopped.getpixel((0, 0))[3], 0)
            self.assertEqual(stopped.getpixel((91, 91))[3], 255)
            thinking = orb.render("thinking", start + 2.1)
            later = orb.render("thinking", start + 2.2)
            self.assertEqual(thinking is later, not animate)


class OverlayStateTests(unittest.TestCase):
    def test_terminal_state_clears_stale_annotations(self):
        overlay = TkOverlay.__new__(TkOverlay)
        overlay._apply(("state", "thinking", {"step": "Scan", "say": "Old title"}))
        overlay._apply(("state", "acting", {"rect": (0, 0, 10, 10), "label": "Target"}))
        self.assertEqual(overlay._say, "")
        overlay._apply(("state", "stopped", {"say": "Stopped"}))
        self.assertEqual(overlay._say, "Stopped")
        self.assertEqual(overlay._step, "")
        self.assertIsNone(overlay._rect)

    def test_fallback_close_skips_draw_after_destroy(self):
        overlay = TkOverlay.__new__(TkOverlay)
        overlay._closed = False
        overlay._q = queue.Queue()
        overlay.root = Mock()
        overlay._draw = Mock()
        overlay.close()
        overlay._tick()
        overlay.root.destroy.assert_called_once()
        overlay.root.after.assert_not_called()
        overlay._draw.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows HUD")
    def test_hud_queue_and_saved_position_recovery(self):
        from localpilot import hud

        overlay = hud.HudOverlay.__new__(hud.HudOverlay)
        overlay._q = queue.Queue()
        overlay.W, overlay.H = 320, 200
        overlay.sphere = AnimatedOrb(150, 12, animate=False)
        with patch("builtins.open", side_effect=OSError):
            self.assertEqual(overlay._load_pos(), (114, 0))
        self.assertEqual(overlay._clamp(-20, 400), (0, 18))
        overlay.think("Scan", "Old title")
        overlay.act((0, 0, 10, 10), "Target", "")
        overlay._drain()
        self.assertEqual(overlay._say, "")
        overlay.done("Done")
        overlay._drain()
        self.assertEqual(overlay._step, "")
        self.assertIsNone(overlay._rect)
        self.assertIsNone(overlay._cur_target)

    @unittest.skipUnless(sys.platform == "win32", "Windows HUD")
    def test_hud_reuses_static_annotations_until_changed(self):
        from localpilot.hud import HudOverlay

        overlay = HudOverlay.__new__(HudOverlay)
        overlay._state = "done"
        overlay._step = ""
        overlay._say = "Complete"
        overlay._rect = overlay._cur_pos = None
        overlay._main_key = None
        overlay._center = Mock(return_value=(200, 200))
        overlay._main = Mock()
        overlay._compose = Mock()
        overlay._present_main()
        overlay._present_main()
        overlay._main.present.assert_called_once()
        overlay._center.return_value = (220, 200)
        overlay._present_main()
        self.assertEqual(overlay._main.present.call_count, 2)
