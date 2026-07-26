"""Tests for bin/render_viz.py — now a data generator, and for the shell's safety rules.

Since 0.5.0 this script emits data only; the interface lives in `web/`. Two classes of behaviour are
worth locking:

  * the **data contract** the shell depends on (manifest keys, per-domain files, cross/intra split,
    `</script` escaping) — a change here silently blanks the page;
  * the **rules the shell must keep**: no `innerHTML` anywhere (node labels come from other people's
    repositories) and no hardcoded CDN as the primary renderer (the promise is that nothing leaves
    the machine).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest

from _kbtest import BIN

import render_viz

WEB = os.path.abspath(os.path.join(BIN, "..", "web"))

GRAPH = {
    "nodes": [
        {"id": "api::a.php::A", "label": "A", "type": "class", "domain": "Billing", "repo": "api"},
        {"id": "api::a.php::pay()", "label": "pay()", "type": "method", "domain": "Billing",
         "repo": "api"},
        {"id": "web::x.ts::call", "label": "call", "type": "function", "domain": "Web", "repo": "web"},
        {"id": "domain:Billing", "label": "Billing", "type": "domain"},
    ],
    "links": [
        {"source": "api::a.php::A", "target": "api::a.php::pay()", "relation": "method"},
        {"source": "web::x.ts::call", "target": "api::a.php::A", "relation": "http_request"},
        {"source": "api::a.php::A", "target": "domain:Billing", "relation": "in_domain"},
    ],
}


def parse_js(path: str) -> tuple[str, list]:
    """A generated file is exactly one call: `fn(arg, …);`. Return (fn, args)."""
    text = open(path, encoding="utf-8").read().strip()
    m = re.match(r"^(\w+)\((.*)\);$", text, re.S)
    assert m, f"{path} is not a single call: {text[:80]}"
    return m.group(1), json.loads(f"[{m.group(2)}]")


class DataContractTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        graph_path = os.path.join(self.dir.name, "graph.json")
        with open(graph_path, "w", encoding="utf-8") as fh:
            json.dump(GRAPH, fh)
        argv = sys.argv
        sys.argv = ["render_viz.py", graph_path, self.dir.name]
        try:
            with contextlib.redirect_stdout(io.StringIO()):   # the generator prints a summary line
                self.assertEqual(render_viz.main(), 0)
        finally:
            sys.argv = argv

    def test_manifest_carries_everything_the_shell_needs(self):
        fn, (manifest,) = parse_js(os.path.join(self.dir.name, render_viz.MANIFEST))
        self.assertEqual(fn, "kbManifest")
        for key in ("domains", "superNodes", "fileMap", "dataPath", "nodeCount", "generatedAt"):
            self.assertIn(key, manifest, key)
        self.assertEqual(manifest["nodeCount"], 3, "the synthetic domain node is not a member")
        self.assertEqual(sorted(manifest["domains"]), ["Billing", "Web"])
        self.assertTrue(manifest["dataPath"].endswith(render_viz.DATA_DIR + "/"))

    def test_super_nodes_are_keyed_for_collapse(self):
        _, (manifest,) = parse_js(os.path.join(self.dir.name, render_viz.MANIFEST))
        keys = {n["domkey"] for n in manifest["superNodes"]}
        self.assertEqual(keys, {"Billing", "Web"}, "collapse() looks a super-node up by domkey")

    def test_one_file_per_domain_with_members_and_intra_edges(self):
        fn, (domain, payload) = parse_js(
            os.path.join(self.dir.name, render_viz.DATA_DIR, "Billing.js"))
        self.assertEqual((fn, domain), ("kbRecv", "Billing"))
        self.assertEqual({n["id"] for n in payload["nodes"]},
                         {"api::a.php::A", "api::a.php::pay()"})
        self.assertEqual(len(payload["edges"]), 1, "the method edge is domain-internal")

    def test_cross_domain_edges_go_to_the_cross_file(self):
        fn, (cross,) = parse_js(os.path.join(self.dir.name, render_viz.DATA_DIR, "_cross.js"))
        self.assertEqual(fn, "kbCross")
        self.assertEqual(cross, [["web::x.ts::call", "Web", "api::a.php::A", "Billing",
                                  "http_request"]])

    def test_in_domain_edges_are_dropped(self):
        _, (cross,) = parse_js(os.path.join(self.dir.name, render_viz.DATA_DIR, "_cross.js"))
        self.assertFalse([e for e in cross if e[4] == "in_domain"])

    def test_search_index_is_label_id_domain(self):
        fn, (index,) = parse_js(os.path.join(self.dir.name, render_viz.DATA_DIR, "_index.js"))
        self.assertEqual(fn, "kbIndex")
        self.assertEqual(len(index), 3)
        self.assertEqual(len(index[0]), 3)

    def test_status_snapshot_is_stamped(self):
        fn, (status,) = parse_js(os.path.join(self.dir.name, render_viz.STATUS))
        self.assertEqual(fn, "kbStatus")
        self.assertEqual(status["graph"]["nodes"], 4)
        self.assertEqual(status["graph"]["projects"], 2)
        self.assertIn("built_at", status["graph"])
        self.assertIn("takenAt", status, "a snapshot must say when it was taken")
        self.assertIn("knowledge-index", status["versions"])


class EscapingTest(unittest.TestCase):
    def test_script_end_tag_inside_a_label_cannot_break_out(self):
        """A repo may contain a symbol named `</script>`; the generated file must survive it."""
        out = render_viz._js_call("kbRecv", "d", {"label": "</script><img src=x>"})
        self.assertNotIn("</script", out)
        self.assertIn("<\\/script", out)

    def test_generated_file_is_a_single_call(self):
        self.assertTrue(render_viz._js_call("kbCross", []).endswith(");\n"))


class ShellRulesTest(unittest.TestCase):
    """The shell is a source file now, so its rules can be tested instead of hoped for."""

    def read(self, name: str) -> str:
        return open(os.path.join(WEB, name), encoding="utf-8").read()

    def test_the_shell_exists_as_versioned_files(self):
        for name in ("index.html", "app.js", "style.css"):
            self.assertTrue(os.path.isfile(os.path.join(WEB, name)), name)

    def test_no_innerhtml_assignments(self):
        """Node labels come from indexed repositories: textContent only, always.

        The check looks for the property access (`.innerHTML`) rather than the bare word, so prose
        about the rule is allowed while a use of it is not.
        """
        for name in ("app.js", "index.html"):
            self.assertNotIn(".innerHTML", self.read(name), name)

    def test_the_primary_renderer_is_local_not_a_cdn(self):
        html = self.read("index.html")
        first_script = re.search(r"<script src=\"([^\"]+)\"", html).group(1)
        self.assertTrue(first_script.startswith("vendor/"),
                        "the offline copy must be tried first; the CDN is only a fallback")

    def test_the_shell_reads_the_generated_manifest(self):
        html = self.read("index.html")
        self.assertIn(render_viz.MANIFEST, html)
        self.assertIn(render_viz.STATUS, html)

    def test_no_leftover_template_placeholders(self):
        """0.5.0 removed the `__PLACEHOLDER__` substitution; nothing may reintroduce it."""
        self.assertNotIn("__", re.sub(r"__kb\w+", "", self.read("index.html")))

    def test_the_interface_is_english(self):
        """Committed UI strings stay English (public repo, readers who did not write the corpus)."""
        for name in ("index.html", "app.js", "style.css"):
            self.assertFalse(re.search(r"[А-Яа-яЁё]", self.read(name)), name)


if __name__ == "__main__":
    unittest.main()
