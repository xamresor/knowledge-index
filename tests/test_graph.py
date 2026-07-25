"""Tests for bin/graph.py — the artefact contract: atomic writes, id format, shape validation.

The point of this module is that a crash must never leave a truncated `graph.json`, because the
pipeline has no recovery step: eight scripts rewrite the same file in place. So the atomicity and the
"refuse to write nonsense" guards are the behaviours under test, not conveniences.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import graph  # noqa: F401  (path set up by _kbtest)
from _kbtest import BIN  # noqa: F401


GRAPH = {"nodes": [{"id": "api::a.php::A", "label": "A"}], "links": []}


class NodeIdTest(unittest.TestCase):
    def test_build_and_parse_round_trip(self):
        nid = graph.node_id("api", "app/Models/User.php", "User")
        self.assertEqual(nid, "api::app/Models/User.php::User")
        self.assertEqual(graph.parse_id(nid), ("api", "app/Models/User.php", "User"))

    def test_id_without_a_symbol(self):
        self.assertEqual(graph.parse_id("api::app/Models/User.php"),
                         ("api", "app/Models/User.php", None))

    def test_symbol_containing_the_separator_is_kept_whole(self):
        """A namespaced symbol must not be truncated by a naive split."""
        self.assertEqual(graph.parse_id("api::f.php::Ns::Cls"), ("api", "f.php", "Ns::Cls"))

    def test_repo_of_is_the_cheap_question(self):
        self.assertEqual(graph.repo_of("web::src/api.ts::call()"), "web")

    def test_bare_id(self):
        self.assertEqual(graph.parse_id("api"), ("api", "", None))


class VocabularyTest(unittest.TestCase):
    def test_relations_and_confidence_are_constants(self):
        """Typos become AttributeError instead of a silently wrong edge."""
        self.assertEqual(graph.Relation.HTTP_REQUEST, "http_request")
        self.assertEqual(graph.Confidence.INFERRED, "INFERRED")
        with self.assertRaises(AttributeError):
            _ = graph.Relation.HTTP_REQEUST  # noqa: B018  (deliberate typo)


class IoTest(unittest.TestCase):
    def test_save_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "graph.json")
            graph.save(GRAPH, path)
            self.assertEqual(graph.load(path), GRAPH)

    def test_save_leaves_no_temp_debris(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "graph.json")
            graph.save(GRAPH, path)
            self.assertEqual(os.listdir(d), ["graph.json"])

    def test_a_failed_serialization_leaves_the_previous_file_intact(self):
        """The reason this module exists: a crash mid-write must not truncate the artefact."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "graph.json")
            graph.save(GRAPH, path)
            unserializable = {"nodes": [{"id": "x", "label": object()}], "links": []}
            with self.assertRaises(TypeError):
                graph.save(unserializable, path)
            self.assertEqual(graph.load(path), GRAPH, "old content must survive a failed write")
            self.assertEqual(os.listdir(d), ["graph.json"], "and no partial temp file may remain")

    def test_load_rejects_a_non_graph(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "graph.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"nodes": []}, fh)          # links missing -> truncated/foreign file
            with self.assertRaises(ValueError):
                graph.load(path)

    def test_save_refuses_a_non_graph(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "graph.json")
            with self.assertRaises(ValueError):
                graph.save({"nodes": []}, path)
            self.assertFalse(os.path.exists(path), "nothing may be written when the shape is wrong")

    def test_write_json_handles_plain_objects(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "labels.json")
            graph.write_json({"0": "api · billing"}, path, indent=0)
            self.assertEqual(graph.read_json(path), {"0": "api · billing"})


if __name__ == "__main__":
    unittest.main()
