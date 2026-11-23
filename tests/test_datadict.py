"""Tests for bin/datadict.py and its hooks in bin/kb_core.py (the `datadict` add-on).

The dictionary is a hand-written file per project. These tests pin: how the file parses and what
mistakes it reports, the check against the graph's tables, that a business term or synonym finds the
table in `search`, that `graph_explain` of a table shows its meaning, and that with the add-on off
nothing changes.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401  (puts bin/ on sys.path)

import datadict
import kb_core

SHOP = """
[[table]]
name = "orders"
term = "Customer order"
grain = "one row per order a customer placed"
summable = ["total_amount"]
not_summable = ["id", "customer_id"]
synonyms = ["sale", "revenue"]
notes = "Cancelled orders stay in the table."

[[table]]
name = "order_items"
term = "Order line"
grain = "one row per product in one order"
summable = ["quantity", "line_total"]
not_summable = ["unit_price"]
synonyms = ["line item"]

[[table]]
name = "ghost_table"
term = "Something old"
"""

GRAPH = {
    "nodes": [
        {"id": "db::orders", "label": "orders", "type": "db_table", "repo": "db"},
        {"id": "db::order_items", "label": "order_items", "type": "db_table", "repo": "db"},
        {"id": "db::customers", "label": "customers", "type": "db_table", "repo": "db"},
        {"id": "db::audit_log", "label": "audit_log", "type": "db_table", "repo": "db"},
        {"id": "m1", "label": "Order", "type": "model", "repo": "shop"},
        {"id": "m2", "label": "Customer", "type": "model", "repo": "shop"},
        {"id": "m3", "label": "Logger", "type": "class", "repo": "other"},
        {"id": "m4", "label": "Sale", "type": "class", "repo": "shop"},
        {"id": "domain::Order", "label": "Order", "type": "domain", "repo": "domain"},
    ],
    "links": [
        {"source": "m1", "target": "db::orders", "relation": "defines_table"},
        {"source": "m2", "target": "db::customers", "relation": "defines_table"},
        {"source": "db::order_items", "target": "db::orders", "relation": "fk"},
        {"source": "m3", "target": "db::audit_log", "relation": "sql"},
        {"source": "db::audit_log", "target": "domain::Order", "relation": "in_domain"},
    ],
}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "datadict")
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "shop.toml"), "w", encoding="utf-8") as f:
            f.write(SHOP)
        self.graph = os.path.join(self.tmp.name, "graph.json")
        with open(self.graph, "w", encoding="utf-8") as f:
            json.dump(GRAPH, f)
        env = mock.patch.dict(os.environ, {"KB_DATADICT_DIR": self.dir})
        env.start()
        self.addCleanup(env.stop)

    def addon(self, on: bool):
        base = "code-mode,tool-multicall"
        return mock.patch.dict(os.environ, {"KB_ADDONS": base + (",datadict" if on else "")})


class ParseTest(unittest.TestCase):
    def test_reads_every_field(self):
        entries, errors = datadict.parse(SHOP)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(entries), ["ghost_table", "order_items", "orders"])
        o = entries["orders"]
        self.assertEqual(o["term"], "Customer order")
        self.assertEqual(o["summable"], ["total_amount"])
        self.assertEqual(o["not_summable"], ["id", "customer_id"])
        self.assertEqual(o["synonyms"], ["sale", "revenue"])
        self.assertIn("Cancelled", o["notes"])

    def test_reports_mistakes_and_keeps_the_good_entries(self):
        text = """
[[table]]
term = "no name"

[[table]]
name = "a"
summable = "x"
colour = "red"

[[table]]
name = "b"
summable = ["x"]
not_summable = ["x"]

[[table]]
name = "b"
"""
        entries, errors = datadict.parse(text)
        self.assertEqual(sorted(entries), ["a", "b"])
        joined = "\n".join(errors)
        self.assertIn("`name` is required", joined)
        self.assertIn("`summable` must be a list of strings", joined)
        self.assertIn("unknown key `colour`", joined)
        self.assertIn("in both `summable` and `not_summable`: x", joined)
        self.assertIn("listed twice", joined)

    def test_bad_toml_is_an_error_not_a_crash(self):
        entries, errors = datadict.parse("[[table]\nname=")
        self.assertEqual(entries, {})
        self.assertTrue(errors[0].startswith("not valid TOML"))

    def test_missing_folder_is_an_empty_dictionary(self):
        with mock.patch.dict(os.environ, {"KB_DATADICT_DIR": "/nonexistent/datadict"}):
            self.assertEqual(datadict.entries(), {})


class ValidateTest(Fixture):
    def test_unknown_and_missing_tables(self):
        rows = datadict.problems(self.graph, None, {"shop", "other"})
        kinds = {(r["kind"], r["where"]) for r in rows}
        self.assertIn(("datadict-unknown-table", "ghost_table"), kinds)
        # customers: the shop model uses it and the file has no entry
        self.assertIn(("datadict-missing", "customers"), kinds)
        # audit_log belongs to another project (and in_domain is no use); orders has an entry
        self.assertNotIn(("datadict-missing", "audit_log"), kinds)
        self.assertNotIn(("datadict-missing", "orders"), kinds)
        self.assertTrue(all(r["project"] == "shop" for r in rows))

    def test_a_file_for_no_project_is_reported(self):
        rows = datadict.problems(self.graph, None, {"other"})
        self.assertIn("datadict-invalid", {r["kind"] for r in rows})

    def test_a_limited_caller_sees_only_its_projects(self):
        self.assertEqual(datadict.problems(self.graph, {"other"}, {"shop", "other"}), [])

    def test_problems_tool_lists_them_without_a_built_index(self):
        with self.addon(True), mock.patch.object(kb_core, "GRAPH", self.graph), \
                mock.patch.object(kb_core.contracts, "PROBLEMS", os.path.join(self.tmp.name, "none.json")):
            out = kb_core._dispatch("problems", {}, None)
        self.assertIn("shop/datadict-unknown-table 1", out)
        self.assertIn("ghost_table", out)

    def test_problems_tool_without_the_addon_is_unchanged(self):
        with self.addon(False), mock.patch.object(kb_core, "GRAPH", self.graph), \
                mock.patch.object(kb_core.contracts, "PROBLEMS", os.path.join(self.tmp.name, "none.json")):
            with self.assertRaises(kb_core.ToolError):   # "not built yet", as before
                kb_core._dispatch("problems", {}, None)


class SearchTest(Fixture):
    def test_term_and_synonym_find_the_table(self):
        book = datadict.entries()
        self.assertEqual(datadict.matches("which table holds revenue per customer", book)[0][2]["name"],
                         "orders")
        self.assertEqual(datadict.matches("where are the line items stored", book)[0][2]["name"],
                         "order_items")
        self.assertEqual(datadict.matches("customer orders", book)[0][2]["name"], "orders")
        self.assertEqual(datadict.matches("weather forecast", book), [])

    def test_search_code_puts_the_block_after_the_scope_line(self):
        graph_out = "[projects: all]\n\nTraversal: x\nNODE Order [src=a.php]"
        with self.addon(True), mock.patch.object(kb_core, "graph_call", return_value=graph_out):
            out = kb_core.search_code({"q": "total sales revenue"}, None)
        self.assertTrue(out.startswith("[projects: all]\n\nData dictionary"))
        self.assertIn("TABLE orders  [shop]", out)
        self.assertIn("Sum:        total_amount", out)
        self.assertTrue(out.endswith(graph_out.split("\n\n", 1)[1]))

    def test_a_limited_caller_does_not_see_other_dictionaries(self):
        with self.addon(True), mock.patch.object(kb_core, "graph_call", return_value="[projects: x]\n\nNODE A"):
            out = kb_core.search_code({"q": "revenue"}, {"other"})
        self.assertNotIn("Data dictionary", out)

    def test_addon_off_changes_nothing(self):
        graph_out = "[projects: all]\n\nNODE Order [src=a.php]"
        with self.addon(False), mock.patch.object(kb_core, "graph_call", return_value=graph_out):
            self.assertEqual(kb_core.search_code({"q": "revenue"}, None), graph_out)


EXPLAIN = ("Node: orders\n  ID:        db::orders\n  Source:\n  Type:      \n  Community: 1\n\n"
           "Connections (1):\n  <-- Order [defines_table] [INFERRED]")


class ExplainTest(Fixture):
    def test_table_node_shows_its_meaning(self):
        out = datadict.with_explain(EXPLAIN, datadict.entries())
        head, conns = out.split("\n\nConnections", 1)
        self.assertIn("Meaning:   (data dictionary of shop)", head)
        self.assertIn("Grain:      one row per order a customer placed", head)
        self.assertIn("Do not sum: id, customer_id", head)
        self.assertIn("<-- Order", conns)

    def test_other_nodes_are_left_alone(self):
        text = "Node: Order\n  ID:        m1\n\nConnections (0):"
        self.assertEqual(datadict.with_explain(text, datadict.entries()), text)

    def test_graph_explain_with_the_addon(self):
        with self.addon(True), mock.patch.object(kb_core, "GRAPH", self.graph), \
                mock.patch.object(kb_core, "graph_call", return_value="[projects: all]\n\n" + EXPLAIN) as gc:
            out = kb_core._dispatch("graph_explain", {"symbol": "orders"}, None)
        self.assertIn("Term:       Customer order", out)
        self.assertEqual(gc.call_args[0][0], ["graphify", "explain", "orders"])

    def test_a_business_term_is_read_as_its_table(self):
        with self.addon(True), mock.patch.object(kb_core, "GRAPH", self.graph), \
                mock.patch.object(kb_core, "graph_call", return_value="[projects: all]\n\n" + EXPLAIN) as gc:
            out = kb_core._dispatch("graph_explain", {"symbol": "Revenue"}, None)
        self.assertEqual(gc.call_args[0][0], ["graphify", "explain", "orders"])
        self.assertTrue(out.startswith("[data dictionary: 'Revenue' is the table orders]"))

    def test_a_node_label_wins_over_a_term(self):
        # `sale` is a synonym of orders, but `Sale` is also a class in the graph: the class wins
        with self.addon(True), mock.patch.object(kb_core, "GRAPH", self.graph), \
                mock.patch.object(kb_core, "graph_call", return_value="x") as gc:
            out = kb_core._dispatch("graph_explain", {"symbol": "Sale"}, None)
        self.assertEqual(gc.call_args[0][0], ["graphify", "explain", "Sale"])
        self.assertEqual(out, "x")

    def test_addon_off_changes_nothing(self):
        raw = "[projects: all]\n\n" + EXPLAIN
        with self.addon(False), mock.patch.object(kb_core, "GRAPH", self.graph), \
                mock.patch.object(kb_core, "graph_call", return_value=raw) as gc:
            out = kb_core._dispatch("graph_explain", {"symbol": "revenue"}, None)
        self.assertEqual(out, raw)
        self.assertEqual(gc.call_args[0][0], ["graphify", "explain", "revenue"])


class EvalHarnessTest(unittest.TestCase):
    def test_top_tables_in_order_of_first_mention(self):
        import importlib.util
        path = os.path.join(os.path.dirname(BIN), "bench", "datadict_eval.py")
        spec = importlib.util.spec_from_file_location("datadict_eval", path)
        ev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ev)
        text = "TABLE orders ... NODE customers ... app/Models/order_items.php ... orders again"
        got = ev.top_tables(text, {"orders", "customers", "order_items", "invoices"})
        self.assertEqual(got, ["orders", "customers"])
        self.assertEqual(ev.recall(got, ["orders", "invoices"]), 0.5)
        # without the add-on the answer names the model, not the table: the model counts as its table
        got = ev.top_tables("NODE Invoice ... NODE customers ... invoices", {"customers", "invoices"},
                            models={"Invoice": "invoices"})
        self.assertEqual(got, ["invoices", "customers"])


if __name__ == "__main__":
    unittest.main()
