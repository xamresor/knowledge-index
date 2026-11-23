#!/usr/bin/env python3
"""Paging: every tool but `version` takes `page`, long answers are cut at line ends, nothing is lost."""
from __future__ import annotations

import re
import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401

import kb_core
from lang import php_laravel


class Paginate(unittest.TestCase):
    def test_every_tool_but_version_takes_page_and_page_length(self):
        for tool in kb_core.TOOLS:
            props = tool["inputSchema"]["properties"]
            for key in ("page", "page_length"):
                self.assertEqual(key in props, tool["name"] != "version", (tool["name"], key))

    def test_optional_arguments_show_their_defaults(self):
        props = {t["name"]: t["inputSchema"]["properties"] for t in kb_core.TOOLS}
        self.assertEqual(props["search"]["domain"]["default"], "both")
        self.assertEqual(props["search"]["limit"]["default"], 5)
        self.assertEqual(props["graph_routes"]["k"]["default"], 3)
        for name, p in props.items():
            if name != "version":
                self.assertEqual(p["page"]["default"], 1)
                self.assertEqual(p["page_length"]["default"], kb_core.PAGE_CHARS)

    def test_page_length_sets_the_page_size(self):
        long_text = "\n".join("row %d" % i for i in range(3000))
        with mock.patch.object(kb_core, "_index", lambda fn, *a: long_text):
            short = kb_core.call_tool("contracts_search", {"query": "x", "page_length": 1000})
            wide = kb_core.call_tool("contracts_search", {"query": "x", "page_length": 20000})
            with self.assertRaises(kb_core.ToolError):
                kb_core.call_tool("contracts_search", {"query": "x", "page_length": 10})
        self.assertLess(len(short), 1200)
        self.assertGreater(len(wide), 15000)

    def test_pages_cover_the_text_without_loss(self):
        text = "[projects: api]\n\n" + "\n".join(f"- line {i} " + "x" * 40 for i in range(400))
        pages, n = [], 1
        while True:
            out = kb_core.paginate(text, n, size=2000)
            self.assertTrue(out.startswith("[projects: api]\n\n"))
            body = out.split("\n\n", 1)[1]
            pages.append(body.split("\n\n[page ")[0])
            if "next: page" not in out:
                break
            n += 1
        self.assertGreater(n, 5)
        joined = "\n".join(pages)
        for i in range(400):
            self.assertIn(f"- line {i} ", joined)
        self.assertIn("no page", kb_core.paginate(text, n + 1, size=2000))

    def test_cursor_serves_the_same_answer_to_its_owner_only(self):
        calls = []

        def produce(fn, *a):
            calls.append(1)
            return "\n".join("row %d" % i for i in range(3000))
        with mock.patch.object(kb_core, "_index", produce):
            first = kb_core.call_tool("contracts_search", {"query": "x"}, {"api"}, "ann")
            token = re.search(r'cursor: "([^"]+)"', first).group(1)
            second = kb_core.call_tool("contracts_search", {"cursor": token, "page": 2}, {"api"}, "ann")
            self.assertEqual(len(calls), 1)            # page 2 did not run the query again
            self.assertIn("row 1000", second + kb_core.call_tool(
                "contracts_search", {"cursor": token, "page": 3}, {"api"}, "ann"))
            for who, projects, tool in (("bob", {"api"}, "contracts_search"),
                                        ("ann", {"api", "web"}, "contracts_search"),
                                        ("ann", {"api"}, "connections")):
                with self.assertRaises(kb_core.ToolError):
                    kb_core.call_tool(tool, {"cursor": token, "page": 2}, projects, who)

    def test_cursor_alone_walks_the_pages_with_a_frozen_size(self):
        with mock.patch.object(kb_core, "_index", lambda fn, *a: "\n".join("row %d" % i for i in range(3000))):
            first = kb_core.call_tool("contracts_search", {"query": "x", "page_length": 5000}, {"api"}, "ann")
            token = re.search(r'cursor: "([^"]+)"', first).group(1)
            total = re.search(r"page 1 of (\d+)", first).group(1)
            p2 = kb_core.call_tool("contracts_search", {"cursor": token}, {"api"}, "ann")
            self.assertIn(f"[page 2 of {total}", p2)
            p3 = kb_core.call_tool("contracts_search", {"cursor": token, "page_length": 1000}, {"api"}, "ann")
            self.assertIn(f"[page 3 of {total}", p3)   # page_length cannot re-cut an answer behind a cursor
            back = kb_core.call_tool("contracts_search", {"cursor": token, "page": 1}, {"api"}, "ann")
            self.assertIn(f"[page 1 of {total}", back)
            self.assertIn(f"[page 2 of {total}", kb_core.call_tool("contracts_search", {"cursor": token}, {"api"}, "ann"))

    def test_cursor_expires(self):
        store = kb_core.Cursors(ttl=0.0, cap=10)
        token = store.put("x", "search", (None, None))
        with self.assertRaises(kb_core.ToolError):
            store.get(token, "search", (None, None))

    def test_short_answer_has_no_page_note(self):
        self.assertEqual(kb_core.paginate("short", 1), "short")

    def test_page_reaches_the_tool_through_call_tool(self):
        long_text = "\n".join("row %d" % i for i in range(3000))
        with mock.patch.object(kb_core, "_index", lambda fn, *a: long_text):
            first = kb_core.call_tool("contracts_search", {"query": "x"})
            second = kb_core.call_tool("contracts_search", {"query": "x", "page": 2})
        self.assertIn("next: page: 2", first)
        self.assertNotEqual(first, second)
        with self.assertRaises(kb_core.ToolError):
            kb_core.call_tool("contracts_search", {"query": "x", "page": 0})

    def test_docs_search_returns_at_most_limit_hits(self):
        # a search with limit 2 printed 8 documents: page 1 asked qmd for 4x the limit
        seen = []

        def fake_docs(args, allowed=None):
            seen.append(args.get("limit"))
            return "[projects: api]\n\n" + "".join(
                f"qmd://kb-api/d{i}.md:1 #{i}\nTitle: D{i}\n\n" for i in range(int(args.get("limit") or 5)))
        with mock.patch.object(kb_core, "search_docs", fake_docs):
            out = kb_core.call_tool("search", {"q": "release cut", "domain": "docs", "limit": 2})
        self.assertEqual(out.count("qmd://"), 2, out)
        self.assertEqual(seen, [2])

    def test_docs_page_two_is_cut_from_page_one_text(self):
        # page 2 without a cursor runs the same `limit` and cuts the same text (no hits 6-10 of a new query)
        seen = []

        def fake_docs(args, allowed=None):
            seen.append(args.get("limit"))
            return "[projects: api]\n\n" + "".join(
                f"qmd://kb-api/d{i}.md:1 #{i}\nTitle: D{i}\n\n" + "t" * 400 + "\n\n" for i in range(int(args["limit"])))
        with mock.patch.object(kb_core, "search_docs", fake_docs):
            out = kb_core.call_tool("search", {"q": "x", "domain": "docs", "limit": 5, "page": 2,
                                               "page_length": 1000})
        self.assertEqual(seen, [5])
        self.assertIn("page 2 of", out)


class BackendCalls(unittest.TestCase):
    def test_php_calls_with_versioned_paths(self):
        src = ("$this->send($this->bridge(), 'post', 'v1/sw-bridge/vehicles/' . $vid . '/commands', []);\n"
               'Http::withToken($t)->get("v2/users/{$uid}/roles");\n'
               "$cache->get('v1-key'); $arr->get('name');\n")
        self.assertEqual([(v, p) for _, v, p in php_laravel.iter_backend_calls(src)],
                         [("post", "v1/sw-bridge/vehicles/{}/commands"), ("get", "v2/users/{}/roles")])


if __name__ == "__main__":
    unittest.main()
