"""Tests for bin/kb_core.py — the one place both machine surfaces dispatch through.

What matters here is not the subprocess calls (those are graphify/qmd), but the contract around
them: the schema list both adapters publish, the shallow validation that replaces a jsonschema
dependency, the routing of `search` by domain, and the deprecated shims translating to `search`
rather than duplicating it.
"""
from __future__ import annotations

import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401  (puts bin/ on sys.path)

import kb_core


class SchemaTest(unittest.TestCase):
    def test_search_is_the_front_door_and_old_names_are_marked_deprecated(self):
        names = [t["name"] for t in kb_core.TOOLS]
        self.assertEqual(names[0], "search", "the generic entry point comes first")
        deprecated = {t["name"] for t in kb_core.TOOLS if t.get("deprecated")}
        self.assertEqual(deprecated, {"docs_search", "graph_query"})

    def test_every_tool_has_a_description_and_schema(self):
        for tool in kb_core.TOOLS:
            self.assertTrue(tool.get("description"), tool["name"])
            self.assertIn("inputSchema", tool, tool["name"])

    def test_version_reports_two_separate_axes(self):
        info = kb_core.version_info()
        self.assertIn("repo", info)
        self.assertIn("api_contract", info)
        self.assertNotEqual("repo", "api_contract")


class ValidateTest(unittest.TestCase):
    def test_unknown_tool(self):
        with self.assertRaises(kb_core.ToolError):
            kb_core.validate("nope", {})

    def test_missing_required_argument(self):
        with self.assertRaisesRegex(kb_core.ToolError, "'q' is required"):
            kb_core.validate("search", {"domain": "docs"})

    def test_empty_string_counts_as_missing(self):
        """A blank query would happily return the whole corpus — that is a bug, not a search."""
        with self.assertRaises(kb_core.ToolError):
            kb_core.validate("search", {"q": ""})

    def test_unknown_argument_is_rejected(self):
        """Typos must fail loudly: a silently ignored `mod=lex` looks like the mode did nothing."""
        with self.assertRaisesRegex(kb_core.ToolError, "unknown argument"):
            kb_core.validate("search", {"q": "x", "mod": "lex"})

    def test_wrong_type(self):
        with self.assertRaisesRegex(kb_core.ToolError, "must be integer"):
            kb_core.validate("search", {"q": "x", "limit": "5"})

    def test_bool_is_not_an_integer(self):
        """`True` is an int in Python; for a wire contract it is not."""
        with self.assertRaises(kb_core.ToolError):
            kb_core.validate("search", {"q": "x", "limit": True})

    def test_enum_membership(self):
        with self.assertRaisesRegex(kb_core.ToolError, "one of docs, code, both"):
            kb_core.validate("search", {"q": "x", "domain": "sideways"})
        kb_core.validate("search", {"q": "x", "domain": "both"})  # must not raise

    def test_tools_without_required_accept_empty_args(self):
        kb_core.validate("version", {})


class QmdCommandTest(unittest.TestCase):
    def test_mode_maps_to_the_qmd_entry_points(self):
        self.assertEqual(kb_core._qmd_cmd({"mode": "lex"}, "q")[1], "search")
        self.assertEqual(kb_core._qmd_cmd({"mode": "vec"}, "q")[1], "vsearch")
        self.assertEqual(kb_core._qmd_cmd({"mode": "auto"}, "q")[1], "query")
        self.assertEqual(kb_core._qmd_cmd({}, "q")[1], "query", "auto is the default")

    def test_scope_overrides_the_default_collection(self):
        cmd = kb_core._qmd_cmd({"scope": "librarian"}, "q")
        self.assertIn("librarian", cmd)

    def test_optional_flags_are_only_added_when_asked(self):
        plain = kb_core._qmd_cmd({}, "q")
        self.assertNotIn("-n", plain)
        self.assertNotIn("--min-score", plain)
        self.assertNotIn("--explain", plain)
        rich = kb_core._qmd_cmd({"limit": 7, "min_score": 0.5, "explain": True}, "q")
        self.assertIn("-n", rich)
        self.assertIn("7", rich)
        self.assertIn("--min-score", rich)
        self.assertIn("--explain", rich)


class RoutingTest(unittest.TestCase):
    """`search` must route by domain, and the deprecated names must translate — not duplicate."""

    def setUp(self):
        self.docs = mock.patch.object(kb_core, "search_docs", return_value="DOCS").start()
        self.code = mock.patch.object(kb_core, "search_code", return_value="CODE").start()
        self.addCleanup(mock.patch.stopall)

    def test_domain_docs(self):
        self.assertEqual(kb_core.call_tool("search", {"q": "x", "domain": "docs"}), "DOCS")
        self.code.assert_not_called()

    def test_domain_code(self):
        self.assertEqual(kb_core.call_tool("search", {"q": "x", "domain": "code"}), "CODE")
        self.docs.assert_not_called()

    def test_both_is_the_default_and_labels_the_sections(self):
        out = kb_core.call_tool("search", {"q": "x"})
        self.assertIn("=== docs ===", out)
        self.assertIn("=== code ===", out)
        self.assertIn("DOCS", out)
        self.assertIn("CODE", out)

    def test_deprecated_docs_search_delegates_to_search(self):
        self.assertEqual(kb_core.call_tool("docs_search", {"query": "x"}), "DOCS")
        self.docs.assert_called_once()

    def test_deprecated_graph_query_delegates_to_search(self):
        self.assertEqual(kb_core.call_tool("graph_query", {"question": "x"}), "CODE")
        self.code.assert_called_once()

    def test_call_tool_validates_before_running_anything(self):
        with self.assertRaises(kb_core.ToolError):
            kb_core.call_tool("search", {"q": "x", "domain": "nope"})
        self.docs.assert_not_called()
        self.code.assert_not_called()


class AliasIntegrationTest(unittest.TestCase):
    def test_expansion_is_applied_and_announced(self):
        with mock.patch.object(kb_core, "ALIASES", [("oldname", "newname")]), \
             mock.patch.object(kb_core, "run", return_value="results") as run:
            out = kb_core.search_docs({"q": "where is oldname"})
        self.assertIn("alias expansion", out)
        self.assertIn("newname", run.call_args[0][0][2], "the expanded query reaches qmd")

    def test_expansion_can_be_switched_off_per_call(self):
        with mock.patch.object(kb_core, "ALIASES", [("oldname", "newname")]), \
             mock.patch.object(kb_core, "run", return_value="results") as run:
            out = kb_core.search_docs({"q": "where is oldname", "expand_aliases": False})
        self.assertNotIn("alias expansion", out)
        self.assertNotIn("newname", run.call_args[0][0][2])

    def test_timeout_falls_back_to_keyword_search_only_in_auto_mode(self):
        with mock.patch.object(kb_core, "run", side_effect=["(timed out)", "bm25 hits"]) as run:
            out = kb_core.search_docs({"q": "x"})
        self.assertIn("keyword (BM25) results below", out)
        self.assertEqual(run.call_count, 2)

        with mock.patch.object(kb_core, "run", return_value="(timed out)") as run:
            out = kb_core.search_docs({"q": "x", "mode": "vec"})
        self.assertEqual(run.call_count, 1, "an explicit mode must not silently switch strategy")


if __name__ == "__main__":
    unittest.main()
