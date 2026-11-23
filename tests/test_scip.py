#!/usr/bin/env python3
"""bin/link_scip.py (add-on `scip`): decode a SCIP index, map its symbols to graph nodes, add calls."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _kbtest import BIN

import link_scip


# --- a tiny protobuf encoder for the fixture ---------------------------------------------------
def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def field(num: int, value) -> bytes:
    if isinstance(value, int):
        return varint(num << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint(num << 3 | 2) + varint(len(value)) + value


def packed(num: int, ints: list[int]) -> bytes:
    return field(num, b"".join(varint(i) for i in ints))


def occurrence(symbol: str, rng: list[int], roles: int = 0, enclosing: list[int] | None = None,
               unpacked: bool = False) -> bytes:
    body = b"".join(field(1, i) for i in rng) if unpacked else packed(1, rng)
    body += field(2, symbol) + (field(3, roles) if roles else b"")
    return body + (packed(7, enclosing) if enclosing else b"")


def document(path: str, occs: list[bytes], symbols: list[str]) -> bytes:
    body = field(4, "php") + field(1, path) + b"".join(field(2, o) for o in occs)
    return body + b"".join(field(3, field(1, s) + field(6, s.split("#")[-1])) for s in symbols)


def index(docs: list[bytes]) -> bytes:
    meta = field(1, 0) + field(2, field(1, "scip-php") + field(2, "0.0.2")) + field(3, "file:///app")
    return field(1, meta) + b"".join(field(2, d) for d in docs) + field(3, field(1, "php . . Ext#x()."))


P = "scip-php composer app 1.0 "
ESCROW, CHARGE, OTHER = P + "App/Services/Escrow#", P + "App/Services/Escrow#charge().", P + "App/Services/Escrow#other()."
BOOK, AMOUNT = P + "App/Contracts/Payouts#book().", P + "App/Models/Rental#amount()."
GHOST = P + "App/Services/Ghost#haunt()."       # defined in a file the graph does not have
VENDOR = P + "Illuminate/Support/Collection#map()."   # never defined in the index


def fixture(root: str = "") -> bytes:
    escrow = document(root + "app/Services/Escrow.php", [
        occurrence(ESCROW, [5, 6, 12], 1),
        occurrence(CHARGE, [9, 20, 26], 1, enclosing=[9, 4, 13, 5]),
        occurrence(BOOK, [11, 24, 28]),                      # $this->payouts->book()  -> new edge
        occurrence(AMOUNT, [12, 24, 30], unpacked=True),     # $rental->amount(), already in the graph
        occurrence(VENDOR, [12, 40, 43]),                    # vendor method: does not map
        occurrence(GHOST, [12, 50, 55]),                     # defined only in an unmapped file
        occurrence(OTHER, [15, 20, 25], 1),                  # no enclosing range: falls back to the line
        occurrence(BOOK, [16, 8, 12]),                       # inside other() -> new edge
        occurrence(CHARGE, [17, 8, 14]),                     # other() -> charge(): the graph has it
        occurrence("local 3", [17, 2, 6]),
    ], [ESCROW, CHARGE, OTHER])
    payouts = document(root + "app/Contracts/Payouts.php", [occurrence(BOOK, [6, 20, 24], 1)], [BOOK])
    rental = document(root + "app/Models/Rental.php", [occurrence(AMOUNT, [8, 20, 26], 1)], [AMOUNT])
    ghost = document(root + "app/Services/Ghost.php", [occurrence(GHOST, [3, 20, 25], 1)], [GHOST])
    return index([escrow, payouts, rental, ghost])


def n(nid, label, typ, f, line, repo="api"):
    return {"id": nid, "label": label, "type": typ, "source_file": f, "source_location": f"L{line}", "repo": repo}


def sample_graph() -> dict:
    return {"directed": False, "nodes": [
        n("e", "Escrow", "service", "app/Services/Escrow.php", 6),
        n("e.charge", ".charge()", "method", "app/Services/Escrow.php", 10),
        n("e.other", ".other()", "method", "app/Services/Escrow.php", 16),
        n("p.book", ".book()", "method", "app/Contracts/Payouts.php", 7),
        n("r.amount", ".amount()", "method", "app/Models/Rental.php", 9),
        n("w.book", ".book()", "method", "app/Contracts/Payouts.php", 7, repo="web"),   # other project
    ], "links": [{"relation": "calls", "confidence": "EXTRACTED", "source": "e.other", "target": "e.charge"},
                  {"relation": "calls", "confidence": "INFERRED", "source": "r.amount", "target": "e.charge"}]}


class Decode(unittest.TestCase):
    def test_documents_occurrences_and_symbols(self):
        docs = link_scip.decode(fixture())
        self.assertEqual([d["path"] for d in docs], ["app/Services/Escrow.php", "app/Contracts/Payouts.php",
                                                     "app/Models/Rental.php", "app/Services/Ghost.php"])
        occ = docs[0]["occurrences"]
        self.assertEqual(occ[1], {"range": [9, 20, 26], "symbol": CHARGE, "roles": 1, "enclosing": [9, 4, 13, 5]})
        self.assertEqual(occ[3]["range"], [12, 24, 30])      # unpacked repeated int32
        self.assertEqual(docs[0]["symbols"], [ESCROW, CHARGE, OTHER])

    def test_large_varints_and_truncation(self):
        self.assertEqual(link_scip._varint(varint(300_000), 0), (300_000, 3))
        with self.assertRaises(ValueError):
            link_scip.decode(fixture()[:-3])

    def test_method_names(self):
        self.assertEqual(link_scip.method_name(CHARGE), "charge")
        self.assertEqual(link_scip.method_name(P + "App/X#run(+1)."), "run")
        self.assertEqual(link_scip.method_name("scip-typescript npm web 1.0 src/`api.ts`/fetchAll()."), "fetchAll")
        self.assertIsNone(link_scip.method_name(ESCROW))
        self.assertIsNone(link_scip.method_name(P + "App/X#$limit."))
        self.assertIsNone(link_scip.method_name("local 3"))


class Link(unittest.TestCase):
    def test_calls_mapped_deduped_and_unknown_skipped(self):
        g = sample_graph()
        added = link_scip.link(g, "api", link_scip.decode(fixture()))
        new = [e for e in g["links"] if e.get("_origin") == "kb-scip"]
        self.assertEqual({(e["source"], e["target"]) for e in new}, {("e.charge", "p.book"), ("e.other", "p.book")})
        self.assertEqual(added, 2)
        for e in new:
            self.assertEqual((e["relation"], e["confidence"]), ("calls", "EXTRACTED"))
        self.assertEqual(next(e for e in new if e["source"] == "e.charge")["source_location"], "L12")
        # charge() -> amount() and other() -> charge() are skipped: the graph has an edge between them
        # already (the first one in the other direction), and the graph is undirected

    def test_second_run_adds_nothing(self):
        g = sample_graph()
        docs = link_scip.decode(fixture())
        link_scip.link(g, "api", docs)
        before = len(g["links"])
        self.assertEqual(link_scip.link(g, "api", docs), 0)
        self.assertEqual(len(g["links"]), before)

    def test_other_project_and_rooted_paths(self):
        g = sample_graph()
        self.assertEqual(link_scip.link(g, "nope", link_scip.decode(fixture())), 0)
        # an index rooted one folder higher (`api/app/...`) still maps
        g = sample_graph()
        docs = link_scip.decode(fixture("api/"))
        self.assertTrue(docs[0]["path"].startswith("api/"))
        self.assertEqual(link_scip.link(g, "api", docs), 2)


class Runner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.repos = os.path.join(self.data, "repos")
        os.makedirs(os.path.join(self.repos, "api", "app"))
        self.root = os.path.join(self.data, "src")
        os.makedirs(os.path.join(self.root, "vendor"))
        with open(os.path.join(self.root, "composer.json"), "w") as f:
            f.write("{}")
        self.graph = os.path.join(self.data, "graph.json")
        with open(self.graph, "w") as f:
            json.dump(sample_graph(), f)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, addons: str, **env) -> subprocess.CompletedProcess:
        e = {k: v for k, v in os.environ.items() if k != "KB_SCIP_PHP"}
        e.update(KB_ADDONS=addons, PATH="/usr/bin:/bin", **env)   # no scip-php on this PATH
        return subprocess.run([sys.executable, os.path.join(BIN, "link_scip.py"), self.graph, self.repos,
                               f"api={self.root}"], env=e, capture_output=True, text=True, check=True)

    def edges(self) -> list:
        with open(self.graph) as f:
            return [e for e in json.load(f)["links"] if e.get("_origin") == "kb-scip"]

    def test_off_changes_nothing(self):
        with open(os.path.join(self.root, "index.scip"), "wb") as f:
            f.write(fixture())
        with open(self.graph, "rb") as f:
            before = f.read()
        p = self.run_main("metrics")
        self.assertEqual(p.stdout + p.stderr, "")
        with open(self.graph, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_no_indexer_no_index_skips_with_one_line(self):
        with open(self.graph, "rb") as f:
            before = f.read()
        p = self.run_main("scip")
        self.assertEqual(len(p.stdout.strip().splitlines()), 1)
        self.assertIn("skipped", p.stdout)
        with open(self.graph, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_ready_index_in_the_project_root(self):
        with open(os.path.join(self.root, "index.scip"), "wb") as f:
            f.write(fixture())
        p = self.run_main("scip")
        self.assertIn("SCIP calls: 2 edges", p.stdout)
        self.assertEqual(len(self.edges()), 2)

    def test_configured_indexer_runs_in_the_staged_copy(self):
        fx = os.path.join(self.data, "fixture.scip")
        with open(fx, "wb") as f:
            f.write(fixture())
        tool = os.path.join(self.data, "fake-scip-php")
        with open(tool, "w") as f:   # checks that vendor/ is linked in, then writes the index
            f.write(f"#!/bin/sh\ntest -d vendor && test -f composer.json || exit 3\ncp {fx} index.scip\n")
        os.chmod(tool, os.stat(tool).st_mode | stat.S_IEXEC)
        p = self.run_main("scip", KB_SCIP_PHP=tool)
        self.assertIn("(indexed)", p.stdout)
        self.assertEqual(len(self.edges()), 2)
        staged = os.path.join(self.repos, "api")
        self.assertEqual(sorted(os.listdir(staged)), ["app"])       # links and index.scip removed
        self.assertTrue(os.path.isfile(os.path.join(self.data, "scip", "api.scip")))

    def test_indexer_gets_the_autoload_paths_the_staging_left_out(self):
        # scip-php stops on a missing autoload path; the code-only staging leaves out database/, tests/.
        with open(os.path.join(self.root, "composer.json"), "w") as f:
            json.dump({"autoload": {"psr-4": {"App\\": "app/", "Database\\Factories\\": "database/factories/"},
                                    "files": ["helpers.php"]},
                       "autoload-dev": {"psr-4": {"Tests\\": "tests/"}}}, f)
        fx = os.path.join(self.data, "fixture.scip")
        with open(fx, "wb") as f:
            f.write(fixture())
        tool = os.path.join(self.data, "fake-scip-php")
        with open(tool, "w") as f:   # like scip-php: every autoload path must exist
            f.write(f"#!/bin/sh\nfor p in app database/factories tests helpers.php; do test -e $p || exit 4; done\n"
                    f"cp {fx} index.scip\n")
        os.chmod(tool, os.stat(tool).st_mode | stat.S_IEXEC)
        p = self.run_main("scip", KB_SCIP_PHP=tool)
        self.assertIn("(indexed)", p.stdout)
        self.assertEqual(sorted(os.listdir(os.path.join(self.repos, "api"))), ["app"])   # placeholders removed

    def test_failing_indexer_falls_back_to_the_store(self):
        os.makedirs(os.path.join(self.data, "scip"))
        with open(os.path.join(self.data, "scip", "api.scip"), "wb") as f:
            f.write(fixture())
        p = self.run_main("scip", KB_SCIP_PHP="/bin/false")
        self.assertIn("(ready)", p.stdout)
        self.assertIn("indexer failed", p.stderr)
        self.assertEqual(len(self.edges()), 2)

    def test_addon_is_known(self):
        with mock.patch.dict(os.environ, {"KB_ADDONS": "scip"}):
            import addons
            self.assertTrue(addons.on("scip"))


if __name__ == "__main__":
    unittest.main()
