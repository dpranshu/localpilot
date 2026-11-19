"""Interruption safety: a paused or stopped task NEVER resumes without explicit permission."""
import threading
import time
import unittest
from unittest.mock import patch

from localpilot import agent, voice
from localpilot import session as S
from localpilot.config import Config


class CheckStopTests(unittest.TestCase):
    """agent._check_stop is called before every action: it must block while paused, forever."""

    def setUp(self):
        self.cfg = Config()
        self.cfg.stop_event, self.cfg.hold_event = threading.Event(), threading.Event()

    def run_check(self):
        out = {}
        def go():
            try:
                agent._check_stop(self.cfg); out["r"] = "proceeded"
            except agent._RunStopped as e:
                out["r"] = f"stopped: {e}"
        t = threading.Thread(target=go, daemon=True); t.start()
        return t, out

    def test_pause_has_no_timeout(self):
        """The old code resumed after 10 s. Jump the clock 1 hour: still paused."""
        self.cfg.hold_event.set()
        real = time.time
        with patch.object(agent.time, "time", side_effect=lambda: real() + 3600), \
             patch.object(agent, "_key_down", return_value=False):
            t, out = self.run_check(); t.join(0.6)
            self.assertTrue(t.is_alive(), "a paused task proceeded on its own")
            self.cfg.hold_event.clear(); t.join(1)               # explicit continue
        self.assertEqual(out["r"], "proceeded")

    def test_stop_while_paused_cancels(self):
        self.cfg.hold_event.set()
        with patch.object(agent, "_key_down", return_value=False):
            t, out = self.run_check(); time.sleep(0.2)
            self.cfg.stop_event.set(); t.join(1)
        self.assertTrue(out["r"].startswith("stopped"))

    def test_f12_works_while_paused(self):
        self.cfg.hold_event.set()
        with patch.object(agent, "_key_down", return_value=True):
            t, out = self.run_check(); t.join(1)
        self.assertIn("F12", out["r"])

    def test_gate_blocks_fast_path_actions(self):
        """Fast paths (site search, first-result click, Calculator, save...) go through _gate()."""
        self.cfg.hold_event.set()
        agent._CFG = self.cfg
        try:
            with patch.object(agent, "_key_down", return_value=False):
                done = []
                t = threading.Thread(target=lambda: (agent._gate(), done.append(1)), daemon=True)
                t.start(); t.join(0.5)
                self.assertFalse(done, "a fast-path action ran while paused")
                self.cfg.hold_event.clear(); t.join(1)
            self.assertEqual(done, [1])
        finally:
            agent._CFG = None

    def test_fast_path_helpers_never_swallow_a_stop(self):
        self.cfg.stop_event.set()
        agent._CFG = self.cfg
        try:
            from unittest.mock import MagicMock
            with patch.object(agent.web, "find_search_box", return_value=MagicMock()), \
                 patch.object(agent, "_title_matches", return_value=True), \
                 patch.object(agent, "_stopping", return_value=False), \
                 patch("uiautomation.ControlFromHandle"), \
                 patch.object(agent.actions.inputs, "write") as typed:
                with self.assertRaises(agent._RunStopped):
                    agent._app_search("mouse", type("L", (), {"log": lambda s, r: None})(), 1,
                                      self.cfg, "Settings")
            typed.assert_not_called()
        finally:
            agent._CFG = None


class SessionPauseTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.cfg = Config(use_memory=False)
        self.s = S.Session(self.cfg, None, lambda k, d: self.events.append((k, d)))
        self.release_task = threading.Event()

    def says(self):
        return [d["text"] for k, d in self.events if k == "say"]

    def fake_task(self, text, cfg, on_event, prefs, convo):
        """'Acts' every 30 ms, checking pause/stop before each action like the real agent."""
        self.actions = 0
        for _ in range(400):
            while cfg.hold_event.is_set() and not cfg.stop_event.is_set():
                time.sleep(0.01)
            if cfg.stop_event.is_set():
                return "stopped: you stopped it"
            self.actions += 1
            time.sleep(0.03)
            if self.release_task.is_set():
                return "done: finished"
        return "done: finished"

    def start(self):
        p = patch.object(S.assistant, "handle", side_effect=self.fake_task)
        p.start(); self.addCleanup(p.stop)
        self.assertEqual(self.s.submit("open notepad and type a story", "text"), "started")
        time.sleep(0.15)

    def frozen_for(self, secs):
        a = self.actions; time.sleep(secs); return self.actions == a

    def test_unrelated_speech_keeps_it_paused(self):
        self.start(); self.s.hold()
        self.assertEqual(self.s.submit("what time is it", "voice"), "paused")
        self.assertTrue(self.frozen_for(0.4), "task resumed after unrelated speech")
        self.assertTrue(any("I've paused" in t and "continue" in t for t in self.says()))
        self.s.interrupt("stop"); self.s.wait(2)

    def test_unclear_audio_keeps_it_paused(self):
        self.start(); self.s.hold()
        self.s.unheard()                                   # VAD fired, nothing transcribed
        self.assertTrue(self.frozen_for(0.4))
        self.assertTrue(any("didn't catch that" in t for t in self.says()))
        self.s.interrupt("stop"); self.s.wait(2)

    def test_only_continue_resumes(self):
        self.start(); self.s.hold()
        self.assertTrue(self.frozen_for(0.3))
        self.assertEqual(self.s.submit("continue", "voice"), "resumed")
        time.sleep(0.3)
        self.assertGreater(self.actions, 0)
        self.assertFalse(self.frozen_for(0.2), "did not resume after explicit continue")
        self.assertIn("Continuing: open notepad and type a story.", self.says())
        self.release_task.set(); self.s.wait(2)

    def test_stop_while_paused_cancels_for_good(self):
        self.start(); self.s.hold()
        self.assertEqual(self.s.submit("Wait, don't change that file", "voice"), "control")
        self.s.wait(2)
        self.assertFalse(self.s.busy)
        self.assertEqual(self.s.submit("continue", "voice"), "nothing")   # a stopped task stays stopped
        self.assertIn("There's nothing paused right now.", self.says())

    def test_continue_with_nothing_paused_does_nothing(self):
        self.assertEqual(self.s.submit("continue", "text"), "nothing")

    def test_resume_phrases(self):
        for t in ("continue", "Continue.", "ok continue", "yes, go on", "carry on", "resume", "keep going"):
            self.assertTrue(S.is_resume(t), t)
        for t in ("open notepad", "what time is it", "stop", "don't continue"):
            self.assertFalse(S.is_resume(t), t)


class VoiceUnheardTests(unittest.TestCase):
    def test_empty_transcript_does_not_resume(self):
        v = voice.Voice.__new__(voice.Voice)
        calls = []
        class Sess:
            def unheard(self): calls.append("unheard")
            def release(self): calls.append("RELEASE")
            def submit(self, *a): calls.append("submit")
        v.session, v.mode, v.log = Sess(), "hands-free", lambda m: None
        v._heard("")
        self.assertEqual(calls, ["unheard"])


if __name__ == "__main__":
    unittest.main()


class TranscriberPreloadTests(unittest.TestCase):
    """Whisper first use: background load, visible progress, callers wait instead of failing."""

    def test_one_shared_engine(self):
        self.assertIs(voice.get_transcriber("base.en"), voice.get_transcriber("base.en"))

    def test_preload_then_transcribe_waits_and_works(self):
        t = voice.Transcriber("base.en")
        msgs = []
        class Seg:
            text = "open notepad"
        class Model:
            def __init__(self, *a, **k): time.sleep(0.3)             # slow load
            def transcribe(self, *a, **k): return [Seg()], None
        with patch.object(t, "is_cached", return_value=True), \
             patch("faster_whisper.WhisperModel", Model):
            th = t.preload(msgs.append)
            self.assertEqual(t("audio" and __import__("numpy").zeros(1600)), "open Notepad")  # waited
            th.join(2)
        self.assertEqual(t.state, "ready")
        self.assertEqual(msgs, ["Loading speech recognition...", "Speech recognition ready."])

    def test_download_shows_progress_then_ready(self):
        t = voice.Transcriber("base.en")
        msgs, sizes = [], iter([0, 40, 90, 148] + [148] * 50)
        class Model:
            def __init__(self, *a, **k): pass
        def slow_download(*a, **k):
            time.sleep(1.3); return "C:/x"
        with patch.object(t, "is_cached", return_value=False), \
             patch.object(t, "_downloaded_mb", side_effect=lambda: next(sizes)), \
             patch("faster_whisper.utils.download_model", slow_download), \
             patch("faster_whisper.WhisperModel", Model):
            t.preload(msgs.append).join(5)
        pct = [m for m in msgs if "%" in m]
        self.assertGreaterEqual(len(pct), 2)
        self.assertTrue(pct[0].startswith("Downloading speech recognition"))
        self.assertEqual(msgs[-1], "Speech recognition ready.")
        self.assertEqual(t.state, "ready")

    def test_failure_is_reported_not_hidden(self):
        t = voice.Transcriber("base.en")
        msgs = []
        with patch.object(t, "is_cached", return_value=True), \
             patch("faster_whisper.WhisperModel", side_effect=OSError("disk full")):
            t.preload(msgs.append).join(2)
        self.assertEqual(t.state, "error")
        self.assertIn("disk full", msgs[-1])
