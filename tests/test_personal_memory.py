"""Personal assistant layer: learned facts, recall, forgetting, persistence across restarts, rolling
summary, statements never acted on, single instance, presence orb. No network / model needed
(Qwen calls are stubbed)."""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from localpilot import profile as P
from localpilot.memory import Memory


def mem():
    return Memory(os.path.join(tempfile.mkdtemp(), "memory.json"))


class LearnTests(unittest.TestCase):
    def test_learns_clear_first_person_statements(self):
        cases = {
            "Hi! My name is pranshu and I'm building LocalPilot": {"The user's name is Pranshu.",
                                                                  "The user is building LocalPilot."},
            "hi, I'm Pranshu": {"The user's name is Pranshu."},
            "I live in Pune, India": {"The user lives in Pune, India."},
            "I really like Punjabi music and I prefer short answers.": {"The user likes Punjabi music.",
                                                                       "The user prefers short answers."},
            "my favourite browser is Brave": {"The user's favourite browser is Brave."},
            "My cat is called Mochi.": {"The user's cat is called Mochi."},
            "I'm learning Rust these days": {"The user is learning Rust these days."},
        }
        for text, want in cases.items():
            self.assertEqual({f for _, f in P.learn(text)}, want, text)

    def test_does_not_learn_from_questions_or_moods(self):
        for t in ("what is my name?", "I'm not sure", "I'm tired", "I'm Fine", "can you open Spotify"):
            self.assertEqual(P.learn(t), [], t)

    def test_statement_vs_request(self):
        self.assertTrue(P.is_statement("I really like Punjabi music."))
        for t in ("I like music, play some", "Can you remember I like tea?", "Open Spotify", "what's up"):
            self.assertFalse(P.is_statement(t), t)


class MemoryTests(unittest.TestCase):
    def test_facts_replace_by_key_and_dedupe(self):
        m = mem()
        m.add_fact("The user's name is Ravi.", key="name")
        m.add_fact("The user's name is Pranshu.", key="name")
        m.add_fact("The user likes tea.")
        m.add_fact("The user likes tea")
        self.assertEqual([f["text"].rstrip(".") for f in m.facts()],
                         ["The user's name is Pranshu", "The user likes tea"])

    def test_secrets_are_never_stored(self):
        m = mem()
        for t in ("My password is hunter2", "my bank PIN is 4321", "the api key is sk-123", "card 4111 1111 1111 1111"):
            r = m.add_fact(t)
            self.assertTrue(r is None or "4111" not in r["text"], t)
        self.assertFalse(any("hunter2" in f["text"] or "4321" in f["text"] or "sk-123" in f["text"]
                             for f in m.facts()))
        self.assertIsNotNone(m.add_fact("The user likes pinball."), "'pin' inside a word is fine")

    def test_conversation_and_summary_persist(self):
        m = mem()
        m.save_conversation([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
        m.set_summary("User is Pranshu.")
        m2 = Memory(m.path)
        self.assertEqual([t["content"] for t in m2.conversation()], ["hi", "hello"])
        self.assertEqual(m2.summary(), "User is Pranshu.")

    def test_profile_keeps_pinned_facts_when_many(self):
        m = mem()
        m.add_fact("The user's name is Pranshu.", key="name")
        for i in range(60):
            m.add_fact(f"The user likes topic number {i}.")
        prof = m.profile("tell me about topic number 7", k=10)
        self.assertIn("Pranshu", prof)
        self.assertIn("topic number 7", prof)
        self.assertLessEqual(prof.count("\n") + 1, 11)


class ExplicitTests(unittest.TestCase):
    def test_remember_recall_forget(self):
        m = mem()
        self.assertIn("remember", P.explicit("remember that my birthday is 5 March", m))
        self.assertIn("won't store", P.explicit("remember my bank password is x1", m))
        self.assertIn("haven't told me", P.explicit("what's my name?", m))
        m.add_fact("The user's name is Pranshu.", key="name")
        self.assertEqual(P.explicit("what is my name", m), "You're Pranshu.")
        about = P.explicit("what do you know about me", m)
        self.assertIn("birthday is 5 March", about); self.assertIn("Pranshu", about)
        self.assertIn("your birthday", P.explicit("forget my birthday", m))
        self.assertNotIn("birthday", P.explicit("what do you know about me", m))
        P.explicit("forget everything about me", m)
        self.assertEqual(m.facts(), [])
        self.assertIsNone(P.explicit("forget it", m), "'forget it' is conversation, not a command")


class SessionPersonalTests(unittest.TestCase):
    def _session(self, m, **cfg_kw):
        from localpilot.config import Config
        from localpilot.session import Session
        ev = []
        cfg = Config(use_memory=True, research_enabled=False)
        for k, v in cfg_kw.items():
            setattr(cfg, k, v)
        return Session(cfg, m, lambda k, d: ev.append((k, d))), ev

    def test_statement_is_learned_and_never_planned(self):
        m = mem()
        s, ev = self._session(m)
        with mock.patch("localpilot.brain.answer", return_value="Nice - noted.") as ans, \
                mock.patch("localpilot.assistant.handle") as handle:
            s.submit("I really like Punjabi music.", "text"); s.wait(10)
        handle.assert_not_called()
        self.assertTrue(ans.call_args.kwargs.get("acknowledge"))
        self.assertIn("The user likes Punjabi music.", [f["text"] for f in m.facts()])
        self.assertIn(("learned", {"fact": "The user likes Punjabi music."}), ev)

    def test_brain_gets_personal_context_and_conversation_survives_restart(self):
        m = mem()
        m.add_fact("The user's name is Pranshu.", key="name")
        s, _ = self._session(m)
        seen = {}

        def handle(text, cfg, relay, prefs, convo, research=None):
            seen["prefs"], seen["convo"] = prefs, convo
            return "It's sunny."
        with mock.patch("localpilot.assistant.handle", side_effect=handle):
            s.submit("how's it going", "text"); s.wait(10)
        self.assertIn("Pranshu", seen["prefs"])
        s2, _ = self._session(m)                                   # "restart"
        self.assertEqual([t["content"] for t in s2.conversation][-2:], ["how's it going", "It's sunny."])

    def test_long_conversation_is_folded_into_summary(self):
        m = mem()
        s, _ = self._session(m, conversation_keep=4)
        with mock.patch("localpilot.assistant.handle", return_value="ok"), \
                mock.patch("localpilot.research.qwen.chat", return_value="User talked about owls.") as chat:
            for i in range(4):
                s.submit(f"tell me fact {i} about owls", "text"); s.wait(10)
            end = time.time() + 5
            while (s._summarizing or not m.summary()) and time.time() < end:
                time.sleep(0.05)
        self.assertTrue(chat.called)
        self.assertEqual(m.summary(), "User talked about owls.")
        self.assertLessEqual(len(s.conversation), 4 + 2)
        self.assertIn("Earlier conversation", s.personal_context("owls"))

    def test_memory_off_means_nothing_is_stored(self):
        m = mem()
        s, _ = self._session(m, use_memory=False)
        with mock.patch("localpilot.assistant.handle", return_value="ok"):
            s.submit("My name is Pranshu and I live in Pune, can you help", "text"); s.wait(10)
        self.assertEqual(m.facts(), [])
        self.assertEqual(m.conversation(), [])


class InstanceTests(unittest.TestCase):
    def test_second_start_asks_the_first_to_show(self):
        from localpilot import instance
        name = f"Local\\LocalPilotTest.{os.getpid()}.{time.time_ns()}"
        self.assertTrue(instance.claim(mutex=name + ".m", show=name + ".s"))
        shown = threading.Event()
        instance.on_show(shown.set)
        self.assertFalse(instance.claim(mutex=name + ".m", show=name + ".s"))
        self.assertTrue(shown.wait(3))
        shown.clear()
        self.assertFalse(instance.claim(background=True, mutex=name + ".m", show=name + ".s"))
        self.assertFalse(shown.wait(0.5), "a background (login) start must not pop the window up")
        instance.release()


class PresenceTests(unittest.TestCase):
    def test_orb_states_visibility_and_menu(self):
        from localpilot.hud import HudOverlay
        from localpilot.presence import PresenceOrb
        calls = []
        o = PresenceOrb(on_open=lambda: calls.append("open"), on_pause=lambda: calls.append("pause"),
                        on_resume=lambda: calls.append("resume"), on_stop=lambda: calls.append("stop"),
                        on_quit=lambda: calls.append("quit"),
                        pos_file=os.path.join(tempfile.mkdtemp(), "p.json")).start()
        try:
            self.assertTrue(o.ok and o.visible)
            f = o.frames; time.sleep(0.6)
            self.assertGreater(o.frames, f, "idle orb keeps moving: LocalPilot is running")
            HudOverlay.active += 1
            self.assertTrue(_wait(lambda: not o.visible), "hidden while a task HUD orb is up")
            HudOverlay.active -= 1
            self.assertTrue(_wait(lambda: o.visible))
            o.hide(); self.assertTrue(_wait(lambda: not o.visible)); o.show(); self.assertTrue(_wait(lambda: o.visible))
            for cmd in (1, 2, 3, 4, 7):
                o._command(cmd)
            self.assertTrue(_wait(lambda: len(calls) == 5))
            self.assertEqual(sorted(calls), sorted(["open", "pause", "resume", "stop", "quit"]))
        finally:
            o.close()


def _wait(cond, t=3.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


if __name__ == "__main__":
    unittest.main()
