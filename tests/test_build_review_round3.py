#!/usr/bin/env python3
"""Second-round review of the build (2026-11-24): one test class per finding.

Linkers that crossed projects with the same class names, a self-serving frontend without HTTP edges,
stale staged projects, a pruned project base class, secrets in the staged code, quiet failures of
kb-refresh and of the docs staging, a config without checks, and docs read without limits.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _kbtest import BIN, load_script

import link_php_calls  # noqa: E402
from lang import markdown  # noqa: E402

render_viz = load_script("render_viz.py")
_loader = importlib.machinery.SourceFileLoader("kb_extract_r3", os.path.join(BIN, "extract"))
extract = importlib.util.module_from_spec(importlib.util.spec_from_loader("kb_extract_r3", _loader))
sys.modules["kb_extract_r3"] = extract
_loader.exec_module(extract)


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def exe(path: str, text: str) -> None:
    write(path, text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def run(script: str, *args: str) -> str:
    p = subprocess.run([sys.executable, os.path.join(BIN, script), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


def save_graph(path: str, nodes: list[dict], links: list[dict] | None = None) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"directed": False, "multigraph": False, "graph": {}, "nodes": nodes,
                   "links": links or []}, fh)


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def node(nid: str, label: str, repo: str, sf: str, line: int = 1, **kw) -> dict:
    return {"id": nid, "label": label, "repo": repo, "source_file": sf, "source_location": f"L{line}", **kw}


# --- 1. Eloquent and $table per project -----------------------------------------------------------
MODEL = "<?php\nnamespace App\\Models;\n\nclass {cls} extends Model\n{{\n{body}}}\n"


class EloquentPerProject(unittest.TestCase):
    """Two Laravel projects with the same model names: each model links inside its own project."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name
        self.repos = os.path.join(self.d, "repos")
        self.gp = os.path.join(self.d, "graph.json")
        nodes, links = [], []
        tables = {"shop-api": "orders", "billing-api": "billing_orders"}
        for repo, table in tables.items():
            for cls, body in (("Order", f"    protected $table = '{table}';\n"
                                         "    public function customer() { return $this->belongsTo(Customer::class); }\n"),
                              ("Customer", "")):
                sf = f"app/Models/{cls}.php"
                write(os.path.join(self.repos, repo, sf), MODEL.format(cls=cls, body=body))
                nodes += [node(f"{repo}::{cls}.php", f"{cls}.php", repo, sf),
                          node(f"{repo}::{cls}", cls, repo, sf, 4)]
                links.append({"source": f"{repo}::{cls}.php", "target": f"{repo}::{cls}",
                              "relation": "contains"})
        save_graph(self.gp, nodes, links)

    def test_eloquent_edges_stay_in_their_project(self):
        run("link_data.py", self.gp, self.repos, "--migrations", os.path.join(self.d, "none"))
        got = {(e["source"], e["target"]) for e in load(self.gp)["links"] if e["relation"] == "eloquent"}
        self.assertEqual(got, {("shop-api::Order", "shop-api::Customer"),
                               ("billing-api::Order", "billing-api::Customer")})

    def test_each_model_links_to_the_table_it_declares(self):
        run("enrich.py", self.gp, self.repos)
        got = {(e["source"], e["target"]) for e in load(self.gp)["links"] if e["relation"] == "defines_table"}
        self.assertEqual(got, {("shop-api::Order", "db::orders"),
                               ("billing-api::Order", "db::billing_orders")})


# --- 2. A frontend that calls its own routes ------------------------------------------------------
class SelfServingProject(unittest.TestCase):
    def test_a_project_that_serves_itself_gets_its_http_edges(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "app", "resources", "js")
            write(os.path.join(src, "orders.js"),
                  "export function save() {\n  return apiFetch(`/orders`, { method: 'POST' });\n}\n")
            gp, rp = os.path.join(d, "graph.json"), os.path.join(d, "routes.json")
            save_graph(gp, [
                node("app::ctl", "OrderController", "app", "app/Http/Controllers/OrderController.php", 5),
                node("app::store", ".store()", "app", "app/Http/Controllers/OrderController.php", 9),
                node("app::file", "orders.js", "app", "js/orders.js"),
                node("app::save", "save()", "app", "js/orders.js", 1)])
            write(rp, json.dumps([{"method": "POST", "uri": "orders",
                                   "action": "App\\Http\\Controllers\\OrderController@store"}]))
            run("link_http.py", "--from", "app", gp, rp, "app", src)
            got = {(e["source"], e["target"]) for e in load(gp)["links"] if e["relation"] == "http_request"}
            self.assertEqual(got, {("app::save", "app::store")})


# --- 4. The project's own memberless base class ---------------------------------------------------
class DeclutterKeepsOwnCode(unittest.TestCase):
    def test_a_memberless_base_class_of_the_project_stays(self):
        with tempfile.TemporaryDirectory() as d:
            gp, hp = os.path.join(d, "graph.json"), os.path.join(d, "hubs.txt")
            write(hp, "Controller\nModel\n")
            sf = "app/Http/Controllers/Controller.php"
            save_graph(gp, [node("api::file", "Controller.php", "api", sf),
                            node("api::ctl", "Controller", "api", sf, 5),
                            node("api::orders", "OrderController", "api", "app/Http/Controllers/O.php", 7),
                            {"id": "api::Model", "label": "Model", "repo": "api", "source_file": "",
                             "source_location": ""}],
                       [{"source": "api::file", "target": "api::ctl", "relation": "contains"},
                        {"source": "api::orders", "target": "api::ctl", "relation": "inherits"},
                        {"source": "api::orders", "target": "api::Model", "relation": "uses"}])
            run("declutter.py", gp, hp)
            g = load(gp)
            self.assertEqual({n["id"] for n in g["nodes"]}, {"api::file", "api::ctl", "api::orders"})
            self.assertIn(("api::orders", "api::ctl"), {(e["source"], e["target"]) for e in g["links"]})


# --- 9. Typed calls stay in their project ---------------------------------------------------------
class TypedCallsStayInTheirProject(unittest.TestCase):
    def test_no_fallback_to_a_class_of_another_project(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "web", "app", "Jobs", "Sync.php"),
                  "<?php\nclass Sync\n{\n    public function run(Client $client)\n    {\n"
                  "        $client->push();\n    }\n}\n")
            g = {"nodes": [node("w.sync", "Sync", "web", "app/Jobs/Sync.php", 2, type="class"),
                           node("w.run", ".run()", "web", "app/Jobs/Sync.php", 4, type="method"),
                           node("o.client", "Client", "other", "app/Client.php", 2, type="class"),
                           node("o.push", ".push()", "other", "app/Client.php", 4, type="method")],
                 "links": []}
            self.assertEqual(link_php_calls.link(g, d), 0)
            self.assertEqual(g["links"], [])


# --- 10. Rationale nodes carry the code's source_file --------------------------------------------
class RationaleSourceFile(unittest.TestCase):
    def test_the_rationale_node_has_the_path_inside_its_repo(self):
        with tempfile.TemporaryDirectory() as d:
            repos = os.path.join(d, "repos")
            write(os.path.join(repos, "web", "src", "cart.js"), "// WHY: kept in memory\nexport const a = 1\n")
            gp = os.path.join(d, "graph.json")
            save_graph(gp, [node("web::file", "cart.js", "web", "src/cart.js")])
            run("link_rationale.py", gp, repos)
            r = [n for n in load(gp)["nodes"] if n.get("type") == "rationale"]
            self.assertEqual([(n["repo"], n["source_file"]) for n in r], [("web", "src/cart.js")])


# --- 11. Status panel -----------------------------------------------------------------------------
QMD_STATUS = """QMD Status

Collections
  other (qmd://other/)
    Pattern:  **/*.md
    Files:    99 (updated 1h ago)
  kb-api (qmd://kb-api/)
    Pattern:  **/*.md
    Files:    12 (updated 5m ago)
  kb-web (qmd://kb-web/)
    Pattern:  **/*.md
    Files:    3 (updated 2h ago)
"""


class StatusPanel(unittest.TestCase):
    def test_docs_count_sums_the_collections_of_every_project(self):
        out = mock.Mock(stdout=QMD_STATUS)
        with mock.patch.object(render_viz.subprocess, "run", return_value=out):
            stats = render_viz._qmd_collection_stats("kb")
        self.assertEqual(stats["files"], 15)
        self.assertEqual(stats["collections"], 2)

    def test_quality_is_read_next_to_the_graph(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "bench-latest.json"),
                  json.dumps({"timestamp": "t", "summary": {"hybrid": {"avg_recall": 0.5, "avg_mrr": 0.25}}}))
            self.assertEqual(render_viz._quality(d)["metrics"], {"hybrid recall": 0.5, "hybrid MRR": 0.25})


# --- 15b. A failed extraction is not retried until the source changes ----------------------------
class FailingParser:
    __name__ = "parsers.failing"
    REQUIRES: tuple = ()
    CONFIDENCE = "EXTRACTED"
    calls = 0

    @classmethod
    def extract(cls, path: Path) -> str:
        cls.calls += 1
        raise extract.parsers.ExtractError("broken file")


class ExtractFailureIsRemembered(unittest.TestCase):
    def run_extract(self, root: Path, stage: Path, *files: Path) -> str:
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("".join(f"{f}\0" for f in files))), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", err), \
                mock.patch.object(extract.parsers, "runnable", return_value=[FailingParser]):
            extract.main(["--into", str(stage), "--root", str(root), "--prune", "--from0"])
        return err.getvalue()

    def test_same_source_is_not_tried_again(self):
        with tempfile.TemporaryDirectory() as d:
            root, stage = Path(d, "docs"), Path(d, "stage")
            root.mkdir()
            pdf = root / "bad.pdf"
            pdf.write_bytes(b"%PDF broken")
            FailingParser.calls = 0
            self.assertIn("broken file", self.run_extract(root, stage, pdf))
            self.assertEqual(self.run_extract(root, stage, pdf), "")
            self.assertEqual(FailingParser.calls, 1)
            os.utime(pdf, (time.time() + 5, time.time() + 5))
            self.run_extract(root, stage, pdf)
            self.assertEqual(FailingParser.calls, 2)


# --- 16. Docs are read with limits ---------------------------------------------------------------
class DocsReadSafely(unittest.TestCase):
    def test_non_regular_and_outside_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            docs = os.path.join(d, "docs")
            write(os.path.join(docs, "guide.md"), "# Guide\n")
            write(os.path.join(d, "secret.md"), "OrderService secret\n")
            os.symlink(os.path.join(d, "secret.md"), os.path.join(docs, "leak.md"))
            os.symlink("/dev/zero", os.path.join(docs, "zero.md"))
            g = {"nodes": [], "links": []}
            markdown.enrich(g, [("api", docs)])
            self.assertEqual({n["source_file"] for n in g["nodes"]}, {"guide.md"})

    def test_a_long_document_is_read_up_to_the_cap(self):
        with tempfile.TemporaryDirectory() as d:
            docs = os.path.join(d, "docs")
            write(os.path.join(docs, "big.md"), "x " * (markdown.MAX_DOC_BYTES // 2) + "OrderService\n")
            g = {"nodes": [node("api::svc", "OrderService", "api", "app/OrderService.php")], "links": []}
            markdown.enrich(g, [("api", docs)])
            self.assertEqual([e for e in g["links"] if e["relation"] == "documents"], [])

    def test_one_long_word_is_tokenised_in_linear_time(self):
        text = "a" * 200_000 + " OrderService::run() done()"
        start = time.monotonic()
        words = markdown._words(text)
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertTrue({"OrderService::run", "run()", "OrderService", "run", "done()"} <= words)
        self.assertNotIn("rderService", words)


# --- bin/kb and bin/kb-refresh --------------------------------------------------------------------
SAFE_GRAPHIFY = r"""#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1" in
  --version) echo "graphify 0.0.0-fake" ;;
  update) mkdir -p "$2/graphify-out"
          echo '{"directed": true, "multigraph": false, "graph": {}, "nodes": [{"id": "new::n", "label": "n"}], "links": []}' > "$2/graphify-out/graph.json" ;;
  merge-graphs) first="$2"; while [ "$#" -gt 0 ] && [ "$1" != --out ]; do shift; done; cp "$first" "$2" ;;
esac
exit 0
"""


class KbBuild(unittest.TestCase):
    """bin/kb with a fake graphify and qmd (no real tools)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = d = self._tmp.name
        self.home, self.fake = os.path.join(d, "kb"), os.path.join(d, "fakebin")
        self.src = os.path.join(d, "app", "src")
        write(os.path.join(self.src, "a.py"), "x = 1\n")
        os.makedirs(self.home)
        exe(os.path.join(self.fake, "graphify"), SAFE_GRAPHIFY)
        exe(os.path.join(self.fake, "qmd"), "#!/bin/sh\nexit 0\n")
        self.log = os.path.join(d, "calls.log")
        self.env = {**os.environ, "KB_HOME": self.home, "FAKE_LOG": self.log, "KB_ADDONS": "none",
                    "PATH": self.fake + os.pathsep + os.environ["PATH"]}
        self.env.pop("KB_ROUTES_DIR", None)
        self.config(f'[[project]]\nname = "one"\ncode = ["{self.src}"]\n')

    def config(self, text: str) -> None:
        write(os.path.join(self.home, "kb.projects.toml"), text)

    def kb(self, cmd: str = "graph", **extra) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", os.path.join(BIN, "kb"), cmd], env={**self.env, **extra},
                              capture_output=True, text=True, timeout=120)

    def ok(self, p: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(p.returncode, 0, p.stdout[-800:] + p.stderr[-800:])
        return p

    def argv_logger(self, scripts: str) -> str:
        """A wrapper `python3` that logs the argv of the named bin scripts, then runs the real one."""
        real = subprocess.run(["sh", "-c", "command -v python3"], capture_output=True, text=True).stdout.strip()
        log = os.path.join(self.d, "argv.log")
        exe(os.path.join(self.fake, "python3"),
            f'#!/bin/sh\ncase "$1" in {scripts}) echo "$*" >> "{log}";; esac\nexec {real} "$@"\n')
        return log

    # 3
    def test_a_project_that_left_the_config_leaves_repos(self):
        stale = os.path.join(self.home, "repos", "gone", "src", "old.php")
        write(stale, "<?php class Old {}\n")
        outside = os.path.join(self.d, "outside")
        write(os.path.join(outside, "keep.txt"), "x\n")
        os.symlink(outside, os.path.join(self.home, "repos", "link"))
        self.ok(self.kb())
        self.assertEqual(sorted(os.listdir(os.path.join(self.home, "repos"))), ["one"])
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep.txt")))

    # 5 and 14
    def test_staging_leaves_out_secrets_and_non_code(self):
        app = os.path.join(self.d, "app")
        for rel in ("src/.env", "src/.env.testing", "src/.env.example", "src/bootstrap/cache/config.php",
                    "src/package.json", "src/composer.lock", "src/public/index.php", "src/x.min.js",
                    "src/b.js.map", "src/font.woff2"):
            write(os.path.join(app, rel), "secret\n")
        self.ok(self.kb())
        staged = os.path.join(self.home, "repos", "one", "src")
        found = {os.path.relpath(os.path.join(dp, f), staged)
                 for dp, _, fs in os.walk(staged) for f in fs}
        self.assertEqual(found, {"a.py", ".env.example"})

    # 7
    def test_an_unreadable_docs_folder_warns_and_the_next_project_is_staged(self):
        if os.geteuid() == 0:
            self.skipTest("root reads every folder")
        d1, d2 = os.path.join(self.d, "docs1"), os.path.join(self.d, "docs2")
        write(os.path.join(d1, "a.md"), "# A\n")
        write(os.path.join(d1, "locked", "x.md"), "# X\n")
        write(os.path.join(d2, "b.md"), "# B\n")
        locked = os.path.join(d1, "locked")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        self.config(f'[[project]]\nname = "p1"\ndocs = "{d1}"\n\n[[project]]\nname = "p2"\ndocs = "{d2}"\n')
        p = self.ok(self.kb("kb", KB_QMD_BACKEND="none"))
        self.assertIn("p1", p.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".docs", "p2", "b.md")))

    # 8
    def test_config_checks(self):
        cases = {
            f'[[project]]\nname = "a"\ncode = ["{self.src}"]\n\n[[project]]\nname = "a"\ndocs = "/x"\n':
                "twice",
            f'[[project]]\nname = "db"\ncode = ["{self.src}"]\n': "reserved",
            f'[[project]]\nname = "domain"\ncode = ["{self.src}"]\n': "reserved",
            f'[[project]]\ncode = ["{self.src}"]\n': "no `name`",
        }
        for text, why in cases.items():
            with self.subTest(why=why, text=text):
                self.config(text)
                p = self.kb()
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(why, p.stderr)
        self.config(f'[[project]]\nname = "one"\ncode = ["{self.src}"]\nserve = "api"\n')
        p = self.ok(self.kb())
        self.assertIn("unknown key `serve`", p.stderr)

    # 12
    def test_a_docs_only_kb_builds_its_contract_docs(self):
        docs = os.path.join(self.d, "docs")
        write(os.path.join(docs, "contracts", "billing.md"),
              "---\nfrom: manual\nto: billing\nkind: http\n---\n# Billing\n")
        self.config(f'[[project]]\nname = "manual"\ndocs = "{docs}"\n')
        self.ok(self.kb())
        out = os.path.join(self.home, "graphify-out")
        self.assertTrue(os.path.isfile(os.path.join(out, "connections.json")))
        self.assertTrue(os.path.isfile(os.path.join(out, "problems.json")))

    def test_the_kb_contract_docs_count_in_the_build_signature(self):
        self.ok(self.kb())
        self.assertIn("Inputs unchanged", self.ok(self.kb()).stdout)
        write(os.path.join(self.home, "docs", "contracts", "kb.md"), "---\nfrom: kb\nto: x\n---\n")
        self.assertNotIn("Inputs unchanged", self.ok(self.kb()).stdout)

    # 13
    def test_scip_root_of_a_json_route_table_is_the_code_parent(self):
        log = self.argv_logger("*link_scip.py")
        rt = os.path.join(self.d, "routes", "one.json")
        write(rt, "[]")
        self.config(f'[[project]]\nname = "one"\ncode = ["{self.src}"]\nroutes = "{rt}"\n')
        self.ok(self.kb())
        with open(log) as f:
            self.assertIn(f"one={os.path.join(self.d, 'app')}", f.read().split())

    # 15a
    def test_a_docs_folder_with_only_extracted_files_stages_quietly(self):
        docs = os.path.join(self.d, "docs")
        write(os.path.join(docs, "a.md"), "# A\n")
        write(os.path.join(docs, "sub", "deck.pdf"), "%PDF\n")
        write(os.path.join(self.home, ".docs", "manual", "sub", "deck.pdf.md"), "extracted\n")
        self.config(f'[[project]]\nname = "manual"\ndocs = "{docs}"\n')
        p = self.ok(self.kb("kb", KB_EXTRACT="0", KB_QMD_BACKEND="none"))
        self.assertNotIn("cannot delete non-empty directory", p.stdout + p.stderr)


class KbRefreshBrokenConfig(unittest.TestCase):
    def test_a_broken_config_is_one_log_line_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, "kb")
            write(os.path.join(home, "kb.projects.toml"), '[[project]]\nname = "one\n')
            env = {**os.environ, "KB_HOME": home}
            p = subprocess.run(["bash", os.path.join(BIN, "kb-refresh")], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertNotEqual(p.returncode, 0)
            self.assertNotIn("Traceback", p.stdout + p.stderr)
            logs = os.path.join(home, "logs")
            with open(os.path.join(logs, "refresh.log")) as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("kb.projects.toml", lines[0])
            with open(os.path.join(logs, "update.log")) as f:
                self.assertIn("kb.projects.toml", f.read())
            subprocess.run(["bash", os.path.join(BIN, "kb-refresh")], env=env, capture_output=True, timeout=60)
            with open(os.path.join(logs, "refresh.log")) as f:
                self.assertEqual(len(f.read().splitlines()), 1)   # the same error is not logged each minute


if __name__ == "__main__":
    unittest.main()
