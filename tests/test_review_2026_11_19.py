#!/usr/bin/env python3
"""Fixes from the first outside users' review of the shared server (2026-11-19).

- graph_affected for a model named like its domain anchor (`Order`) stopped at "ambiguous".
- A call site that lost its path prefix (`${baseUrl.value}/send`) was linked to the shortest route
  ending in `/send`, i.e. to the wrong controller.
- List and export composables (`usePaginatedList(() => \`v2/…\`)`) were not seen as API callers.
- The code search told MCP callers to use CLI options they do not have.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from _kbtest import BIN, load_script  # noqa: F401

import access
import kb_core
from lang import js_ts

link_http = load_script("link_http.py")


def calls(src: str) -> list[tuple[str, str]]:
    return [(v, p) for _, v, p in js_ts.iter_calls(src.split("\n"))]


class AffectedSkipsDomainAnchors(unittest.TestCase):
    def test_affected_gets_the_graph_without_anchors(self):
        g = {"directed": True, "multigraph": False, "graph": {}, "hyperedges": [],
             "nodes": [{"id": "api::M", "label": "Order", "repo": "api"},
                       {"id": "api::S", "label": "OrderService", "repo": "api"},
                       {"id": "domain::Order", "label": "Order", "repo": "domain", "type": "domain"}],
             "links": [{"source": "api::S", "target": "api::M", "relation": "calls"},
                       {"source": "api::M", "target": "domain::Order", "relation": "in_domain"}]}
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "graph.json")
            with open(src, "w") as f:
                json.dump(g, f)
            seen = []

            def fake_run(cmd, **kw):
                with open(cmd[cmd.index("--graph") + 1]) as f:
                    seen.append(json.load(f))
                return "ok"

            with mock.patch.object(access, "GRAPH", src), \
                    mock.patch.object(access, "FILTERED_DIR", os.path.join(d, "f")), \
                    mock.patch.object(kb_core, "GRAPH", src), \
                    mock.patch.object(kb_core, "PROJECTS", {"api": []}), \
                    mock.patch.object(kb_core, "HOME_PROJECTS", []), \
                    mock.patch.object(kb_core, "run", fake_run):
                kb_core.call_tool("graph_affected", {"symbol": "Order"})
                kb_core.call_tool("graph_explain", {"symbol": "Order"})
        labels = [n["label"] for n in seen[0]["nodes"]]
        self.assertEqual(labels.count("Order"), 1)            # affected: no anchor, no ambiguity
        self.assertIn("domain::Order", {n["id"] for n in seen[1]["nodes"]})  # explain keeps it


class AmbiguousSuffix(unittest.TestCase):
    routes = {("post", "v1/invoices/{}/send"): ("Invoice.php", "send"),
              ("post", "v1/orders/{}/receipts/{}/send"): ("Receipt.php", "send"),
              ("get", "v2/drivers"): ("Drivers.php", "index"),
              ("get", "v2/customers/{}/drivers"): ("Drivers.php", "byCustomer")}

    def test_two_candidates_is_no_match(self):
        self.assertIsNone(link_http.match_route(self.routes, "post", "send"))

    def test_base_prefix_still_matches(self):
        self.assertEqual(link_http.match_route(self.routes, "get", "drivers"), ("Drivers.php", "index"))

    def test_one_candidate_still_matches(self):
        self.assertEqual(link_http.match_route(self.routes, "get", "customers/{}/drivers"),
                         ("Drivers.php", "byCustomer"))


class VersionedPathArguments(unittest.TestCase):
    def test_list_composable_on_the_next_line(self):
        self.assertEqual(calls("const x = usePaginatedList(\n  () => `v2/customers/${id}/drivers`,\n  {})"),
                         [("get", "v2/customers/${id}/drivers")])

    def test_export_helper(self):
        self.assertEqual(calls("exportTable(`v2/customers/${c}/drivers/export`, {filename: 'x'})"),
                         [("get", "v2/customers/${c}/drivers/export")])

    def test_method_of_the_next_call_is_not_borrowed(self):
        self.assertEqual(calls("exportTable(`v2/a/export`, {})\nawait $api('v2/b', { method: 'POST' })"),
                         [("get", "v2/a/export"), ("post", "v2/b")])

    def test_not_a_client(self):
        self.assertEqual(calls("t('v2/not/a/url')\nif ('v1/x'.startsWith(y)) {}"), [])
        self.assertEqual(calls("$api<Page<Item>>('v2/items')"), [("get", "v2/items")])  # counted once


class SearchHint(unittest.TestCase):
    def test_cli_hint_is_replaced(self):
        out = kb_core._mcp_hint("... (truncated — 152 more nodes cut by ~1800-token budget. Narrow "
                                "with context_filter=['call'] or use get_node for a specific symbol)")
        self.assertNotIn("context_filter", out)
        self.assertNotIn("get_node", out)
        self.assertIn("budget", out)

    def test_budget_is_clamped(self):
        self.assertEqual(kb_core._budget({}), 1800)
        self.assertEqual(kb_core._budget({"budget": 99999}), kb_core.CODE_BUDGET_MAX)
        self.assertEqual(kb_core._budget({"budget": 10}), 300)

    def test_instructions_name_the_tools(self):
        text = kb_core.instructions(["api", "shop-web"])
        for word in ("contracts_search", "graph_affected", "in_domain", "INFERRED", "api, shop-web"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
