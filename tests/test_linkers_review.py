"""Tests for the linker review of 2026-11-23.

Each class pins one finding: a caller found in the wrong repo, a package controller without HTTP
edges, FK edges that never appear, a hub filter that deletes the project's own classes, a domain
prefix that ignores the repo, missed doc mentions and links, missed HTTP client shapes, and viz
output that changed on every build.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from _kbtest import BIN, load_script

lh = load_script("link_http.py")
enrich = load_script("enrich.py")
render_viz = load_script("render_viz.py")
label_namespaces = load_script("label_namespaces.py")

import graph as graph_io  # noqa: E402  (bin/ is on sys.path after _kbtest)
from lang import js_ts, markdown  # noqa: E402


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def run(script: str, *args: str) -> str:
    p = subprocess.run([sys.executable, os.path.join(BIN, script), *args],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


def save_graph(path: str, nodes: list[dict], links: list[dict] | None = None) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"directed": False, "multigraph": False, "graph": {},
                   "nodes": nodes, "links": links or []}, fh)


def load_links(path: str, relation: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [e for e in json.load(fh)["links"] if e.get("relation") == relation]


def node(nid: str, label: str, repo: str, sf: str, line: int = 1, **kw) -> dict:
    return {"id": nid, "label": label, "repo": repo, "source_file": sf,
            "source_location": f"L{line}", **kw}


API_NODES = [
    node("api::ctl", "OrderController", "api", "app/Http/Controllers/OrderController.php", 5),
    node("api::store", ".store()", "api", "app/Http/Controllers/OrderController.php", 9),
]
STORE_ROUTE = [{"method": "POST", "uri": "orders",
                "action": "App\\Http\\Controllers\\OrderController@store"}]
CART = "export function checkout() {\n  return apiFetch(`/orders`, { method: 'POST' });\n}\n"


def two_frontends() -> list[dict]:
    """shop-admin comes first: the old suffix scan took the first repo with the same path."""
    out = []
    for repo in ("shop-admin", "shop-web"):
        out += [node(f"{repo}::file", "cart.js", repo, "src/stores/cart.js"),
                node(f"{repo}::checkout", "checkout()", repo, "src/stores/cart.js")]
    return out


class HttpCallerStaysInItsRepo(unittest.TestCase):
    """Finding 1: two frontends with the same relative path must not swap their call sites."""

    def link(self, root_name: str, *extra: str) -> set[tuple[str, str]]:
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, root_name, "src")
            write(os.path.join(src, "stores", "cart.js"), CART)
            gp, rp = os.path.join(d, "graph.json"), os.path.join(d, "routes.json")
            save_graph(gp, API_NODES + two_frontends())
            with open(rp, "w") as fh:
                json.dump(STORE_ROUTE, fh)
            run("link_http.py", *extra, gp, rp, "api", src)
            return {(e["source"], e["target"]) for e in load_links(gp, "http_request")}

    def test_from_names_the_calling_repo(self):
        self.assertEqual(self.link("checkout-dir", "--from", "shop-web"),
                         {("shop-web::checkout", "api::store")})

    def test_root_dir_named_like_the_repo_is_enough(self):
        self.assertEqual(self.link("shop-web"), {("shop-web::checkout", "api::store")})

    def test_ambiguous_repo_without_from_gives_no_edge(self):
        self.assertEqual(self.link("checkout-dir"), set())


class PackageControllers(unittest.TestCase):
    """Finding 2: a controller in packages/<pkg>/src gets its HTTP edges."""

    def link(self, action: str, api_nodes: list[dict]) -> set[tuple[str, str]]:
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "web", "src")
            write(os.path.join(src, "api.js"), "export function load() {\n  apiFetch(`/vehicles`);\n}\n")
            gp, rp = os.path.join(d, "graph.json"), os.path.join(d, "routes.json")
            save_graph(gp, api_nodes + [node("web::load", "load()", "web", "src/api.js")])
            with open(rp, "w") as fh:
                json.dump([{"method": "GET|HEAD", "uri": "vehicles", "action": action}], fh)
            run("link_http.py", "--from", "web", gp, rp, "api", src)
            return {(e["source"], e["target"]) for e in load_links(gp, "http_request")}

    def ctl(self, nid: str, sf: str) -> list[dict]:
        return [node(nid, "VehicleController", "api", sf, 5),
                node(nid + ".index", ".index()", "api", sf, 9)]

    def test_namespace_root_differs_from_the_folder(self):
        nodes = self.ctl("api::sw", "packages/sw/src/Http/Controllers/VehicleController.php")
        self.assertEqual(self.link(r"SmartMonitor\Sw\Http\Controllers\VehicleController@index", nodes),
                         {("web::load", "api::sw.index")})

    def test_the_longer_namespace_match_wins(self):
        nodes = (self.ctl("api::app", "app/Http/Controllers/VehicleController.php")
                 + self.ctl("api::pkg", "packages/sw/src/Http/Controllers/VehicleController.php"))
        self.assertEqual(self.link(r"App\Http\Controllers\VehicleController@index", nodes),
                         {("web::load", "api::app.index")})

    def test_two_equal_candidates_are_no_match(self):
        nodes = (self.ctl("api::a", "packages/a/src/Http/Controllers/VehicleController.php")
                 + self.ctl("api::b", "packages/b/src/Http/Controllers/VehicleController.php"))
        self.assertEqual(self.link(r"Vendor\Http\Controllers\VehicleController@index", nodes), set())

    def test_a_bare_file_name_match_is_not_enough(self):
        nodes = self.ctl("api::x", "app/Legacy/VehicleController.php")
        self.assertEqual(self.link(r"App\Http\Controllers\VehicleController@index", nodes), set())


class RationaleAttachesInItsRepo(unittest.TestCase):
    """Finding 1 for link_rationale: the comment goes to the node of the same repo and path."""

    def test_same_path_in_two_repos(self):
        with tempfile.TemporaryDirectory() as d:
            repos = os.path.join(d, "repos")
            write(os.path.join(repos, "shop-admin", "src", "stores", "cart.js"), "export const a = 1\n")
            write(os.path.join(repos, "shop-web", "src", "stores", "cart.js"),
                  "// WHY: the cart is kept in memory\nexport const b = 1\n")
            gp = os.path.join(d, "graph.json")
            save_graph(gp, two_frontends())
            run("link_rationale.py", gp, repos)
            self.assertEqual([e["target"] for e in load_links(gp, "explains")], ["shop-web::file"])

    def test_suffix_of_another_file_name_is_not_a_match(self):
        with tempfile.TemporaryDirectory() as d:
            repos = os.path.join(d, "repos")
            write(os.path.join(repos, "web", "src", "metadata.js"), "// NOTE: x\n")
            gp = os.path.join(d, "graph.json")
            save_graph(gp, [node("web::data", "data.js", "web", "data.js")])
            run("link_rationale.py", gp, repos)
            self.assertEqual(load_links(gp, "explains"), [])


class DataLinks(unittest.TestCase):
    def nodes(self) -> tuple[list[dict], list[dict]]:
        nodes, links = [], []
        for repo in ("shop-admin", "shop-web"):
            nodes += [node(f"{repo}::Report", "Report", repo, "app/Services/Report.php", 3),
                      node(f"{repo}::run", ".run()", repo, "app/Services/Report.php", 5)]
            links.append({"source": f"{repo}::Report", "target": f"{repo}::run", "relation": "method"})
        return nodes, links

    def test_sql_attaches_in_its_repo(self):
        """Finding 1 for link_data: the SQL edge comes from the class of the same repo."""
        with tempfile.TemporaryDirectory() as d:
            repos = os.path.join(d, "repos")
            write(os.path.join(repos, "shop-admin", "app", "Services", "Report.php"),
                  "<?php\nclass Report {}\n")
            write(os.path.join(repos, "shop-web", "app", "Services", "Report.php"),
                  "<?php\nclass Report { function run() { DB::table('orders'); } }\n")
            gp = os.path.join(d, "graph.json")
            save_graph(gp, *self.nodes())
            run("link_data.py", gp, repos)
            self.assertEqual([e["source"] for e in load_links(gp, "sql")], ["shop-web::Report"])

    MIGRATION = ("<?php\nSchema::create('orders', function ($t) {\n"
                 "  $t->foreignId('customer_id')->constrained('customers');\n});\n")

    def test_migrations_from_the_command_line(self):
        """Finding 3: `--migrations <dir>` gives FK edges, whatever sits next to repos_dir."""
        with tempfile.TemporaryDirectory() as d:
            mig = os.path.join(d, "somewhere", "database", "migrations")
            write(os.path.join(mig, "2026_01_01_create_orders.php"), self.MIGRATION)
            repos = os.path.join(d, "data", "repos")
            os.makedirs(repos)
            gp = os.path.join(d, "graph.json")
            save_graph(gp, [])
            run("link_data.py", gp, repos, "--migrations", mig)
            self.assertEqual([(e["source"], e["target"]) for e in load_links(gp, "fk")],
                             [("db::orders", "db::customers")])

    def test_fallback_config_with_single_quotes(self):
        """Finding 3: the fallback reads `routes = '...'` too."""
        with tempfile.TemporaryDirectory() as d:
            project = os.path.join(d, "shop-api")
            write(os.path.join(project, "database", "migrations", "2026_01_01_create_orders.php"),
                  self.MIGRATION)
            write(os.path.join(d, "kb", "kb.projects.toml"),
                  f"[[project]]\nname = 'shop-api'\nroutes = '{project}'\n")
            repos = os.path.join(d, "kb", "repos")
            os.makedirs(repos)
            gp = os.path.join(d, "graph.json")
            save_graph(gp, [])
            run("link_data.py", gp, repos)
            self.assertEqual(len(load_links(gp, "fk")), 1)


class DeclutterKeepsOwnClasses(unittest.TestCase):
    """Finding 4: a hub label that the project defines itself stays."""

    def test_defined_class_with_a_hub_name_survives(self):
        with tempfile.TemporaryDirectory() as d:
            gp, hp = os.path.join(d, "graph.json"), os.path.join(d, "hubs.txt")
            save_graph(gp, [node("api::own", "Controller", "api", "app/Http/Controllers/Controller.php"),
                            node("api::own.m", ".callAction()", "api", "app/Http/Controllers/Controller.php"),
                            # an external reference: graphify gives it no source_file and no line
                            {"id": "api::ref", "label": "Controller", "repo": "api", "source_file": "",
                             "source_location": ""}],
                       [{"source": "api::own", "target": "api::own.m", "relation": "method"}])
            write(hp, "Controller\n")
            run("declutter.py", gp, hp)
            with open(gp) as fh:
                ids = {n["id"] for n in json.load(fh)["nodes"]}
        self.assertEqual(ids, {"api::own", "api::own.m"})


class LayerBucketUsesTheRepo(unittest.TestCase):
    """Finding 5: app/<Layer> of any repo lands in that repo's bucket."""

    def test_second_laravel_app(self):
        self.assertEqual(enrich.layer_bucket("app/Http/Kernel.php", "admin"), "admin:Http")
        self.assertEqual(enrich.layer_bucket("app/Services/X.php", "api"), "api:Services")


class MarkdownMentionsAndLinks(unittest.TestCase):
    """Finding 6: a method mention `createOrder()` and a link with an anchor are both edges."""

    def test_method_mention_and_anchor_link(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "a.md"), "Call createOrder() first. See [b](b.md#setup).\n")
            write(os.path.join(d, "b.md"), "# Setup\n")
            g = {"nodes": [node("api::m", ".createOrder()", "api", "app/S.php")], "links": []}
            markdown.enrich(g, [("api", d)])
        pairs = {(e["source"], e["target"], e["relation"]) for e in g["links"]}
        self.assertIn(("api::a.md", "api::b.md", "links_to"), pairs)
        self.assertIn(("api::a.md", "api::m", "documents"), pairs)

    def test_two_methods_with_one_name_stay_ambiguous(self):
        g = {"nodes": [node("api::a", ".createOrder()", "api", "a.php"),
                       node("api::b", ".createOrder()", "api", "b.php")], "links": []}
        self.assertEqual(markdown._symbols_for("api", *markdown._symbol_index(g)), {})


class HttpClientShapes(unittest.TestCase):
    """Finding 7: axios, $http and http clients with a path literal count; other `.get()` does not."""

    def calls(self, src: str) -> list[tuple[str, str]]:
        return [(v, p) for _, v, p in js_ts.iter_calls(src.splitlines())]

    def test_common_clients(self):
        self.assertEqual(self.calls("axios.get('/orders')"), [("get", "/orders")])
        self.assertEqual(self.calls("this.$http.post(`/orders/${id}`, body)"),
                         [("post", "/orders/${id}")])
        self.assertEqual(self.calls("http.delete('orders/1')"), [("delete", "orders/1")])
        self.assertEqual(self.calls("this.$axios.$get('/orders')"), [("get", "/orders")])
        self.assertEqual(self.calls("axios.patch(\n  '/orders/1',\n  body)"), [("patch", "/orders/1")])

    def test_no_false_positives(self):
        for src in ("http.get(url)", "axios.get(`${url}`)", "map.get('key')", "client.get('key')",
                    "http.get('https://example.com/x')", "http.get('token')", "params.get('page')"):
            self.assertEqual(self.calls(src), [], src)


class VizStableAndSafe(unittest.TestCase):
    """Finding 8: colours and file names do not change between builds; a failed write keeps the file."""

    def test_color_is_the_same_in_another_process(self):
        code = ("import sys; sys.path.insert(0, %r); import render_viz; "
                "print(render_viz.color_for('Billing'))" % BIN)
        out = set()
        for seed in ("1", "2"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            out.add(subprocess.run([sys.executable, "-c", code], env=env,
                                   capture_output=True, text=True).stdout.strip())
        self.assertEqual(len(out), 1, out)

    def test_file_names_are_unique(self):
        names = render_viz.file_names(["a b", "a_b", "a/b", "c"])
        self.assertEqual(len(set(names.values())), 4)
        self.assertEqual(names["c"], "c")

    def test_failed_write_keeps_the_old_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.js")
            write(path, "old")
            with self.assertRaises(TypeError):
                render_viz.write_js(path, "kbRecv", object())
            with open(path) as fh:
                self.assertEqual(fh.read(), "old")
            self.assertEqual(os.listdir(d), ["x.js"])


class GraphIdsAndNamespaces(unittest.TestCase):
    """Finding 9: no unused id parser; label_namespaces keeps its behaviour after the dead branch."""

    def test_no_parse_id(self):
        self.assertFalse(hasattr(graph_io, "parse_id"))

    def test_domains(self):
        d = label_namespaces.domain
        self.assertEqual(d({"source_file": "src/stores/cart.js"}), "stores")
        self.assertEqual(d({"source_file": "assets-js/pages/a.js"}), "pages")
        self.assertEqual(d({"source_file": "app/Http/Controllers/V2/X.php"}), "Http\\Controllers\\V2")
        self.assertEqual(d({"source_file": "packages/sw/src/X.php"}), "sw")


if __name__ == "__main__":
    unittest.main()
