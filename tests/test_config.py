from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from iris.config import load_config


class ConfigSafetyTests(unittest.TestCase):
    def test_max_failures_cannot_exceed_tracker_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("[auth]\nmax_failures = 1000\n", encoding="utf-8")

            self.assertEqual(load_config(path)["auth"]["max_failures"], 64)


if __name__ == "__main__":
    unittest.main()
