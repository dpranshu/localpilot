import unittest
from unittest.mock import patch

from localpilot import inputs


class InputTests(unittest.TestCase):
    def test_key_names(self):
        self.assertEqual(inputs.key_code("ctrl"), 0x11)
        self.assertEqual(inputs.key_code("Enter"), 0x0D)
        self.assertEqual(inputs.key_code("l"), ord("L"))
        self.assertEqual(inputs.key_code("5"), ord("5"))
        self.assertEqual(inputs.key_code("f4"), 0x73)
        self.assertEqual(inputs.key_code("win"), 0x5B)
        self.assertIsNotNone(inputs.key_code(","))
        self.assertIsNone(inputs.key_code("notakey"))

    def test_write_sends_unicode_and_enter(self):
        sent = []
        with patch.object(inputs, "_send", side_effect=lambda *i: sent.append(i)), \
             patch.object(inputs, "_failsafe"):
            inputs.write("aé\n")
        units = [i[0].ki for i in sent]
        self.assertEqual(units[0].wScan, ord("a"))
        self.assertTrue(units[0].dwFlags & inputs.KEYEVENTF_UNICODE)
        self.assertEqual(units[1].wScan, ord("é"))
        self.assertEqual(units[2].wVk, 0x0D)             # newline -> real Enter key

    def test_emoji_sent_as_one_surrogate_pair_and_slowed(self):
        sent, slept = [], []
        with patch.object(inputs, "_send", side_effect=lambda *i: sent.append(i)), \
             patch.object(inputs, "_failsafe"), patch.object(inputs.time, "sleep", side_effect=slept.append):
            inputs.write("😀a")
        self.assertEqual(len(sent[0]), 4)                # hi-down, lo-down, hi-up, lo-up together
        self.assertTrue(any(s >= 0.06 for s in slept))   # paced so the next char isn't garbled

    def test_hotkey_order_and_unknown_key(self):
        sent = []
        with patch.object(inputs, "_send", side_effect=lambda *i: sent.append(i[0])), \
             patch.object(inputs, "_failsafe"), patch.object(inputs.time, "sleep"):
            inputs.hotkey("ctrl", "l")
        vks = [(s.ki.wVk, bool(s.ki.dwFlags & inputs.KEYEVENTF_KEYUP)) for s in sent]
        self.assertEqual(vks, [(0x11, False), (ord("L"), False), (ord("L"), True), (0x11, True)])
        with self.assertRaises(ValueError):
            inputs.hotkey("ctrl", "nosuchkey")

    def test_failsafe_corner_aborts(self):
        def corner(pt):
            pt._obj.x = 0; pt._obj.y = 0
            return True
        with patch.object(inputs.user32, "GetCursorPos", side_effect=corner):
            with self.assertRaises(inputs.FailSafeException):
                inputs.press("a")
