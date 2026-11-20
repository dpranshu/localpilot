"""One session for text and voice; control / inspect / command routing; interruptions."""
import threading
import time
import unittest
from unittest.mock import patch

from localpilot import session as S
from localpilot import inspect_pc, voice
from localpilot.config import Config


class RoutingTests(unittest.TestCase):
    def test_control(self):
        for t in ("stop", "Stop!", "Wait, don't change that file.", "hold on", "cancel that",
                  "never mind", "no", "okay stop"):
            self.assertTrue(S.is_control(t), t)
        for t in ("open notepad", "what is the stopwatch", "stopwatch app", "who wrote Harry Potter"):
            self.assertFalse(S.is_control(t), t)

    def test_inspect(self):
        for t in ("What's happening on my computer?", "what apps are open", "why is my pc slow",
                  "what am I looking at"):
            self.assertTrue(inspect_pc.is_inspect(t), t)
        for t in ("open VS Code and fix that error", "what is python", "open notepad"):
            self.assertFalse(inspect_pc.is_inspect(t), t)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.cfg = Config(use_memory=False)
        self.s = S.Session(self.cfg, None, lambda k, d: self.events.append((k, d)))

    def says(self):
        return [d["text"] for k, d in self.events if k == "say"]

    def slow_task(self, text, cfg, on_event, prefs, convo, **kw):
        """Pretend task: 'acts' every 50 ms and checks the stop flag before each action,
        like the real agent loop does (_check_stop before every action)."""
        self.actions_done = 0
        for _ in range(100):
            if cfg.stop_event.is_set():
                return "stopped: you stopped it"
            self.actions_done += 1
            time.sleep(0.05)
        return "done: finished"

    def test_stop_halts_a_running_task_fast(self):
        with patch.object(S.assistant, "handle", side_effect=self.slow_task):
            self.assertEqual(self.s.submit("open notepad and type a long story", "text"), "started")
            time.sleep(0.3)
            t0 = time.time()
            self.assertEqual(self.s.submit("Wait, don't change that file.", "voice"), "control")
            self.s.wait(2)
        self.assertFalse(self.s.busy)
        self.assertLess(time.time() - t0, 0.5)                 # halted before its next action
        self.assertLess(self.actions_done, 15)
        self.assertIn("stop_speaking", [k for k, _ in self.events])
        self.assertIn("Stopped. I won't make any more changes.", self.says())

    def test_new_request_while_busy_is_not_started(self):
        with patch.object(S.assistant, "handle", side_effect=self.slow_task):
            self.s.submit("task one", "text")
            self.assertEqual(self.s.submit("task two", "voice"), "busy")
            self.assertTrue(any("still working on: task one" in t for t in self.says()))
            self.s.interrupt("stop"); self.s.wait(2)

    def test_text_and_voice_share_one_conversation(self):
        seen = []
        def fake(text, cfg, on_event, prefs, convo, **kw):
            seen.append(list(convo)); return f"reply to {text}"
        with patch.object(S.assistant, "handle", side_effect=fake):
            self.s.submit("my name is Pranshu", "text"); self.s.wait(2)
            self.s.submit("what's my name", "voice"); self.s.wait(2)
        self.assertEqual(seen[1][0]["content"], "my name is Pranshu")     # voice saw the text turn
        self.assertEqual([e[1]["source"] for e in self.events if e[0] == "you"], ["text", "voice"])

    def test_inspect_reads_real_state_not_the_planner(self):
        with patch.object(S.inspect_pc, "explain", return_value="You're in Notepad.") as ex, \
             patch.object(S.assistant, "handle", side_effect=AssertionError("planner used")):
            self.s.submit("What's happening on my computer?", "voice"); self.s.wait(2)
        ex.assert_called_once()
        self.assertIn("You're in Notepad.", self.says())

    def test_progress_is_spoken_for_multi_step_plans(self):
        def fake(text, cfg, on_event, prefs, convo, **kw):
            on_event("step", {"i": 1, "n": 2, "step": "Open Brave"})
            on_event("step", {"i": 2, "n": 2, "step": "Search YouTube for lofi"})
            return "done"
        with patch.object(S.assistant, "handle", side_effect=fake):
            self.s.submit("set me up", "voice"); self.s.wait(2)
        self.assertIn("Step 2 of 2: Search YouTube for lofi.", self.says())


class VoiceGlueTests(unittest.TestCase):
    def test_stop_silences_speech_and_barge_in(self):
        v = voice.Voice.__new__(voice.Voice)
        v.speak_enabled = True
        calls = []
        class Sp:
            speaking = True
            def stop(self): calls.append("stop")
            def say(self, t, interrupt=False): calls.append(("say", t, interrupt))
        v.speaker = Sp()
        class Sess:
            def hold(self): calls.append("hold")
        v.session = Sess()
        v.on_session_event("stop_speaking", {})
        v._speech_started()                                    # user talks over LocalPilot
        v.on_session_event("say", {"text": "Stopped.", "interrupt": True})
        # silenced, AND the task held before its next action while the user speaks
        self.assertEqual(calls, ["stop", "stop", "hold", ("say", "Stopped.", True)])

    def test_normalize(self):
        self.assertEqual(voice.normalize("Search U Tube for x"), "Search YouTube for x")
        self.assertEqual(voice.normalize("open vs code"), "open VS Code")

    def test_sentences(self):
        self.assertEqual(voice._sentences("Done. Opened it! Next?"), ["Done.", "Opened it!", "Next?"])


if __name__ == "__main__":
    unittest.main()


class SpeakerTests(unittest.TestCase):
    def test_falls_back_to_the_female_windows_voice(self):
        with patch.object(voice, "KokoroTTS", side_effect=RuntimeError("not downloaded")):
            tts = voice.make_tts("kokoro", "af_heart")
        self.assertIsInstance(tts, voice.WindowsTTS)
        self.assertEqual(tts.voice_hint, "zira")                 # never the default male voice

    def test_stop_drops_queued_sentences(self):
        sp = voice.Speaker.__new__(voice.Speaker)
        import queue as _q
        sp._text, sp._audio, sp._gen, sp._pending, sp.speaking = _q.Queue(), _q.Queue(), 0, 0, False
        sp.say("One. Two. Three.")
        self.assertEqual((sp._text.qsize(), sp.speaking), (3, True))
        with patch("sounddevice.stop"):
            sp.stop()
        self.assertEqual((sp._text.qsize(), sp._gen, sp.speaking), (0, 1, False))

    def test_lofi_spelling(self):
        self.assertEqual(voice.normalize("Search YouTube for low-fee beats"), "Search YouTube for lofi beats")
