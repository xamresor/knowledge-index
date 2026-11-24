#!/usr/bin/env python3
"""Fourth audit round, query side: kb-mcp survives any tool fault, nested calls report any fault in
place, review_context caps diff paths, `contracts: all` opens only the contract docs themselves,
`problems` hides only the project-name slots, the filtered-copy cut runs in a job slot, and a
run_code program does not wait past its own time for a slot."""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from _kbtest import BIN

import access
import contracts
import docs_grade
import kb_core
import review
import settings


def _load_kb_mcp():
    loader = importlib.machinery.SourceFileLoader("kb_mcp", os.path.join(BIN, "kb-mcp"))
    spec = importlib.util.spec_from_loader("kb_mcp", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


kb_mcp = _load_kb_mcp()


def _serve(lines: list) -> list[dict]:
    out = io.StringIO()
    with mock.patch.object(kb_mcp.sys, "stdout", out):
        kb_mcp.serve([json.dumps(x) if not isinstance(x, str) else x for x in lines])
    return [json.loads(x) for x in out.getvalue().splitlines()]


class KbMcpSurvives(unittest.TestCase):
    """1: a fault that is not a ToolError is a tool error answer; the server keeps serving."""

    def test_unexpected_exception_is_a_tool_error(self):
        def boom(*a, **kw):
            raise ValueError("dictionary update sequence element #0 has length 8")
        with mock.patch.object(kb_core, "call_tool", boom):
            answers = _serve([
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "graph_explain", "arguments": {"symbol": "x"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
        self.assertEqual([a["id"] for a in answers], [1, 2])
        self.assertTrue(answers[0]["result"]["isError"])
        self.assertIn("ValueError", answers[0]["result"]["content"][0]["text"])
        self.assertEqual(answers[1]["result"], {})

    def test_a_message_that_is_not_an_object_does_not_stop_the_loop(self):
        answers = _serve(["[1, 2]", "42", '"x"',
                          {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": "nope"},
                          {"jsonrpc": "2.0", "id": 8, "method": "ping"}])
        self.assertEqual(answers[-1], {"jsonrpc": "2.0", "id": 8, "result": {}})
        self.assertIn(7, [a.get("id") for a in answers])

    def test_tool_error_still_reads_as_bad_request(self):
        def bad(*a, **kw):
            raise kb_core.ToolError("missing 'symbol'")
        with mock.patch.object(kb_core, "call_tool", bad):
            answers = _serve([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "graph_explain", "arguments": {}}}])
        self.assertEqual(answers[0]["result"]["content"][0]["text"], "(bad request: missing 'symbol')")


class _FakeSandbox:
    """A sandbox service on a unix socket that sends the given kb() calls and returns the replies."""

    def __init__(self, test, calls, pause=0.0):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "s.sock")
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(1)
        test.addCleanup(self.srv.close)
        self.calls, self.replies = calls, []
        threading.Thread(target=self._one, daemon=True).start()

    def _one(self):
        conn, _ = self.srv.accept()
        with conn, conn.makefile("r", encoding="utf-8") as rd, conn.makefile("w", encoding="utf-8") as wr:
            rd.readline()   # the program
            for c in self.calls:
                wr.write(json.dumps(c) + "\n")
                wr.flush()
                self.replies.append(json.loads(rd.readline()))
            wr.write(json.dumps({"done": True, "output": "", "value": "ok", "calls": len(self.calls)}) + "\n")
            wr.flush()


class NestedCallsReportAnyFault(unittest.TestCase):
    """1: a nested call with a non-object args or a plain exception gets an error reply."""

    def test_run_code_array_args_and_plain_exception(self):
        fake = _FakeSandbox(self, [{"call": "graph_explain", "args": ["Customer"]},
                                   {"call": "repo_map", "args": {}}])

        def boom(name, args, *a, **kw):
            raise RuntimeError("no node")
        with mock.patch.object(kb_core, "SANDBOX_SOCKET", fake.path), \
                mock.patch.object(kb_core, "call_tool", boom):
            msg = kb_core._run_sandbox("return 1", None, None)
        self.assertEqual(msg["value"], "ok")
        self.assertIn("object", fake.replies[0]["error"])
        self.assertIn("RuntimeError", fake.replies[1]["error"])

    def test_missing_node_is_a_tool_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.makedirs(os.path.join(tmp.name, "node_modules", "quickjs-emscripten"))

        def no_node(*a, **kw):
            raise FileNotFoundError(2, "No such file or directory", "node")
        with mock.patch.object(kb_core, "SANDBOX_SOCKET", ""), mock.patch.object(kb_core, "SANDBOX", tmp.name), \
                mock.patch.object(kb_core.subprocess, "Popen", no_node):
            with self.assertRaises(kb_core.ToolError) as cm:
                kb_core._run_sandbox("return 1", None, None)
        self.assertIn("cannot start node", str(cm.exception))

    def test_batch_reports_a_plain_exception_in_place(self):
        real = kb_core.call_tool

        def flaky(name, args, allowed=None, caller=None, nested=False):
            if name == "repo_map":
                raise OSError("gone")
            return "fine"
        with mock.patch.object(kb_core, "call_tool", flaky):
            out = kb_core.batch([{"tool": "repo_map", "args": {}}, {"tool": "version", "args": {}}], None, None)
        self.assertIn("OSError", out)
        self.assertIn("fine", out)
        self.assertIs(kb_core.call_tool, real)

    def test_nested_call_time_does_not_count_against_the_safety_deadline(self):
        fake = _FakeSandbox(self, [{"call": "version", "args": {}}])

        def slow(*a, **kw):
            time.sleep(0.6)
            return "v"
        with mock.patch.object(kb_core, "SANDBOX_SOCKET", fake.path), \
                mock.patch.object(kb_core, "RUN_SECONDS", 0), mock.patch.object(kb_core, "RUN_SLACK", 0.3), \
                mock.patch.object(kb_core, "call_tool", slow):
            msg = kb_core._run_sandbox("return 1", None, None)
        self.assertEqual(msg["value"], "ok")


class SlotWaitStopsAtTheProgramDeadline(unittest.TestCase):
    """5: a nested call of run_code waits for a job slot only until the program's time is up."""

    def test_wait_is_bounded(self):
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        token = kb_core._WAIT_UNTIL.set(time.monotonic() + 0.2)
        try:
            t0 = time.monotonic()
            with self.assertRaises(kb_core.ToolError):
                with kb_core._Job(slots, "graph"):
                    pass
            self.assertLess(time.monotonic() - t0, 5)
        finally:
            kb_core._WAIT_UNTIL.reset(token)
            slots.release()


class ReviewPathCap(unittest.TestCase):
    """2: a diff names at most MAX_FILES paths; the path lookup does not scan every graph file."""

    def test_too_many_paths_are_refused(self):
        diff = "".join(f"+++ b/x{i}\n" for i in range(review.MAX_FILES + 1))
        with self.assertRaises(ValueError):
            review.parse_diff(diff)
        self.assertEqual(len(review.parse_diff("".join(f"+++ b/x{i}\n" for i in range(10)))), 10)

    def test_lookup_matches_suffix_longest_first(self):
        g = {"nodes": [
            {"id": "a", "label": "A", "type": "class", "source_file": "app/A.php", "repo": "api",
             "source_location": "L1"},
            {"id": "b", "label": "B", "type": "class", "source_file": "A.php", "repo": "api",
             "source_location": "L1"}], "links": []}
        out = review.analyze(g, {"repo/app/A.php": set()})
        self.assertIn("A", out)
        self.assertNotIn("api:B", out)

    def test_many_paths_on_a_large_graph_are_fast(self):
        g = {"nodes": [{"id": f"n{i}", "label": f"C{i}", "type": "class", "source_file": f"src/d{i}/C{i}.php",
                        "repo": "api", "source_location": "L1"} for i in range(60_000)], "links": []}
        changes = {f"other/x{i}.php": set() for i in range(review.MAX_FILES)}
        t0 = time.monotonic()
        review.analyze(g, changes)
        self.assertLess(time.monotonic() - t0, 1.0)   # 2 s with the scan per path


class _Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(settings, "state_file", lambda: os.path.join(self.tmp.name, "settings.json"))
        p.start()
        self.addCleanup(p.stop)
        settings._cache.clear()
        env = mock.patch.dict(os.environ, {"KB_CONTRACTS": "all"})
        env.start()
        self.addCleanup(env.stop)


class OnlyContractDocsOpen(_Settings):
    """3: under `contracts: all` a hidden project's doc collection stays closed; only its contract
    collection opens (its README and sub-folders are never staged there, test_audit5)."""

    def test_doc_allowed(self):
        ok = ["qmd://kb_contracts-sw/contracts/sw-vraa.md", "qmd://kb_contracts-sw/contracts/sw-vraa.md  #c3",
              "qmd://kb_contracts-sw/contracts/sw-vraa.md:12"]
        closed = ["qmd://kb-sw/contracts/sw-vraa.md", "qmd://kb-sw/contracts/README.md",
                  "qmd://kb-sw/contracts/drafts/x.md", "qmd://kb-sw/secret.md", "qmd://kb_contracts/x.md",
                  "qmd://kb_contracts-/x.md"]
        for p in ok:
            self.assertTrue(access.doc_allowed(p, {"web"}), p)
        for p in closed:
            self.assertFalse(access.doc_allowed(p, {"web"}), p)
        self.assertTrue(access.doc_allowed("qmd://kb-web/contracts/drafts/x.md", {"web"}))

    def test_grade_counts_only_contract_docs(self):
        path = os.path.join(self.tmp.name, "index.sqlite")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, collection TEXT, path TEXT, "
                    "active INTEGER NOT NULL DEFAULT 1)")
        con.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(filepath, title, body)")
        docs = [("kb-web", "a.md"), ("kb_contracts-sw", "contracts/sw-web.md"), ("kb-sw", "contracts/sw-web.md"),
                ("kb-sw", "contracts/README.md"), ("kb-sw", "secret.md")]
        for i, (c, p) in enumerate(docs, 1):
            con.execute("INSERT INTO documents (id, collection, path) VALUES (?, ?, ?)", (i, c, p))
            con.execute("INSERT INTO documents_fts (rowid, filepath, title, body) VALUES (?, ?, ?, ?)",
                        (i, p, p, "word"))
        con.commit()
        con.close()
        idx = docs_grade.Index(path, ["kb-web", "kb_contracts-sw"])
        self.addCleanup(idx.close)
        self.assertEqual(idx.size(), 2)
        self.assertEqual(idx.df('"word"'), 2)
        local = docs_grade.Index(path, None, "kb_contracts-")   # a local caller: no copies counted twice
        self.addCleanup(local.close)
        self.assertEqual(local.size(), 4)


class HideOnlyNameSlots(unittest.TestCase):
    """4: `problems` under `own` hides the project-name slots, not path segments or URIs."""

    def test_unknown_route(self):
        r = {"project": "web", "kind": "unknown-route", "where": "src/api/users.ts:L5",
             "message": "Calls GET api/v1/users, but api has no such route.", "others": ["api"]}
        out = contracts._hide_ends(r, {"web"})
        self.assertEqual(out["where"], "src/api/users.ts:L5")
        self.assertEqual(out["message"], "Calls GET api/v1/users, but (hidden) has no such route.")

    def test_unknown_route_two_apis_one_visible(self):
        r = {"project": "web", "kind": "unknown-route", "where": "src/api/a.ts:L1",
             "message": "Calls GET api/x, but api / shop has no such route.", "others": ["api", "shop"]}
        out = contracts._hide_ends(r, {"web", "shop"})
        self.assertEqual(out["message"], "Calls GET api/x, but (hidden) / shop has no such route.")

    def test_missing_contract(self):
        r = {"project": "web", "kind": "missing-contract", "where": "web -> api",
             "message": "The code calls api (http, GET api/v1/users), but no contracts/*.md has from: web, "
                        "to: api.", "others": ["api"]}
        out = contracts._hide_ends(r, {"web"})
        self.assertEqual(out["where"], "web -> (hidden)")
        self.assertEqual(out["message"], "The code calls (hidden) (http, GET api/v1/users), but no "
                                         "contracts/*.md has from: web, to: (hidden).")

    def test_old_index_without_others(self):
        r = {"project": "web", "kind": "unknown-route", "where": "src/api/users.ts:L5",
             "message": "Calls GET api/v1/users, but api has no such route."}
        out = contracts._hide_ends(r, {"web"})
        self.assertEqual(out["message"], "Calls GET api/v1/users, but (hidden) has no such route.")


class CutInsideAJobSlot(unittest.TestCase):
    """6: the filtered-copy cut takes a graph job slot, and the wait for the filter lock is bounded."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("FILTERED_DIR", self.tmp.name), ("GRAPH", os.path.join(self.tmp.name, "g.json")),
                            ("contracts_open", lambda: False)):
            p = mock.patch.object(access, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_cut_runs_in_the_slot(self):
        held = []

        @contextlib.contextmanager
        def slot():
            held.append("in")
            yield
            held.append("out")

        def cut(argv, src, path, what):
            held.append("cut")
        with mock.patch.object(access, "job_slot", slot), mock.patch.object(access, "_cut", cut):
            access.graph_for({"web"}, ())
        self.assertEqual(held, ["in", "cut", "out"])

    def test_core_wires_the_graph_slot(self):
        with mock.patch.object(kb_core, "GRAPH_JOBS", threading.BoundedSemaphore(1)) as sem:
            with access.job_slot():
                self.assertFalse(sem.acquire(blocking=False))
            self.assertTrue(sem.acquire(blocking=False))

    def test_lock_wait_is_bounded(self):
        with mock.patch.object(access, "wait_seconds", lambda: 0.1):
            access._filter_lock.acquire()
            try:
                with self.assertRaises(access.Denied):
                    access.graph_for({"web"}, ())
                with self.assertRaises(access.Denied):
                    access.path_graph(os.path.join(self.tmp.name, "x", "graph.json"))
            finally:
                access._filter_lock.release()


if __name__ == "__main__":
    unittest.main()
