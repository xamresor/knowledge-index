#!/usr/bin/env python3
"""batch: several tool calls in one request, each with the caller's own rights."""
from __future__ import annotations

import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401

import kb_core


class AddonOff(unittest.TestCase):
    def test_an_addon_that_is_off_has_no_tool(self):
        with mock.patch.dict("os.environ", {"KB_ADDONS": "none"}):
            names = [t["name"] for t in kb_core.tools()]
            self.assertNotIn("batch", names)
            self.assertNotIn("run_code", names)
            self.assertIn("search", names)
            with self.assertRaises(kb_core.ToolError) as e:
                kb_core.call_tool("batch", {"calls": [{"tool": "version"}]})
            self.assertIn("kb addon install tool-multicall", str(e.exception))


class Batch(unittest.TestCase):
    def setUp(self):
        self.seen = []
        real = kb_core.call_tool

        def spy(name, args, allowed=None, caller=None):
            self.seen.append((name, allowed, caller))
            return real(name, args, allowed, caller)
        p = mock.patch.object(kb_core, "call_tool", spy)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(kb_core, "_index", lambda fn, *a: f"index {a[0]}")
        q.start()
        self.addCleanup(q.stop)

    def test_answers_in_order_and_one_error_stays_local(self):
        out = kb_core.call_tool("batch", {"calls": [
            {"tool": "contracts_search", "args": {"query": "login"}},
            {"tool": "graph_explain", "args": {}},
            {"tool": "contracts_search", "args": {"query": "drivers"}}]}, {"api"}, "ann")
        self.assertLess(out.index("index login"), out.index("index drivers"))
        self.assertIn("(error: graph_explain: 'symbol' is required)", out)

    def test_each_call_keeps_the_callers_rights(self):
        kb_core.call_tool("batch", {"calls": [{"tool": "contracts_search", "args": {"query": "x"}}]},
                          {"api"}, "ann")
        inner = [s for s in self.seen if s[0] == "contracts_search"]
        self.assertEqual(inner, [("contracts_search", {"api"}, "ann")])

    def test_limits(self):
        for calls in ([], [{"tool": "version"}] * (kb_core.BATCH_MAX + 1),
                      [{"tool": "batch", "args": {"calls": []}}], [{"args": {}}]):
            with self.assertRaises(kb_core.ToolError):
                kb_core.call_tool("batch", {"calls": calls})


if __name__ == "__main__":
    unittest.main()
