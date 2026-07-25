"""Tests for bin/dedupe.py — the destructive transform that merges duplicate symbol nodes.

Why these tests exist: dedupe rewires **every** edge in the graph onto canonical ids and deletes
nodes. Until now the only tested script was the HTTP matcher (a pure parser), i.e. coverage sat
where the risk wasn't. The three behaviours locked here are exactly the ones its docstring promises:

  * one definition exists            -> merge the reference copies into it
  * no definition exists             -> merge into one shared node
  * several definitions share a label -> ambiguous, leave everything untouched

Plus the two edge-level guarantees: self-loops and duplicate edges disappear after rewiring.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from _kbtest import BIN

DEDUPE = os.path.join(BIN, "dedupe.py")


def node(nid: str, label: str, repo: str = "api") -> dict:
    return {"id": nid, "label": label, "repo": repo}


def edge(src: str, tgt: str, relation: str = "uses") -> dict:
    return {"source": src, "target": tgt, "relation": relation}


def run_dedupe(graph: dict) -> dict:
    """Run the script as a subprocess (the real entry point) and return the rewritten graph."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "graph.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(graph, fh)
        proc = subprocess.run([sys.executable, DEDUPE, path], capture_output=True, text=True)
        assert proc.returncode == 0, f"dedupe failed: {proc.stderr}"
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)


class DedupeTest(unittest.TestCase):
    def test_reference_copies_merge_into_the_single_definition(self):
        g = {
            "nodes": [
                node("api::app/Models/User.php::User", "User"),        # the definition
                node("api::app/Http/C.php::User", "User"),             # a reference copy
                node("api::app/Services/S.php::User", "User"),         # another reference copy
                node("api::app/Models/User.php::save()", "save()"),
                node("api::app/Http/C.php::store()", "store()"),
            ],
            "links": [
                edge("api::app/Models/User.php::User", "api::app/Models/User.php::save()", "method"),
                edge("api::app/Http/C.php::store()", "api::app/Http/C.php::User"),
                edge("api::app/Services/S.php::S", "api::app/Services/S.php::User"),
            ],
        }
        out = run_dedupe(g)
        ids = {n["id"] for n in out["nodes"]}
        self.assertIn("api::app/Models/User.php::User", ids, "the definition must survive")
        self.assertNotIn("api::app/Http/C.php::User", ids, "reference copies must be gone")
        self.assertNotIn("api::app/Services/S.php::User", ids)
        targets = {e["target"] for e in out["links"] if e.get("relation") == "uses"}
        self.assertEqual(targets, {"api::app/Models/User.php::User"},
                         "both users must now point at the canonical node")

    def test_without_a_definition_copies_collapse_into_one_shared_node(self):
        g = {
            "nodes": [
                node("api::a.php::FormRequest", "FormRequest"),
                node("api::b.php::FormRequest", "FormRequest"),
                node("api::c.php::FormRequest", "FormRequest"),
            ],
            "links": [
                edge("api::a.php::A", "api::a.php::FormRequest"),
                edge("api::b.php::B", "api::b.php::FormRequest"),
                edge("api::c.php::C", "api::c.php::FormRequest"),
            ],
        }
        out = run_dedupe(g)
        survivors = [n for n in out["nodes"] if n["label"] == "FormRequest"]
        self.assertEqual(len(survivors), 1, "three copies with no definition -> one shared node")
        self.assertEqual({e["target"] for e in out["links"]}, {survivors[0]["id"]})

    def test_ambiguous_labels_are_left_untouched(self):
        """Two real definitions of the same name: merging would invent a fact. Don't."""
        g = {
            "nodes": [
                node("api::app/Billing/Status.php::Status", "Status"),
                node("api::app/Orders/Status.php::Status", "Status"),
                node("api::app/Billing/Status.php::code()", "code()"),
                node("api::app/Orders/Status.php::code()", "code()"),
            ],
            "links": [
                edge("api::app/Billing/Status.php::Status", "api::app/Billing/Status.php::code()", "method"),
                edge("api::app/Orders/Status.php::Status", "api::app/Orders/Status.php::code()", "method"),
            ],
        }
        out = run_dedupe(g)
        ids = {n["id"] for n in out["nodes"]}
        self.assertIn("api::app/Billing/Status.php::Status", ids)
        self.assertIn("api::app/Orders/Status.php::Status", ids)
        self.assertEqual(len(out["links"]), 2, "no edge may be rewired when the label is ambiguous")

    def test_self_loops_and_duplicate_edges_are_dropped_after_rewiring(self):
        g = {
            "nodes": [
                node("api::m.php::Order", "Order"),        # definition
                node("api::x.php::Order", "Order"),        # copy
                node("api::m.php::pay()", "pay()"),
            ],
            "links": [
                edge("api::m.php::Order", "api::m.php::pay()", "method"),
                # after remapping both endpoints become the canonical node -> self-loop
                edge("api::x.php::Order", "api::m.php::Order"),
                # two identical edges that collapse onto the same pair -> one must remain
                edge("api::m.php::pay()", "api::x.php::Order"),
                edge("api::m.php::pay()", "api::m.php::Order"),
            ],
        }
        out = run_dedupe(g)
        self.assertFalse([e for e in out["links"] if e["source"] == e["target"]],
                         "self-loops must be dropped")
        uses = [e for e in out["links"] if e.get("relation") == "uses"]
        self.assertEqual(len(uses), 1, "duplicate edges must collapse to one")

    def test_repos_are_isolated(self):
        """Same label in two repos is two different symbols — merging across repos would be wrong."""
        g = {
            "nodes": [
                node("api::a.php::Client", "Client", repo="api"),
                node("web::a.ts::Client", "Client", repo="web"),
            ],
            "links": [],
        }
        out = run_dedupe(g)
        self.assertEqual(len(out["nodes"]), 2)


if __name__ == "__main__":
    unittest.main()
