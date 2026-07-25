"""Tests for bin/declutter.py — the other destructive transform: it deletes nodes and their edges.

The behaviour worth locking is the *restraint* the script documents: it prunes **only** the curated
framework symbols listed in `kb.hubs.txt`, with no degree-based auto-detection, because after dedupe
a high-degree definition-less node is usually a real domain service whose definition graphify never
captured. A future "smart" heuristic that starts eating those would pass no test — unless the
restraint itself is a test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from _kbtest import BIN

DECLUTTER = os.path.join(BIN, "declutter.py")


def node(nid: str, label: str) -> dict:
    return {"id": nid, "label": label, "repo": "api"}


def run_declutter(graph: dict, hubs: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        gpath = os.path.join(d, "graph.json")
        hpath = os.path.join(d, "hubs.txt")
        with open(gpath, "w", encoding="utf-8") as fh:
            json.dump(graph, fh)
        with open(hpath, "w", encoding="utf-8") as fh:
            fh.write(hubs)
        proc = subprocess.run([sys.executable, DECLUTTER, gpath, hpath],
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"declutter failed: {proc.stderr}"
        with open(gpath, encoding="utf-8") as fh:
            return json.load(fh)


BASE = {
    "nodes": [
        node("api::vendor::Model", "Model"),                 # curated framework hub
        node("api::app/Services/Billing.php::Billing", "Billing"),  # real service, high degree
        node("api::app/A.php::A", "A"),
        node("api::app/B.php::B", "B"),
        node("api::app/C.php::C", "C"),
    ],
    "links": [
        {"source": "api::app/A.php::A", "target": "api::vendor::Model", "relation": "extends"},
        {"source": "api::app/B.php::B", "target": "api::vendor::Model", "relation": "extends"},
        {"source": "api::app/C.php::C", "target": "api::vendor::Model", "relation": "extends"},
        {"source": "api::app/A.php::A", "target": "api::app/Services/Billing.php::Billing",
         "relation": "uses"},
        {"source": "api::app/B.php::B", "target": "api::app/Services/Billing.php::Billing",
         "relation": "uses"},
        {"source": "api::app/C.php::C", "target": "api::app/Services/Billing.php::Billing",
         "relation": "uses"},
    ],
}


class DeclutterTest(unittest.TestCase):
    def test_curated_hub_and_its_edges_are_pruned(self):
        out = run_declutter(json.loads(json.dumps(BASE)), "Model\n")
        ids = {n["id"] for n in out["nodes"]}
        self.assertNotIn("api::vendor::Model", ids)
        self.assertFalse([e for e in out["links"] if "Model" in e["target"]],
                         "edges into a pruned hub must go too, or the graph dangles")

    def test_a_real_service_with_the_same_degree_survives(self):
        """The restraint: only the curated list is pruned, never 'looks like a hub'."""
        out = run_declutter(json.loads(json.dumps(BASE)), "Model\n")
        ids = {n["id"] for n in out["nodes"]}
        self.assertIn("api::app/Services/Billing.php::Billing", ids)
        self.assertEqual(len([e for e in out["links"] if e.get("relation") == "uses"]), 3)

    def test_comments_and_blank_lines_in_the_hub_list_are_ignored(self):
        hubs = "# framework base classes\n\nModel   # the Eloquent base\n\n"
        out = run_declutter(json.loads(json.dumps(BASE)), hubs)
        self.assertNotIn("api::vendor::Model", {n["id"] for n in out["nodes"]})

    def test_an_empty_hub_list_changes_nothing(self):
        out = run_declutter(json.loads(json.dumps(BASE)), "# nothing curated yet\n")
        self.assertEqual(len(out["nodes"]), len(BASE["nodes"]))
        self.assertEqual(len(out["links"]), len(BASE["links"]))

    def test_labels_are_matched_exactly_not_by_substring(self):
        """`Model` must not eat `ModelFactory` — substring pruning would silently gut the graph."""
        graph = json.loads(json.dumps(BASE))
        graph["nodes"].append(node("api::app/F.php::ModelFactory", "ModelFactory"))
        graph["links"].append({"source": "api::app/A.php::A",
                               "target": "api::app/F.php::ModelFactory", "relation": "uses"})
        out = run_declutter(graph, "Model\n")
        ids = {n["id"] for n in out["nodes"]}
        self.assertIn("api::app/F.php::ModelFactory", ids)


if __name__ == "__main__":
    unittest.main()
