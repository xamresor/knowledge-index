"""Tests for bin/query_build.py — the query document that replaces a raw phrase.

Why this exists at all is a measurement: qmd joins a phrase's terms with AND, so the page holding the
answer is excluded when it lacks one word of the question (recall 0.14 on the golden set). Splitting
the question into one `lex:` line per content word lets qmd fuse instead of intersect (recall 0.67,
MRR 0.59) — without patching qmd.

The behaviours locked here are the ones that would silently undo that: stop words creeping back in
(each surviving term is its own sub-query, so a stop word is its own noise source), compound tokens
being split (`doc-search`, `renget-api` are the most discriminative words in the corpus), and a
caller's hand-written query document being rewritten.
"""
from __future__ import annotations

import unittest

from _kbtest import BIN  # noqa: F401  (puts bin/ on sys.path)

import query_build as qb


class TermsTest(unittest.TestCase):
    def test_stop_words_are_dropped_in_both_languages(self):
        self.assertEqual(qb.terms("на каком порту работает клетка"),
                         ["порту", "работает", "клетка"])
        self.assertEqual(qb.terms("what is the monthly minimum"), ["monthly", "minimum"])

    def test_compound_tokens_survive_intact(self):
        """Splitting these would throw away the words that actually discriminate."""
        for query, expected in [
            ("порт doc-search", ["порт", "doc-search"]),
            ("репозиторий renget-api", ["репозиторий", "renget-api"]),
            ("где graph.json", ["graph.json"]),
            ("в /srv/oldname лежит", ["srv/oldname", "лежит"]),
        ]:
            with self.subTest(query=query):
                self.assertEqual(qb.terms(query), expected)

    def test_order_is_preserved_and_duplicates_dropped(self):
        self.assertEqual(qb.terms("alias alias table alias"), ["alias", "table"])

    def test_single_characters_are_ignored(self):
        self.assertEqual(qb.terms("a b порт"), ["порт"])

    def test_term_count_is_capped(self):
        query = " ".join(f"term{i}" for i in range(40))
        self.assertEqual(len(qb.terms(query)), qb.MAX_TERMS)
        self.assertEqual(len(qb.terms(query, max_terms=3)), 3)

    def test_case_is_folded(self):
        self.assertEqual(qb.terms("ConnectPay SETUP"), ["connectpay", "setup"])


class BuildTest(unittest.TestCase):
    def test_document_has_intent_one_lex_per_term_and_a_vector_line(self):
        doc = qb.build("на каком порту работает клетка doc-search")
        lines = doc.splitlines()
        self.assertEqual(lines[0], "intent: на каком порту работает клетка doc-search")
        self.assertEqual([l for l in lines if l.startswith("lex: ")],
                         ["lex: порту", "lex: работает", "lex: клетка", "lex: doc-search"])
        self.assertEqual(lines[-1], "vec: на каком порту работает клетка doc-search")

    def test_vector_line_can_be_omitted(self):
        doc = qb.build("порт doc-search", want_vector=False)
        self.assertNotIn("vec:", doc)
        self.assertIn("lex: порт", doc)

    def test_a_hand_written_document_is_left_alone(self):
        """If a caller already speaks qmd's syntax, rewriting it would override their intent."""
        original = 'lex: "exact phrase" -baseball\nvec: something'
        self.assertEqual(qb.build(original), original)

    def test_a_query_of_only_stop_words_falls_back_to_the_raw_text(self):
        """Better to ask qmd the original question than to send it an empty document."""
        self.assertEqual(qb.build("что и как"), "что и как")

    def test_empty_query_is_returned_unchanged(self):
        self.assertEqual(qb.build(""), "")

    def test_the_intent_line_keeps_the_original_wording(self):
        """qmd uses intent for snippets and disambiguation, so it must not be the stripped version."""
        doc = qb.build("почему нам отказал платёжный провайдер")
        self.assertIn("intent: почему нам отказал платёжный провайдер", doc)


class IntegrationTest(unittest.TestCase):
    def test_core_uses_a_document_for_auto_and_hybrid_but_not_for_explicit_modes(self):
        import kb_core
        auto = kb_core._qmd_cmd({"mode": "auto"}, "на каком порту работает doc-search")[2]
        self.assertIn("lex: порту", auto)
        lex = kb_core._qmd_cmd({"mode": "lex"}, "на каком порту работает doc-search")[2]
        self.assertNotIn("lex: ", lex, "an explicit mode is the caller's decision, not ours")
        vec = kb_core._qmd_cmd({"mode": "vec"}, "на каком порту")[2]
        self.assertNotIn("lex: ", vec)


if __name__ == "__main__":
    unittest.main()
