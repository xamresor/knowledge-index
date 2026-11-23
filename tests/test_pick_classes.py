"""graph_explain / graph_affected: a name typed as a class means the class, not its lower-case homonyms."""
import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401  (puts bin/ on sys.path)

import kb_core

ROWS = [
    (".customer()", "method", "api", "app/Models/User.php", "m1"),
    ("Customer", "model", "api", "app/Models/Customer.php", "c1"),
    ("customer", "template", "bo2", "app/pages/clients/edit.vue", "t1"),
    ("ObjectController", "controller", "api", "app/Http/Controllers/V1/ObjectController.php", "o1"),
    ("ObjectController", "controller", "api", "app/Http/Controllers/V2/ObjectController.php", "o2"),
    ("Lonely", "class", "api", "app/Lonely.php", "l1"),
    ("Customer", "domain", "api", "", "d1"),
]


class PickClasses(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(kb_core, "labels", lambda graph=None: ROWS)
        p.start()
        self.addCleanup(p.stop)

    def test_the_class_wins_over_methods(self):
        classes, others = kb_core._pick_classes("Customer", None)
        self.assertEqual([r[4] for r in classes], ["c1"])
        self.assertEqual({r[4] for r in others}, {"m1", "t1"})   # the domain anchor is never a match

    def test_projects_limit_the_matches(self):
        classes, others = kb_core._pick_classes("Customer", {"api"})
        self.assertEqual([r[4] for r in others], ["m1"])

    def test_two_classes_with_one_name_are_both_kept(self):
        classes, others = kb_core._pick_classes("ObjectController", None)
        self.assertEqual([r[4] for r in classes], ["o1", "o2"])
        self.assertEqual(others, [])

    def test_no_change_without_homonyms_or_a_class(self):
        self.assertIsNone(kb_core._pick_classes("Lonely", None))
        self.assertIsNone(kb_core._pick_classes("customer", None))      # typed in lower case: as before
        self.assertIsNone(kb_core._pick_classes("api:Customer", None))  # pinned: as before
        self.assertIsNone(kb_core._pick_classes("User.customer", None))


class ExplainPicked(unittest.TestCase):
    def test_explain_answers_the_class_by_id_and_names_the_rest(self):
        with mock.patch.object(kb_core, "labels", lambda graph=None: ROWS), \
                mock.patch.object(kb_core.warm, "graph", lambda: None), \
                mock.patch.object(kb_core.warm, "graph_once",
                                  lambda argv, view: "All connections (1):\n  <-- User [eloquent]" if argv[1] == "c1" else ""):
            out = kb_core.call_tool("graph_explain", {"symbol": "Customer"}, None)
        self.assertIn("Node: Customer", out)
        self.assertIn("ID:        c1", out)
        self.assertIn("<-- User [eloquent]", out)
        self.assertIn("Also named like this, not shown: .customer()", out)


if __name__ == "__main__":
    unittest.main()
