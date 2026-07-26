"""Tests for bin/lang/ — language knowledge for the graph, one plugin per language.

Two things are pinned. First the **registry**: the file-type sets used to be copy-pasted into four
enrichers, and the failure mode of that was a *silently partial graph* — nothing looks broken, edges
are simply missing. So the sets live here, and `FRONTEND` must stay narrower than `ALL_CODE` (a merged
"code extensions" list would quietly change what `link_http` scans).

Then the **markdown plugin**, the first one for a format that is not code. Its risk is the opposite:
too many edges. A doc mentioning `User` must not attach itself to every `User` in every repo, so the
stinginess rules are the tests.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _kbtest import BIN  # noqa: F401  (puts bin/ on sys.path)

import graph as graph_io
import lang
from lang import markdown


class RegistryTest(unittest.TestCase):
    def test_frontend_is_narrower_than_all_code(self):
        """link_http scans frontend files only; merging the sets would change its behaviour."""
        self.assertNotIn(".php", lang.FRONTEND)
        self.assertTrue(set(lang.FRONTEND) < set(lang.ALL_CODE))
        self.assertIn(".php", lang.ALL_CODE)

    def test_the_enrichers_no_longer_carry_their_own_copies(self):
        """The duplication this module removed must not come back."""
        for name in ("enrich.py", "link_data.py", "link_http.py", "link_rationale.py"):
            source = (Path(BIN) / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertIn("import lang", source)
                self.assertNotIn('(".js", ".ts", ".tsx", ".vue")', source)

    def test_comment_prefixes_are_language_knowledge(self):
        self.assertIn("#", lang.comment_prefixes_for("a.py"))
        self.assertNotIn("//", lang.comment_prefixes_for("a.py"))
        self.assertIn("//", lang.comment_prefixes_for("a.ts"))
        self.assertIn("//", lang.comment_prefixes_for("unknown.xyz"), "a sane default, not a crash")

    def test_plugins_declare_the_contract(self):
        for plugin in lang.plugins():
            with self.subTest(plugin=plugin.NAME):
                self.assertTrue(plugin.EXTENSIONS)
                self.assertIn("relations", plugin.CONTRIBUTES)
                self.assertTrue(callable(plugin.enrich))

    def test_the_registry_separates_pipelines_from_patterns(self):
        """A knowledge module has no enrich(); calling it as a pipeline step would be a crash."""
        self.assertEqual({m.NAME for m in lang.plugins()}, {"markdown"})
        self.assertEqual({m.NAME for m in lang.patterns()}, {"php_laravel", "js_ts"})
        for module in lang.patterns():
            self.assertFalse(hasattr(module, "enrich"), module.NAME)

    def test_describe_is_status_output(self):
        described = lang.describe()
        self.assertIn("links_to", described["markdown"]["relations"])
        self.assertEqual(described["markdown"]["role"], "plugin")
        self.assertEqual(described["php_laravel"]["role"], "patterns")
        self.assertIn("eloquent", described["php_laravel"]["relations"])

    def test_the_enrichers_compile_no_patterns_of_their_own(self):
        """The point of the move: language patterns in lang/, pipelines in the enrichers.

        Asserted structurally rather than by searching for `belongsTo` or `node_modules` — those
        words legitimately appear in prose explaining what an edge means. A compiled pattern is the
        thing that must not live here.
        """
        for name in ("enrich.py", "link_data.py", "link_http.py"):
            source = (Path(BIN) / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertNotIn("re.compile", source)


def graph_with(*labels: str) -> dict:
    return {"nodes": [{"id": f"api::src/{i}.php::{lab}", "label": lab, "repo": "api"}
                      for i, lab in enumerate(labels)],
            "links": []}


class DistinctivenessTest(unittest.TestCase):
    def test_camel_case_methods_and_namespaced_names_qualify(self):
        for label in ("BookingService", "commissionFor()", "Ns::Cls"):
            self.assertTrue(markdown._is_distinctive(label), label)

    def test_short_or_common_words_do_not(self):
        """`User` in a doc would otherwise link to every User in every repo — worse than no edge."""
        for label in ("User", "app", "index.php", "get"):
            self.assertFalse(markdown._is_distinctive(label), label)

    def test_ambiguous_labels_are_skipped(self):
        """Same rule as dedupe.py: when a label has two owners, do not invent a fact."""
        index = markdown._symbol_index(graph_with("BookingService", "BookingService", "PaymentGate"))
        self.assertNotIn("BookingService", index)
        self.assertIn("PaymentGate", index)


class MarkdownEnrichTest(unittest.TestCase):
    def setUp(self):
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.docs = Path(self.dir.name)

    def write(self, name: str, body: str) -> None:
        path = self.docs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def run_enrich(self, graph: dict) -> tuple[int, int]:
        return markdown.enrich(graph, [("api", str(self.docs))])

    def test_each_markdown_file_becomes_a_doc_node(self):
        self.write("auth.md", "# Auth\n")
        self.write("sub/deep.md", "# Deep\n")
        g = graph_with("BookingService")
        nodes, _ = self.run_enrich(g)
        self.assertEqual(nodes, 2)
        docs = [n for n in graph_io.nodes(g) if n.get("type") == "doc"]
        self.assertEqual({n["label"] for n in docs}, {"auth.md", "deep.md"})
        self.assertTrue(all(n["domain"] == "docs" for n in docs))

    def test_wikilinks_and_relative_links_are_extracted_edges(self):
        self.write("a.md", "see [[b]] and [text](c.md)\n")
        self.write("b.md", "b\n")
        self.write("c.md", "c\n")
        g = graph_with()
        self.run_enrich(g)
        links = [e for e in graph_io.links(g) if e["relation"] == "links_to"]
        self.assertEqual(len(links), 2)
        self.assertTrue(all(e["confidence"] == graph_io.Confidence.EXTRACTED for e in links),
                        "the link is literally written in the file")

    def test_a_symbol_mention_is_inferred_never_extracted(self):
        self.write("a.md", "The BookingService owns the flow.\n")
        g = graph_with("BookingService")
        self.run_enrich(g)
        documents = [e for e in graph_io.links(g) if e["relation"] == "documents"]
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["confidence"], graph_io.Confidence.INFERRED)
        self.assertLess(documents[0]["confidence_score"], 1.0)

    def test_partial_word_matches_do_not_count(self):
        self.write("a.md", "BookingServiceFactory is a different thing\n")
        g = graph_with("BookingService")
        self.run_enrich(g)
        self.assertFalse([e for e in graph_io.links(g) if e["relation"] == "documents"])

    def test_mentions_are_capped_per_document(self):
        many = [f"ThingNumber{i}" for i in range(30)]
        self.write("index.md", " ".join(many))
        g = graph_with(*many)
        self.run_enrich(g)
        documents = [e for e in graph_io.links(g) if e["relation"] == "documents"]
        self.assertEqual(len(documents), markdown.MAX_MENTIONS_PER_DOC,
                         "a doc mentioning everything is an index, not a description")

    def test_a_broken_wikilink_adds_nothing(self):
        self.write("a.md", "see [[nonexistent]]\n")
        g = graph_with()
        _, edges = self.run_enrich(g)
        self.assertEqual(edges, 0)

    def test_vendor_and_node_modules_are_ignored(self):
        self.write("node_modules/pkg/readme.md", "# vendored\n")
        self.write("vendor/lib/readme.md", "# vendored\n")
        self.write("real.md", "# real\n")
        g = graph_with()
        nodes, _ = self.run_enrich(g)
        self.assertEqual(nodes, 1)

    def test_a_missing_docs_root_is_not_an_error(self):
        g = graph_with()
        self.assertEqual(markdown.enrich(g, [("api", "/nope/does/not/exist")]), (0, 0))

    def test_rerunning_does_not_duplicate_nodes(self):
        self.write("a.md", "# A\n")
        g = graph_with()
        self.run_enrich(g)
        nodes, _ = self.run_enrich(g)
        self.assertEqual(nodes, 0, "the pipeline reruns; nodes must be idempotent")


if __name__ == "__main__":
    unittest.main()
