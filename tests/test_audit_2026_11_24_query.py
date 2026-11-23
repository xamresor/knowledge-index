#!/usr/bin/env python3
"""Second review round of the query tools (2026-11-24): input caps of review_context, the full-graph
shortcut, one text for every search page, nested calls without cursors, repo_map unused, Class::method,
search limits, the contracts text, datadict oracles, filtered-copy freshness, warm worker locks,
contract docs behind symlinks, and the grep fallback."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401

import access
import contracts
import datadict
import kb_core
import repomap
import review
import warm


def _projects(test, projects=("api", "web"), docs=("api", "web")):
    for name, value in (("PROJECTS", {p: [] for p in projects}), ("DOC_PROJECTS", set(docs)),
                        ("HOME_PROJECTS", [])):
        p = mock.patch.object(kb_core, name, value)
        p.start()
        test.addCleanup(p.stop)


def _graph_file(test, nodes, links=()) -> str:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = os.path.join(tmp.name, "graph.json")
    with open(path, "w") as f:
        json.dump({"directed": False, "multigraph": False, "graph": {}, "nodes": list(nodes),
                   "links": list(links)}, f)
    p = mock.patch.object(kb_core, "GRAPH", path)
    p.start()
    test.addCleanup(p.stop)
    kb_core._LABELS.clear()
    return path


class ReviewInputCaps(unittest.TestCase):
    """1: `files` is capped by items and by expanded lines; the diff limit matches kb-http's body limit."""

    def test_many_wide_ranges_are_refused(self):
        with self.assertRaises(ValueError):
            review.parse_files([f"a{i}:1-9999" for i in range(2000)])
        _projects(self)
        with self.assertRaises(kb_core.ToolError):
            kb_core.call_tool("review_context", {"files": [f"a{i}:1-9999" for i in range(2000)]})

    def test_small_input_still_parses(self):
        self.assertEqual(review.parse_files(["a.php:3-4"]), {"a.php": {3, 4}})

    def test_diff_limit_is_what_the_server_accepts(self):
        self.assertLessEqual(review.MAX_DIFF, 1024 * 1024)
        desc = kb_core.TOOLS_BY_NAME["review_context"]["inputSchema"]["properties"]["diff"]["description"]
        self.assertNotIn("2 MB", desc)


class FullViewNeedsEveryGraphRepo(unittest.TestCase):
    """2: a project built into graph.json after the start is not served to a caller of the old ones."""

    def test_new_repo_in_the_graph_keeps_the_filter(self):
        _projects(self, ("api", "web"))
        _graph_file(self, [{"id": "a", "label": "A", "repo": "api"}, {"id": "w", "label": "W", "repo": "web"},
                           {"id": "n", "label": "N", "repo": "newproj"}])
        self.assertFalse(kb_core._full({"api", "web"}))
        self.assertFalse(kb_core._full({"api", "web", "newproj"}))   # a list never skips the filter
        self.assertTrue(kb_core._full(None))
        seen = []

        class Worker:
            def run(self, argv, view=None, _remember=True):
                seen.append(view)
                return "Node: X"
        with mock.patch.object(kb_core.warm, "graph", lambda: Worker()):
            kb_core.call_tool("graph_explain", {"symbol": "Zzz", "project": "all"}, {"api", "web"})
        self.assertTrue(seen)
        self.assertEqual(seen[0]["allowed"], ["api", "web"])

    def test_graph_for_of_a_list_is_always_a_filtered_copy(self):
        _projects(self, ("api",))
        _graph_file(self, [{"id": "a", "label": "A", "repo": "api"}, {"id": "n", "label": "N", "repo": "newproj"}])
        with mock.patch.object(access, "graph_for", lambda allowed, known: (sorted(allowed), sorted(known))):
            self.assertEqual(kb_core.graph_for({"api", "newproj"}), (["api", "newproj"], []))


class SearchPageWithoutCursor(unittest.TestCase):
    """3: `page: N` without the cursor is the cursor's page N, for both halves."""

    def test_page_two_is_the_cursor_page_two(self):
        _projects(self)
        docs_args = []

        def docs(args, allowed=None):
            docs_args.append(dict(args))
            n = int(args.get("limit") or 5)
            return "[projects: api]\n\n" + "".join(f"qmd://kb-api/d{i}.md:1 #{i}\nTitle: D{i}\n\n" + "t" * 300 + "\n\n"
                                                 for i in range(n))

        def code(args, allowed=None):
            n = int(args.get("_wide") or args.get("budget") or 1800) // 20
            return "[projects: api]\n\n" + "\n".join(f"NODE n{i} " + "x" * 60 for i in range(n))
        with mock.patch.object(kb_core, "search_docs", docs), mock.patch.object(kb_core, "search_code", code):
            first = kb_core.call_tool("search", {"q": "x", "limit": 8, "page_length": 1000})
            cursor = first.rsplit('cursor: "', 1)[1].split('"', 1)[0]
            pages_by_cursor = [kb_core.call_tool("search", {"cursor": cursor}) for _ in range(3)]
            pages_by_number = [kb_core.call_tool("search", {"q": "x", "limit": 8, "page_length": 1000, "page": n})
                               for n in (2, 3, 4)]
        body = lambda s: s.split("\n\n[page ")[0]  # noqa: E731
        self.assertEqual([body(p) for p in pages_by_cursor], [body(p) for p in pages_by_number])
        self.assertEqual({a.get("limit") for a in docs_args}, {8})
        self.assertNotIn(False, [a.get("refuse") for a in docs_args])
        joined = body(first) + "".join(body(p) for p in pages_by_number)
        self.assertIn("d4.md", joined)          # no document is skipped between pages


class NestedCallsAreWhole(unittest.TestCase):
    """4: kb() in run_code and the calls of batch get the whole answer and store no cursor."""

    def test_batch_call_gets_all_and_keeps_no_cursor(self):
        long_text = "\n".join("row %d" % i for i in range(3000))
        before = len(kb_core.CURSORS.items)
        with mock.patch.object(kb_core, "_index", lambda fn, *a: long_text):
            out = kb_core.call_tool("contracts_search", {"query": "x"}, {"api"}, "ann", nested=True)
            stored = len(kb_core.CURSORS.items)
            via_batch = kb_core.batch([{"tool": "contracts_search", "args": {"query": "x"}}], {"api"}, "ann2")
        self.assertEqual(out, long_text)
        self.assertEqual(stored, before)
        self.assertIn("row 2999", via_batch)

    def test_whole_is_capped(self):
        out = kb_core._whole("x" * (kb_core.MAX_OUT + 10), None, None)
        self.assertLess(len(out), kb_core.MAX_OUT + 100)


class UnusedNamesPerProject(unittest.TestCase):
    """5: two candidates with one name are judged each in its own project."""

    def test_used_api_helper_goes_unused_sw_helper_stays(self):
        with tempfile.TemporaryDirectory() as d:
            for repo, rel, text in (("api", "app/Helper.php", "class Helper {}"),
                                    ("api", "app/Other.php", "Helper::run();"),
                                    ("sw", "src/Helper.php", "class Helper {}")):
                os.makedirs(os.path.dirname(os.path.join(d, repo, rel)), exist_ok=True)
                with open(os.path.join(d, repo, rel), "w") as f:
                    f.write(text)
            found = {"api:app/Helper.php": ["Helper"], "sw:src/Helper.php": ["Helper"]}
            left, complete = repomap.scan_names(found, {"api": os.path.join(d, "api"), "sw": os.path.join(d, "sw")})
        self.assertTrue(complete)
        self.assertEqual(left, {"sw:src/Helper.php": ["Helper"]})

    def test_a_use_from_a_project_without_that_name_still_counts(self):
        with tempfile.TemporaryDirectory() as d:
            for repo, rel, text in (("api", "app/Lonely.php", "class Lonely {}"), ("web", "x.ts", "Lonely")):
                os.makedirs(os.path.dirname(os.path.join(d, repo, rel)), exist_ok=True)
                with open(os.path.join(d, repo, rel), "w") as f:
                    f.write(text)
            left, _ = repomap.scan_names({"api:app/Lonely.php": ["Lonely"]},
                                         {"api": os.path.join(d, "api"), "web": os.path.join(d, "web")})
        self.assertEqual(left, {})


class ClassMember(unittest.TestCase):
    """6: `Class::method` and `Class.method` resolve through the class's method edge."""

    NODES = [{"id": "c1", "label": "OrderService", "type": "class", "repo": "api", "source_file": "app/O.php"},
             {"id": "m1", "label": ".create()", "type": "method", "repo": "api", "source_file": "app/O.php"},
             {"id": "c2", "label": "UserService", "type": "class", "repo": "api", "source_file": "app/U.php"},
             {"id": "m2", "label": ".create()", "type": "method", "repo": "api", "source_file": "app/U.php"}]
    LINKS = [{"source": "c1", "target": "m1", "relation": "method"},
             {"source": "c2", "target": "m2", "relation": "method"}]

    def test_explain_finds_the_member(self):
        _projects(self, ("api",))
        _graph_file(self, self.NODES, self.LINKS)
        for term in ("OrderService::create", "OrderService.create", "OrderService.create()"):
            asked = []
            with mock.patch.object(kb_core.warm, "graph", lambda: None), \
                    mock.patch.object(kb_core.warm, "graph_once",
                                      lambda argv, view: asked.append(argv[1]) or "All connections (0):"), \
                    mock.patch.object(kb_core, "run", return_value="No node matching"):
                out = kb_core.call_tool("graph_explain", {"symbol": term})
            self.assertEqual(asked, ["m1"], term)
            self.assertIn("ID:        m1", out)

    @unittest.skipUnless(warm.graphify_python(), "graphify (and its networkx) is not installed")
    def test_routes_resolve_finds_the_member(self):
        script = (
            "import json, sys; sys.path.insert(0, sys.argv[1]); import routes\n"
            "data = json.loads(sys.argv[2]); g, nodes = routes._build(data)\n"
            "print(json.dumps([routes.resolve(t, nodes, g)[0] for t in sys.argv[3:]]))\n")
        data = {"nodes": self.NODES, "links": self.LINKS + [
            {"source": "m1", "target": "m2", "relation": "calls"}]}
        p = subprocess.run([warm.graphify_python(), "-c", script, BIN, json.dumps(data),
                            "OrderService::create", "UserService.create()", "api:OrderService::create"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(p.stdout.strip().splitlines()[-1]), ["m1", "m2", "m1"], p.stderr)


class UnusedScanLimits(unittest.TestCase):
    """7: the unused scan takes a graph job slot, has a time budget and is cached per build."""

    def test_scan_is_cached_and_takes_a_slot(self):
        _graph_file(self, [])
        calls = []

        def scan(found, roots, deadline=None):
            calls.append(deadline)
            return {}, True
        kb_core._UNUSED_CACHE.clear()
        with mock.patch.object(repomap, "scan_names", scan):
            kb_core._unused_scan({"api:a.php": ["Aaaa"]}, {"api": "/nowhere"})
            kb_core._unused_scan({"api:a.php": ["Aaaa"]}, {"api": "/nowhere"})
            self.assertEqual(len(calls), 1)
            self.assertIsNotNone(calls[0])
            kb_core._UNUSED_CACHE.clear()
            with mock.patch.object(kb_core, "GRAPH_JOBS", threading.BoundedSemaphore(1)) as slots, \
                    mock.patch.object(kb_core, "JOB_WAIT", 0.05):
                slots.acquire()
                with self.assertRaises(kb_core.ToolError):
                    kb_core._unused_scan({"api:a.php": ["Aaaa"]}, {"api": "/nowhere"})

    def test_deadline_stops_the_walk(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "x.php"), "w") as f:
                f.write("Lonely")
            left, complete = repomap.scan_names({"api:a.php": ["Lonely"]}, {"api": d}, time.monotonic() - 1)
        self.assertFalse(complete)
        self.assertEqual(left, {"api:a.php": ["Lonely"]})


class SearchBounds(unittest.TestCase):
    """8: `limit` 1..50, `page` 1..SEARCH_PAGE_MAX."""

    def test_limit_is_clamped_and_page_bounded(self):
        _projects(self)
        seen = []

        def docs(args, allowed=None):
            seen.append(args.get("limit"))
            return "(no results)"
        with mock.patch.object(kb_core, "search_docs", docs):
            kb_core.call_tool("search", {"q": "x", "domain": "docs", "limit": -3})
            kb_core.call_tool("search", {"q": "x", "domain": "docs", "limit": 999})
        self.assertEqual(seen, [1, 50])
        with self.assertRaises(kb_core.ToolError):
            kb_core.call_tool("search", {"q": "x", "page": kb_core.SEARCH_PAGE_MAX + 1})


class ContractsTextAndWideBudget(unittest.TestCase):
    """9: the contracts_search text names the setting; page 1's code budget really is 4x."""

    def test_description_does_not_promise_every_project(self):
        desc = kb_core.TOOLS_BY_NAME["contracts_search"]["description"]
        self.assertNotIn("whatever the caller's project access", desc)
        self.assertIn("contracts: own", desc)

    def test_widened_budget_reaches_graphify(self):
        _projects(self)
        argv = []
        with mock.patch.object(kb_core.warm, "graph", lambda: None), \
                mock.patch.object(access, "path_graph", lambda g: g), \
                mock.patch.object(kb_core, "run", lambda cmd, **kw: argv.append(cmd) or "NODE x"):
            kb_core.call_tool("search", {"q": "x", "domain": "code", "budget": 8000})
        self.assertEqual(argv[0][argv[0].index("--budget") + 1], str(8000 * kb_core.CURSOR_WIDEN))


class DatadictNoOracle(unittest.TestCase):
    """10: the data dictionary checks see only the caller's projects."""

    def test_explain_ignores_hidden_nodes(self):
        rows = [("payout", "class", "hidden", "app/P.php", "h1")]
        asked = []
        with mock.patch.object(kb_core, "labels", lambda graph=None: rows), \
                mock.patch.object(kb_core.addons, "on", lambda name: name == "datadict"), \
                mock.patch.object(datadict, "entries", lambda scope: {"api": {"payouts": {}}}), \
                mock.patch.object(datadict, "term_tables", lambda symbol, book: ["payouts"]), \
                mock.patch.object(datadict, "with_explain", lambda out, book: out), \
                mock.patch.object(kb_core, "graph_call", lambda cmd, args, allowed, *t: asked.append(cmd[2]) or "x"):
            kb_core._explain({"symbol": "payout"}, {"api"})
            kb_core._explain({"symbol": "payout"}, None)
        self.assertEqual(asked, ["payouts", "payout"])

    def test_problems_hide_tables_of_hidden_projects(self):
        tables = {"secret_tbl": {"hidden"}, "orders": {"api"}}
        with mock.patch.object(datadict, "graph_tables", lambda g: tables), \
                mock.patch.object(datadict, "files", lambda allowed=None: {"api": "/x/api.toml"}), \
                mock.patch.object(datadict, "load", lambda p: ({"secret_tbl": {}, "nope": {}, "orders": {}}, [])):
            limited = datadict.problems("g", {"api"})
            local = datadict.problems("g", None)
        unknown = lambda rows: sorted(r["where"] for r in rows if r["kind"] == "datadict-unknown-table")  # noqa: E731
        self.assertEqual(unknown(limited), ["nope", "secret_tbl"])   # the same answer as for a missing table
        self.assertEqual(unknown(local), ["nope"])


class FilteredCopyFreshness(unittest.TestCase):
    """11 and 13: a copy is fresh only for the source it was cut from; the cache key is a hash."""

    def test_copy_of_an_old_source_is_stale(self):
        with tempfile.TemporaryDirectory() as d:
            src, copy = os.path.join(d, "graph.json"), os.path.join(d, "f", "graph.json")
            with open(src, "w") as f:
                f.write('{"nodes": [], "links": []}')
            old_id = access._source_id(src)
            os.makedirs(os.path.dirname(copy))
            with open(copy, "w") as f:
                f.write("{}")
            with open(copy + ".src", "w") as f:
                json.dump(old_id, f)
            self.assertTrue(access._fresh(copy, src))
            # a rebuild replaces graph.json while the cut runs; the copy ends later, so its mtime is newer
            with open(src + ".new", "w") as f:
                f.write('{"nodes": [{"id": 1}], "links": []}')
            os.replace(src + ".new", src)
            os.utime(copy, (time.time() + 60, time.time() + 60))
            self.assertFalse(access._fresh(copy, src))

    def test_graph_for_records_the_source(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "graph.json")
            with open(src, "w") as f:
                json.dump({"nodes": [{"id": "a", "repo": "api"}, {"id": "b", "repo": "web"}], "links": []}, f)
            with mock.patch.object(access, "GRAPH", src), mock.patch.object(access, "FILTERED_DIR", os.path.join(d, "f")):
                path = access.graph_for({"api"}, ["api", "web"])
                with open(path + ".src") as f:
                    self.assertEqual(json.load(f), access._source_id(src))
                self.assertTrue(access._fresh(path, src))

    def test_key_has_no_collisions(self):
        self.assertNotEqual(access.view_key({"a", "b"}, True), access.view_key({"a+b"}, True))
        self.assertNotEqual(access.view_key({"a"}, True), access.view_key({"a"}, False))
        self.assertEqual(access.view_key({"b", "a"}, True), access.view_key(["a", "b"], True))


class WarmLocks(unittest.TestCase):
    """12: JSON-RPC ids, recent views and the watcher start are thread safe."""

    def test_ids_are_unique_across_threads(self):
        w = warm.QmdWorker()
        ids = []
        lock = threading.Lock()

        def take():
            for _ in range(2000):
                i = w._next_id()
                with lock:
                    ids.append(i)
        threads = [threading.Thread(target=take) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(ids)), 8000)

    def test_one_watcher_whatever_the_threads(self):
        w = warm.GraphWorker()
        started = []

        class FakeThread:
            def __init__(self, *a, **kw):
                time.sleep(0.01)    # widen the gap between the check and the start
                started.append(1)

            def start(self):
                pass
        real = threading.Thread
        callers = [real(target=w._remember, args=("/g.json", {"allowed": [str(i)]}, False)) for i in range(6)]
        with mock.patch.object(warm.threading, "Thread", FakeThread):
            for t in callers:
                t.start()
            for t in callers:
                t.join()
        self.assertEqual(len(started), 1)


class ContractSymlinks(unittest.TestCase):
    """14: a contract doc that leaves its project's docs root is not indexed."""

    def test_links_out_of_the_root_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a", "docs"), os.path.join(d, "b", "docs")
            os.makedirs(os.path.join(a, "contracts"))
            os.makedirs(os.path.join(b, "contracts"))
            body = "---\nfrom: b\nto: x\nprotocol: http\nsummary: private\n---\n"
            with open(os.path.join(b, "contracts", "private.md"), "w") as f:
                f.write(body)
            with open(os.path.join(a, "contracts", "own.md"), "w") as f:
                f.write(body.replace("from: b", "from: a"))
            with open(os.path.join(a, "inside.md"), "w") as f:
                f.write(body.replace("from: b", "from: a2"))
            os.symlink(os.path.join(b, "contracts", "private.md"), os.path.join(a, "contracts", "copied.md"))
            os.symlink(os.path.join(a, "inside.md"), os.path.join(a, "contracts", "inside.md"))
            names = [fn for fn, _ in contracts._contract_files(a)]
            self.assertEqual(names, ["inside.md", "own.md"])
            rows = contracts.build_connections({"a": a}, [])
            self.assertNotIn("b", {r["from"] for r in rows})
            # a symlinked contracts folder that points at another project
            c = os.path.join(d, "c", "docs")
            os.makedirs(c)
            os.symlink(os.path.join(b, "contracts"), os.path.join(c, "contracts"))
            self.assertEqual(list(contracts._contract_files(c)), [])


class GrepFallbackHidesSecrets(unittest.TestCase):
    """15: without ripgrep, .env files and bootstrap/cache stay out too."""

    def test_grep_fallback(self):
        with tempfile.TemporaryDirectory() as kb:
            root = os.path.join(kb, "repos", "api")
            for rel in (".env", ".env.production", "bootstrap/cache/config.php",
                        "packages/x/bootstrap/cache/r.php", "app/ok.php"):
                os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
                with open(os.path.join(root, rel), "w") as f:
                    f.write("DB_PASSWORD=hit\n")
            with mock.patch.object(kb_core, "KB", kb), mock.patch.object(kb_core, "PROJECTS", {"api": []}), \
                    mock.patch.object(kb_core.shutil, "which", return_value=None):
                out = kb_core.call_tool("code_grep", {"pattern": "DB_PASSWORD"})
        self.assertIn("app/ok.php", out)
        self.assertNotIn(".env", out)
        self.assertNotIn("bootstrap/cache", out)


if __name__ == "__main__":
    unittest.main()
