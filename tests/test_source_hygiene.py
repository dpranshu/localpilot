"""Guards against an editing hazard seen during development: invisible control characters
(e.g. a backspace replacing a regex \b) silently breaking code. Every source file must be clean."""
import glob
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SourceHygieneTests(unittest.TestCase):
    def test_no_control_characters_in_sources(self):
        bad = []
        for path in glob.glob(os.path.join(ROOT, "localpilot", "**", "*.py"), recursive=True) + \
                glob.glob(os.path.join(ROOT, "tests", "*.py")):
            data = open(path, "rb").read()
            for c in range(32):
                if c not in (9, 10, 13) and bytes([c]) in data:
                    bad.append(f"{os.path.relpath(path, ROOT)}: byte 0x{c:02x}")
        self.assertEqual(bad, [], "control characters in source files")


if __name__ == "__main__":
    unittest.main()
