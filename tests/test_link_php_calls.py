#!/usr/bin/env python3
"""bin/link_php_calls.py: calls through typed parameters and properties become `calls` edges."""
from __future__ import annotations

import os
import tempfile
import unittest

from _kbtest import BIN  # noqa: F401

import link_php_calls

SERVICE = """<?php
namespace App\\Services;

use App\\Models\\Rental;

class Escrow
{
    public function __construct(private readonly Payouts $payouts, private int $limit = 3) {}

    public function charge(?Rental $rental, string $note): int
    {
        $this->payouts->book($rental);
        return $rental->amount() + $this->total();
    }

    public function other(Rental $r): void
    {
        $unknown->amount();
    }
}
"""


def n(nid, label, typ, f, line, repo="api"):
    return {"id": nid, "label": label, "type": typ, "source_file": f, "source_location": f"L{line}", "repo": repo}


class TypedCalls(unittest.TestCase):
    def test_parameters_and_promoted_properties(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "api", "app", "Services", "Escrow.php")
            os.makedirs(os.path.dirname(p))
            with open(p, "w") as f:
                f.write(SERVICE)
            g = {"nodes": [n("e", "Escrow", "service", "app/Services/Escrow.php", 6),
                           n("e.ctor", ".__construct()", "method", "app/Services/Escrow.php", 8),
                           n("e.charge", ".charge()", "method", "app/Services/Escrow.php", 10),
                           n("e.total", ".total()", "method", "app/Services/Escrow.php", 30),
                           n("e.other", ".other()", "method", "app/Services/Escrow.php", 16),
                           n("r", "Rental", "model", "app/Models/Rental.php", 5),
                           n("r.amount", ".amount()", "method", "app/Models/Rental.php", 9),
                           n("p", "Payouts", "interface", "app/Contracts/Payouts.php", 5),
                           n("p.book", "book()", "symbol", "app/Contracts/Payouts.php", 7)],
                 "links": []}
            added = link_php_calls.link(g, d)
        edges = {(e["source"], e["target"]) for e in g["links"]}
        self.assertEqual(edges, {("e.charge", "p.book"), ("e.charge", "r.amount")})
        self.assertEqual(added, 2)
        self.assertTrue(all(e["confidence"] == "INFERRED" for e in g["links"]))


if __name__ == "__main__":
    unittest.main()
