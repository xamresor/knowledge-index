"""Tests for bin/alias_expand.py — shipped measured but untested in 0.3.0; this pays that debt.

The two rules that make the difference between a useful and a harmful expansion are exactly the ones
that must not regress: expansion is **directional** (never symmetric), and matching happens on
**whole tokens of the raw query** (so an old path segment triggers, but a word that merely contains
the alias does not).
"""
from __future__ import annotations

import os
import tempfile
import unittest

from _kbtest import load_script

ae = load_script("alias_expand.py")

TABLE = [("oldname", "newname newer"), ("wrongperson", "rightperson")]


class ExpandTest(unittest.TestCase):
    def test_old_name_appends_the_canonical_terms(self):
        self.assertEqual(ae.expand("where is oldname", TABLE), "where is oldname newname newer")

    def test_the_original_query_is_preserved_verbatim(self):
        """Expansion may only add: the user's own wording still carries the strongest signal."""
        out = ae.expand("что мы решили с wrongperson по пилоту", TABLE)
        self.assertTrue(out.startswith("что мы решили с wrongperson по пилоту"))

    def test_expansion_is_directional_not_symmetric(self):
        """Querying the NEW name must not drag in the old one — otherwise pages that merely
        *discuss* the rename (changelogs, logs, planning notes) win the top rank."""
        self.assertEqual(ae.expand("where is newname", TABLE), "where is newname")

    def test_path_segments_match(self):
        out = ae.expand("репозиторий в /srv/oldname/app", TABLE)
        self.assertEqual(out, "репозиторий в /srv/oldname/app newname newer")

    def test_substring_does_not_match(self):
        """`oldnames` or `veryoldname` are different words; expanding them would be noise."""
        for query in ("veryoldname here", "oldnamed thing", "oldnames plural"):
            self.assertEqual(ae.expand(query, TABLE), query, query)

    def test_case_insensitive(self):
        self.assertEqual(ae.expand("Where is OLDNAME", TABLE), "Where is OLDNAME newname newer")

    def test_canonical_term_already_present_is_not_repeated(self):
        out = ae.expand("oldname vs newname", TABLE)
        self.assertEqual(out, "oldname vs newname newer", "only the missing canonical term is added")

    def test_no_match_leaves_the_query_untouched(self):
        self.assertEqual(ae.expand("ordinary question", TABLE), "ordinary question")

    def test_empty_table_is_a_no_op(self):
        self.assertEqual(ae.expand("where is oldname", []), "where is oldname")

    def test_empty_query_is_safe(self):
        self.assertEqual(ae.expand("", TABLE), "")
        self.assertEqual(ae.expand("   ", TABLE), "   ")

    def test_multiple_aliases_expand_in_table_order(self):
        out = ae.expand("oldname and wrongperson", TABLE)
        self.assertEqual(out, "oldname and wrongperson newname newer rightperson")


class LoadTableTest(unittest.TestCase):
    def test_missing_file_means_feature_off(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ae.load_aliases(os.path.join(d, "nope.toml")), [])

    def test_entries_are_read_and_trimmed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "aliases.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('[[alias]]\nfrom = "  a  "\nto = "  b c  "\n')
            self.assertEqual(ae.load_aliases(path), [("a", "b c")])

    def test_incomplete_entries_are_skipped_not_fatal(self):
        """A half-written table must degrade, not break search."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "aliases.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('[[alias]]\nfrom = "a"\n\n[[alias]]\nto = "b"\n\n'
                         '[[alias]]\nfrom = ""\nto = "c"\n\n[[alias]]\nfrom = "x"\nto = "y"\n')
            self.assertEqual(ae.load_aliases(path), [("x", "y")])


if __name__ == "__main__":
    unittest.main()
