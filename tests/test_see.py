"""Seeing: routing, Windows' built-in OCR (real), the vision step, fallbacks, and the session route."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from localpilot import see
from localpilot import session as S
from localpilot.config import Config


class RoutingTests(unittest.TestCase):
    YES = ["analyze this image", "Analyze this image, what do you think?", "what do you think of this picture",
           "read this error", "what does this error say", "what's on my screen", "describe the screen",
           "what am I looking at", "can you see this", "analyze this", "explain this chart",
           "summarize this page", "this photo, what do you think?", "check this screenshot",
           "what does the dialog say"]
    NO = ["check the weather this weekend", "tell me about image generation models",
          "research the latest image models", "open paint", "what is the capital of France",
          "describe quantum computing", "what's happening on my computer", "analyze the stock market today",
          "read my emails", "take a screenshot", "open this page in chrome later"]

    def test_screen_questions(self):
        for q in self.YES:
            self.assertTrue(see.wants_screen(q), q)
        for q in self.NO:
            self.assertFalse(see.wants_screen(q), q)

    def test_visual_questions_use_the_vision_model(self):
        for q in ("analyze this image", "what do you think of this picture", "describe the screen",
                  "explain this chart"):
            self.assertTrue(see.wants_visual(q), q)
        for q in ("read this error", "what does the dialog say"):
            self.assertFalse(see.wants_visual(q), q)

    def test_image_path(self):
        fd, p = tempfile.mkstemp(suffix=".png"); os.close(fd)
        try:
            self.assertEqual(see.image_path_in(f"what is in {p}?"), p)
            self.assertIsNone(see.image_path_in(r"describe C:\nope\missing.png"))
            self.assertIsNone(see.image_path_in("analyze this image"))
        finally:
            os.remove(p)


def text_image(lines, size=(900, 300)):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("segoeui.ttf", 28)
    except OSError:
        f = ImageFont.load_default()
    for i, t in enumerate(lines):
        d.text((30, 30 + i * 60), t, fill="black", font=f)
    return img


@unittest.skipUnless(sys.platform == "win32", "Windows OCR")
class OcrTests(unittest.TestCase):
    def test_reads_text_with_boxes(self):
        o = see.ocr(text_image(["Invoice total 1,284.50", "Due date 12 December"]))
        self.assertEqual(o.error, "")
        self.assertIn("1,284.50", o.text)
        self.assertIn("December", o.text)
        x1, y1, x2, y2 = o.lines[0][1]
        self.assertTrue(0 <= x1 < x2 <= 900 and 0 <= y1 < y2 <= 300, o.lines[0])   # in image pixels
        self.assertLess(o.seconds, 15)

    def test_temp_file_is_removed(self):
        before = set(f for f in os.listdir(tempfile.gettempdir()) if f.startswith("lp_ocr_"))
        see.ocr(text_image(["hello"]))
        after = set(f for f in os.listdir(tempfile.gettempdir()) if f.startswith("lp_ocr_"))
        self.assertEqual(after - before, set())

    def test_blank_image_is_not_an_error(self):
        from PIL import Image
        o = see.ocr(Image.new("RGB", (400, 200), "white"))
        self.assertEqual((o.lines, o.error), ([], ""))


class SeeLogicTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(use_memory=False)
        fd, self.path = tempfile.mkstemp(suffix=".png"); os.close(fd)
        text_image(["Q2 sales 340"]).save(self.path)

    def tearDown(self):
        os.remove(self.path)

    def test_image_file_goes_to_the_vision_model_with_ocr_grounding(self):
        seen = {}

        def fake_look(cfg, q, img, ocr_text="", where=""):
            seen.update(q=q, ocr=ocr_text, where=where); return "A chart-like note: Q2 sales 340."
        with patch.object(see, "look", fake_look), \
                patch.object(see, "ocr", return_value=see.OcrResult(lines=[("Q2 sales 340", (0, 0, 1, 1))])):
            a = see.explain(self.cfg, f"analyze {self.path}")
        self.assertIn("340", a)
        self.assertEqual(seen["ocr"], "Q2 sales 340")
        self.assertEqual(seen["where"], os.path.basename(self.path))

    def test_text_question_skips_the_vision_model(self):
        with patch.object(see, "look") as lk, \
                patch.object(see, "capture", return_value=(text_image(["x"]), "Test window")), \
                patch.object(see, "ocr", return_value=see.OcrResult(
                    lines=[("Error 0x80070005: Access is denied while saving report.docx", (0, 0, 1, 1))])), \
                patch.object(see.requests, "post", side_effect=OSError("offline")):
            a = see.explain(self.cfg, "what does this error say")
        lk.assert_not_called()
        self.assertIn("Access is denied", a)                 # offline: answered from the OCR text itself

    def test_no_text_and_no_vision_says_so(self):
        with patch.object(see, "capture", return_value=(text_image([]), "Paint")), \
                patch.object(see, "ocr", return_value=see.OcrResult()), \
                patch.object(see.requests, "post", side_effect=OSError("offline")):
            a = see.explain(self.cfg, "describe the screen")
        self.assertIn("Paint", a)
        self.assertIn("vision model", a)

    def test_target_window_is_never_localpilot_itself(self):
        import tkinter as tk
        r = tk.Tk(); r.title("LocalPilot test self"); r.geometry("400x300+50+50"); r.update()
        try:
            r.lift(); r.attributes("-topmost", True); r.update()
            h, title = see.target_window()
            self.assertNotEqual(title, "LocalPilot test self")
        finally:
            r.destroy()


@unittest.skipUnless(sys.platform == "win32", "Windows capture + OCR")
class CaptureTests(unittest.TestCase):
    def test_captures_another_programs_window_and_reads_it(self):
        code = ("import tkinter as tk; r=tk.Tk(); r.title('LP see capture test'); r.geometry('640x260+120+120');"
                "r.attributes('-topmost',True);"
                "tk.Label(r,text='Backup finished: 42 files copied',font=('Segoe UI',18)).pack(pady=60);"
                "r.after(30000,r.destroy); r.mainloop()")
        p = subprocess.Popen([sys.executable, "-c", code])
        try:
            end = time.time() + 15
            while time.time() < end and see.target_window()[1] != "LP see capture test":
                time.sleep(0.2)
            img, where = see.capture("window")
            self.assertEqual(where, "LP see capture test")
            self.assertIn("42 files", see.ocr(img).text)
        finally:
            p.kill(); p.wait()


class SessionRouteTests(unittest.TestCase):
    def test_session_routes_seeing_questions(self):
        cfg = Config(use_memory=False)
        events = []
        s = S.Session(cfg, None, lambda k, d: events.append((k, d)))
        with patch.object(S.see, "explain", return_value="It's a bar chart; Q2 is highest.") as ex:
            s.submit("analyze this image, what do you think?", "text"); s.wait(10)
        ex.assert_called_once()
        self.assertTrue(any(k == "plan" and d.get("intent") == "see" for k, d in events))
        self.assertTrue(any(k == "say" and "Q2" in d.get("text", "") for k, d in events))

    def test_can_be_turned_off(self):
        cfg = Config(use_memory=False); cfg.see_enabled = False
        s = S.Session(cfg, None, lambda k, d: None)
        with patch.object(S.see, "explain") as ex, patch.object(S.inspect_pc, "explain", return_value="ok"), \
                patch.object(S.assistant, "handle", return_value="ok"):
            s.submit("analyze this image", "text"); s.wait(10)
        ex.assert_not_called()


if __name__ == "__main__":
    unittest.main()
