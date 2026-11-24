#!/usr/bin/env python3
"""Third audit round of the build (2026-11-24): one test class per finding.

Project names that reach `rm -rf`, a failed copy that left a half-staged project, a forced refresh
that did nothing, a curated hub that is also a real method, `kb label` damaging the graph, an unknown
`serves` project, extraction without fallback, budgets or retries, doc mentions across projects, a
first refresh that never built, stale dashboard files, packaging, and unsafe links in the staged code.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _kbtest import BIN, load_script

import graph as graph_io  # noqa: E402
import parsers  # noqa: E402
from lang import markdown, php_laravel  # noqa: E402
from parsers import pdf_ocr, pdf_text  # noqa: E402

render_viz = load_script("render_viz.py")
_loader = importlib.machinery.SourceFileLoader("kb_extract_a3", os.path.join(BIN, "extract"))
extract = importlib.util.module_from_spec(importlib.util.spec_from_loader("kb_extract_a3", _loader))
sys.modules["kb_extract_a3"] = extract
_loader.exec_module(extract)

ROOT = os.path.dirname(BIN)


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def exe(path: str, text: str) -> None:
    write(path, text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


GRAPHIFY = r"""#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1" in
  --version) echo "graphify 0.0.0-fake" ;;
  update) mkdir -p "$2/graphify-out"
          echo '{"directed": true, "multigraph": false, "graph": {}, "nodes": [{"id": "new::n", "label": "n"}], "links": []}' > "$2/graphify-out/graph.json" ;;
  merge-graphs) first="$2"; while [ "$#" -gt 0 ] && [ "$1" != --out ]; do shift; done; cp "$first" "$2" ;;
esac
exit 0
"""


class KbCase(unittest.TestCase):
    """bin/kb in a temp KB_HOME with a fake graphify and qmd."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = d = self._tmp.name
        self.home, self.fake = os.path.join(d, "kb"), os.path.join(d, "fakebin")
        self.src = os.path.join(d, "app", "src")
        write(os.path.join(self.src, "a.py"), "x = 1\n")
        os.makedirs(self.home)
        exe(os.path.join(self.fake, "graphify"), GRAPHIFY)
        exe(os.path.join(self.fake, "qmd"), "#!/bin/sh\nexit 0\n")
        self.log = os.path.join(d, "calls.log")
        open(self.log, "w").close()
        self.env = {**os.environ, "KB_HOME": self.home, "FAKE_LOG": self.log, "KB_ADDONS": "none",
                    "PATH": self.fake + os.pathsep + os.environ["PATH"]}
        self.env.pop("KB_ROUTES_DIR", None)
        self.config(f'[[project]]\nname = "one"\ncode = ["{self.src}"]\n')

    def config(self, text: str) -> None:
        write(os.path.join(self.home, "kb.projects.toml"), text)

    def kb(self, cmd: str = "graph", **extra) -> subprocess.CompletedProcess:
        open(self.log, "w").close()
        return subprocess.run(["bash", os.path.join(BIN, "kb"), cmd], env={**self.env, **extra},
                              capture_output=True, text=True, timeout=120)

    def calls(self) -> list[str]:
        with open(self.log) as f:
            return [c.strip() for c in f if not c.startswith("--version")]

    def ok(self, p: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(p.returncode, 0, p.stdout[-800:] + p.stderr[-800:])
        return p


# --- 1. Project names ------------------------------------------------------------------------------
class ProjectNames(KbCase):
    def test_a_name_that_is_a_path_is_refused_and_deletes_nothing(self):
        keep = os.path.join(self.home, "repos", "other", "keep.txt")
        write(keep, "x\n")
        for name in (".", "..", "../x", "a/b", "-rf", ".hidden", "a b", ""):
            with self.subTest(name=name):
                self.config(f'[[project]]\nname = "{name}"\ncode = ["{self.src}"]\n')
                p = self.kb()
                self.assertNotEqual(p.returncode, 0)
                self.assertTrue(os.path.isfile(keep))
                self.assertTrue(os.path.isfile(os.path.join(self.home, "kb.projects.toml")))

    def test_letters_digits_dash_and_underscore_pass(self):
        self.config(f'[[project]]\nname = "sw-api_2"\ncode = ["{self.src}"]\n')
        self.ok(self.kb())

    def test_the_name_of_the_kb_own_docs_is_refused_when_they_exist(self):
        write(os.path.join(self.home, "docs", "x.md"), "# x\n")
        self.config(f'[[project]]\nname = "kb"\ncode = ["{self.src}"]\n')
        p = self.kb()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("KB_SELF_DOCS_NAME", p.stderr)
        self.ok(self.kb(KB_SELF_DOCS_NAME="kb-self"))


# --- 2. A failed copy keeps the last good stage ----------------------------------------------------
class FailedCopyKeepsTheStage(KbCase):
    def test_a_failed_rsync_keeps_the_previous_stage_and_the_next_run_retries(self):
        if os.geteuid() == 0:
            self.skipTest("root reads every file")
        self.ok(self.kb())
        staged = os.path.join(self.home, "repos", "one", "src", "a.py")
        write(os.path.join(self.src, "a.py"), "x = 2  # changed\n")
        locked = os.path.join(self.src, "b.py")
        write(locked, "y = 1\n")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o644)
        p = self.kb()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("copying", p.stderr)
        self.assertEqual(read(staged), "x = 1\n")             # the old copy, not a partial new one
        self.assertEqual([n for n in os.listdir(os.path.join(self.home, "repos")) if n != "one"], [])
        os.chmod(locked, 0o644)
        p = self.ok(self.kb())
        self.assertNotIn("unchanged — graph kept", p.stdout)
        self.assertEqual(read(staged), "x = 2  # changed\n")
        self.assertTrue(any(c.startswith("update") for c in self.calls()))

    def test_the_graphify_cache_survives_the_swap(self):
        self.ok(self.kb())
        cache = os.path.join(self.home, "repos", "one", "graphify-out", "cache.marker")
        write(cache, "x\n")
        write(os.path.join(self.src, "a.py"), "x = 3\n")
        self.ok(self.kb())
        self.assertTrue(os.path.isfile(cache))


# --- 12. Unsafe links and special files in the staged code -----------------------------------------
class StagingLinks(KbCase):
    def test_links_out_of_the_code_folder_and_devices_are_not_staged(self):
        secret = os.path.join(self.d, "secret.php")
        write(secret, "<?php // NOTE: secret\n")
        os.symlink("/dev/zero", os.path.join(self.src, "block.php"))
        os.symlink(secret, os.path.join(self.src, "leak.php"))
        os.symlink("a.py", os.path.join(self.src, "inside.py"))
        os.mkfifo(os.path.join(self.src, "pipe.php"))
        self.ok(self.kb())
        staged = os.path.join(self.home, "repos", "one", "src")
        names = set(os.listdir(staged))
        self.assertNotIn("block.php", names)
        self.assertNotIn("leak.php", names)
        self.assertNotIn("pipe.php", names)
        self.assertIn("inside.py", names)


class SafeSourceReads(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name
        self.repos = os.path.join(self.d, "repos")
        self.root = os.path.join(self.repos, "api")
        write(os.path.join(self.root, "app", "A.php"), "<?php class A {}\n")
        write(os.path.join(self.d, "outside.php"), "<?php // NOTE: outside the project\nclass Out {}\n")

    def test_a_device_link_is_not_read(self):
        link = os.path.join(self.root, "app", "block.php")
        os.symlink("/dev/zero", link)
        self.assertIsNone(php_laravel.read(link))
        self.assertIsNone(graph_io.read_source(link))

    def test_a_link_out_of_the_project_is_not_read(self):
        link = os.path.join(self.root, "app", "leak.php")
        os.symlink(os.path.join(self.d, "outside.php"), link)
        self.assertIsNone(graph_io.read_source(link, self.root))
        self.assertIsNone(php_laravel.read(link, self.root))
        self.assertIsNotNone(graph_io.read_source(os.path.join(self.root, "app", "A.php"), self.root))

    def test_a_read_is_bounded(self):
        big = os.path.join(self.root, "app", "Big.php")
        write(big, "x" * 5000)
        self.assertEqual(len(graph_io.read_source(big, self.root, limit=100)), 100)

    def test_rationale_skips_a_link_out_of_the_project(self):
        os.symlink(os.path.join(self.d, "outside.php"), os.path.join(self.root, "app", "leak.php"))
        gp = os.path.join(self.d, "graph.json")
        with open(gp, "w") as fh:
            json.dump({"nodes": [{"id": "api::leak", "label": "leak.php", "repo": "api",
                                  "source_file": "app/leak.php"}], "links": []}, fh)
        p = subprocess.run([sys.executable, os.path.join(BIN, "link_rationale.py"), gp, self.repos],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        labels = [n["label"] for n in json.load(open(gp))["nodes"]]
        self.assertFalse(any("outside the project" in x for x in labels), labels)


# --- 3 and 9. kb-refresh ---------------------------------------------------------------------------
class KbRefresh(unittest.TestCase):
    """bin/kb-refresh with a fake `kb` next to it that logs how it was called."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = d = self._tmp.name
        self.bin = os.path.join(d, "bin")
        os.makedirs(self.bin)
        for f in ("kb-refresh", "paths.py"):
            shutil.copy2(os.path.join(BIN, f), self.bin)
        self.log = os.path.join(d, "kb.log")
        exe(os.path.join(self.bin, "kb"), f'#!/bin/sh\necho "$* force=${{KB_FORCE:-0}}" >> "{self.log}"\n')
        self.home = os.path.join(d, "kb")
        self.code = os.path.join(d, "code")        # not a git checkout
        os.makedirs(self.code)
        self.routes = os.path.join(d, "routes")
        os.makedirs(self.routes)
        write(os.path.join(self.home, "kb.projects.toml"),
              f'[[project]]\nname = "one"\ncode = ["{self.code}"]\n')
        self.env = {**os.environ, "KB_HOME": self.home, "KB_ROUTES_DIR": self.routes}

    def refresh(self, *args: str) -> list[str]:
        p = subprocess.run(["bash", os.path.join(self.bin, "kb-refresh"), *args], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return read(self.log).splitlines() if os.path.exists(self.log) else []

    def test_force_rebuilds_everything(self):
        self.assertEqual(self.refresh("--force"), ["update force=1"])

    def test_the_first_run_builds_without_a_git_checkout(self):
        self.assertEqual(self.refresh(), ["update force=0"])
        self.assertEqual(len(self.refresh()), 1)                    # nothing changed: no build

    def test_a_config_or_route_table_change_builds(self):
        self.refresh()
        write(os.path.join(self.home, "kb.projects.toml"),
              f'[[project]]\nname = "one"\ncode = ["{self.code}"]\ndocs = "{self.code}"\n')
        self.assertEqual(len(self.refresh()), 2)
        write(os.path.join(self.routes, "one.json"), "[]\n")
        self.assertEqual(len(self.refresh()), 3)
        self.assertEqual(len(self.refresh()), 3)


# --- 4. A curated hub label that is also a real method ---------------------------------------------
class DeclutterDefinedHub(unittest.TestCase):
    def run_declutter(self, g: dict, hubs: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            gp, hp = os.path.join(d, "graph.json"), os.path.join(d, "hubs.txt")
            with open(gp, "w") as fh:
                json.dump(g, fh)
            write(hp, hubs)
            p = subprocess.run([sys.executable, os.path.join(BIN, "declutter.py"), gp, hp],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.load(open(gp))

    def test_a_defined_hub_keeps_its_node_and_real_edges_but_loses_the_guessed_calls(self):
        log = {"id": "api::s.php::log", "label": ".log()", "repo": "api", "source_file": "app/S.php",
               "source_location": "L9"}
        cls = {"id": "api::s.php::S", "label": "S", "repo": "api", "source_file": "app/S.php",
               "source_location": "L3"}
        callers = [{"id": f"api::c{i}", "label": f"c{i}()", "repo": "api"} for i in range(3)]
        links = [{"source": "api::s.php::S", "target": log["id"], "relation": "method",
                  "confidence": "EXTRACTED"},
                 {"source": "api::s.php::S", "target": log["id"], "relation": "calls",
                  "confidence": "EXTRACTED"}]
        links += [{"source": c["id"], "target": log["id"], "relation": "calls", "confidence": "INFERRED"}
                  for c in callers]
        out = self.run_declutter({"nodes": [log, cls, *callers], "links": links}, ".log()\n")
        self.assertIn(log["id"], {n["id"] for n in out["nodes"]})
        kept = [(e["relation"], e["confidence"]) for e in out["links"] if e["target"] == log["id"]]
        self.assertEqual(sorted(kept), [("calls", "EXTRACTED"), ("method", "EXTRACTED")])

    def test_a_defined_class_hub_keeps_its_extracted_edges(self):
        ctl = {"id": "api::Controller", "label": "Controller", "repo": "api",
               "source_file": "app/Http/Controllers/Controller.php", "source_location": "L5"}
        sub = {"id": "api::Orders", "label": "Orders", "repo": "api"}
        out = self.run_declutter({"nodes": [ctl, sub], "links": [
            {"source": "api::Orders", "target": "api::Controller", "relation": "inherits",
             "confidence": "EXTRACTED"}]}, "Controller\n")
        self.assertEqual(len(out["links"]), 1)

    @unittest.skipUnless(os.path.isfile("/var/www/kb/repos/api/graphify-out/graph.json"),
                         "the real api graph of this machine")
    def test_real_api_graph_drops_the_guessed_log_calls(self):
        with open("/var/www/kb/repos/api/graphify-out/graph.json") as fh:
            g = json.load(fh)
        hubs = read(os.path.join(ROOT, "kb.hubs.txt"))
        out = self.run_declutter(g, hubs)
        hub = [n["id"] for n in g["nodes"] if n["label"] == ".log()"
               and n.get("source_file") and n.get("source_location")]
        inferred = [e for e in out["links"] if e["target"] in hub and e.get("confidence") == "INFERRED"]
        self.assertEqual(inferred, [])
        self.assertGreater(len(g["links"]) - len(out["links"]), 600)


# --- 5. kb label ------------------------------------------------------------------------------------
class KbLabel(KbCase):
    def test_label_runs_the_build_path_from_the_merge(self):
        self.ok(self.kb())
        p = self.ok(self.kb("label"))
        self.assertIn("Merging into one cross-repo graph", p.stdout)
        self.assertIn("Enriching", p.stdout)
        self.assertNotIn("Inputs unchanged", p.stdout)
        self.assertFalse(any(c.startswith("update") for c in self.calls()))   # no project rebuild
        self.assertIn("Inputs unchanged", self.ok(self.kb()).stdout)


# --- 6. An unknown project in `serves` --------------------------------------------------------------
class ServesUnknownProject(KbCase):
    def test_serves_must_name_a_project_with_routes(self):
        self.config(f'[[project]]\nname = "web"\ncode = ["{self.src}"]\ncalls = ["{self.src}"]\n'
                    'serves = "apii"\n')
        p = self.kb()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("apii", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.config(f'[[project]]\nname = "api"\ndocs = "{self.d}"\n\n'
                    f'[[project]]\nname = "web"\ncode = ["{self.src}"]\ncalls = ["{self.src}"]\n'
                    'serves = "api"\n')
        p = self.kb()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("routes", p.stderr)


    def test_link_http_skips_a_missing_route_table(self):
        gp = os.path.join(self.d, "graph.json")
        with open(gp, "w") as fh:
            json.dump({"nodes": [], "links": []}, fh)
        p = subprocess.run([sys.executable, os.path.join(BIN, "link_http.py"), gp,
                            os.path.join(self.d, "routes.apii.json"), "apii", self.src],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("no route table", p.stderr)


# --- 7 and 13. Extraction --------------------------------------------------------------------------
def fake_parser(name: str, result=None, error: Exception | None = None):
    class P:
        __name__ = f"parsers.{name}"
        REQUIRES: tuple = ()
        CONFIDENCE = "EXTRACTED"
        calls: list = []

        @classmethod
        def extract(cls, path):
            cls.calls.append(str(path))
            if error is not None:
                raise error
            return result
    P.__name__ = f"parsers.{name}"           # a class body cannot set the class name
    return P


class ExtractFallback(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        self.root, self.stage = self.d / "docs", self.d / "stage"
        self.root.mkdir()
        self.stage.mkdir()
        self.pdf = self.root / "scan.pdf"
        self.pdf.write_bytes(b"%PDF scan")

    def run_extract(self, cands: list) -> str:
        err = io.StringIO()
        with mock.patch.object(extract.parsers, "runnable", return_value=cands), \
                mock.patch("sys.stdin", io.StringIO(f"{self.pdf}\0")), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", err):
            extract.main(["--into", str(self.stage), "--root", str(self.root), "--prune", "--from0"])
        return err.getvalue()

    def target(self) -> Path:
        return self.stage / "scan.pdf.md"

    def test_a_failed_parser_falls_back_to_the_next_one(self):
        text = fake_parser("pdf_text", error=parsers.ExtractError("needs OCR"))
        ocr = fake_parser("pdf_ocr", result="scanned words")
        self.run_extract([text, ocr])
        self.assertIn("scanned words", self.target().read_text())
        self.assertIn("extracted_by: \"pdf_ocr\"", self.target().read_text())
        self.run_extract([text, ocr])                    # unchanged: no second extraction
        self.assertEqual(len(ocr.calls), 1)

    def test_a_new_parser_retries_a_cached_failure(self):
        text = fake_parser("pdf_text", error=parsers.ExtractError("needs OCR"))
        self.run_extract([text])
        self.run_extract([text])
        self.assertEqual(len(text.calls), 1)             # the failure is cached
        ocr = fake_parser("pdf_ocr", result="scanned words")
        self.run_extract([text, ocr])                    # tesseract was installed
        self.assertTrue(self.target().is_file())

    def test_a_transient_failure_is_not_cached_and_keeps_the_old_extract(self):
        good = fake_parser("pdf_text", result="first text " * 10)
        self.run_extract([good])
        self.assertTrue(self.target().is_file())
        os.utime(self.pdf, (time.time() + 5, time.time() + 5))
        slow = fake_parser("pdf_text", error=parsers.TransientError("timed out"))
        self.run_extract([slow])
        self.assertIn("first text", self.target().read_text())
        self.run_extract([slow])
        self.assertEqual(len(slow.calls), 2)             # tried again on the next run

    def test_an_oversized_file_is_skipped(self):
        p = fake_parser("pdf_text", result="text")
        with mock.patch.dict(os.environ, {"KB_EXTRACT_MAX_MB": "0.000001"}):
            err = self.run_extract([p])
        self.assertEqual(p.calls, [])
        self.assertIn("KB_EXTRACT_MAX_MB", err)


class ParserErrors(unittest.TestCase):
    def test_runnable_lists_every_parser_with_its_tools(self):
        with mock.patch("shutil.which", return_value="/usr/bin/x"):
            names = [m.__name__.rsplit(".", 1)[-1] for m in parsers.runnable(Path("x.pdf"))]
        self.assertEqual(names, ["pdf_text", "pdf_ocr"])

    def test_a_timeout_is_transient(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("pdftotext", 1)):
            with self.assertRaises(parsers.TransientError):
                pdf_text.extract(Path("a.pdf"))


class OcrBudget(unittest.TestCase):
    """OCR of a PDF renders one page at a time, at a capped size, and stops at KB_OCR_MAX_PAGES."""

    def test_pages_are_rendered_one_at_a_time_and_capped(self):
        rendered: list[list[str]] = []
        live: list[int] = []

        def fake_run(cmd, **kw):
            if cmd[0] == "pdftoppm":
                rendered.append(cmd)
                prefix = cmd[-1]
                first = cmd[cmd.index("-f") + 1]
                folder = os.path.dirname(prefix)
                live.append(len(os.listdir(folder)))
                Path(f"{prefix}-{first}.png").write_bytes(b"png")
                return mock.Mock(returncode=0, stdout=b"", stderr=b"")
            return mock.Mock(returncode=0, stdout="words", stderr="")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(pdf_ocr, "shutil_which", return_value="/usr/bin/pdftoppm"), \
                mock.patch("subprocess.run", side_effect=fake_run), \
                mock.patch.dict(os.environ, {"KB_OCR_MAX_PAGES": "3"}):
            pdf = Path(d) / "many.pdf"
            pdf.write_bytes(b"%PDF")
            text = pdf_ocr.extract(pdf)
        self.assertEqual(len(rendered), 4)               # 3 pages read, the 4th only seen
        self.assertTrue(all("-scale-to" in c for c in rendered))
        self.assertEqual(max(live), 0)                   # the previous page is gone before the next
        self.assertIn("first 3 pages", text)

    def test_a_timeout_is_transient(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("tesseract", 1)):
            with self.assertRaises(parsers.TransientError):
                pdf_ocr.extract(Path("a.png"))


# --- 8. Doc mentions resolve in the doc's own project ------------------------------------------------
class DocMentionsPerProject(unittest.TestCase):
    def enrich(self, nodes: list[dict]) -> list[tuple[str, str]]:
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "api", "billing.md"), "The OrderService and the InvoiceMailer.\n")
            g = {"nodes": nodes, "links": []}
            markdown.enrich(g, [("api", os.path.join(d, "api"))])
        return sorted((e["source"], e["target"]) for e in g["links"] if e["relation"] == "documents")

    def test_a_label_in_two_projects_resolves_to_the_doc_project(self):
        got = self.enrich([{"id": "api::OrderService", "label": "OrderService", "repo": "api"},
                           {"id": "sw-api::OrderService", "label": "OrderService", "repo": "sw-api"}])
        self.assertEqual(got, [("api::billing.md", "api::OrderService")])

    def test_a_label_only_in_another_project_is_a_fallback(self):
        got = self.enrich([{"id": "sw-api::InvoiceMailer", "label": "InvoiceMailer", "repo": "sw-api"}])
        self.assertEqual(got, [("api::billing.md", "sw-api::InvoiceMailer")])

    def test_a_label_twice_in_the_doc_project_stays_ambiguous(self):
        got = self.enrich([{"id": "api::a::OrderService", "label": "OrderService", "repo": "api"},
                           {"id": "api::b::OrderService", "label": "OrderService", "repo": "api"},
                           {"id": "sw::OrderService", "label": "OrderService", "repo": "sw"}])
        self.assertEqual(got, [])


# --- 10. Stale dashboard files -----------------------------------------------------------------------
class StaleDashboardFiles(unittest.TestCase):
    def test_files_of_removed_domains_are_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            gp, out = os.path.join(d, "graph.json"), os.path.join(d, "out")
            data = os.path.join(out, render_viz.DATA_DIR)
            write(os.path.join(data, "d_gone.js"), "kbRecv('Gone', {})\n")
            write(os.path.join(data, "notes.txt"), "not ours\n")
            with open(gp, "w") as fh:
                json.dump({"nodes": [{"id": "api::A", "label": "A", "domain": "Billing", "repo": "api"}],
                           "links": []}, fh)
            with mock.patch.object(sys, "argv", ["render_viz.py", gp, out]), \
                    mock.patch.object(render_viz.paths, "stage_web", return_value=d), \
                    mock.patch("sys.stdout", io.StringIO()):
                render_viz.main()
            files = sorted(os.listdir(data))
        self.assertNotIn("d_gone.js", files)
        self.assertIn("notes.txt", files)
        self.assertIn("_cross.js", files)
        self.assertIn("_index.js", files)
        self.assertEqual(len([f for f in files if f.endswith(".js")]), 3)


# --- 11. Packaging ----------------------------------------------------------------------------------
class Packaging(unittest.TestCase):
    def setUp(self):
        self.text = read(os.path.join(ROOT, "pyproject.toml"))

    def section(self, name: str) -> str:
        return self.text.split(f"[{name}]", 1)[1].split("\n[", 1)[0]

    def test_contrib_ships_in_the_wheel_and_the_sdist(self):
        self.assertIn('"contrib"', self.section("tool.hatch.build.targets.wheel"))
        self.assertRegex(self.section("tool.hatch.build.targets.wheel.force-include"), r'(?m)^contrib = "contrib"')
        sdist = self.section("tool.hatch.build.targets.sdist")
        for item in ('"contrib"', '"Dockerfile"', '"docker"', '".dockerignore"'):
            self.assertIn(item, sdist)

    def test_demo_build_works_without_git(self):
        text = read(os.path.join(ROOT, "examples", "demo", "build.sh"))
        self.assertIn("rsync -a", text)
        self.assertRegex(text, r"git -C \"\$KB\" rev-parse")


if __name__ == "__main__":
    unittest.main()
