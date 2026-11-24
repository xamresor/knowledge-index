#!/usr/bin/env python3
"""bin/warm.py and its use in kb_core: long-lived graphify and qmd, with the old path as fallback."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from _kbtest import BIN

import kb_core
import warm


class Formatting(unittest.TestCase):
    def test_hits_in_the_cli_layout_that_the_access_filter_reads(self):
        text = warm.format_hits([{"file": "kb-web/a.md", "line": 46, "docid": "#ab12", "title": "A",
                                  "score": 0.5, "snippet": "46: @@ -45,4 @@\n47: | x |"}])
        self.assertTrue(text.startswith("qmd://kb-web/a.md:46 #ab12\nTitle: A\nScore:  50%"))
        self.assertIn("\n| x |", text)                     # line numbers dropped
        self.assertEqual(warm.format_hits([]), "No results found.")

    def test_searches_follow_the_mode(self):
        self.assertEqual(kb_core._searches({"mode": "lex"}, "a b")[0], [{"type": "lex", "query": "a b"}])
        searches, intent = kb_core._searches({}, "order status buttons")
        self.assertIn({"type": "vec", "query": "order status buttons"}, searches)
        self.assertIn({"type": "lex", "query": "order"}, searches)
        self.assertEqual(intent, "order status buttons")


class Fallback(unittest.TestCase):
    def test_off_by_default_so_tests_and_builds_spawn_nothing(self):
        with mock.patch.object(warm, "ENABLED", False):
            self.assertIsNone(warm.graph())
            self.assertIsNone(warm.qmd())

    def test_no_qmd_worker_before_the_first_build(self):
        # `qmd mcp` picks its index at start: started before `<kb>/.qmd` exists, it keeps the empty
        # global index (the Docker order: the server is up before `kb build`).
        with tempfile.TemporaryDirectory() as kb, mock.patch.object(warm, "ENABLED", True), \
                mock.patch.object(warm, "_qmd", None), mock.patch.object(warm, "QmdWorker") as worker:
            self.assertIsNone(warm.qmd(kb))
            worker.assert_not_called()
            os.makedirs(os.path.join(kb, ".qmd"))
            open(os.path.join(kb, ".qmd", "index.yml"), "w").close()
            self.assertIs(warm.qmd(kb), worker.return_value)

    def test_graph_worker_failure_falls_back_to_a_process(self):
        dead = mock.Mock(run=mock.Mock(return_value=None))
        with mock.patch.object(warm, "graph", return_value=dead), \
                mock.patch.object(kb_core.warm, "run_capped", return_value=(0, "from cli", "", False)) as sp:
            self.assertEqual(kb_core.run(["graphify", "explain", "X", "--graph", "/g.json"]), "from cli")
            sp.assert_called_once()

    def test_graph_worker_answer_skips_the_process(self):
        alive = mock.Mock(run=mock.Mock(return_value="  warm answer \n"))
        with mock.patch.object(warm, "graph", return_value=alive), \
                mock.patch.object(kb_core.warm, "run_capped") as sp:
            self.assertEqual(kb_core.run(["graphify", "path", "A", "B", "--graph", "/g.json"]), "warm answer")
            sp.assert_not_called()

    def test_other_commands_never_use_the_worker(self):
        alive = mock.Mock(run=mock.Mock(return_value="warm"))
        with mock.patch.object(warm, "graph", return_value=alive), \
                mock.patch.object(kb_core.warm, "run_capped", return_value=(0, "cli", "", False)):
            self.assertEqual(kb_core.run(["graphify", "merge-graphs", "a", "b"]), "cli")
            alive.run.assert_not_called()

    def test_docs_explain_stays_on_the_cli(self):
        worker = mock.Mock()
        with mock.patch.object(warm, "qmd", return_value=worker):
            self.assertIsNone(kb_core._docs_warm({"explain": True}, "x"))
            worker.query.assert_not_called()


@unittest.skipUnless(warm.graphify_python(), "graphify is not installed")
class RealGraphWorker(unittest.TestCase):
    """The worker's answer is the CLI's answer, and a changed file is read again."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.graph = os.path.join(self._tmp.name, "graph.json")
        self.write(["A", "B"])
        self.worker = warm.GraphWorker()
        self.addCleanup(self.worker.kill)

    def write(self, labels):
        nodes = [{"id": f"r::{x}", "label": x, "source_file": f"{x}.php"} for x in labels]
        links = [{"source": nodes[0]["id"], "target": n["id"], "relation": "calls",
                  "confidence": "EXTRACTED"} for n in nodes[1:]]
        with open(self.graph, "w") as f:
            json.dump({"directed": True, "multigraph": False, "graph": {}, "nodes": nodes,
                       "links": links, "hyperedges": []}, f)

    def test_same_answer_as_the_cli(self):
        argv = ["explain", "A", "--graph", self.graph]
        cli = subprocess.run(["graphify", *argv], capture_output=True, text=True)
        self.assertEqual(self.worker.run(argv).strip(), (cli.stdout + cli.stderr).strip())

    def test_changed_file_is_read_again(self):
        self.assertNotIn("Zeta", self.worker.run(["explain", "A", "--graph", self.graph]))
        self.write(["A", "B", "Zeta"])
        st = os.stat(self.graph)
        os.utime(self.graph, (st.st_atime, st.st_mtime + 5))
        self.assertIn("Zeta", self.worker.run(["explain", "A", "--graph", self.graph]))

    def test_view_answers_like_the_filtered_file(self):
        """The in-memory view of a caller's projects gives the CLI's answer on the filtered file."""
        import access
        nodes = [{"id": "api::Ctl", "label": "Ctl", "repo": "api", "source_file": "Ctl.php"},
                 {"id": "api::Svc", "label": "Svc", "repo": "api", "source_file": "Svc.php"},
                 {"id": "admin::Secret", "label": "Secret", "repo": "admin", "source_file": "S.php"},
                 {"id": "domain::Auth", "label": "Auth", "repo": "domain", "type": "domain"}]
        links = [{"source": "api::Ctl", "target": "api::Svc", "relation": "calls", "confidence": "EXTRACTED"},
                 {"source": "admin::Secret", "target": "api::Svc", "relation": "calls", "confidence": "EXTRACTED"},
                 {"source": "api::Ctl", "target": "domain::Auth", "relation": "in_domain", "confidence": "DERIVED"}]
        g = {"directed": True, "multigraph": False, "graph": {}, "nodes": nodes, "links": links,
             "hyperedges": []}
        with open(self.graph, "w") as f:
            json.dump(g, f)
        filtered = os.path.join(self._tmp.name, "api.json")
        with open(filtered, "w") as f:
            json.dump(access.strip_domains(access.filter_graph(g, {"api"})), f)
        for argv in (["explain", "Svc"], ["affected", "Svc"], ["path", "Ctl", "Svc"]):
            cli = subprocess.run(["graphify", *argv, "--graph", filtered], capture_output=True, text=True)
            got = self.worker.run([*argv, "--graph", self.graph], {"allowed": ["api"], "routes": True})
            self.assertEqual(got.strip(), (cli.stdout + cli.stderr).strip(), argv)
            self.assertNotIn("Secret", got)
        full = self.worker.run(["explain", "Svc", "--graph", self.graph])
        self.assertIn("Secret", full)  # the same worker still answers the unfiltered graph
        # views must not share graphify's per-graph caches (its trigram index lives in G.graph)
        for view in ({"allowed": ["admin", "api"]}, {"allowed": ["api"], "routes": True}, None,
                     {"allowed": ["api"]}):
            for argv in (["query", "Svc calls"], ["path", "Ctl", "Svc"], ["explain", "Auth"]):
                out = self.worker.run([*argv, "--graph", self.graph], view)
                self.assertNotIn("graph worker error", out, (argv, view))

    def test_only_lookup_commands(self):
        self.assertIn("not allowed", self.worker.run(["update", "/tmp"]))


if __name__ == "__main__":
    unittest.main()


class RewarmAfterRebuild(unittest.TestCase):
    """After a build the worker cuts the recent views again by itself (bin/warm.py GraphWorker)."""

    def setUp(self):
        import tempfile
        import warm
        self.warm = warm
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph = os.path.join(self.tmp.name, "graph.json")
        self.mark = os.path.join(self.tmp.name, ".kb-post-manifest")
        for p in (self.graph, self.mark):
            with open(p, "w") as f:
                f.write("{}")
        w = warm.GraphWorker.__new__(warm.GraphWorker)
        warm._Proc.__init__(w, ["fake"])
        w.recent, w.graph_path, w.rewarmed, w._watcher = warm.OrderedDict(), None, 0, None
        self.sent = []
        w.exchange = lambda m: self.sent.append(m) or {"out": "ok"}
        w.POLL = 0.05
        self.w = w

    def touch(self, path, t):
        os.utime(path, (t, t))

    def test_recent_views_are_cut_again_once_the_build_is_done(self):
        import time
        self.w.run(["explain", "X", "--graph", self.graph], {"allowed": ["api"], "routes": False})
        self.w.run(["routes", "{}", "--graph", self.graph], {"allowed": ["web"], "routes": True})
        self.sent.clear()
        now = time.time()
        self.touch(self.graph, now + 10)            # a build is rewriting graph.json, the mark is old
        time.sleep(0.3)
        self.assertEqual(self.w.rewarmed, 0)
        self.touch(self.mark, now + 11)             # the build has finished
        deadline = time.time() + 3
        while self.w.rewarmed == 0 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.w.rewarmed, 1)
        argv = [m["argv"][0] for m in self.sent]
        self.assertEqual(argv.count("explain"), 2)  # both views
        self.assertEqual(argv.count("routes"), 1)   # the routes graph only where routes were used
        self.assertEqual(len(self.w.recent), 2)     # warming does not count as use

    def test_recent_is_capped(self):
        for i in range(self.w.RECENT_VIEWS + 3):
            self.w.run(["explain", "X", "--graph", self.graph], {"allowed": [f"p{i}"], "routes": False})
        self.assertEqual(len(self.w.recent), self.w.RECENT_VIEWS)


class KeepLoaded(unittest.TestCase):
    """`qmd mcp` unloads its model after 5 minutes without a query, and the next search waits for the
    load (measured 2026-11-24: 13.5 s against 1.2 s). The worker asks a tiny query before that."""

    def worker(self, alive: bool, idle: float) -> "warm.QmdWorker":
        w = warm.QmdWorker.__new__(warm.QmdWorker)
        w.argv = ["qmd", "mcp"]
        w.used = warm.time.monotonic() - idle
        w._alive = lambda: alive
        w.query = mock.Mock(return_value=[])
        return w

    def test_an_idle_worker_gets_a_tiny_query(self):
        w = self.worker(alive=True, idle=300)
        self.assertTrue(w.keep_loaded(240))
        w.query.assert_called_once_with([{"type": "vec", "query": "warmup"}], [], 1, None, rerank=False)

    def test_a_worker_in_use_is_left_alone(self):
        w = self.worker(alive=True, idle=10)
        self.assertFalse(w.keep_loaded(240))
        w.query.assert_not_called()

    def test_a_stopped_worker_is_not_started(self):
        w = self.worker(alive=False, idle=3000)
        self.assertFalse(w.keep_loaded(240))
        w.query.assert_not_called()

    def test_a_real_query_counts_as_use(self):
        w = warm.QmdWorker.__new__(warm.QmdWorker)
        w.argv, w.used, w._id, w._id_lock = ["qmd", "mcp"], 0.0, 0, warm.threading.Lock()
        w.exchange = mock.Mock(return_value={"result": {"structuredContent": {"results": []}}})
        w.query([{"type": "lex", "query": "x"}], [], 1, None, rerank=False)
        self.assertGreater(w.used, warm.time.monotonic() - 5)

    def test_the_interval_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"KB_WARM_KEEPALIVE": "0"}):
            self.assertEqual(warm.keepalive_seconds(), 0)
        with mock.patch.dict(os.environ, {"KB_WARM_KEEPALIVE": "junk"}):
            self.assertEqual(warm.keepalive_seconds(), 240)
        with mock.patch.dict(os.environ, {"KB_WARM_KEEPALIVE": "90"}):
            self.assertEqual(warm.keepalive_seconds(), 90)

    def test_off_means_no_thread(self):
        with mock.patch.dict(os.environ, {"KB_WARM_KEEPALIVE": "0"}), mock.patch.object(warm, "ENABLED", True), \
                mock.patch.object(warm, "_keepalive_on", False), \
                mock.patch.object(warm.threading, "Thread") as thread:
            warm.start_keepalive("/kb")
            thread.assert_not_called()

    def test_one_thread_however_often_it_is_asked(self):
        with mock.patch.dict(os.environ, {"KB_WARM_KEEPALIVE": "240"}), mock.patch.object(warm, "ENABLED", True), \
                mock.patch.object(warm, "_keepalive_on", False), \
                mock.patch.object(warm.threading, "Thread") as thread:
            warm.start_keepalive("/kb")
            warm.start_keepalive("/kb")
            thread.assert_called_once()
