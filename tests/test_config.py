from __future__ import annotations

import logging
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from iris import config as config_module
from iris.config import DEFAULTS, defaults, load_config, save_config


class ConfigSafetyTests(unittest.TestCase):
    def test_max_failures_cannot_exceed_tracker_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("[auth]\nmax_failures = 1000\n", encoding="utf-8")

            self.assertEqual(load_config(path)["auth"]["max_failures"], 64)


class _ConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="iris-config-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "config.toml"
        # Loader complaints are expected in most of these tests; keep them off
        # stderr and let individual tests assert on them where it matters.
        patcher = mock.patch.object(config_module._LOG, "disabled", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def load(self, text: str) -> dict[str, Any]:
        self.path.write_text(text, encoding="utf-8")
        return load_config(self.path)

    def logs(self, level: int = logging.WARNING) -> Any:
        """assertLogs on iris.config, re-enabling the logger for this test."""
        config_module._LOG.disabled = False
        self.addCleanup(setattr, config_module._LOG, "disabled", True)
        return self.assertLogs("iris.config", level)


class LoadNeverBreaksAuthTests(_ConfigCase):
    """A broken configuration must never break authentication."""

    def test_missing_file_gives_defaults(self) -> None:
        self.assertEqual(load_config(self.dir / "absent.toml"), DEFAULTS)

    def test_malformed_toml_gives_defaults(self) -> None:
        for text in ("[auth\nenabled = true", "auth.enabled = = 1", "\x00\x01", "[auth]\nenabled = tru"):
            with self.subTest(text=text):
                with self.logs(logging.ERROR) as logs:
                    self.assertEqual(self.load(text), DEFAULTS)
                self.assertTrue(any("malformed TOML" in line for line in logs.output))

    def test_unreadable_path_gives_defaults(self) -> None:
        self.assertEqual(load_config(self.dir), DEFAULTS)  # a directory

    def test_permission_denied_gives_defaults(self) -> None:
        with mock.patch.object(config_module, "open", side_effect=PermissionError(13, "EACCES"), create=True):
            self.assertEqual(load_config(self.path), DEFAULTS)

    def test_unexpected_error_gives_defaults(self) -> None:
        self.path.write_text("[auth]\nenabled = false\n", encoding="utf-8")
        with mock.patch.object(config_module, "_merge_into", side_effect=RuntimeError("bug")):
            self.assertEqual(load_config(self.path), DEFAULTS)

    def test_every_result_is_a_private_copy(self) -> None:
        cfg = load_config(self.dir / "absent.toml")
        cfg["auth"]["enabled"] = False
        cfg["new"] = {}
        self.assertTrue(DEFAULTS["auth"]["enabled"])
        self.assertNotIn("new", DEFAULTS)
        self.assertIsNot(defaults()["auth"], DEFAULTS["auth"])


class MergeAndTypeTests(_ConfigCase):
    def test_partial_file_merges_over_defaults(self) -> None:
        cfg = self.load("[auth]\ntimeout = 5.0\n")
        self.assertEqual(cfg["auth"]["timeout"], 5.0)
        self.assertEqual(cfg["auth"]["max_failures"], DEFAULTS["auth"]["max_failures"])
        self.assertEqual(cfg["camera"], DEFAULTS["camera"])

    def test_wrong_types_fall_back_per_key(self) -> None:
        cases = {
            'width = "640"': ("camera", "width"),
            "width = true": ("camera", "width"),          # bool is not an int
            "width = 640.0": ("camera", "width"),         # float is not an int
            "ir_mode = 1": ("camera", "ir_mode"),         # int is not a bool
            "device = 2": ("camera", "device"),
            "min_frame_brightness = true": ("camera", "min_frame_brightness"),
            'min_frame_brightness = "20"': ("camera", "min_frame_brightness"),
            "min_frame_brightness = nan": ("camera", "min_frame_brightness"),
            "min_frame_brightness = inf": ("camera", "min_frame_brightness"),
            "min_frame_brightness = [1]": ("camera", "min_frame_brightness"),
        }
        for line, (section, key) in cases.items():
            with self.subTest(line=line):
                cfg = self.load(f"[{section}]\n{line}\nheight = 240\n")
                self.assertEqual(cfg[section][key], DEFAULTS[section][key])
                self.assertEqual(cfg["camera"]["height"], 240)  # neighbours survive

    def test_int_is_widened_to_float(self) -> None:
        cfg = self.load("[auth]\ntimeout = 8\n[recognition]\nthreshold = 1\n")
        self.assertIsInstance(cfg["auth"]["timeout"], float)
        self.assertEqual(cfg["recognition"]["threshold"], 1.0)
        self.assertIsInstance(cfg["recognition"]["threshold"], float)

    def test_non_table_top_level_keys_are_ignored(self) -> None:
        cfg = self.load('auth = "off"\nversion = 2\n[camera]\nwidth = 320\n')
        self.assertEqual(cfg["auth"], DEFAULTS["auth"])
        self.assertNotIn("version", cfg)
        self.assertEqual(cfg["camera"]["width"], 320)

    def test_unknown_scalars_survive_and_structures_are_dropped(self) -> None:
        cfg = self.load(
            "[auth]\nnote = \"hand edit\"\nlist = [1, 2]\n"
            "[experimental]\nflag = true\nnested = { a = 1 }\n"
        )
        self.assertEqual(cfg["auth"]["note"], "hand edit")
        self.assertNotIn("list", cfg["auth"])
        self.assertEqual(cfg["experimental"], {"flag": True})


class ClampTests(_ConfigCase):
    def test_out_of_range_values_are_clamped(self) -> None:
        cases = [
            ("auth", "timeout", "0", 0.5),
            ("auth", "timeout", "-3.0", 0.5),
            ("auth", "timeout", "1e9", 60.0),   # PAM must never hang a login
            ("auth", "max_failures", "0", 1),
            ("auth", "max_failures", "1000", 64),
            ("auth", "lockout_seconds", "-1", 0),
            ("auth", "lockout_seconds", "999999999", 86_400),
            ("recognition", "threshold", "5.0", 1.0),
            ("recognition", "threshold", "-5.0", -1.0),
            ("recognition", "detect_score", "2.0", 1.0),
            ("recognition", "required_matches", "0", 1),
            ("recognition", "max_frames", "0", 1),
            ("camera", "width", "0", 1),
            ("camera", "height", "100000", 8192),
            ("camera", "min_frame_brightness", "300.0", 255.0),
            ("liveness", "min_variance", "-1.0", 0.0),
        ]
        for section, key, raw, expected in cases:
            with self.subTest(f"{section}.{key} = {raw}"):
                value = self.load(f"[{section}]\n{key} = {raw}\n")[section][key]
                self.assertEqual(value, expected)
                self.assertIs(type(value), type(DEFAULTS[section][key]))

    def test_in_range_values_are_untouched(self) -> None:
        cfg = self.load("[auth]\ntimeout = 0.5\nmax_failures = 64\n[recognition]\nthreshold = -1.0\n")
        self.assertEqual((cfg["auth"]["timeout"], cfg["auth"]["max_failures"]), (0.5, 64))
        self.assertEqual(cfg["recognition"]["threshold"], -1.0)

    def test_permissive_threshold_is_warned_about(self) -> None:
        with self.logs() as logs:
            self.load("[recognition]\nthreshold = 0.1\n")
        self.assertTrue(any("dangerously permissive" in line for line in logs.output))

    def test_blank_paths_fall_back_to_defaults(self) -> None:
        cfg = self.load('[camera]\ndevice = "  "\n[recognition]\nmodel_dir = ""\n')
        self.assertEqual(cfg["camera"]["device"], DEFAULTS["camera"]["device"])
        self.assertEqual(cfg["recognition"]["model_dir"], DEFAULTS["recognition"]["model_dir"])


class SaveTests(_ConfigCase):
    def test_round_trip(self) -> None:
        cfg = defaults()
        cfg["auth"].update(enabled=False, timeout=12.5, max_failures=7)
        cfg["camera"]["device"] = "/dev/video4"
        cfg["recognition"]["threshold"] = 0.42
        save_config(cfg, self.path)
        self.assertEqual(load_config(self.path), cfg)

    def test_written_file_is_complete_world_readable_toml(self) -> None:
        save_config({"auth": {"enabled": False}}, self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        parsed = tomllib.loads(self.path.read_text(encoding="utf-8"))
        for section, values in DEFAULTS.items():
            self.assertEqual(set(parsed[section]), set(values))
        self.assertIs(parsed["auth"]["enabled"], False)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["config.toml"])

    def test_types_survive_a_round_trip(self) -> None:
        # bool must not be written as 1, and a whole float must keep its ".0",
        # or the next load rejects it and silently uses the default.
        save_config({"camera": {"ir_mode": False}, "auth": {"timeout": 8.0}}, self.path)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("ir_mode = false", text)
        self.assertIn("timeout = 8.0", text)
        cfg = load_config(self.path)
        self.assertIs(cfg["camera"]["ir_mode"], False)
        self.assertIsInstance(cfg["auth"]["timeout"], float)

    def test_awkward_strings_and_keys_round_trip(self) -> None:
        tricky = [
            'quote " and backslash \\',
            "tab\tnewline\nreturn\r",
            "bell\x07 del\x7f nul-ish\x01",
            "zoë 🙂 ümlaut",
            "",
        ]
        cfg = defaults()
        cfg["notes"] = {f"key {i}": value for i, value in enumerate(tricky)}
        cfg["notes"]["dotted.key"] = "x"
        cfg["notes"]['quo"te'] = "y"
        save_config(cfg, self.path)
        self.assertEqual(load_config(self.path)["notes"], cfg["notes"])

    def test_save_clamps_and_rejects_like_load(self) -> None:
        save_config({"auth": {"timeout": 1e9, "enabled": "yes"}}, self.path)
        # The file itself holds the safe values, not just what load makes of it.
        on_disk = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["auth"]["timeout"], 60.0)
        cfg = load_config(self.path)
        self.assertEqual(cfg["auth"]["timeout"], 60.0)
        self.assertIs(cfg["auth"]["enabled"], True)

    def test_hand_edits_to_unknown_keys_survive_a_save(self) -> None:
        self.path.write_text("[auth]\nnote = \"ask Rory\"\n[site]\nowner = \"it\"\n", encoding="utf-8")
        cfg = load_config(self.path)
        cfg["auth"]["enabled"] = False
        save_config(cfg, self.path)
        again = load_config(self.path)
        self.assertEqual(again["auth"]["note"], "ask Rory")
        self.assertEqual(again["site"], {"owner": "it"})

    def test_creates_missing_directories(self) -> None:
        target = self.dir / "etc" / "iris" / "config.toml"
        save_config(defaults(), target)
        self.assertEqual(load_config(target), DEFAULTS)

    def test_failed_write_raises_and_leaves_nothing_behind(self) -> None:
        self.path.write_text("[auth]\nenabled = false\n", encoding="utf-8")
        with mock.patch.object(config_module.os, "replace", side_effect=OSError(28, "ENOSPC")):
            with self.assertRaises(OSError):
                save_config(defaults(), self.path)
        # The old file is intact and no temp file is left next to it.
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[auth]\nenabled = false\n")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["config.toml"])

    def test_write_into_a_directory_path_raises(self) -> None:
        # The temp file is created next to the target, i.e. in its parent.
        parent = self.dir.parent
        before = {p.name for p in parent.iterdir() if p.name.startswith(".config.toml.")}
        with self.assertRaises(OSError):
            save_config(defaults(), self.dir)
        after = {p.name for p in parent.iterdir() if p.name.startswith(".config.toml.")}
        self.assertEqual(after, before)
        self.assertTrue(self.dir.is_dir())

    def test_unrepresentable_value_is_a_type_error(self) -> None:
        with self.assertRaises(TypeError):
            config_module._emit_scalar(None, "auth.x")


class DaemonAgreementTests(unittest.TestCase):
    def test_daemon_type_check_matches_the_loader(self) -> None:
        # irisd validates config_set requests with its own _same_kind so that a
        # mistyped value is refused over the wire instead of being silently
        # replaced by its default on the next load. The two must agree.
        from iris.daemon import _same_kind

        samples = [True, False, 0, 7, -1, 2.5, 8.0, "", "text",
                   float("nan"), float("inf"), float("-inf")]
        for section, values in DEFAULTS.items():
            for key, default in values.items():
                for value in samples:
                    with self.subTest(key=f"{section}.{key}", value=value):
                        accepted, _ = config_module._coerce(default, value)
                        self.assertEqual(_same_kind(default, value), accepted)


if __name__ == "__main__":
    unittest.main()
