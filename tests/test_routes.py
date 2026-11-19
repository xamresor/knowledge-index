#!/usr/bin/env python3
"""graph_routes (bin/routes.py through the graph worker): several weighted ways between two places."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401

import kb_core
import warm


def fixture() -> dict:
    """A page calls the customers route; the SW backend calls the API; a framework hub joins all."""
    def n(i, repo, typ="class", src=True):
        return {"id": i, "label": i.split("::")[1], "repo": repo, "type": typ,
                "source_file": f"{i.split('::')[1]}.x" if src else "", "source_location": "L1"}

    def e(s, t, rel="calls", **kw):
        return {"source": s, "target": t, "relation": rel, "confidence": "EXTRACTED", **kw}

    nodes = [n("api::Customer", "api", "model"), n("api::CustomerController.index()", "api", "method"),
             n("api::BridgeController.clients()", "api", "method"),
             n("sw-api::HomeApiClient.clients()", "sw-api", "method"),
             n("sw-api::TicketController", "sw-api", "controller"),
             n("bo2::Clients.vue", "bo2", "template"),
             n("api::Illuminate\\Response", "api", "class", src=False),
             n("admin::Secret", "admin"),
             n("domain::Customer", "domain", "domain")]
    links = [e("api::CustomerController.index()", "api::Customer", "references"),
             e("bo2::Clients.vue", "api::CustomerController.index()", "http_request",
               http_method="GET", http_uri="v2/customers"),
             e("api::BridgeController.clients()", "api::Customer", "calls"),
             e("sw-api::HomeApiClient.clients()", "api::BridgeController.clients()", "http_request",
               http_method="GET", http_uri="v1/sw-bridge/clients"),
             e("sw-api::TicketController", "sw-api::HomeApiClient.clients()", "calls"),
             # shortcuts that must lose: a framework hub and the domain grouping
             e("api::Customer", "api::Illuminate\\Response", "references"),
             e("sw-api::TicketController", "api::Illuminate\\Response", "references"),
             e("api::Customer", "domain::Customer", "in_domain"),
             e("sw-api::TicketController", "domain::Customer", "in_domain"),
             e("admin::Secret", "api::Customer", "calls"),
             e("admin::Secret", "sw-api::TicketController", "calls")]
    return {"directed": True, "multigraph": False, "graph": {}, "nodes": nodes, "links": links,
            "hyperedges": []}


@unittest.skipUnless(warm.graphify_python(), "graphify (and its networkx) is not installed")
class GraphRoutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = os.path.join(self._tmp.name, "graph.json")
        with open(path, "w") as f:
            json.dump(fixture(), f)
        for target, name, value in ((kb_core, "GRAPH", path),
                                    (kb_core, "PROJECTS", {"api": [], "sw-api": [], "bo2": [], "admin": []}),
                                    (warm, "ENABLED", False)):  # the one-shot path; the worker runs the same code
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_real_chain_with_its_route_beats_the_shortcuts(self):
        out = kb_core.call_tool("graph_routes", {"from": "api:Customer", "to": "TicketController"},
                                {"api", "sw-api", "bo2"})
        first = out.split("Path 2")[0]
        self.assertIn("GET v1/sw-bridge/clients", first)
        self.assertIn("HomeApiClient.clients()", first)
        self.assertNotIn("Illuminate", first)   # the hub costs too much
        self.assertNotIn("in_domain", out)      # the domain grouping is not a path

    def test_hidden_project_never_appears(self):
        out = kb_core.call_tool("graph_routes", {"from": "Customer", "to": "TicketController"},
                                {"api", "sw-api", "bo2"})
        self.assertNotIn("Secret", out)
        with self.assertRaises(kb_core.ToolError):
            kb_core.call_tool("graph_routes", {"from": "admin:Secret", "to": "Customer"}, {"api"})

    def test_unknown_name(self):
        out = kb_core.call_tool("graph_routes", {"from": "Nope", "to": "Customer"})
        self.assertIn('No node matches "Nope"', out)


if __name__ == "__main__":
    unittest.main()
