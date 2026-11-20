#!/usr/bin/env python3
"""bin/repomap.py: the most depended-on parts first, frontend modules counted, budget split per project."""
from __future__ import annotations

import unittest

from _kbtest import BIN  # noqa: F401

import repomap
from test_review import GRAPH, ROUTES, node


class RepoMap(unittest.TestCase):
    def test_ranks_by_dependents_and_shows_routes(self):
        out = repomap.build(GRAPH, ROUTES, ["api"], 1500)
        self.assertIn("## api:", out)
        lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
        self.assertTrue(lines[0].startswith("- OrderController"), out)   # web and index() depend on it
        self.assertIn("serves 2 routes", lines[0])
        self.assertIn("store() ", lines[0])

    def test_frontend_module_is_a_unit(self):
        out = repomap.build(GRAPH, ROUTES, ["web"], 1500)
        self.assertIn("- orders.ts (src/orders.ts) — 1 dependents", out)  # OrdersPage imports it

    def test_budget_is_split_and_respected(self):
        g = {"nodes": [node(f"a::c{i}", f"C{i}", "class", f"app/C{i}.php", 1, "a") for i in range(60)]
                      + [node(f"a::u{i}", f"U{i}", "class", f"app/U{i}.php", 1, "a") for i in range(60)]
                      + [node("b::x", "X", "class", "app/X.php", 1, "b"), node("b::y", "Y", "class", "app/Y.php", 1, "b")],
             "links": [{"relation": "calls", "source": f"a::u{i}", "target": f"a::c{i}", "source_file": f"app/U{i}.php"}
                       for i in range(60)]
                      + [{"relation": "calls", "source": "b::y", "target": "b::x", "source_file": "app/Y.php"}]}
        out = repomap.build(g, [], ["a", "b"], 300)
        self.assertIn("## b:", out)                  # the second project still gets its share
        self.assertIn("- X (app/X.php)", out)
        self.assertIn("more: raise 'budget'", out)
        self.assertLess(len(out), 300 * 4 + 400)


class Unused(unittest.TestCase):
    G = {"nodes": [node("a::lonely", "Lonely", "class", "app/Support/Lonely.php", 3, "a"),
                   node("a::named", "Named", "class", "app/Support/Named.php", 3, "a"),
                   node("a::job", "SendMail", "class", "app/Jobs/SendMail.php", 3, "a"),
                   node("a::used", "Used", "class", "app/Support/Used.php", 3, "a"),
                   node("a::user", "User", "class", "app/Models/User.php", 3, "a"),
                   node("w::page", "page.tsx", "file", "src/app/orders/page.tsx", 1, "w")],
         "links": [{"relation": "calls", "source": "a::used", "target": "a::user", "source_file": "app/Models/User.php"}]}

    def test_candidates_skip_conventions_entry_points_and_used(self):
        out = repomap.unused(self.G, ["a", "w"])
        self.assertIn("a:app/Support/Lonely.php", out)
        self.assertIn("a:app/Support/Named.php", out)
        self.assertNotIn("SendMail", out)            # a job is wired by the framework
        self.assertNotIn("Used.php", out)            # User calls it
        self.assertNotIn("page.tsx", out)            # a file-system route

    def test_a_name_in_another_file_keeps_it(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "app", "Services"))
            with open(os.path.join(d, "app", "Services", "Factory.php"), "w") as f:
                f.write("<?php return ['x' => new Named()];\n")
            out = repomap.unused(self.G, ["a"], roots={"a": d})
        self.assertIn("Lonely.php", out)
        self.assertNotIn("Named.php", out)


if __name__ == "__main__":
    unittest.main()
