#!/usr/bin/env python3
"""Safety net for the graph tools (graph_path, graph_affected, graph_explain).

Two kinds of tests:

- GUARD tests pass today and must keep passing after any change to path search: a limited caller
  never gets another project's nodes, the main graph file is never rewritten, domain anchors stay
  where the dashboard and `explain` need them.
- TARGET tests pin two bugs fixed on 2026-11-19, so they cannot come back:
    1. graph_path walks `in_domain` edges, so two symbols that only share a domain look connected.
    2. graph_affected does not follow `http_request`, so a controller shows no frontend callers.

The first half stubs graphify and looks at the graph file the tool would hand to it. The second half
runs the real `graphify` and is skipped when it is not installed or does not start.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401  (puts bin/ on sys.path)

import access
import kb_core


def fixture() -> dict:
    """api + web + admin. `api::Report` and `api::Mailer` share the Billing domain and nothing else.
    `admin::Secret` belongs to a project the limited caller ("web") may not see."""
    def n(i, repo, typ="code"):
        return {"id": i, "label": i.split("::")[1], "repo": repo, "type": typ,
                "file_type": "code", "source_file": f"{i.replace('::', '/')}.x"}

    def e(s, t, rel="calls"):
        return {"source": s, "target": t, "relation": rel, "confidence": "EXTRACTED", "weight": 1.0}

    return {
        "directed": True, "multigraph": False, "graph": {}, "hyperedges": [],
        "nodes": [
            n("api::LoginCtl", "api"), n("api::AuthService", "api"), n("api::UserRepo", "api"),
            n("api::Report", "api"), n("api::Mailer", "api"),
            n("web::LoginPage", "web"), n("web::Store", "web"),
            n("admin::Secret", "admin"),
            n("domain::Auth", "domain", "domain"), n("domain::Billing", "domain", "domain"),
        ],
        "links": [
            e("api::LoginCtl", "api::AuthService"),
            e("api::AuthService", "api::UserRepo"),
            e("web::LoginPage", "api::LoginCtl", "http_request"),
            e("web::LoginPage", "web::Store", "imports"),
            e("admin::Secret", "api::UserRepo"),
            e("api::LoginCtl", "domain::Auth", "in_domain"),
            e("web::LoginPage", "domain::Auth", "in_domain"),
            e("admin::Secret", "domain::Auth", "in_domain"),
            e("api::Report", "domain::Billing", "in_domain"),
            e("api::Mailer", "domain::Billing", "in_domain"),
        ],
    }


PROJECTS = {"api": [], "web": [], "admin": []}


class _Sandbox(unittest.TestCase):
    """A temp data dir with the fixture as the main graph; kb_core and access both point at it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = self._tmp.name
        self.graph_file = os.path.join(d, "graph.json")
        with open(self.graph_file, "w") as f:
            json.dump(fixture(), f)
        for target, name, value in (
                (access, "GRAPH", self.graph_file),
                (access, "FILTERED_DIR", os.path.join(d, "filtered")),
                (kb_core, "GRAPH", self.graph_file),
                (kb_core, "PROJECTS", PROJECTS),
                (kb_core, "HOME_PROJECTS", [])):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def digest(self) -> str:
        with open(self.graph_file, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()


class _Stubbed(_Sandbox):
    """graphify is replaced by a recorder that loads the graph file it was pointed at."""

    def setUp(self):
        super().setUp()
        self.calls: list[tuple[list[str], dict]] = []

        def fake_run(cmd, **kw):
            with open(cmd[cmd.index("--graph") + 1]) as f:
                self.calls.append((cmd, json.load(f)))
            return "ok"

        p = mock.patch.object(kb_core, "run", fake_run)
        p.start()
        self.addCleanup(p.stop)

    def graph_given_to(self, tool: str, args: dict, allowed=None) -> tuple[list[str], dict]:
        self.calls.clear()
        kb_core.call_tool(tool, args, allowed)
        self.assertEqual(len(self.calls), 1)
        return self.calls[0]

    def view_given_to_path(self, args: dict, allowed=None) -> dict:
        """graph_path runs bin/routes.py (k=1) on a view of the main graph: the view is the access cut."""
        seen = []
        with mock.patch.object(kb_core.warm, "graph", lambda: None), \
                mock.patch.object(kb_core.warm, "graph_once", lambda argv, view: seen.append((argv, view)) or "Path 1"):
            kb_core.call_tool("graph_path", args, allowed)
        self.assertEqual(len(seen), 1)
        argv, view = seen[0]
        self.assertEqual(argv[0], "routes")
        self.assertEqual(json.loads(argv[1])["k"], 1)
        return view

    @staticmethod
    def ids(g: dict) -> set[str]:
        return {n["id"] for n in g["nodes"]}

    @staticmethod
    def relations(g: dict) -> set[str]:
        return {e["relation"] for e in g["links"]}


PATH_ARGS = {"from": "LoginPage", "to": "UserRepo"}


class AccessGuards(_Stubbed):
    """Must pass before and after the path fix."""

    def test_limited_caller_path_graph_has_no_hidden_project(self):
        view = self.view_given_to_path(PATH_ARGS, {"web"})
        self.assertEqual(view, {"allowed": ["web"], "routes": True})

    def test_limited_caller_affected_and_explain_have_no_hidden_project(self):
        for tool in ("graph_affected", "graph_explain"):
            _, g = self.graph_given_to(tool, {"symbol": "LoginPage"}, {"web"})
            self.assertNotIn("admin::Secret", self.ids(g), tool)

    def test_project_argument_cannot_widen_access(self):
        with self.assertRaises(kb_core.ToolError):
            kb_core.call_tool("graph_path", {**PATH_ARGS, "project": "admin"}, {"web"})

    def test_project_argument_narrows_a_full_caller(self):
        view = self.view_given_to_path({**PATH_ARGS, "project": "web"}, None)
        self.assertEqual(view["allowed"], ["web"])

    def test_main_graph_file_is_never_rewritten(self):
        before = self.digest()
        for allowed in (None, {"web"}, {"api", "web", "admin"}):
            kb_core.call_tool("graph_path", PATH_ARGS, allowed)
            kb_core.call_tool("graph_affected", {"symbol": "LoginCtl"}, allowed)
            kb_core.call_tool("graph_explain", {"symbol": "LoginCtl"}, allowed)
        self.assertEqual(self.digest(), before)

    def test_explain_keeps_domain_anchors(self):
        """`explain` shows which domain a symbol is in; the dashboard reads the same main graph."""
        for allowed in (None, {"web"}):
            _, g = self.graph_given_to("graph_explain", {"symbol": "LoginPage"}, allowed)
            self.assertIn("in_domain", self.relations(g))
            self.assertIn("domain::Auth", self.ids(g))

    def test_real_relations_survive_in_the_path_graph(self):
        for allowed in (None, {"api", "web"}):
            _, g = self.graph_given_to("graph_affected", {"symbol": "LoginCtl"}, allowed)
            self.assertTrue({"calls", "http_request", "imports"} <= self.relations(g))


class PathTargets(_Stubbed):
    """Bug 1: path search must not walk `in_domain`."""

    def test_full_caller_path_graph_has_no_domain_edges(self):
        self.assertTrue(self.view_given_to_path(PATH_ARGS, None)["routes"])   # routes view = no anchors

    def test_limited_caller_path_graph_has_no_domain_edges(self):
        self.assertTrue(self.view_given_to_path(PATH_ARGS, {"web"})["routes"])


class AffectedTargets(_Stubbed):
    """Bug 2: impact analysis must cross the HTTP boundary."""

    def test_affected_follows_http_request(self):
        cmd, _ = self.graph_given_to("graph_affected", {"symbol": "LoginCtl"}, None)
        self.assertIn("http_request", cmd)

    def test_affected_never_follows_in_domain(self):
        cmd, _ = self.graph_given_to("graph_affected", {"symbol": "LoginCtl"}, None)
        self.assertNotIn("in_domain", cmd)


def _graphify_works() -> bool:
    try:
        return subprocess.run(["graphify", "--version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(_graphify_works(), "graphify is not installed or does not start")
class RealGraphify(_Sandbox):
    """The same questions, answered by the real graphify on the fixture graph."""

    def ask(self, tool: str, args: dict, allowed=None) -> str:
        return kb_core.call_tool(tool, args, allowed)

    def test_graphify_starts(self):
        """Guards against a silently dead graphify (stubbed tests cannot see it)."""
        out = self.ask("graph_explain", {"symbol": "LoginCtl"})
        self.assertNotIn("Traceback", out)
        self.assertIn("LoginCtl", out)

    def test_connected_symbols_get_a_path(self):
        out = self.ask("graph_path", {"from": "LoginPage", "to": "UserRepo"})
        self.assertIn("Path 1:", out)

    def test_path_is_the_call_chain_not_the_domain_shortcut(self):
        """Both routes are 3 hops; the one through the domain anchor used to win."""
        out = self.ask("graph_path", {"from": "LoginPage", "to": "UserRepo"})
        self.assertNotIn("in_domain", out)
        for hop in ("LoginPage", "LoginCtl", "AuthService", "UserRepo"):
            self.assertIn(hop, out)

    def test_limited_caller_cannot_reach_hidden_nodes(self):
        out = self.ask("graph_path", {"from": "LoginPage", "to": "Secret"}, {"web"})
        self.assertNotIn("admin", out.split("\n", 1)[1])
        out = self.ask("graph_explain", {"symbol": "LoginPage"}, {"web"})
        self.assertNotIn("Secret", out)

    def test_explain_shows_the_domain(self):
        self.assertIn("in_domain", self.ask("graph_explain", {"symbol": "LoginCtl"}))

    def test_same_domain_only_is_not_a_path(self):
        out = self.ask("graph_path", {"from": "Report", "to": "Mailer"})
        self.assertNotIn("Path 1:", out)          # a domain anchor is no node of a path

    def test_affected_lists_the_frontend_caller(self):
        self.assertIn("LoginPage", self.ask("graph_affected", {"symbol": "LoginCtl"}))


if __name__ == "__main__":
    unittest.main()
