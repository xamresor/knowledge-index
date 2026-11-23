#!/usr/bin/env python3
"""bin/usage.py: one SQLite row per call, shared by several processes, the old JSON imported once."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from _kbtest import BIN  # noqa: F401

from usage import Usage


class Counters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "logs", "usage-local.json")

    def test_two_processes_add_up(self):
        a = Usage(self.path)    # kb-api
        b = Usage(self.path)    # a Claude session's kb-mcp, open at the same time
        for _ in range(3):
            a.record("search", "local", {"q": "x"}, True, 0.5)
        for _ in range(2):
            b.record("search", "local", {"q": "y", "project": "api,web"}, False, 1.0)
        b.record("graph_explain", "local", {"symbol": "A"}, True, 0.1)
        d = Usage(self.path).snapshot()
        self.assertEqual(d["tools"]["search"], {"calls": 5, "errors": 2, "seconds": 3.5})
        self.assertEqual(d["tools"]["graph_explain"]["calls"], 1)
        self.assertEqual(d["args"]["search"], {"x": 3, "y": 2})  # the project has its own column
        self.assertEqual(d["projects"]["api"]["search"], 2)
        self.assertEqual(d["projects"]["all"]["search"], 3)
        self.assertEqual(sorted(d["recent"]["search"]), [0.5, 0.5, 0.5, 1.0, 1.0])
        self.assertEqual(a.p95("search"), 1.0)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, "logs", "usage-local.sqlite")))

    def test_old_json_is_imported_once(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"since": "2026-11-01", "tools": {"search": {"calls": 4, "errors": 1, "seconds": 2.0}},
                       "days": {"2026-11-01": {"search": 4}}, "users": {"local": {"search": 4}},
                       "args": {"search": {"x": 4}}, "hours": {}, "projects": {}, "recent": {"search": [0.4]}}, f)
        u = Usage(self.path)
        u.record("search", "local", {"q": "x"}, True, 1.0)
        d = Usage(self.path).snapshot()                 # a second open must not import again
        self.assertEqual(d["tools"]["search"]["calls"], 5)
        self.assertEqual(d["args"]["search"]["x"], 5)
        self.assertEqual(d["since"], "2026-11-01")
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(os.path.exists(self.path + ".imported"))

    def test_slow_calls_and_diff_not_stored(self):
        u = Usage(self.path)
        u.record("search", "ann", {"q": "a"}, True, 0.2)
        u.record("review_context", "ann", {"diff": "x" * 5000, "project": "api"}, True, 4.0)
        slow = u.slow(limit=1)
        self.assertEqual(slow[0][1:5], ("review_context", "ann", "api", 4.0))
        self.assertEqual(slow[0][5], "")                # the diff text is not kept as an argument

    def test_report_has_p95(self):
        u = Usage(self.path)
        u.record("search", "ann", {"q": "a"}, True, 0.2)
        self.assertIn("p95 s", u.report(with_users=True))
        self.assertIn("ann: 1", u.report(with_users=True))


if __name__ == "__main__":
    unittest.main()
