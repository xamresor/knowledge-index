"""Tests for bin/paths.py — the split between code and data.

This module exists because `uv tool install` / `pipx` put the code in a **read-only** site-packages
directory, while the tool still has to write a graph, staged repos and generated data somewhere. Two
failure modes are worth pinning:

  * an installed copy mistaking itself for a checkout and writing **into site-packages** (this
    happened: the first heuristic looked for `VERSION` + `bin/kb`, and the wheel ships both);
  * a checkout silently moving its files to `~/.local/share`, which would strand an existing clone.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _kbtest import BIN  # noqa: F401  (puts bin/ on sys.path)

import paths


class CheckoutDetectionTest(unittest.TestCase):
    def test_the_real_checkout_is_detected(self):
        self.assertTrue(paths._is_checkout(paths.PACKAGE_ROOT))
        self.assertEqual(paths.describe()["mode"], "checkout")

    def test_a_wheel_layout_is_not_a_checkout(self):
        """The wheel ships VERSION and bin/, so those cannot be the marker — pyproject.toml can."""
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d)
            (fake / "bin").mkdir()
            (fake / "bin" / "kb").write_text("#!/bin/bash\n")
            (fake / "VERSION").write_text("1.2.3\n")
            self.assertFalse(paths._is_checkout(fake), "an installed copy must not claim checkout mode")
            (fake / "pyproject.toml").write_text("[project]\n")
            self.assertTrue(paths._is_checkout(fake))


class LocationTest(unittest.TestCase):
    def test_a_checkout_keeps_everything_in_place(self):
        """No migration for an existing clone: data and config stay where they always were."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KB_HOME", None)
            self.assertEqual(paths.data_home(), paths.PACKAGE_ROOT)
            self.assertEqual(paths.config_home(), paths.PACKAGE_ROOT)

    def test_kb_home_overrides_both(self):
        with mock.patch.dict(os.environ, {"KB_HOME": "/srv/kb"}):
            self.assertEqual(paths.data_home(), Path("/srv/kb"))
            self.assertEqual(paths.config_home(), Path("/srv/kb"))
            self.assertEqual(paths.out_dir(), Path("/srv/kb/graphify-out"))
            self.assertEqual(paths.graph_path(), Path("/srv/kb/graphify-out/graph.json"))

    def test_installed_mode_follows_xdg(self):
        with mock.patch.object(paths, "_is_checkout", return_value=False), \
             mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/x/data",
                                          "XDG_CONFIG_HOME": "/x/config"}, clear=False):
            os.environ.pop("KB_HOME", None)
            self.assertEqual(paths.data_home(), Path("/x/data/knowledge-index"))
            self.assertEqual(paths.config_home(), Path("/x/config/knowledge-index"))

    def test_installed_mode_falls_back_to_home_when_xdg_is_unset(self):
        with mock.patch.object(paths, "_is_checkout", return_value=False), \
             mock.patch.dict(os.environ, {}, clear=False):
            for var in ("KB_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
                os.environ.pop(var, None)
            self.assertEqual(paths.data_home(), Path.home() / ".local/share/knowledge-index")
            self.assertEqual(paths.config_home(), Path.home() / ".config/knowledge-index")

class StageWebTest(unittest.TestCase):
    def test_in_a_checkout_staging_is_a_no_op(self):
        self.assertEqual(paths.stage_web().resolve(), paths.web_source().resolve())

    def test_installed_the_shell_is_copied_next_to_the_data(self):
        """The page loads its data with relative <script src>, so shell and data must be siblings."""
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KB_HOME": d}):
            staged = paths.stage_web()
            self.assertEqual(staged, Path(d) / "web")
            for name in ("index.html", "app.js", "style.css"):
                self.assertTrue((staged / name).is_file(), name)

    def test_staging_does_not_touch_a_vendored_renderer(self):
        """`vendor/` is downloaded per machine and must survive an upgrade of the tool."""
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KB_HOME": d}):
            vendor = Path(d) / "web" / "vendor"
            vendor.mkdir(parents=True)
            (vendor / "vis-network.min.js").write_text("/* mine */")
            paths.stage_web()
            self.assertEqual((vendor / "vis-network.min.js").read_text(), "/* mine */")


class ShellExportsTest(unittest.TestCase):
    def test_bash_and_python_agree(self):
        """bin/kb evals these, so a missing variable becomes an empty path and a build writes to /."""
        with mock.patch.dict(os.environ, {"KB_HOME": "/srv/kb"}):
            lines = dict(line.split("=", 1) for line in paths._sh_exports().splitlines())
        self.assertEqual(set(lines), {"KB_PACKAGE", "KB_DATA", "KB_CONFIG", "KB_OUT", "KB_WEB"})
        for key, value in lines.items():
            self.assertTrue(value.startswith('"') and value.endswith('"'), f"{key} must be quoted")
            self.assertTrue(len(value) > 2, f"{key} must not be empty")


if __name__ == "__main__":
    unittest.main()
