"""Contracts of the iris.cli package layout itself."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from iris import cli
from iris.cli import doctor

REPOSITORY = Path(__file__).resolve().parents[1]


class CliPackageTests(unittest.TestCase):
    def test_public_entry_points(self) -> None:
        self.assertTrue(callable(cli.main))
        self.assertEqual(cli.EXIT_OK, 0)
        self.assertEqual(cli.EXIT_PERMISSION, 4)

    def test_runs_as_a_module(self) -> None:
        # /usr/bin/iris and bin/iris both run `python3 -m iris.cli`.
        env = dict(os.environ, PYTHONPATH=os.fspath(REPOSITORY / "src"), NO_COLOR="1")
        completed = subprocess.run(
            [sys.executable, "-m", "iris.cli", "--help"],
            cwd=REPOSITORY, env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("doctor", completed.stdout)

    def test_doctor_finds_models_in_the_source_checkout(self) -> None:
        # doctor locates <repo>/models relative to its own file; that depth
        # changes whenever the module moves.
        with tempfile.TemporaryDirectory() as missing:
            check = doctor.check_models({"recognition": {"model_dir": missing}})
        self.assertEqual(check.status, "warn")
        self.assertIn(os.fspath(REPOSITORY / "models"), check.detail)


if __name__ == "__main__":
    unittest.main()
