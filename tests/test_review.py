#!/usr/bin/env python3
"""bin/review.py: diff parsing, line -> symbol mapping, dependents with undirected edges, routes,
docs, notes; and review_context through kb_core with the caller's projects."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401

import kb_core
import review

DIFF = """diff --git a/apps/api/app/Http/OrderController.php b/apps/api/app/Http/OrderController.php
--- a/apps/api/app/Http/OrderController.php
+++ b/apps/api/app/Http/OrderController.php
@@ -20,4 +20,5 @@ class OrderController
     public function store()
     {
-        return 1;
+        $x = 2;
+        return $x;
     }
diff --git a/apps/api/app/New.php b/apps/api/app/New.php
--- /dev/null
+++ b/apps/api/app/New.php
@@ -0,0 +1,2 @@
+<?php
+class Fresh {}
diff --git a/apps/api/app/Gone.php b/apps/api/app/Gone.php
--- a/apps/api/app/Gone.php
+++ /dev/null
@@ -1,2 +0,0 @@
-<?php
-class Gone {}
"""


def node(nid, label, typ, f, line, repo="api"):
    return {"id": nid, "label": label, "type": typ, "source_file": f, "source_location": f"L{line}", "repo": repo}


GRAPH = {"directed": False, "nodes": [
    node("api::ctl", "OrderController", "class", "app/Http/OrderController.php", 5),
    node("api::ctl_index", ".index()", "method", "app/Http/OrderController.php", 10),
    node("api::ctl_store", ".store()", "method", "app/Http/OrderController.php", 20),
    node("api::svc", "OrderService", "class", "app/Services/OrderService.php", 3),
    node("api::svc_run", ".run()", "method", "app/Services/OrderService.php", 8),
    node("web::orders", "orders.ts", "file", "src/orders.ts", 1, "web"),
    node("web::page", "OrdersPage.vue", "file", "src/OrdersPage.vue", 1, "web"),
    {"id": "api::doc", "label": "orders.md", "type": "doc", "source_file": "docs/orders.md", "repo": "api"},
    {"id": "api::why", "label": "WHY: store must stay idempotent", "type": "rationale", "repo": "api"},
], "links": [
    {"relation": "contains", "source": "api::ctl", "target": "api::ctl_store"},
    {"relation": "method", "source": "api::svc", "target": "api::svc_run"},
    # store() calls run(): written in the controller, but stored the other way round (undirected graph)
    {"relation": "calls", "source": "api::svc_run", "target": "api::ctl_store",
     "source_file": "app/Http/OrderController.php"},
    # store() calls nothing that matters here; index() calls store()
    {"relation": "calls", "source": "api::ctl_index", "target": "api::ctl_store",
     "source_file": "app/Http/OrderController.php"},
    {"relation": "http_request", "source": "web::orders", "target": "api::ctl_store", "source_file": "src/orders.ts"},
    {"relation": "imports_from", "source": "web::page", "target": "web::orders", "source_file": "src/OrdersPage.vue"},
    {"relation": "documents", "source": "api::doc", "target": "api::ctl_store"},
    {"relation": "rationale_for", "source": "api::why", "target": "api::ctl_store"},
]}
ROUTES = [{"project": "api", "method": "POST", "uri": "orders", "action": "App\\Http\\OrderController@store",
           "callers": [{"repo": "web", "at": "src/orders.ts:L4"}]},
          {"project": "api", "method": "GET", "uri": "orders", "action": "App\\Http\\OrderController@index",
           "callers": []}]


class ParseTests(unittest.TestCase):
    def test_diff_lines(self):
        ch = review.parse_diff(DIFF)
        self.assertEqual(ch["apps/api/app/Http/OrderController.php"], {22, 23})
        self.assertEqual(ch["apps/api/app/New.php"], {1, 2})
        self.assertEqual(ch["apps/api/app/Gone.php"], {1, 2})   # old numbers for a deleted file

    def test_files(self):
        self.assertEqual(review.parse_files(["a.php", "b.php:3-5,9"]), {"a.php": set(), "b.php": {3, 4, 5, 9}})

    def test_too_big(self):
        with self.assertRaises(ValueError):
            review.parse_diff("x" * (review.MAX_DIFF + 1))


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.out = review.analyze(GRAPH, review.parse_diff(DIFF), ROUTES)

    def test_changed_symbol_by_line(self):
        self.assertIn("- api:app/Http/OrderController.php: .store()\n", self.out)

    def test_dependents_respect_the_written_side(self):
        dep = self.out.split("### Depends on the change")[1].split("###")[0]
        self.assertIn("web:orders.ts", dep)          # http_request: frontend -> controller
        self.assertIn("web:OrdersPage.vue", dep)     # 2 steps, through the import
        self.assertIn("api:.index()", dep)           # index() calls store()
        self.assertNotIn(".run()", dep)              # store() calls run(): run is a dependency, not a dependent

    def test_routes_docs_notes_missing(self):
        self.assertIn("POST /orders", self.out)
        self.assertIn("called from: web src/orders.ts:L4", self.out)
        self.assertIn("GET /orders", self.out)       # index() depends on store()
        self.assertIn("api:docs/orders.md", self.out)
        self.assertIn("idempotent", self.out)
        self.assertIn("apps/api/app/New.php", self.out.split("### Not in the graph")[1])

    def test_whole_file_means_its_classes(self):
        out = review.analyze(GRAPH, review.parse_files(["app/Services/OrderService.php"]), ROUTES)
        self.assertIn("- api:app/Services/OrderService.php: OrderService\n", out)
        self.assertIn("api:.store()", out.split("### Depends on the change")[1])


class ToolTests(unittest.TestCase):
    def run_tool(self, allowed):
        seen = {}

        def fake_once(argv, view):
            seen["payload"], seen["view"] = json.loads(argv[1]), view
            return "ok"
        with mock.patch.object(kb_core, "PROJECTS", {"api": [], "web": []}), \
                mock.patch.object(kb_core.contracts, "load_index", lambda p: ROUTES), \
                mock.patch.object(kb_core.warm, "graph", lambda: None), \
                mock.patch.object(kb_core.warm, "graph_once", fake_once):
            out = kb_core.call_tool("review_context", {"diff": DIFF}, allowed)
        return out, seen

    def test_callers_outside_the_callers_projects_are_dropped(self):
        out, seen = self.run_tool({"api"})
        self.assertEqual(out, "ok")
        self.assertEqual(seen["view"]["allowed"], ["api"])
        self.assertEqual([r["callers"] for r in seen["payload"]["routes"]], [[], []])

    def test_full_access(self):
        _, seen = self.run_tool(None)
        self.assertIsNone(seen["view"]["allowed"])
        self.assertEqual(seen["payload"]["routes"][0]["callers"][0]["repo"], "web")

    def test_needs_input_and_known_project(self):
        with self.assertRaises(kb_core.ToolError):
            kb_core.call_tool("review_context", {})
        with mock.patch.object(kb_core, "PROJECTS", {"api": []}), self.assertRaises(kb_core.ToolError):
            kb_core.call_tool("review_context", {"files": ["a.php"], "project": "secret"}, {"api"})


if __name__ == "__main__":
    unittest.main()
