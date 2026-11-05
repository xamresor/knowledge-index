#!/usr/bin/env python3
"""Tests for the shared server: bin/kb-http (auth, cache, usage, MCP over HTTP), the access filter
(bin/access.py, through kb_core.call_tool) and the contract layer (bin/contracts.py).

Standard library only. No graphify, qmd or real identity endpoint: a fake endpoint on loopback
answers per token, and the graphify/qmd calls are stubbed.
"""
from __future__ import annotations

import http.client
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from _kbtest import BIN

import access
import contracts
import kb_core


def load_script(name: str):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), os.path.join(BIN, name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


kb_http = load_script("kb-http")

# token -> (status, body) of the fake identity endpoint
FAKE_ME = {
    "all": (200, {"user": {"id": 1, "name": "ann"}, "projects": ["api", "admin", "web"]}),
    "fe": (200, {"user": {"id": 2, "name": "bob"}, "projects": ["web"]}),
    "none": (200, {"user": {"id": 3, "name": "cid"}, "projects": []}),
    "bad-shape": (200, {"user": {"id": 4}}),
    "noaccess": (403, {"message": "forbidden"}),
    "gone": (404, {"message": "not found"}),
    "boom": (500, {"message": "error"}),
}


class FakeIdentity(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):  # noqa: N802
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        status, body = FAKE_ME.get(token, (401, {"message": "Unauthenticated."}))
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class OffByDefaultTests(unittest.TestCase):
    def test_refuses_without_switch_and_auth_url(self):
        self.assertIn("KB_SERVER=on", kb_http.refuse_reason({}))
        self.assertIn("KB_SERVER=on", kb_http.refuse_reason({"KB_AUTH_URL": "http://x"}))
        self.assertIn("KB_AUTH_URL", kb_http.refuse_reason({"KB_SERVER": "on"}))
        self.assertIsNone(kb_http.refuse_reason({"KB_SERVER": "on", "KB_AUTH_URL": "http://x"}))

    def test_no_default_auth_url(self):
        self.assertEqual(kb_http.Authenticator(url="").url, kb_http.AUTH_URL)
        with mock.patch.dict(os.environ, {}, clear=True):
            mod = load_script("kb-http")
        self.assertEqual(mod.AUTH_URL, "")


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idp = serve(ThreadingHTTPServer(("127.0.0.1", 0), FakeIdentity))
        cls.url = f"http://127.0.0.1:{cls.idp.server_address[1]}/me"

    @classmethod
    def tearDownClass(cls):
        cls.idp.shutdown()
        cls.idp.server_close()

    def auth(self, **kw):
        return kb_http.Authenticator(url=self.url, **kw)

    def status_of(self, header):
        try:
            self.auth().check(header)
            return 200
        except kb_http.AuthError as e:
            return e.status

    def test_header_forms(self):
        for header in (None, "", "Basic abc", "Bearer "):
            self.assertEqual(self.status_of(header), 401)
        self.assertEqual(self.status_of("bearer all"), 200)

    def test_status_mapping(self):
        self.assertEqual(self.status_of("Bearer unknown"), 401)
        self.assertEqual(self.status_of("Bearer noaccess"), 403)
        self.assertEqual(self.status_of("Bearer gone"), 403)
        self.assertEqual(self.status_of("Bearer boom"), 403)
        self.assertEqual(self.status_of("Bearer none"), 403)       # empty project list
        self.assertEqual(self.status_of("Bearer bad-shape"), 503)  # 200 without projects

    def test_unreachable_is_503_and_not_cached(self):
        a = kb_http.Authenticator(url="http://127.0.0.1:9/me", timeout=2)
        for _ in range(2):
            with self.assertRaises(kb_http.AuthError) as cm:
                a.check("Bearer all")
            self.assertEqual(cm.exception.status, 503)
        self.assertEqual(a.upstream_calls, 2)

    def test_identity(self):
        ident = self.auth().check("Bearer fe")
        self.assertEqual(ident.projects, {"web"})
        self.assertEqual(ident.name, "bob")

    def test_cache_per_token_hash(self):
        a = self.auth(ttl=60)
        a.check("Bearer all")
        a.check("Bearer all")
        for _ in range(2):
            with self.assertRaises(kb_http.AuthError):
                a.check("Bearer unknown")  # a 401 is cached too
        self.assertEqual(a.upstream_calls, 2)
        self.assertNotIn("all", a._cache)  # keys are hashes, not tokens
        self.assertTrue(all(len(k) == 64 for k in a._cache))

    def test_cache_expires(self):
        a = self.auth(ttl=0)
        a.check("Bearer all")
        a.check("Bearer all")
        self.assertEqual(a.upstream_calls, 2)


class FilterTests(unittest.TestCase):
    def graph(self):
        n = lambda i, repo, **kw: {"id": i, "label": i.split("::")[1], "repo": repo, **kw}  # noqa: E731
        e = lambda s, t, rel="calls": {"source": s, "target": t, "relation": rel}  # noqa: E731
        return {
            "directed": False, "multigraph": False, "graph": {}, "hyperedges": [{"x": 1}],
            "nodes": [
                n("api::Ctl", "api", community=1, community_name="api · Http"),
                n("api::Ctl.login", "api", community=1, community_name="api · Http"),
                n("api::Service", "api", community=1, community_name="api · Http"),
                n("web::Login.vue", "web", community=1, community_name="api · Http"),
                n("web::store", "web", community=2, community_name="web · stores"),
                n("admin::Page.vue", "admin", community=3, community_name="admin · pages"),
                n("domain::web:Auth", "domain"),
                n("domain::api:Auth", "domain"),
                n("db::users", "db"),
            ],
            "links": [
                e("api::Ctl", "api::Ctl.login", "method"),
                e("api::Ctl.login", "api::Service"),
                e("web::Login.vue", "api::Ctl.login", "http_request"),
                e("web::Login.vue", "web::store"),
                e("admin::Page.vue", "api::Ctl", "http_request"),
                e("web::Login.vue", "domain::web:Auth", "in_domain"),
                e("api::Ctl", "domain::api:Auth", "in_domain"),
                e("api::Service", "db::users", "sql"),
            ],
        }

    def ids(self, g):
        return {n["id"] for n in g["nodes"]}

    def test_frontend_only_with_contracts(self):
        g = access.filter_graph(self.graph(), {"web"}, keep_contracts=True)
        self.assertEqual(self.ids(g), {"web::Login.vue", "web::store", "domain::web:Auth",
                                       "api::Ctl.login"})
        rels = {(e["source"], e["target"], e["relation"]) for e in g["links"]}
        # the contract node keeps only its http_request edge, not its api internals
        self.assertIn(("web::Login.vue", "api::Ctl.login", "http_request"), rels)
        self.assertFalse(any("api::Service" in (e["source"], e["target"]) for e in g["links"]))
        self.assertEqual(g["hyperedges"], [])
        names = {n["id"]: n.get("community_name") for n in g["nodes"]}
        self.assertEqual(names["web::Login.vue"], "community 1")  # the hidden repo's name is gone
        self.assertEqual(names["web::store"], "web · stores")

    def test_frontend_only_without_contracts(self):
        g = access.filter_graph(self.graph(), {"web"}, keep_contracts=False)
        self.assertEqual(self.ids(g), {"web::Login.vue", "web::store", "domain::web:Auth"})
        self.assertFalse(any(e["relation"] == "http_request" for e in g["links"]))

    def test_api_only(self):
        g = access.filter_graph(self.graph(), {"api"}, keep_contracts=True)
        self.assertEqual(self.ids(g), {"api::Ctl", "api::Ctl.login", "api::Service",
                                       "domain::api:Auth", "db::users",
                                       "web::Login.vue", "admin::Page.vue"})
        for e in g["links"]:
            if "admin::Page.vue" in (e["source"], e["target"]):
                self.assertEqual(e["relation"], "http_request")

    def test_graph_for_full_access_uses_main_graph(self):
        self.assertEqual(access.graph_for({"api", "web", "admin", "x"}, ["api", "web", "admin"]),
                         access.GRAPH)
        self.assertEqual(access.graph_for(None, ["api"]), access.GRAPH)

    def test_graph_for_builds_filtered_file(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "graph.json")
            with open(src, "w") as f:
                json.dump(self.graph(), f)
            with mock.patch.object(access, "GRAPH", src), \
                    mock.patch.object(access, "FILTERED_DIR", os.path.join(d, "filtered")):
                path = access.graph_for({"admin"}, ["api", "web", "admin"])
                self.assertTrue(path.endswith("filtered/admin/graph.json"))
                with open(path) as f:
                    ids = {n["id"] for n in json.load(f)["nodes"]}
                self.assertIn("admin::Page.vue", ids)
                self.assertNotIn("api::Service", ids)

    def test_dash_arguments_refused(self):
        with self.assertRaises(kb_core.ToolError):
            kb_core.call_tool("graph_explain", {"symbol": "--graph=/x"}, {"api"})
        with self.assertRaises(kb_core.ToolError):
            kb_core.call_tool("docs_search", {"query": 5}, {"api"})

    def test_graph_tools_get_the_filtered_graph(self):
        seen = []
        with mock.patch.object(kb_core, "PROJECTS", {"api": [], "web": []}), \
                mock.patch.object(access, "graph_for", lambda allowed, known: "/f/graph.json"), \
                mock.patch.object(kb_core, "run", lambda cmd, **kw: seen.append(cmd) or "ok"):
            kb_core.call_tool("graph_explain", {"symbol": "X"}, {"web"})
            kb_core.call_tool("graph_explain", {"symbol": "X"})
        self.assertEqual(seen[0][-1], "/f/graph.json")
        self.assertEqual(seen[1][-1], kb_core.GRAPH)


class DocsFilterTests(unittest.TestCase):
    OUT = ("[note]\n"
           "qmd://kb-api/auth.md:1 #a1\nTitle: Auth\nScore:  90%\n\napi text\n\n"
           "qmd://kb-web/login.md:3 #b2\nTitle: Login\nScore:  80%\n\nfe text\n\n"
           "qmd://kb-api/contracts/web-api.md #c3\nTitle: Contract\nScore:  70%\n\nc text\n\n"
           "qmd://other/web/x.md #d4\nTitle: Other collection\nScore:  60%\n\nx\n")

    def test_search_drops_other_projects_but_keeps_contracts(self):
        cmds = []
        with mock.patch.object(kb_core, "run", lambda cmd, **kw: cmds.append(cmd) or self.OUT):
            out = kb_core.call_tool("search", {"q": "login", "domain": "docs"}, {"web"})
        self.assertIn("qmd://kb-web/login.md", out)
        self.assertIn("qmd://kb-api/contracts/web-api.md", out)  # contracts are open to all
        self.assertNotIn("qmd://kb-api/auth.md", out)
        self.assertNotIn("Other collection", out)
        self.assertNotIn("-c", cmds[0])        # every collection is searched, then filtered
        self.assertIn("25", cmds[0])           # 5x the shown hits are asked for

    def test_nothing_allowed(self):
        with mock.patch.object(kb_core, "run", lambda cmd, **kw: self.OUT):
            out = kb_core.search_docs({"q": "x"}, {"nobody"})
        self.assertIn("contracts/web-api.md", out)
        with mock.patch.object(kb_core, "run", lambda cmd, **kw: "qmd://kb-api/a.md #1\nx"):
            self.assertIn("no results in your projects", kb_core.search_docs({"q": "x"}, {"web"}))

    def test_get_checks_resolved_path(self):
        with mock.patch.object(kb_core, "run", lambda cmd, **kw: "qmd://kb-api/auth.md  #a1\n---\n1: # Auth"):
            self.assertIn("not found in your projects", kb_core.call_tool("docs_get", {"path": "#a1"}, {"web"}))
            self.assertIn("1: # Auth", kb_core.call_tool("docs_get", {"path": "#a1"}, {"api"}))
            self.assertIn("1: # Auth", kb_core.call_tool("docs_get", {"path": "#a1"}))
        with mock.patch.object(kb_core, "run", lambda cmd, **kw: "Document not found: x"):
            self.assertIn("not found in your projects", kb_core.call_tool("docs_get", {"path": "x"}, {"api"}))

    def test_doc_allowed(self):
        self.assertTrue(access.doc_allowed("qmd://kb-api/contracts/x.md", {"web"}))
        self.assertFalse(access.doc_allowed("qmd://kb-api/auth.md", {"web"}))
        self.assertTrue(access.doc_allowed("qmd://kb-shop-api/a.md", {"shop-api"}))
        self.assertFalse(access.doc_allowed("qmd://other/contracts/x.md", {"web"}))

    def test_light_docs_mode_skips_the_reranker(self):
        with mock.patch.object(kb_core, "DOCS_MODE", "light"):
            self.assertIn("--no-rerank", kb_core._qmd_cmd({}, "q"))
        with mock.patch.object(kb_core, "DOCS_MODE", "bm25"):
            self.assertEqual(kb_core._qmd_cmd({}, "q")[1], "search")

    def test_version_hides_other_projects(self):
        with mock.patch.object(kb_core, "PROJECTS", {"api": [], "web": []}):
            info = json.loads(kb_core.call_tool("version", {}, {"web"}))
        self.assertEqual(info["projects"], ["web"])


class ContractTests(unittest.TestCase):
    GRAPH = {"nodes": [
        {"id": "web::a_vue", "repo": "web"},
        {"id": "admin::b_vue", "repo": "admin"},
        {"id": "api::user_store", "repo": "api"},
    ], "links": [
        {"relation": "http_request", "http_method": "POST", "http_uri": "v2/users",
         "source": "web::a_vue", "target": "api::user_store",
         "source_file": "src/a.vue", "source_location": "L10"},
        {"relation": "http_request", "http_method": "POST", "http_uri": "v2/users",
         "source": "admin::b_vue", "target": "api::user_store",
         "source_file": "app/b.vue", "source_location": "L7"},
        {"relation": "calls", "source": "api::user_store", "target": "api::user_store"},
    ]}
    ROUTES = {"api": [
        {"method": "POST", "uri": "v2/users", "name": "users.store",
         "action": "App\\Http\\Controllers\\UserController@store",
         "middleware": ["api", "can:create,App\\Models\\User"], "path": "routes/api.php:12"},
        {"method": "GET|HEAD", "uri": "v2/users/{user}", "name": "users.show",
         "action": "App\\Http\\Controllers\\UserController@show", "middleware": ["api"]},
    ]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "contracts.json")
        with open(self.path, "w") as f:
            json.dump(contracts.build_contracts(self.GRAPH, self.ROUTES), f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_joins_routes_and_callers(self):
        c = {x["uri"]: x for x in contracts.build_contracts(self.GRAPH, self.ROUTES)}
        self.assertEqual(c["v2/users"]["callers"], [
            {"repo": "admin", "at": "app/b.vue:L7"}, {"repo": "web", "at": "src/a.vue:L10"}])
        self.assertEqual(c["v2/users/{user}"]["method"], "GET")  # HEAD dropped
        self.assertEqual(c["v2/users/{user}"]["callers"], [])

    def test_call_without_the_base_prefix_attaches_to_the_route(self):
        g = {"nodes": [{"id": "web::a", "repo": "web"}], "links": [
            {"relation": "http_request", "http_method": "POST", "http_uri": "auth/login",
             "source": "web::a", "source_file": "a.ts", "source_location": "L1"}]}
        rows = {"api": [{"method": "POST", "uri": "api/auth/login"},
                        {"method": "POST", "uri": "admin/api/auth/login"}]}
        c = {x["uri"]: x for x in contracts.build_contracts(g, rows)}
        self.assertEqual(c["api/auth/login"]["callers"], [{"repo": "web", "at": "a.ts:L1"}])
        self.assertEqual(c["admin/api/auth/login"]["callers"], [])  # shortest suffix only

    def test_search_matches_every_word(self):
        none = os.path.join(self.tmp.name, "none.json")
        out = contracts.contracts_search("post /v2/users", self.path, conn_path=none)
        self.assertIn("POST /v2/users  [users.store]  (api)", out)
        self.assertIn("UserController@store  @ routes/api.php:12", out)
        self.assertIn("admin app/b.vue:L7; web src/a.vue:L10", out)
        self.assertNotIn("users.show", out)
        self.assertIn("none found", contracts.contracts_search("show", self.path, conn_path=none))
        self.assertEqual(contracts.contracts_search("nothing here", self.path, conn_path=none),
                         "(no contracts match)")
        with self.assertRaises(ValueError):
            contracts.contracts_search("  ", self.path)

    def test_declared_callers_from_contract_docs(self):
        cdir = os.path.join(self.tmp.name, "api", "contracts")
        os.makedirs(cdir)
        with open(os.path.join(cdir, "media-api.md"), "w") as f:
            f.write("---\nfrom: media\nto: api\nprotocol: http\nsummary: x\n---\n"
                    "- `POST v2/users/{id}`: reader auth\n")
        conn = os.path.join(self.tmp.name, "connections.json")
        with open(conn, "w") as f:
            json.dump(contracts.build_connections({"api": os.path.join(self.tmp.name, "api")}, []), f)
        out = contracts.contracts_search("users.show", self.path, conn_path=conn)
        self.assertIn("callers (contracts): media — qmd://kb-api/contracts/media-api.md", out)
        self.assertNotIn("none found", out)
        self.assertNotIn("media", contracts.contracts_search("users.store", self.path, conn_path=conn))

    def test_missing_index_is_a_tool_error(self):
        with mock.patch.object(contracts, "CONTRACTS", os.path.join(self.tmp.name, "none.json")):
            with self.assertRaises(kb_core.ToolError):
                kb_core.call_tool("contracts_search", {"query": "x"})

    def test_no_project_filter(self):
        # A web-only caller sees the admin call site too: contracts are for every caller.
        with mock.patch.object(contracts, "CONTRACTS", self.path), \
                mock.patch.object(contracts, "CONNECTIONS", os.path.join(self.tmp.name, "n.json")):
            out = kb_core.call_tool("contracts_search", {"query": "users.store"}, {"web"})
        self.assertIn("admin app/b.vue:L7", out)


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cdir = os.path.join(self.tmp.name, "api", "contracts")
        os.makedirs(cdir)
        with open(os.path.join(cdir, "payments.md"), "w") as f:
            f.write("---\nfrom: api\nto: Payments\nprotocol: https soap\n"
                    "summary: Sends invoices to the payment provider\n---\n# Payments\n")
        with open(os.path.join(cdir, "README.md"), "w") as f:
            f.write("---\nfrom: x\nto: y\n---\n")
        with open(os.path.join(cdir, "notes.md"), "w") as f:
            f.write("# no front matter\n")
        rows = [{"project": "api", "callers": [{"repo": "web", "at": "a"}, {"repo": "web", "at": "b"}]},
                {"project": "api", "callers": [{"repo": "admin", "at": "c"}]},
                {"project": "api", "callers": []}]
        self.path = os.path.join(self.tmp.name, "connections.json")
        with open(self.path, "w") as f:
            json.dump(contracts.build_connections({"api": os.path.join(self.tmp.name, "api"),
                                                   "admin": "/nonexistent"}, rows), f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_connections(self):
        out = contracts.connections("", self.path)
        self.assertIn("api -> payments | https soap | Sends invoices to the payment provider\n"
                      "    doc: qmd://kb-api/contracts/payments.md", out)
        self.assertIn("web -> api | http | 1 endpoints", out)
        self.assertIn("admin -> api | http | 1 endpoints", out)
        self.assertNotIn("x -> y", out)  # README is not a contract

    def test_one_service(self):
        self.assertEqual(contracts.connections("Payments", self.path).count("->"), 1)
        self.assertIn("services: admin, api, payments, web", contracts.connections("nope", self.path))


class ProblemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        os.makedirs(f"{t}/api/contracts")
        with open(f"{t}/api/contracts/api-payments.md", "w") as f:
            f.write("---\nfrom: api\nto: payments\nprotocol: https\nsummary: x\n---\n")
        with open(f"{t}/api/contracts/half.md", "w") as f:
            f.write("---\nfrom: api\nto: redis\n---\n")
        os.makedirs(f"{t}/fe/docs")
        os.makedirs(f"{t}/fe/src/node_modules/x")
        with open(f"{t}/fe/src/node_modules/x/i.js", "w") as f:
            f.write("api.get('v2/vendored')\n")                 # dependency: not scanned
        with open(f"{t}/fe/src/a.js", "w") as f:
            f.write("api.get('v2/users')\n"                       # known
                    "api.get(`v2/users/${id}`)\n"                 # known, variable segment
                    "api.post(`v2/users/${id}/${c}-verification`)\n"  # known, variable in segment
                    "api.get('v2/nope')\n"                        # unknown
                    "api.get(`${base}/x`)\n"                      # starts with a variable: skipped
                    "api.delete('v2/users/' + id)\n")             # glued literal: skipped
        with open(f"{t}/fe/src/c.js", "w") as f:
            f.write("api.get('items')\n")                           # base URL supplies `api/`
        with open(f"{t}/fe/src/b.js", "w") as f:
            f.write("api.post(`v2/reports/${id}/generate`)\n"        # optional {date?} dropped
                    "api.get(`v1/users/${uid}/link/web/sign`)\n")  # route params take literals
        routes = [{"method": "POST", "uri": "v2/reports/{report}/generate/{date?}"},
                  {"method": "GET|HEAD", "uri": "v1/users/{user}/link/{env}/{kind}"},
                  {"method": "GET|HEAD", "uri": "v2/users"},
                  {"method": "GET|HEAD", "uri": "api/items"},
                  {"method": "GET|HEAD", "uri": "v2/users/{user}"},
                  {"method": "POST", "uri": "v2/users/{user}/phone-verification"}]
        conn = [{"from": "fe", "to": "api", "protocol": "http", "summary": "3 endpoints (x)", "doc": ""},
                {"from": "api", "to": "payments", "protocol": "https", "summary": "x",
                 "doc": "qmd://kb-api/contracts/api-payments.md"}]
        self.rows = contracts.build_problems({"api": f"{t}/api", "fe": f"{t}/fe/docs"},
                                             {"fe": ("api", [f"{t}/fe/src"])}, {"api": routes}, conn)
        self.path = f"{t}/problems.json"
        with open(self.path, "w") as f:
            json.dump(self.rows, f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_kinds(self):
        got = sorted((r["project"], r["kind"], r["where"]) for r in self.rows)
        self.assertEqual(got, [
            ("api", "bad-contract", "contracts/half.md"),
            ("fe", "missing-contract", "fe -> api"),
            ("fe", "no-contracts-dir", f"{self.tmp.name}/fe/docs/contracts/"),
            ("fe", "unknown-route", "src/a.js:L4"),
        ])

    def test_filtered_by_projects(self):
        out = contracts.problems("", {"api"}, self.path)
        self.assertIn("[api] bad-contract", out)
        self.assertNotIn("[fe]", out)
        self.assertEqual(contracts.problems("", {"other"}, self.path), "(no problems in your projects)")
        self.assertIn("Problems: fe/unknown-route 1", contracts.problems("unknown-route", None, self.path))


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "logs", "usage.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_and_report(self):
        u = kb_http.Usage(self.path, flush_every=3600)
        u.record("graph_explain", "ann", {"symbol": "UserController"}, True, 0.4)
        u.record("graph_explain", "bob", {"symbol": "UserController"}, False, 0.2)
        u.record("connections", "ann", {}, True, 0.0)
        self.assertEqual(u.data["tools"]["graph_explain"], {"calls": 2, "errors": 1, "seconds": 0.6})
        self.assertEqual(u.data["args"]["graph_explain"], {"UserController": 2})
        public = u.report(with_users=False)
        self.assertIn("graph_explain          2       1    0.3", public)
        self.assertIn("2  UserController", public)
        self.assertNotIn("ann", public)
        self.assertIn("ann: 2 (connections 1, graph_explain 1)", u.report(with_users=True))

    def test_saved_and_reloaded(self):
        u = kb_http.Usage(self.path, flush_every=0)  # every call saves
        u.record("problems", "ann", {"project": "api"}, True, 0.1)
        again = kb_http.Usage(self.path)
        self.assertEqual(again.data["tools"]["problems"]["calls"], 1)
        self.assertEqual(again.data["users"], {"ann": {"problems": 1}})


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idp = serve(ThreadingHTTPServer(("127.0.0.1", 0), FakeIdentity))
        auth = kb_http.Authenticator(url=f"http://127.0.0.1:{cls.idp.server_address[1]}/me")
        cls.usage_dir = tempfile.TemporaryDirectory()
        cls.srv = serve(kb_http.make_server(
            "127.0.0.1", 0, auth, kb_http.Usage(os.path.join(cls.usage_dir.name, "u.json"))))
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.seen = []

        def fake_call(name, args, allowed=None):
            cls.seen.append((name, args, allowed))
            if args.get("symbol", "").startswith("-"):
                raise kb_core.ToolError("bad")
            return f"{name} ok"
        cls.patches = [mock.patch.object(kb_core, "call_tool", fake_call),
                       mock.patch.object(kb_core, "warm_qmd", lambda: None)]
        for p in cls.patches:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        for srv in (cls.srv, cls.idp):
            srv.shutdown()
            srv.server_close()
        cls.usage_dir.cleanup()

    def req(self, method="POST", path="/mcp", body=None, token="all"):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read()
                return resp.status, dict(resp.headers), json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, dict(e.headers), json.loads(raw) if raw else None

    def test_no_token_401(self):
        status, headers, _ = self.req(body={"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=None)
        self.assertEqual(status, 401)
        self.assertTrue(headers.get("WWW-Authenticate", "").startswith("Bearer"))

    def test_forbidden_403(self):
        self.assertEqual(self.req(body={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                  token="noaccess")[0], 403)

    def test_error_closes_connection(self):
        # The unread body of a refused request must not be parsed as a second request.
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("POST", "/mcp", body=b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 401)
        self.assertEqual(r.getheader("Connection"), "close")
        c.close()

    def test_get_405(self):
        status, headers, _ = self.req(method="GET")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")
        self.assertEqual(self.req(method="GET", path="/other")[0], 404)

    def test_mcp_flow(self):
        s, h, b = self.req(body={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-06-18"}}, token="fe")
        self.assertEqual(s, 200)
        self.assertEqual(h.get("Content-Type"), "application/json")
        self.assertEqual(b["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("web", b["result"]["instructions"])
        s, _, b = self.req(body={"jsonrpc": "2.0", "method": "notifications/initialized"}, token="fe")
        self.assertEqual((s, b), (202, None))
        s, _, b = self.req(body={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, token="fe")
        self.assertEqual(len(b["result"]["tools"]), len(kb_core.TOOLS) + 1)  # + usage
        s, _, b = self.req(body={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "graph_explain", "arguments": {"symbol": "X"}}}, token="fe")
        self.assertEqual(b["result"]["content"][0]["text"], "graph_explain ok")
        self.assertFalse(b["result"]["isError"])
        self.assertEqual(self.seen[-1][2], {"web"})  # the caller's projects reach the core
        s, _, b = self.req(body={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "usage", "arguments": {}}}, token="fe")
        self.assertIn("graph_explain", b["result"]["content"][0]["text"])

    def test_tool_errors(self):
        _, _, b = self.req(body={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "nope", "arguments": {}}})
        self.assertEqual(b["error"]["code"], -32602)
        _, _, b = self.req(body={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "graph_explain", "arguments": {"symbol": "-x"}}})
        self.assertTrue(b["result"]["isError"])
        _, _, b = self.req(body={"jsonrpc": "2.0", "id": 6, "method": "nope"})
        self.assertEqual(b["error"]["code"], -32601)

    def test_batch_and_parse_error(self):
        s, _, b = self.req(body=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                 {"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(s, 200)
        self.assertEqual(b, [{"jsonrpc": "2.0", "id": 1, "result": {}}])
        r = urllib.request.Request(self.base + "/mcp", data=b"{nope", method="POST")
        r.add_header("Authorization", "Bearer all")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(r, timeout=10)
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()


if __name__ == "__main__":
    unittest.main()
