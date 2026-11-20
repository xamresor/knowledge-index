#!/usr/bin/env python3
"""bin/kb build safety: a build that fails half-way must leave the previous graph in place, and an
update skips what did not change but sees every change.

Runs the real bin/kb in a temp KB_HOME with a fake `graphify` on PATH (no real graphify, qmd or php).
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest

from _kbtest import BIN

FAKE_GRAPHIFY = r"""#!/bin/sh
# update <dir> --force : write a tiny valid graph; cluster-only : fail when told to
case "$1" in
  update) mkdir -p "$2/graphify-out"
          echo '{"directed": true, "multigraph": false, "graph": {}, "nodes": [{"id": "new::n", "label": "n"}], "links": []}' > "$2/graphify-out/graph.json" ;;
  cluster-only) [ -n "$FAKE_FAIL" ] && exit 1 ;;
esac
exit 0
"""


class FailedBuildKeepsTheGraph(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = self._tmp.name
        self.home = os.path.join(d, "kb")
        fake = os.path.join(d, "fakebin")
        src = os.path.join(d, "src")
        for p in (self.home, fake, src):
            os.makedirs(p)
        with open(os.path.join(src, "a.py"), "w") as f:
            f.write("x = 1\n")
        g = os.path.join(fake, "graphify")
        with open(g, "w") as f:
            f.write(FAKE_GRAPHIFY)
        os.chmod(g, os.stat(g).st_mode | stat.S_IEXEC)
        with open(os.path.join(self.home, "kb.projects.toml"), "w") as f:
            f.write(f'[[project]]\nname = "one"\ncode = ["{src}"]\n')
        self.env = {**os.environ, "KB_HOME": self.home, "PATH": fake + os.pathsep + os.environ["PATH"]}
        out = subprocess.run(["python3", os.path.join(BIN, "paths.py"), "--sh"], env=self.env,
                             capture_output=True, text=True, check=True).stdout
        self.out_dir = dict(line.split("=", 1) for line in out.splitlines())["KB_OUT"].strip('"')
        os.makedirs(self.out_dir, exist_ok=True)
        self.graph = os.path.join(self.out_dir, "graph.json")
        with open(self.graph, "w") as f:
            json.dump({"nodes": [{"id": "old::n", "label": "n"}], "links": []}, f)

    def build(self, fail: bool) -> subprocess.CompletedProcess:
        env = {**self.env, **({"FAKE_FAIL": "1"} if fail else {})}
        return subprocess.run(["bash", os.path.join(BIN, "kb"), "graph"], env=env,
                              capture_output=True, text=True, timeout=120)

    def ids(self) -> set[str]:
        with open(self.graph) as f:
            return {n["id"] for n in json.load(f)["nodes"]}

    def test_failed_build_restores_the_previous_graph(self):
        p = self.build(fail=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("kept the previous graph", p.stderr)
        self.assertEqual(self.ids(), {"old::n"})
        self.assertFalse(os.path.exists(self.graph + ".prev"))

    def test_good_build_replaces_the_graph(self):
        p = self.build(fail=False)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        self.assertIn("new::n", self.ids())
        self.assertNotIn("old::n", self.ids())
        self.assertFalse(os.path.exists(self.graph + ".prev"))


LOGGING_GRAPHIFY = r"""#!/bin/sh
# Logs every call. update without --force refuses (like graphify's shrink guard) when FAKE_REFUSE is set.
echo "$*" >> "$FAKE_LOG"
case "$1" in
  --version) echo "graphify 0.0.0-fake" ;;
  update) if [ -n "$FAKE_REFUSE" ] && [ "$3" != "--force" ]; then
            echo "[graphify] WARNING: new graph has 1 nodes but existing graph.json has 2. Refusing to overwrite." >&2
            exit 0
          fi
          mkdir -p "$2/graphify-out"
          echo '{"directed": true, "multigraph": false, "graph": {}, "nodes": [{"id": "new::n", "label": "n"}], "links": []}' > "$2/graphify-out/graph.json" ;;
esac
exit 0
"""


class IncrementalUpdate(unittest.TestCase):
    """`kb update` skips what did not change, and still sees every change."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = self._tmp.name
        self.home, fake, self.src = (os.path.join(d, x) for x in ("kb", "fakebin", "src"))
        for p in (self.home, fake, self.src):
            os.makedirs(p)
        self.write("a.py", "x = 1\n")
        g = os.path.join(fake, "graphify")
        with open(g, "w") as f:
            f.write(LOGGING_GRAPHIFY)
        os.chmod(g, os.stat(g).st_mode | stat.S_IEXEC)
        with open(os.path.join(self.home, "kb.projects.toml"), "w") as f:
            f.write(f'[[project]]\nname = "one"\ncode = ["{self.src}"]\n')
        self.log = os.path.join(d, "calls.log")
        self.env = {**os.environ, "KB_HOME": self.home, "FAKE_LOG": self.log,
                    "PATH": fake + os.pathsep + os.environ["PATH"]}

    def write(self, name, text):
        with open(os.path.join(self.src, name), "w") as f:
            f.write(text)

    def run_kb(self, **extra) -> tuple[str, list[str]]:
        open(self.log, "w").close()
        p = subprocess.run(["bash", os.path.join(BIN, "kb"), "graph"], env={**self.env, **extra},
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        with open(self.log) as f:
            calls = [c.strip() for c in f if not c.startswith("--version")]
        return p.stdout, calls

    def updates(self, calls):
        return [c.split()[-1] if c.endswith("--force") else "plain" for c in calls if c.startswith("update")]

    def test_unchanged_run_calls_nothing(self):
        self.run_kb()
        out, calls = self.run_kb()
        self.assertIn("unchanged — graph kept", out)
        self.assertIn("Inputs unchanged", out)
        self.assertEqual(calls, [])                     # no graphify update, no cluster-only

    def test_changed_file_rebuilds_incrementally(self):
        self.run_kb()
        self.write("a.py", "x = 2  # longer now\n")
        out, calls = self.run_kb()
        self.assertEqual(self.updates(calls), ["plain"])
        self.assertTrue(any(c.startswith("cluster-only") for c in calls))
        self.assertNotIn("Inputs unchanged", out)

    def test_new_file_and_deleted_file_are_seen(self):
        self.run_kb()
        self.write("b.py", "y = 1\n")
        self.assertEqual(self.updates(self.run_kb()[1]), ["plain"])
        os.remove(os.path.join(self.src, "b.py"))
        self.assertEqual(self.updates(self.run_kb()[1]), ["plain"])

    def test_refused_shrink_falls_back_to_a_full_pass(self):
        self.run_kb()
        self.write("a.py", "\n")
        out, calls = self.run_kb(FAKE_REFUSE="1")
        self.assertEqual(self.updates(calls), ["plain", "--force"])
        self.assertIn("full pass", out)

    def test_force_and_build_always_rebuild(self):
        self.run_kb()
        self.assertEqual(self.updates(self.run_kb(KB_FORCE="1")[1]), ["--force"])


if __name__ == "__main__":
    unittest.main()
