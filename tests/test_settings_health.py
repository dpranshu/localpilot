import json
import os
import tempfile
import unittest
from unittest.mock import patch

from localpilot import health, settings
from localpilot.config import Config


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.d.name, "x", "settings.json")

    def tearDown(self):
        self.d.cleanup()

    def test_round_trip(self):
        cfg = Config(backend="hybrid", max_steps=25, harness_enabled=False, use_memory=False)
        self.assertTrue(settings.save_from(cfg, self.path))
        fresh = settings.load_into(Config(), self.path)
        self.assertEqual((fresh.backend, fresh.max_steps, fresh.harness_enabled, fresh.use_memory),
                         ("hybrid", 25, False, False))

    def test_bad_or_hostile_file_never_breaks_startup(self):
        os.makedirs(os.path.dirname(self.path))
        for content in ("{not json", "[1,2]", json.dumps({
                "backend": "rm -rf", "max_steps": 99999, "model": "  ", "harness_enabled": "no",
                "use_memory": 1, "stop_event": "x", "ollama_url": "http://evil"})):
            with open(self.path, "w") as f:
                f.write(content)
            cfg = settings.load_into(Config(), self.path)
            d = Config()
            self.assertEqual((cfg.backend, cfg.max_steps, cfg.model, cfg.harness_enabled,
                              cfg.use_memory, cfg.ollama_url),
                             (d.backend, d.max_steps, d.model, d.harness_enabled,
                              d.use_memory, d.ollama_url))

    def test_missing_file_is_fine(self):
        self.assertEqual(settings.load_into(Config(), self.path).backend, Config().backend)


class HealthTests(unittest.TestCase):
    def resp(self, names):
        class R:
            def raise_for_status(self): pass
            def json(self): return {"models": [{"name": n} for n in names]}
        return R()

    def test_states(self):
        cfg = Config()
        with patch.object(health.requests, "get", return_value=self.resp([cfg.model])):
            self.assertEqual(health.check(cfg)[0], health.OK)
        with patch.object(health.requests, "get", return_value=self.resp(["other:1b"])):
            state, msg = health.check(cfg)
            self.assertEqual(state, health.MODEL_MISSING); self.assertIn("Download model", msg)
        with patch.object(health.requests, "get", side_effect=OSError("refused")):
            with patch.object(health, "find_ollama", return_value="C:/ollama.exe"):
                self.assertEqual(health.check(cfg)[0], health.NOT_RUNNING)
            with patch.object(health, "find_ollama", return_value=None):
                self.assertEqual(health.check(cfg)[0], health.NOT_INSTALLED)

    def test_vision_backend_checks_vision_model(self):
        cfg = Config(backend="vision")
        with patch.object(health.requests, "get", return_value=self.resp([Config().model])):
            self.assertEqual(health.check(cfg)[0], health.MODEL_MISSING)


if __name__ == "__main__":
    unittest.main()
