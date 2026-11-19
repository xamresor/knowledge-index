#!/usr/bin/env python3
"""bin/kb build safety: a build that fails half-way must leave the previous graph in place.

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


if __name__ == "__main__":
    unittest.main()
