#!/usr/bin/env python3
"""Third audit round of the query tools: review_context speed, the qmd worker's sub-query cap,
cursor budgets and owners, project pins, widened picks, contracts_search under `own`, problems
under `own`, grep exit 2, bounded process output, docs_get pre-check, and the unused scan."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401

import contracts
import kb_core
import repomap
import review


def _projects(test, projects=("api", "bo2", "sw"), home=()):
    for name, value in (("PROJECTS", {p: [] for p in projects}), ("DOC_PROJECTS", set(projects)),
                        ("HOME_PROJECTS", list(home))):
        p = mock.patch.object(kb_core, name, value)
        p.start()
        test.addCleanup(p.stop)


class ReviewLineToSymbol(unittest.TestCase):
    """1: changed lines find their symbol by a binary search; lines past the end stay cheap."""

    def graph(self, n):
        nodes = [{"id": f"m{i}", "label": f".m{i}()", "type": "method", "repo": "api",
                  "source_file": "app/Big.php", "source_location": f"L{10 * i + 5}"} for i in range(n)]
        return {"nodes": nodes, "links": []}

    def test_many_lines_on_a_big_file_are_fast(self):
        lines = set(range(1, 20_001))
        start = time.monotonic()
        out = review.analyze(self.graph(1000), {"app/Big.php": lines})
        self.assertLess(time.monotonic() - start, 1)
        self.assertIn("1000 symbol(s)", out)

    def test_owner_is_the_last_symbol_before_the_line(self):
        g = self.graph(3)   # symbols at lines 5, 15, 25
        out = review.analyze(g, {"app/Big.php": {3}})
        self.assertNotIn(".m0()", out)
        out = review.analyze(g, {"app/Big.php": {16, 9999}})
        self.assertIn(".m1()", out)
        self.assertIn(".m2()", out)
        self.assertNotIn(".m0()", out)


class WorkerSearchCap(unittest.TestCase):
    """2: the qmd worker gets at most 10 sub-queries, the vec line kept."""

    def test_long_question_is_cut_to_ten(self):
        q = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet "
             "verylongspecificterm kilo")
        with mock.patch.object(kb_core, "DOCS_MODE", "light"):
            searches, _ = kb_core._searches({}, q)
            self.assertLessEqual(len(searches), 10)
            self.assertEqual(searches[-1]["type"], "vec")
            self.assertIn("verylongspecificterm", [s["query"] for s in searches])
            lex, _ = kb_core._searches({"_lex_only": True}, q)
            self.assertLessEqual(len(lex), 10)
            self.assertTrue(all(s["type"] == "lex" for s in lex))

    def test_short_question_is_unchanged(self):
        with mock.patch.object(kb_core, "DOCS_MODE", "light"):
            searches, _ = kb_core._searches({}, "customer invoice")
        self.assertEqual([s["type"] for s in searches], ["lex", "lex", "vec"])


class CursorBudgets(unittest.TestCase):
    """3: a byte cap per owner; under pressure the putting owner's own oldest answers go first."""

    def test_one_owner_cannot_push_out_another(self):
        c = kb_core.Cursors(600, 100, max_bytes=800, per_owner=50)   # 100 bytes per owner
        b = c.put("b" * 50, "t", ("B",))
        for _ in range(10):
            c.put("a" * 90, "t", ("A",))
        self.assertEqual(c.get(b, "t", ("B",))[0], "b" * 50)

    def test_pressure_drops_the_callers_own_first(self):
        c = kb_core.Cursors(600, 100, max_bytes=200, per_owner=50, owner_bytes=150)
        b = c.put("b" * 80, "t", ("B",))
        a1 = c.put("a" * 60, "t", ("A",))
        c.put("a" * 60, "t", ("A",))
        c.put("a" * 30, "t", ("A",))
        self.assertEqual(c.get(b, "t", ("B",))[0], "b" * 80)
        with self.assertRaises(kb_core.ToolError):
            c.get(a1, "t", ("A",))

    def test_neighbour_text_is_capped(self):
        worker = mock.Mock(run=mock.Mock(return_value="x\n" * (kb_core.MAX_OUT)))
        with mock.patch.object(kb_core.warm, "graph", return_value=worker):
            out = kb_core._explain_node(("A", "class", "api", "a.php", "n1"), None)
        self.assertLess(len(out), kb_core.MAX_OUT + 500)
        self.assertIn("cut at", out)


ROWS = [
    ("Customer", "model", "api", "app/Models/Customer.php", "c1"),
    ("Customer", "class", "sw", "src/Customer.php", "c3"),
    ("Other", "class", "bo2", "x.vue", "o1"),
]


class GraphCalls(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(kb_core, "labels", lambda graph=None: ROWS)
        p.start()
        self.addCleanup(p.stop)
        self.cmds = []

    def fake_run(self, answer):
        def run(cmd, **kw):
            self.cmds.append(cmd)
            return answer(cmd)
        return run

    def test_pin_narrows_the_scope_and_strips_the_prefix(self):
        """4: `sw:Customer` asks for `Customer` in the project sw."""
        _projects(self)
        with mock.patch.object(kb_core, "run", self.fake_run(lambda c: "Node: Customer")), \
                mock.patch.object(kb_core.warm, "graph", lambda: None), \
                mock.patch.object(kb_core.access, "path_graph", lambda g: g), \
                mock.patch.object(kb_core, "graph_for", lambda p: "G-" + ",".join(sorted(p or ["all"]))):
            out = kb_core.call_tool("graph_affected", {"symbol": "sw:Customer"}, None)
        self.assertIn("[projects: sw]", out)
        self.assertEqual(self.cmds[0][2], "Customer")

    def test_pin_of_a_hidden_project_is_refused(self):
        _projects(self)
        with self.assertRaisesRegex(kb_core.ToolError, "not allowed project: sw"):
            kb_core.call_tool("graph_affected", {"symbol": "sw:Customer"}, {"api"})

    def test_member_syntax_is_not_a_pin(self):
        _projects(self)
        cmd, args, terms = kb_core._pinned(["graphify", "explain", "Order::create"], {}, None,
                                           ("Order::create",))
        self.assertEqual(terms, ("Order::create",))

    def test_widened_scope_answers_each_definition(self):
        """5: the session's project has nothing; the widened scope holds two classes `Customer`."""
        _projects(self, home=("bo2",))
        def answer(cmd):
            return "No node matching 'Customer'" if "G-bo2" in cmd else "Node: Customer (one only)"
        with mock.patch.object(kb_core, "run", self.fake_run(answer)), \
                mock.patch.object(kb_core.warm, "graph", lambda: None), \
                mock.patch.object(kb_core.warm, "graph_once", lambda argv, view: "All connections (0):"), \
                mock.patch.object(kb_core, "graph_for", lambda p: "G-" + ",".join(sorted(p or ["all"]))):
            out = kb_core.call_tool("graph_explain", {"symbol": "Customer"}, None)
        self.assertIn("ID:        c1", out)
        self.assertIn("ID:        c3", out)
        self.assertIn("all (nothing in bo2)", out)


class ContractsOwn(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def write(self, name, rows):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            json.dump(rows, f)
        return path

    def test_declared_callers_only_for_matching_routes(self):
        """6: a hidden route that does not match the words costs no declared-caller walk."""
        rows = [{"project": "billing", "method": "GET", "uri": f"api/thing{i}", "name": "", "action": "",
                 "middleware": [], "defined": "", "callers": []} for i in range(200)]
        rows.append({"project": "billing", "method": "GET", "uri": "api/invoices", "name": "", "action": "",
                     "middleware": [], "defined": "", "callers": [{"repo": "web", "at": "a.ts:L1"}]})
        cpath, conn = self.write("c.json", rows), self.write("conn.json", [])
        with mock.patch.object(contracts, "declared_callers", wraps=contracts.declared_callers) as dc:
            out = contracts.contracts_search("invoices", cpath, 10, conn, {"web"})
        self.assertIn("api/invoices", out)
        self.assertLessEqual(dc.call_count, 2)

    def test_problems_hide_other_ends(self):
        """7: under `own` a problem names a hidden project as (hidden)."""
        path = self.write("p.json", [
            {"project": "api", "kind": "missing-contract", "where": "api -> billing",
             "message": "The code calls billing (http, x), but no contracts/*.md has from: api, to: billing.",
             "others": ["billing"]},
            {"project": "api", "kind": "missing-contract", "where": "api -> billing2",   # an old index row
             "message": "The code calls billing2 (http, x), but no contracts/*.md has from: api, to: billing2."},
        ])
        out = contracts.problems("", {"api"}, path, None, {"api"})
        self.assertNotIn("billing", out)
        self.assertIn("(hidden)", out)
        self.assertIn("billing", contracts.problems("", {"api"}, path))   # contracts: all

    def test_build_problems_names_the_other_end(self):
        rows = contracts.build_problems({}, {}, {}, [{"from": "api", "to": "billing", "protocol": "http",
                                                      "summary": "s", "doc": ""}])
        self.assertEqual(rows[0]["others"], ["billing"])


@unittest.skipIf(os.geteuid() == 0, "root reads every file")
class GrepExitTwo(unittest.TestCase):
    """8: an unreadable file keeps the other matches; a bad regex is still refused."""

    def test_matches_survive_an_unreadable_file(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.txt"), "w") as f:
                f.write("needle here\n")
            locked = os.path.join(d, "b.txt")
            with open(locked, "w") as f:
                f.write("needle too\n")
            os.chmod(locked, 0)
            try:
                lines: list[str] = []
                kb_core._grep_root(["grep", "-rnI", "-E", "needle", "--", "."], d, "api", "", lines, 50)
            finally:
                os.chmod(locked, stat.S_IRUSR | stat.S_IWUSR)
        self.assertTrue(any("a.txt" in ln for ln in lines))
        self.assertTrue(any("could not be read" in ln for ln in lines))

    def test_bad_pattern_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(kb_core.ToolError, "bad pattern"):
                kb_core._grep_root(["grep", "-rn", "-E", "(", "--", "."], d, "api", "", [], 50)


class BoundedOutput(unittest.TestCase):
    """9: a producer is stopped at the limit; docs_get checks a qmd:// path first."""

    def test_endless_producer_is_stopped(self):
        # small and memory-safe: 10000 bytes, then the producer hangs; the old code waited for it
        code = "import sys, time\nsys.stdout.write('x' * 10000); sys.stdout.flush(); time.sleep(60)"
        start = time.monotonic()
        out = kb_core.run([sys.executable, "-c", code], timeout=15, limit=1000)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(out, "x" * 1000)

    def test_docs_get_refuses_a_hidden_path_before_qmd(self):
        with mock.patch.object(kb_core, "run") as run, \
                mock.patch.object(kb_core.access, "contract_mode", return_value="own"):
            out = kb_core.docs_get("qmd://kb-billing/secret.md", {"api"})
        self.assertIn("not found", out)
        run.assert_not_called()


class CursorOwner(unittest.TestCase):
    """10: the owner key holds the grant type, the contracts mode and the settings generation."""

    def test_policy_changes_change_the_owner(self):
        with mock.patch.object(kb_core.access, "contract_mode", return_value="all"):
            a = kb_core._cursor_owner("ann", {"api"})
            full = kb_core._cursor_owner("ann", kb_core.AllProjects({"api"}))
        with mock.patch.object(kb_core.access, "contract_mode", return_value="own"):
            b = kb_core._cursor_owner("ann", {"api"})
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, full)
        with mock.patch.object(kb_core.access, "contract_mode", return_value="all"), \
                mock.patch.object(kb_core.settings, "generation", create=True, return_value=7):
            c = kb_core._cursor_owner("ann", {"api"})
        self.assertNotEqual(a, c)


class UnusedScanReads(unittest.TestCase):
    """11: the unused scan skips links out of the root and non-regular files."""

    def test_link_out_and_fifo_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root, outside = os.path.join(d, "root"), os.path.join(d, "out")
            os.makedirs(root)
            os.makedirs(outside)
            with open(os.path.join(root, "a.php"), "w") as f:
                f.write("<?php class HelperThing {}")
            with open(os.path.join(outside, "b.php"), "w") as f:
                f.write("<?php new HelperThing();")
            os.symlink(os.path.join(outside, "b.php"), os.path.join(root, "link.php"))
            os.mkfifo(os.path.join(root, "pipe.php"))
            result = {}
            t = threading.Thread(target=lambda: result.update(
                r=repomap.scan_names({"api:a.php": ["HelperThing"]}, {"api": root})), daemon=True)
            t.start()
            t.join(10)
            if t.is_alive():   # unblock the reader of the old code, then fail
                with open(os.path.join(root, "pipe.php"), "w"):
                    pass
                self.fail("the scan blocked on a FIFO")
        unused, complete = result["r"]
        self.assertIn("api:a.php", unused)


if __name__ == "__main__":
    unittest.main()
