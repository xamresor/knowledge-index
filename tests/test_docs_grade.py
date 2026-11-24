"""Tests for bin/docs_grade.py and the "is this enough?" check of the docs half of `search`.

qmd is stubbed: a small SQLite file with the two tables of qmd's index that the grade reads
(`documents`, `documents_fts` with qmd's tokenizer), and `_docs_once` returns fixed qmd text.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401  (puts bin/ on sys.path)

import docs_grade
import kb_core

DOCS = {
    ("kb-a", "server.md"): "The shared server kb-http listens on port 8790. The webhook answer is kept "
                           "for KB_AUTH_CACHE_SECONDS seconds.",
    ("kb-a", "install.md"): "Install the server with uv. The webhook needs a token.",
    ("kb-b", "deploy.md"): "Deployment of the server on Hetzner. The state of the database is backed up.",
    ("kb-b", "notes.md"): "Notes about the server and the port of the database.",
}


def make_index(path: str) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, collection TEXT, path TEXT, "
                "active INTEGER NOT NULL DEFAULT 1)")
    con.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(filepath, title, body, "
                "tokenize='porter unicode61')")
    for i, ((coll, p), body) in enumerate(DOCS.items(), 1):
        con.execute("INSERT INTO documents (id, collection, path) VALUES (?, ?, ?)", (i, coll, p))
        con.execute("INSERT INTO documents_fts (rowid, filepath, title, body) VALUES (?, ?, ?, ?)",
                    (i, f"{coll}/{p}", p, body))
    con.commit()
    con.close()


def hits(*paths: str) -> str:
    return "\n\n".join(f"qmd://{p}:1 #abc\nTitle: t\nScore:  {100 // (i + 1)}%\n\nsnippet"
                       for i, p in enumerate(paths))


class IndexCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "index.sqlite")
        make_index(self.path)
        self.index = docs_grade.Index(self.path)

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()


class FtsQueryTest(unittest.TestCase):
    def test_terms_match_the_way_qmd_lex_matches(self):
        self.assertEqual(docs_grade.fts_query("server"), '"server"*')     # one word: prefix
        self.assertEqual(docs_grade.fts_query("kb-http"), '"kb http"')    # compound: phrase
        self.assertEqual(docs_grade.fts_query("/srv/old"), '"srv old"')
        self.assertEqual(docs_grade.fts_query("kb_auth_cache"), '"kb auth cache"')
        self.assertIsNone(docs_grade.fts_query("--"))

    def test_first_hit(self):
        self.assertEqual(docs_grade.first_hit("[projects: all]\n\n" + hits("kb-a/dir/x.md", "kb-b/y.md")),
                         ("kb-a", "dir/x.md"))
        self.assertIsNone(docs_grade.first_hit("No results found."))


class CoverageTest(unittest.TestCase):
    def test_a_word_in_no_document_outweighs_common_words(self):
        df = {"server": 4, "state": 1, "terraform": 0}
        cov = docs_grade.coverage(list(df), df, {"server", "state"}, n=4)
        self.assertLess(cov, 0.5)
        self.assertEqual(docs_grade.coverage(list(df), df, set(df), n=4), 1.0)

    def test_words_every_document_has_weigh_nothing(self):
        df = {"server": 4, "port": 1}
        self.assertGreater(docs_grade.coverage(list(df), df, {"port"}, n=4), 0.9)


class GradeTest(IndexCase):
    def test_hit_that_holds_the_question_is_good(self):
        g = docs_grade.grade("which port does the shared server kb-http listen on", hits("kb-a/server.md"),
                             self.index)
        self.assertFalse(g.weak, g.label())
        self.assertGreater(g.coverage, docs_grade.COVERAGE_MIN)

    def test_question_about_a_thing_no_document_mentions_is_weak(self):
        g = docs_grade.grade("where is the terraform state of the hetzner server stored",
                             hits("kb-b/deploy.md"), self.index)
        self.assertTrue(g.weak, g.label())
        self.assertIn("terraform", g.missing)
        self.assertIn("in no document: terraform", g.label())

    def test_hit_that_lacks_the_specific_words_is_weak(self):
        g = docs_grade.grade("which webhook token does install need", hits("kb-b/notes.md"), self.index)
        self.assertTrue(g.weak, g.label())
        self.assertIn("webhook", g.absent)

    def test_not_graded_cases_never_count_as_weak(self):
        cases = [("port", hits("kb-a/server.md"), self.index, "too few words"),
                 ("lex: port\nvec: which port", hits("kb-a/server.md"), self.index, "typed query"),
                 ("which port does the server use", hits("kb-a/server.md"), None, "no qmd index"),
                 ("which port does the server use", hits("kb-z/gone.md"), self.index, "not in the index")]
        for q, out, index, reason in cases:
            g = docs_grade.grade(q, out, index)
            self.assertIsNone(g.coverage, q)
            self.assertFalse(g.weak)
            self.assertIn(reason, g.reason)

    def test_foreign_index_does_not_break_search(self):
        bad = os.path.join(self.tmp.name, "other.sqlite")
        sqlite3.connect(bad).close()
        g = docs_grade.grade("which port does the server use", hits("kb-a/server.md"), docs_grade.Index(bad))
        self.assertIsNone(g.coverage)
        self.assertIn("index error", g.reason)

    def test_index_path_follows_qmd(self):
        with mock.patch.dict(os.environ, {"INDEX_PATH": self.path}):
            self.assertEqual(docs_grade.index_path("/nowhere"), self.path)
        with mock.patch.dict(os.environ, {"INDEX_PATH": "", "XDG_CACHE_HOME": self.tmp.name}):
            self.assertIsNone(docs_grade.index_path(self.tmp.name))


class RefusalTest(IndexCase):
    """The check inside `search_docs`: good answers pass unchanged, weak ones fall back or refuse."""

    GOOD_Q = "which port does the shared server kb-http listen on"
    WEAK_Q = "where is the terraform state of the hetzner server stored"

    def setUp(self):
        super().setUp()
        mock.patch.object(docs_grade, "open_index", side_effect=lambda kb, *a, **k: docs_grade.Index(self.path)).start()
        mock.patch.object(kb_core, "DOCS_MODE", "light").start()
        mock.patch.object(kb_core, "ALIASES", []).start()
        self.addCleanup(mock.patch.stopall)

    def search(self, args: dict, answers: dict) -> tuple[str, list[dict]]:
        calls = []

        def once(a, q):
            calls.append(a)
            return answers["lex" if a.get("_lex_only") else "hybrid"]
        with mock.patch.object(kb_core, "_docs_once", side_effect=once):
            return kb_core.search_docs({"project": "all", **args}), calls

    def test_good_result_is_returned_unchanged(self):
        out, calls = self.search({"q": self.GOOD_Q}, {"hybrid": hits("kb-a/server.md", "kb-b/notes.md")})
        self.assertEqual(out, "[projects: all]\n\n" + hits("kb-a/server.md", "kb-b/notes.md"))
        self.assertEqual(len(calls), 1, "no fallback for a good result")

    def test_explain_shows_the_grade_of_a_good_result(self):
        out, _ = self.search({"q": self.GOOD_Q, "explain": True}, {"hybrid": hits("kb-a/server.md")})
        self.assertIn("[grade: coverage", out)

    def test_weak_result_falls_back_to_keywords(self):
        out, calls = self.search({"q": "which webhook token does install need"},
                                 {"hybrid": hits("kb-b/notes.md"), "lex": hits("kb-a/install.md")})
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1]["_lex_only"])
        self.assertIn("keyword-only results below", out)
        self.assertIn("qmd://kb-a/install.md", out)
        self.assertNotIn(docs_grade.REFUSED_MARK, out)

    def test_weak_result_and_weak_fallback_is_not_found(self):
        many = hits("kb-b/deploy.md", "kb-b/notes.md", "kb-a/install.md", "kb-a/server.md")
        out, calls = self.search({"q": self.WEAK_Q}, {"hybrid": many, "lex": hits("kb-b/deploy.md")})
        self.assertEqual(len(calls), 2)
        self.assertIn(docs_grade.REFUSED_MARK, out)
        self.assertIn("in no document: terraform", out)
        self.assertIn("weak: probably not the answer", out)
        self.assertEqual(out.count("qmd://"), kb_core.WEAK_SHOWN, "the nearest hits stay, capped and marked")

    def test_explicit_mode_refuses_without_a_fallback(self):
        out, calls = self.search({"q": self.WEAK_Q, "mode": "vec"}, {"hybrid": hits("kb-b/deploy.md")})
        self.assertEqual(len(calls), 1)
        self.assertIn(docs_grade.REFUSED_MARK, out)

    def test_refuse_false_returns_the_nearest_hits(self):
        out, calls = self.search({"q": self.WEAK_Q, "refuse": False}, {"hybrid": hits("kb-b/deploy.md")})
        self.assertNotIn(docs_grade.REFUSED_MARK, out)
        self.assertEqual(len(calls), 1)

    def test_empty_answer_is_left_alone(self):
        out, calls = self.search({"q": self.WEAK_Q}, {"hybrid": "No results found."})
        self.assertNotIn(docs_grade.REFUSED_MARK, out)

    def test_later_pages_are_cut_from_the_judged_answer(self):
        with mock.patch.object(kb_core, "search_docs", return_value=hits("kb-a/x.md")) as sd:
            kb_core.call_tool("search", {"q": "x", "domain": "docs", "page": 2})
        self.assertNotIn("refuse", sd.call_args[0][0])   # the same call as page 1: graded as usual


class LexOnlyTest(unittest.TestCase):
    def test_lex_only_drops_the_vector_line(self):
        cmd = kb_core._qmd_cmd({"_lex_only": True}, "which port does the server use")
        self.assertIn("lex: port", cmd[2])
        self.assertNotIn("vec:", cmd[2])
        searches, _ = kb_core._searches({"_lex_only": True}, "which port does the server use")
        self.assertEqual({s["type"] for s in searches}, {"lex"})

    def test_refuse_is_a_published_argument(self):
        props = kb_core.TOOLS_BY_NAME["search"]["inputSchema"]["properties"]
        self.assertIs(props["refuse"]["default"], True)
        kb_core.validate("search", {"q": "x", "refuse": False})


class BenchMetricsTest(unittest.TestCase):
    def test_confident_wrong_counts_no_answer_hits_and_wrong_first_files(self):
        path = os.path.join(os.path.dirname(BIN), "bench", "search_baseline.py")
        spec = importlib.util.spec_from_file_location("search_baseline", path)
        bench = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bench)
        rows = [
            {"kind": "gold", "top": ["a"], "first": 1, "found5": 1.0},                 # right
            {"kind": "gold", "top": ["b", "a"], "first": 2, "found5": 1.0},            # confident wrong
            {"kind": "gold", "top": [], "first": None, "found5": 0.0, "refused": True,
             "first_raw": 1},                                                          # refused, cost
            {"kind": "none", "top": ["x"]},                                            # confident wrong
            {"kind": "none", "top": [], "refused": True},                              # honest refusal
        ]
        m = bench.metrics(rows)
        self.assertEqual((m["wrong_gold"], m["wrong_none"]), (1, 1))
        self.assertAlmostEqual(m["wrong"], 2 / 5)
        self.assertAlmostEqual(m["mrr"], (1 + 0.5 + 0) / 3)
        self.assertEqual((m["refused_gold"], m["refused_right"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
