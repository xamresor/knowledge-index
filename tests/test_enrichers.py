"""Tests for the three graph enrichers: enrich.py, link_data.py, link_rationale.py.

These had **no tests at all**, which is why the language-plugin migration stopped here: the files
carry ~700 lines of PHP/Laravel/JS regexes, and moving regexes without a way to prove the edges came
out the same is how a graph silently loses two hundred links. So the pins are laid first, at two
levels:

* the **pure functions** that are about to move into `lang/` plugins (type classification, entity
  stripping, migration-filename parsing) — called directly;
* the **whole scripts**, run as subprocesses against a fixture repo tree and a synthetic graph, so
  the fixture exercises what the regexes are actually for: an Eloquent relation, a `$table`
  declaration, a foreign key in a migration, a query-builder table reference, a rationale comment.

The confidence level of every edge is asserted on purpose. It is the one field that must never drift
during a refactor: a regex-derived edge posing as an AST fact is a lie the whole graph rests on.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _kbtest import BIN, load_script

enrich = load_script("enrich.py")
link_data = load_script("link_data.py")

MIGRATION = """<?php
return new class extends Migration {
    public function up(): void {
        Schema::create('bookings', function (Blueprint $t) {
            $t->foreignId('guest_id')->constrained('guests');
        });
    }
};
"""

BOOKING_MODEL = """<?php
namespace App\\Models;

class Booking extends Model
{
    protected $table = 'bookings';

    public function guest()
    {
        return $this->belongsTo(Guest::class);
    }

    public function payments()
    {
        return $this->hasMany(Payment::class);
    }
}
"""

PRICING_SERVICE = """<?php
namespace App\\Services;

class PricingService
{
    // WHY: prices are cents to keep rounding out of the money path
    public function rates()
    {
        // TODO: cache this
        return DB::table('rates')->get();
    }
}
"""


def node(nid: str, label: str, source_file: str, repo: str = "api") -> dict:
    return {"id": nid, "label": label, "source_file": source_file, "repo": repo}


class FixtureCase(unittest.TestCase):
    """A staged repo tree + a synthetic graph, laid out the way `kb build` produces them."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repos = self.root / "repos"
        self.project = self.root / "proj"          # the Laravel checkout, per kb.projects.toml

        self.write(self.repos / "api/app/Models/Booking.php", BOOKING_MODEL)
        self.write(self.repos / "api/app/Models/Guest.php",
                   "<?php\nnamespace App\\Models;\nclass Guest extends Model {}\n")
        self.write(self.repos / "api/app/Services/PricingService.php", PRICING_SERVICE)
        self.write(self.project / "database/migrations/2026_01_01_000000_create_bookings_table.php",
                   MIGRATION)
        # A migration with no Schema:: call: the owning table can only come from the filename.
        self.write(self.project / "database/migrations/2026_02_02_000000_add_notes_to_guests.php",
                   "<?php\n// $t->foreignId('x')->constrained('bookings');\n"
                   "$t->foreignId('x')->constrained('bookings');\n")
        (self.root / "kb.projects.toml").write_text(f'routes = "{self.project}"\n', encoding="utf-8")

        self.graph_path = self.root / "graph.json"
        self.write_graph({
            "nodes": [
                node("api::app/Models/Booking.php::Booking", "Booking", "app/Models/Booking.php"),
                node("api::app/Models/Guest.php::Guest", "Guest", "app/Models/Guest.php"),
                node("api::app/Models/Payment.php::Payment", "Payment", "app/Models/Payment.php"),
                node("api::app/Services/PricingService.php::PricingService", "PricingService",
                     "app/Services/PricingService.php"),
                node("api::app/Http/Controllers/BookingController.php::BookingController",
                     "BookingController", "app/Http/Controllers/BookingController.php"),
                node("api::app/Models/Booking.php", "Booking.php", "app/Models/Booking.php"),
            ],
            # a `contains` edge is what marks a node as a *definition* (see `defined` in the scripts)
            "links": [{"source": "api::app/Services/PricingService.php::PricingService",
                       "target": "api::app/Services/PricingService.php::PricingService.rates()",
                       "relation": "contains"},
                      {"source": "api::app/Models/Booking.php::Booking",
                       "target": "api::app/Models/Booking.php::Booking.guest()",
                       "relation": "method"}],
        })

    def write(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def write_graph(self, g: dict) -> None:
        self.graph_path.write_text(json.dumps(g), encoding="utf-8")

    def graph(self) -> dict:
        return json.loads(self.graph_path.read_text(encoding="utf-8"))

    def run_step(self, script: str, *args: str) -> str:
        result = subprocess.run([sys.executable, os.path.join(BIN, script),
                                 str(self.graph_path), *(args or (str(self.repos),))],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def edges(self, relation: str) -> list[dict]:
        return [e for e in self.graph()["links"] if e.get("relation") == relation]

    def nodes_of_type(self, node_type: str) -> list[dict]:
        return [n for n in self.graph()["nodes"] if n.get("type") == node_type]


# --- the pure functions that are about to move into lang/ plugins -------------------------------
class PhpClassificationTest(unittest.TestCase):
    def test_node_type_reads_laravel_layers(self):
        cases = [
            ("BookingController", "app/Http/Controllers/BookingController.php", "controller"),
            ("BookingResource", "app/Http/Resources/BookingResource.php", "resource"),
            ("StoreBookingRequest", "app/Http/Requests/StoreBookingRequest.php", "request"),
            ("PricingService", "app/Services/PricingService.php", "service"),
            ("BookingRepository", "app/Repositories/BookingRepository.php", "repository"),
            ("Booking", "app/Models/Booking.php", "model"),
            ("BookingStatus", "app/Enums/BookingStatus.php", "enum"),
            ("Bookable", "app/Contracts/Bookable.php", "class"),
            ("Booking.php", "app/Models/Booking.php", "file"),
            ("Card.vue", "src/components/Card.vue", "template"),
            (".rates()", "app/Services/PricingService.php", "method"),
        ]
        for label, source_file, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(enrich.node_type({"label": label, "source_file": source_file}),
                                 expected)

    def test_entity_strips_layer_suffixes_repeatedly(self):
        self.assertEqual(enrich.entity("BookingController"), "Booking")
        self.assertEqual(enrich.entity("BookingResourceCollection"), "Booking")
        self.assertEqual(enrich.entity("Booking"), "Booking")

    def test_layer_bucket_is_the_fallback_domain(self):
        self.assertEqual(enrich.layer_bucket("app/Services/X.php", "api"), "api:Services")
        self.assertEqual(enrich.layer_bucket("src/stores/x.ts", "web"), "web:stores")
        self.assertEqual(enrich.layer_bucket("weird.txt", "web"), "web:core")

    def test_migration_filename_to_table(self):
        cases = {
            "2026_01_01_000000_create_bookings_table.php": "bookings",
            "2026_01_01_000000_add_notes_to_guests.php": "guests",
            "2026_01_01_000000_alter_payments_table.php": "payments",
            "not_a_migration.php": None,
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(link_data.table_from_filename(filename), expected)


# --- the scripts, end to end --------------------------------------------------------------------
class EnrichTest(FixtureCase):
    def test_types_domains_and_db_tables(self):
        self.run_step("enrich.py")
        by_label = {n["label"]: n for n in self.graph()["nodes"]}
        self.assertEqual(by_label["Booking"]["type"], "model")
        self.assertEqual(by_label["PricingService"]["type"], "service")
        self.assertEqual(by_label["BookingController"]["type"], "controller")
        self.assertTrue(all("domain" in n for n in self.graph()["nodes"]))

    def test_the_table_declared_by_a_model_becomes_a_node(self):
        self.run_step("enrich.py")
        tables = self.nodes_of_type("db_table")
        self.assertEqual([t["label"] for t in tables], ["bookings"])
        self.assertEqual(tables[0]["id"], "db::bookings")

    def test_defines_table_is_inferred_because_the_model_is_matched_by_label(self):
        self.run_step("enrich.py")
        edge, = self.edges("defines_table")
        self.assertEqual(edge["confidence"], "INFERRED")
        self.assertEqual(edge["confidence_score"], 0.85)

    def test_ast_edges_are_backfilled_as_extracted(self):
        self.run_step("enrich.py")
        self.assertTrue(all(e["confidence"] == "EXTRACTED" for e in self.edges("contains")))

    def test_domain_anchors_group_members(self):
        self.run_step("enrich.py")
        anchors = self.nodes_of_type("domain")
        self.assertTrue(anchors)
        self.assertTrue(all(e["confidence"] == "DERIVED" for e in self.edges("in_domain")))


class LinkDataTest(FixtureCase):
    def setUp(self):
        super().setUp()
        self.run_step("enrich.py")          # link_data runs after enrich, and needs its db_table nodes

    def test_eloquent_relations_become_model_to_model_edges(self):
        self.run_step("link_data.py")
        kinds = {(e["kind"], e["target"].split("::")[-1]) for e in self.edges("eloquent")}
        self.assertEqual(kinds, {("belongsTo", "Guest"), ("hasMany", "Payment")})

    def test_an_eloquent_edge_is_inferred_not_extracted(self):
        self.run_step("link_data.py")
        for edge in self.edges("eloquent"):
            self.assertEqual(edge["confidence"], "INFERRED")
            self.assertEqual(edge["confidence_score"], 0.9)

    def test_a_foreign_key_named_by_schema_create_is_inferred(self):
        self.run_step("link_data.py")
        edges = [e for e in self.edges("fk") if e["source"] == "db::bookings"]
        self.assertEqual([e["target"] for e in edges], ["db::guests"])
        self.assertEqual(edges[0]["confidence"], "INFERRED")

    def test_a_foreign_key_whose_owner_came_from_the_filename_is_ambiguous(self):
        """`add_notes_to_guests.php` has no Schema:: call — the owning table is a guess."""
        self.run_step("link_data.py")
        edges = [e for e in self.edges("fk") if e["source"] == "db::guests"]
        self.assertEqual([e["target"] for e in edges], ["db::bookings"])
        self.assertEqual(edges[0]["confidence"], "AMBIGUOUS")
        self.assertEqual(edges[0]["confidence_score"], 0.6)

    def test_a_query_builder_reference_links_code_to_a_table(self):
        self.run_step("link_data.py")
        edge, = self.edges("sql")
        self.assertEqual(edge["source"], "api::app/Services/PricingService.php::PricingService")
        self.assertEqual(edge["target"], "db::rates")
        self.assertEqual(edge["confidence"], "INFERRED")

    def test_a_table_with_no_model_is_created_on_the_fly(self):
        self.run_step("link_data.py")
        self.assertIn("db::rates", {n["id"] for n in self.graph()["nodes"]})

    def test_edges_are_deduplicated(self):
        self.run_step("link_data.py")
        keys = [(e["source"], e["target"], e["relation"]) for e in self.graph()["links"]]
        self.assertEqual(len(keys), len(set(keys)))


class LinkRationaleTest(FixtureCase):
    def setUp(self):
        super().setUp()
        self.run_step("enrich.py")

    def test_a_why_comment_becomes_a_rationale_node(self):
        self.run_step("link_rationale.py")
        nodes = self.nodes_of_type("rationale")
        self.assertEqual(len(nodes), 1)
        self.assertTrue(nodes[0]["label"].startswith("WHY: prices are cents"))
        self.assertEqual(nodes[0]["source_location"], "L6", "the line number is part of the id")

    def test_it_explains_the_class_defined_in_that_file(self):
        self.run_step("link_rationale.py")
        edge, = self.edges("explains")
        self.assertEqual(edge["target"], "api::app/Services/PricingService.php::PricingService")
        self.assertEqual(edge["confidence"], "EXTRACTED")

    def test_plain_todos_are_not_rationale(self):
        self.run_step("link_rationale.py")
        self.assertFalse([n for n in self.nodes_of_type("rationale") if "TODO" in n["label"]])

    def test_markers_are_capped_per_file(self):
        body = "<?php\nclass Guest extends Model {\n" + \
               "".join(f"    // NOTE: reason number {i}\n" for i in range(20)) + "}\n"
        self.write(self.repos / "api/app/Models/Guest.php", body)
        output = self.run_step("link_rationale.py")
        from link_rationale import MAX_PER_FILE  # noqa: PLC0415  (the module under test)
        per_file = [n for n in self.nodes_of_type("rationale")
                    if n["source_file"].endswith("Guest.php")]
        self.assertEqual(len(per_file), MAX_PER_FILE)
        self.assertIn("skipped", output)

    def test_a_comment_in_an_unknown_file_type_is_ignored(self):
        self.write(self.repos / "api/notes.txt", "// WHY: not code\n")
        self.run_step("link_rationale.py")
        self.assertFalse([n for n in self.nodes_of_type("rationale")
                          if "not code" in n["label"]])


if __name__ == "__main__":
    unittest.main()
