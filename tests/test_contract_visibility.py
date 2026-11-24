#!/usr/bin/env python3
"""The `contracts` setting (bin/settings.py): `all` shows every contract to every user, `own` only the
contracts that touch an allowed project, with the hidden side cut to the bare endpoint."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401

import access
import contracts
import docs_grade
import kb_core
import settings


def own():
    return mock.patch.dict(os.environ, {"KB_CONTRACTS": "own"})


def everything():
    return mock.patch.dict(os.environ, {"KB_CONTRACTS": "all"})


class Isolated(unittest.TestCase):
    """Every test reads its own settings.json, never the checkout's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(settings, "state_file", lambda: os.path.join(self.tmp.name, "settings.json"))
        p.start()
        self.addCleanup(p.stop)
        settings._cache.clear()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("KB_CONTRACTS", None)


# ------------------------------------------------------------------------------------------ settings
class SettingsStore(Isolated):
    def test_default_file_and_env(self):
        self.assertEqual(settings.get("contracts"), "all")
        self.assertEqual(settings.source("contracts"), ("default", None))
        settings.set_value("contracts", "own")
        with open(settings.state_file()) as f:
            self.assertEqual(json.load(f), {"contracts": "own"})
        self.assertEqual(settings.get("contracts"), "own")
        self.assertEqual(settings.source("contracts"), ("file", None))
        self.assertEqual([n for n in os.listdir(self.tmp.name) if ".tmp" in n], [])  # atomic: no temp file left
        with mock.patch.dict(os.environ, {"KB_CONTRACTS": "all"}):
            self.assertEqual(settings.get("contracts"), "all")      # the env variable wins
            self.assertEqual(settings.source("contracts"), ("env", "KB_CONTRACTS"))
            with self.assertRaises(settings.Locked):
                settings.set_value("contracts", "own")

    def test_a_change_of_the_file_takes_effect_without_restart(self):
        settings.set_value("contracts", "own")
        self.assertEqual(settings.get("contracts"), "own")
        path = settings.state_file()
        with open(path, "w") as f:
            json.dump({"contracts": "all"}, f)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        self.assertEqual(settings.get("contracts"), "all")

    def test_bad_values(self):
        with self.assertRaises(ValueError):
            settings.set_value("contracts", "some")
        with self.assertRaises(KeyError):
            settings.set_value("nope", "x")
        with mock.patch.dict(os.environ, {"KB_CONTRACTS": "onw"}):   # a typo reads as the closed choice
            self.assertEqual(settings.get("contracts"), "own")
        with open(settings.state_file(), "w") as f:
            json.dump({"contracts": 7, "other": "x"}, f)
        self.assertEqual(settings.get("contracts"), "own")


# ------------------------------------------------------------------------------------------ fixtures
GRAPH = {"nodes": [
    {"id": "web::a_vue", "repo": "web"},
    {"id": "web::v_vue", "repo": "web"},
    {"id": "admin::b_vue", "repo": "admin"},
    {"id": "api::user_store", "repo": "api"},
    {"id": "sw::vehicles", "repo": "sw"},
], "links": [
    {"relation": "http_request", "http_method": "POST", "http_uri": "v2/users",
     "source": "web::a_vue", "target": "api::user_store", "source_file": "src/a.vue", "source_location": "L10"},
    {"relation": "http_request", "http_method": "POST", "http_uri": "v2/users",
     "source": "admin::b_vue", "target": "api::user_store", "source_file": "app/secret-b.vue",
     "source_location": "L7"},
    {"relation": "http_request", "http_method": "GET", "http_uri": "v1/vehicles",
     "source": "web::v_vue", "target": "sw::vehicles", "source_file": "src/v.vue", "source_location": "L5"},
]}
ROUTES = {
    "api": [{"method": "POST", "uri": "v2/users", "name": "users.store",
             "action": "App\\Http\\Controllers\\UserController@store", "middleware": ["api"],
             "path": "routes/api.php:12"}],
    "sw": [{"method": "GET|HEAD", "uri": "v1/vehicles", "name": "sw.vehicles",
            "action": "Sw\\Http\\SecretVehicleController@index", "middleware": ["sw-guard"],
            "path": "packages/sw/routes.php:3"},
           {"method": "GET", "uri": "v1/internal", "name": "sw.internal",
            "action": "Sw\\Http\\InternalController@index", "middleware": []}],
}


class ContractIndexes(Isolated):
    def setUp(self):
        super().setUp()
        d = self.tmp.name
        for project, fn, text in (
                ("sw", "sw-vraa.md", "---\nfrom: sw\nto: vraa\nprotocol: soap\nsummary: Secret SOAP of sw\n---\n"),
                ("web", "web-api.md", "---\nfrom: web\nto: api\nprotocol: http\nsummary: Web uses the API\n---\n"
                                      "`POST v2/users`\n"),
                ("api", "api-payments.md", "---\nfrom: api\nto: payments\nprotocol: https\n"
                                           "summary: Invoices\n---\n"),
                ("sw", "sw-web.md", "---\nfrom: sw\nto: web\nprotocol: ws\nsummary: Pushes secret events\n---\n")):
            os.makedirs(os.path.join(d, project, "contracts"), exist_ok=True)
            with open(os.path.join(d, project, "contracts", fn), "w") as f:
                f.write(text)
        rows = contracts.build_contracts(GRAPH, ROUTES)
        self.contracts = os.path.join(d, "contracts.json")
        self.connections = os.path.join(d, "connections.json")
        with open(self.contracts, "w") as f:
            json.dump(rows, f)
        with open(self.connections, "w") as f:
            json.dump(contracts.build_connections({p: os.path.join(d, p) for p in ("sw", "web", "api")}, rows), f)
        for p in (mock.patch.object(contracts, "CONTRACTS", self.contracts),
                  mock.patch.object(contracts, "CONNECTIONS", self.connections),
                  mock.patch.object(kb_core, "PROJECTS", {"api": [], "web": [], "sw": [], "admin": []})):
            p.start()
            self.addCleanup(p.stop)

    def call(self, name, args, allowed):
        return kb_core.call_tool(name, args, allowed)


class ContractsSearchVisibility(ContractIndexes):
    def test_all_is_todays_behaviour(self):
        with everything():
            out = self.call("contracts_search", {"query": "v2/users"}, {"web"})
        self.assertIn("UserController@store", out)
        self.assertIn("admin app/secret-b.vue:L7", out)

    def test_own_a_frontend_sees_a_hidden_route_only_as_a_bare_endpoint(self):
        with own():
            out = self.call("contracts_search", {"query": "v1/vehicles"}, {"web"})
        self.assertIn("GET /v1/vehicles", out)
        self.assertIn("web src/v.vue:L5", out)                 # its own call site
        for secret in ("SecretVehicleController", "packages/sw", "sw-guard", "sw.vehicles", "(sw)"):
            self.assertNotIn(secret, out)

    def test_own_hides_the_callers_of_hidden_projects(self):
        with own():
            out = self.call("contracts_search", {"query": "v2/users"}, {"web"})
        self.assertIn("POST /v2/users", out)
        self.assertIn("web src/a.vue:L10", out)
        for secret in ("UserController", "admin", "secret-b.vue", "routes/api.php"):
            self.assertNotIn(secret, out)
        with own():
            out = self.call("contracts_search", {"query": "v2/users"}, {"api"})
        self.assertIn("UserController@store", out)             # the own route, in full
        self.assertNotIn("secret-b.vue", out)                  # but no caller path of a hidden project
        self.assertNotIn("src/a.vue", out)

    def test_own_a_hidden_route_does_not_match_by_its_internals(self):
        with own():
            self.assertEqual(self.call("contracts_search", {"query": "SecretVehicleController"}, {"web"}),
                             "(no contracts match)")
            self.assertEqual(self.call("contracts_search", {"query": "v1/internal"}, {"web"}),
                             "(no contracts match)")           # a hidden route nobody visible calls

    def test_own_local_mode_is_unaffected(self):
        with own():
            out = self.call("contracts_search", {"query": "v1/vehicles"}, None)
        self.assertIn("SecretVehicleController", out)


class ConnectionsVisibility(ContractIndexes):
    def test_all_lists_every_service(self):
        with everything():
            out = self.call("connections", {}, {"web"})
        self.assertIn("sw -> vraa", out)

    def test_own_shows_rows_that_touch_an_allowed_project(self):
        with own():
            out = self.call("connections", {}, {"web"})
        self.assertIn("web -> api | http | Web uses the API", out)     # its own contract doc
        self.assertIn("qmd://kb_contracts-web/contracts/web-api.md", out)
        self.assertIn("web -> (hidden) | http", out)                  # its call to sw, the other side cut
        for secret in ("sw", "vraa", "Secret", "payments", "admin", "kb-api", "Pushes"):
            self.assertNotIn(secret, out)
        self.assertIn("(hidden) -> web | ws", out)                    # sw's doc about web: no summary, no doc

    def test_own_an_unknown_or_hidden_service_does_not_list_all_services(self):
        with own():
            for name in ("sw", "nope"):
                out = self.call("connections", {"service": name}, {"web"})
                self.assertTrue(out.startswith(f"(no connections of '{name}'"), out)
                for secret in ("sw,", "vraa", "payments", "admin"):
                    self.assertNotIn(secret, out)
            self.assertIn("web -> api", self.call("connections", {"service": "api"}, {"web"}))

    def test_own_local_mode_is_unaffected(self):
        with own():
            self.assertIn("sw -> vraa", self.call("connections", {}, None))


# ------------------------------------------------------------------------------------------ docs
class DocsVisibility(Isolated):
    HITS = [{"file": "qmd://kb-web/login.md", "line": 3, "docid": "#b2", "title": "Login", "score": 0.8,
             "snippet": "fe text"},
            {"file": "qmd://kb_contracts-sw/contracts/sw-vraa.md", "line": 1, "docid": "#c3", "title": "Contract",
             "score": 0.7, "snippet": "Secret SOAP"}]

    def setUp(self):
        super().setUp()
        for name, value in (("PROJECTS", {"sw": [], "web": []}), ("DOC_PROJECTS", {"sw", "web"}),
                            ("HOME_PROJECTS", [])):
            p = mock.patch.object(kb_core, name, value)
            p.start()
            self.addCleanup(p.stop)

    def qmd(self, cmds):
        def run(cmd, **kw):
            cmds.append(cmd)
            cols = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-c"]
            return json.dumps([h for h in self.HITS
                               if not cols or any(h["file"].startswith(f"qmd://{c}/") for c in cols)])
        return run

    def test_doc_allowed(self):
        with everything():
            self.assertTrue(access.doc_allowed("qmd://kb_contracts-sw/contracts/x.md", {"web"}))
            self.assertFalse(access.doc_allowed("qmd://kb-sw/contracts/x.md", {"web"}))  # its doc collection
        with own():
            self.assertFalse(access.doc_allowed("qmd://kb_contracts-sw/contracts/x.md", {"web"}))
            self.assertTrue(access.doc_allowed("qmd://kb_contracts-web/contracts/x.md", {"web"}))
            self.assertTrue(access.doc_allowed("qmd://kb-web/contracts/x.md", {"web"}))

    def test_own_search_asks_only_the_own_collections(self):
        cmds = []
        with own(), mock.patch.object(kb_core, "run", self.qmd(cmds)):
            out = kb_core.call_tool("search", {"q": "login", "domain": "docs", "refuse": False}, {"web"})
        self.assertIn("qmd://kb-web/login.md", out)
        self.assertNotIn("kb-sw", out)
        self.assertNotIn("contract docs", out)
        self.assertEqual(len(cmds), 1)
        self.assertNotIn("kb-sw", cmds[0])
        cmds = []
        with everything(), mock.patch.object(kb_core, "run", self.qmd(cmds)):
            out = kb_core.call_tool("search", {"q": "login", "domain": "docs", "refuse": False}, {"web"})
        self.assertIn("qmd://kb_contracts-sw/contracts/sw-vraa.md", out)

    def test_own_docs_get_refuses_a_hidden_contract_doc(self):
        with own(), mock.patch.object(kb_core, "run",
                                      lambda cmd, **kw: "qmd://kb_contracts-sw/contracts/sw-vraa.md  #c3\n---\nSecret"):
            self.assertIn("not found in your projects",
                          kb_core.call_tool("docs_get", {"path": "#c3"}, {"web"}))
            self.assertIn("Secret", kb_core.call_tool("docs_get", {"path": "#c3"}))   # local mode

    def test_own_grade_counts_no_contract_docs_of_other_projects(self):
        seen = []

        def open_index(kb, collections=None, skip_prefix=""):
            seen.append(collections)
            return None
        with mock.patch.object(docs_grade, "open_index", open_index), \
                mock.patch.object(docs_grade, "first_hit", lambda out: "x"), \
                mock.patch.object(docs_grade, "grade", lambda q, out, index: mock.Mock(weak=False)):
            with own():
                kb_core._judge({}, "q", "qmd://kb-web/a.md", lambda extra: "", {"web"})
            with everything():
                kb_core._judge({}, "q", "qmd://kb-web/a.md", lambda extra: "", {"web"})
        self.assertEqual(seen, [["kb-web"], ["kb-web", "kb_contracts-sw"]])


# ------------------------------------------------------------------------------------------ graph
class GraphVisibility(Isolated):
    def graph(self):
        return {"nodes": [{"id": "web::Login.vue", "repo": "web"}, {"id": "api::Ctl", "repo": "api"},
                          {"id": "api::Service", "repo": "api"}],
                "links": [{"source": "web::Login.vue", "target": "api::Ctl", "relation": "http_request"},
                          {"source": "api::Ctl", "target": "api::Service", "relation": "calls"}]}

    def test_own_keeps_no_foreign_nodes(self):
        with own():
            g = access.filter_graph(self.graph(), {"web"})
        self.assertEqual({n["id"] for n in g["nodes"]}, {"web::Login.vue"})
        self.assertEqual(g["links"], [])
        with everything():
            g = access.filter_graph(self.graph(), {"web"})
        self.assertIn("api::Ctl", {n["id"] for n in g["nodes"]})

    def test_contract_edges_off_still_hides_the_edges_under_all(self):
        with everything(), mock.patch.object(access, "KEEP_CONTRACTS", False):
            g = access.filter_graph(self.graph(), {"web"})
        self.assertEqual({n["id"] for n in g["nodes"]}, {"web::Login.vue"})

    def test_filtered_copy_per_mode(self):
        d = self.tmp.name
        src = os.path.join(d, "graph.json")
        with open(src, "w") as f:
            json.dump(self.graph(), f)
        with mock.patch.object(access, "GRAPH", src), \
                mock.patch.object(access, "FILTERED_DIR", os.path.join(d, "filtered")):
            with everything():
                shared = access.graph_for({"web"}, ["api", "web"])
            with own():
                closed = access.graph_for({"web"}, ["api", "web"])
        self.assertNotEqual(shared, closed)
        with open(shared) as f:
            self.assertIn("api::Ctl", {n["id"] for n in json.load(f)["nodes"]})
        with open(closed) as f:
            self.assertEqual({n["id"] for n in json.load(f)["nodes"]}, {"web::Login.vue"})


if __name__ == "__main__":
    unittest.main()
