#!/usr/bin/env python3
"""Fourth audit round of the build (2026-11-24): one test class per finding.

Two builds at once, a forced refresh that lost the lock race, graph.json rewritten while it is served,
route tables read from a pipe, a file name in a community label, pseudo repos counted as projects,
refresh and build signatures that missed inputs, call sites read through links, and extraction without
an output or memory cap. Every build here is tiny: a fake graphify, one or two source files.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from unittest import mock

from _kbtest import BIN, load_script

import parsers  # noqa: E402
from parsers import office  # noqa: E402

label_namespaces = load_script("label_namespaces.py")
render_viz = load_script("render_viz.py")

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


# cluster-only copies the graph that is served at that moment, so a test can see what readers saw.
GRAPHIFY = r"""#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1" in
  --version) echo "graphify 0.0.0-fake" ;;
  update) mkdir -p "$2/graphify-out"
          echo '{"directed": true, "multigraph": false, "graph": {}, "nodes": [{"id": "new::n", "label": "n"}], "links": []}' > "$2/graphify-out/graph.json" ;;
  merge-graphs) first="$2"; while [ "$#" -gt 0 ] && [ "$1" != --out ]; do shift; done; cp "$first" "$2" ;;
  cluster-only) [ -f "$KB_HOME/graphify-out/graph.json" ] && cp "$KB_HOME/graphify-out/graph.json" "$FAKE_SEEN"
                [ -n "$FAKE_FAIL" ] && exit 1
                out="$2/graphify-out"
                while [ "$#" -gt 0 ]; do [ "$1" = --graph ] && out=$(dirname "$2"); shift; done
                echo report > "$out/GRAPH_REPORT.md" ;;
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
        self.seen = os.path.join(d, "seen.json")
        open(self.log, "w").close()
        self.env = {**os.environ, "KB_HOME": self.home, "FAKE_LOG": self.log, "FAKE_SEEN": self.seen,
                    "KB_ADDONS": "none", "PATH": self.fake + os.pathsep + os.environ["PATH"]}
        self.env.pop("KB_ROUTES_DIR", None)
        self.config(f'[[project]]\nname = "one"\ncode = ["{self.src}"]\n')
        self.out = os.path.join(self.home, "graphify-out")
        self.lock = os.path.join(self.home, "logs", "update.lock")

    def config(self, text: str) -> None:
        write(os.path.join(self.home, "kb.projects.toml"), text)

    def kb(self, cmd: str = "graph", **extra) -> subprocess.CompletedProcess:
        open(self.log, "w").close()
        return subprocess.run(["bash", os.path.join(BIN, "kb"), cmd], env={**self.env, **extra},
                              capture_output=True, text=True, timeout=120)

    def ok(self, p: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(p.returncode, 0, p.stdout[-800:] + p.stderr[-800:])
        return p

    def hold_lock(self) -> subprocess.Popen:
        """Another build: a process that holds the flock on logs/update.lock until it is killed."""
        os.makedirs(os.path.dirname(self.lock), exist_ok=True)
        holder = subprocess.Popen(["flock", self.lock, "sleep", "60"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        for _ in range(100):
            if subprocess.run(["flock", "-n", self.lock, "true"]).returncode != 0:
                return holder
            subprocess.run(["sleep", "0.05"])
        self.fail("the lock holder did not take the lock")


# --- 1. bin/kb takes the build lock ----------------------------------------------------------------
class BuildLock(KbCase):
    def test_a_build_while_another_holds_the_lock_stops_with_busy(self):
        self.hold_lock()
        for cmd in ("graph", "update", "build", "label", "kb"):
            with self.subTest(cmd=cmd):
                p = self.kb(cmd)
                self.assertEqual(p.returncode, 75, p.stderr)
                self.assertIn("busy", p.stderr)
                self.assertEqual(read(self.log), "")              # graphify never ran
        self.assertFalse(os.path.exists(os.path.join(self.out, "graph.json")))

    def test_a_build_started_by_the_lock_holder_runs(self):
        # kb-refresh holds the lock on fd 8 and runs bin/kb: the child shares that lock.
        os.makedirs(os.path.dirname(self.lock), exist_ok=True)
        p = subprocess.run(["bash", "-c", 'exec 8>"$1"; flock -n 8 || exit 9; bash "$2" graph',
                            "_", self.lock, os.path.join(BIN, "kb")],
                           env=self.env, capture_output=True, text=True, timeout=120)
        self.ok(p)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "graph.json")))

    def test_the_version_needs_no_lock(self):
        self.hold_lock()
        self.ok(self.kb("--version"))


# --- 3. graph.json is replaced in one step ---------------------------------------------------------
class AtomicGraph(KbCase):
    def setUp(self):
        super().setUp()
        self.graph = os.path.join(self.out, "graph.json")
        write(self.graph, json.dumps({"nodes": [{"id": "old::n", "label": "n"}], "links": []}))

    def test_readers_see_the_old_graph_until_the_new_one_is_complete(self):
        self.ok(self.kb())
        self.assertEqual(json.loads(read(self.seen))["nodes"][0]["id"], "old::n")  # during clustering
        ids = {n["id"] for n in json.loads(read(self.graph))["nodes"]}
        self.assertIn("one::new::n", ids)
        self.assertEqual(read(os.path.join(self.out, "GRAPH_REPORT.md")), "report\n")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".build")))
        self.assertFalse(os.path.exists(self.graph + ".prev"))

    def test_a_failed_post_processing_leaves_the_served_graph_untouched(self):
        before = os.stat(self.graph)
        p = self.kb(FAKE_FAIL="1")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("kept the previous graph", p.stderr)
        after = os.stat(self.graph)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".build")))


# --- 4. Route tables are regular files of a bounded size -------------------------------------------
class RouteTableFile(KbCase):
    def setUp(self):
        super().setUp()
        self.routes = os.path.join(self.d, "routes")
        os.makedirs(self.routes)
        self.config(f'[[project]]\nname = "one"\ncode = ["{self.src}"]\nroutes = "{self.d}/app"\n')
        self.table = os.path.join(self.home, "routes.one.json")

    def test_a_pipe_is_refused_without_blocking(self):
        os.mkfifo(os.path.join(self.routes, "one.json"))
        p = self.kb(KB_ROUTES_DIR=self.routes)    # a blocked read would hit the 120 s timeout
        self.ok(p)
        self.assertIn("WARNING", p.stderr)
        self.assertEqual(read(self.table), "[]\n")

    def test_an_oversized_table_is_refused(self):
        big = os.path.join(self.routes, "one.json")
        with open(big, "w") as fh:
            fh.truncate(65 * 2**20)              # sparse: no disk, no memory
        p = self.kb(KB_ROUTES_DIR=self.routes)
        self.ok(p)
        self.assertIn("WARNING", p.stderr)
        self.assertEqual(read(self.table), "[]\n")

    def test_a_good_table_is_read_also_through_a_link(self):
        real = os.path.join(self.d, "elsewhere.json")
        write(real, '[{"method": "GET", "uri": "x", "action": "Closure"}]')
        os.symlink(real, os.path.join(self.routes, "one.json"))
        self.ok(self.kb(KB_ROUTES_DIR=self.routes))
        self.assertEqual(json.loads(read(self.table))[0]["uri"], "x")


# --- 8. The post-build signature includes the `calls` folders --------------------------------------
class CallsFolderSignature(KbCase):
    def test_a_change_in_a_calls_only_folder_reruns_the_merge(self):
        api_routes = os.path.join(self.d, "api.json")
        write(api_routes, "[]")
        calls = os.path.join(self.d, "fe", "src")
        write(os.path.join(calls, "api.ts"), "apiFetch('/a')\n")
        self.config(f'[[project]]\nname = "one"\ncode = ["{self.src}"]\nroutes = "{api_routes}"\n'
                    f'[[project]]\nname = "fe"\ncalls = ["{calls}"]\nserves = ["one"]\n')
        self.ok(self.kb("update"))
        self.assertIn("Inputs unchanged", self.ok(self.kb("update")).stdout)
        write(os.path.join(calls, "api.ts"), "apiFetch('/a')\napiFetch('/bb')\n")
        self.assertNotIn("Inputs unchanged", self.ok(self.kb("update")).stdout)


# --- 2 and 7. kb-refresh: the lock, --force when busy, and its inputs -------------------------------
class KbRefresh(unittest.TestCase):
    """bin/kb-refresh in a copied package with a fake `kb` next to it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = d = self._tmp.name
        self.pkg = os.path.join(d, "pkg")
        self.bin = os.path.join(self.pkg, "bin")
        os.makedirs(self.bin)
        for f in ("kb-refresh", "paths.py"):
            shutil.copy2(os.path.join(BIN, f), self.bin)
        write(os.path.join(self.pkg, "VERSION"), "1.0.0\n")
        self.log = os.path.join(d, "kb.log")
        exe(os.path.join(self.bin, "kb"), f'#!/bin/sh\necho "$* force=${{KB_FORCE:-0}}" >> "{self.log}"\n')
        self.home = os.path.join(d, "kb")
        self.code = os.path.join(d, "code")
        os.makedirs(self.code)
        self.routes_file = os.path.join(d, "outside", "api.json")
        write(self.routes_file, "[]\n")
        write(os.path.join(self.home, "kb.projects.toml"),
              f'[[project]]\nname = "one"\ncode = ["{self.code}"]\nroutes = "{self.routes_file}"\n')
        self.env = {**os.environ, "KB_HOME": self.home}
        self.env.pop("KB_ROUTES_DIR", None)
        self.lock = os.path.join(self.home, "logs", "update.lock")

    def run_refresh(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", os.path.join(self.bin, "kb-refresh"), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def builds(self) -> int:
        return len(read(self.log).splitlines()) if os.path.exists(self.log) else 0

    def hold_lock(self) -> None:
        os.makedirs(os.path.dirname(self.lock), exist_ok=True)
        holder = subprocess.Popen(["flock", self.lock, "sleep", "60"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        for _ in range(100):
            if subprocess.run(["flock", "-n", self.lock, "true"]).returncode != 0:
                return
            subprocess.run(["sleep", "0.05"])
        self.fail("the lock holder did not take the lock")

    def test_force_while_a_build_runs_exits_75_with_busy(self):
        self.hold_lock()
        p = self.run_refresh("--force")
        self.assertEqual(p.returncode, 75)
        self.assertIn("busy", p.stderr)
        self.assertEqual(self.builds(), 0)
        self.assertIn("exit=75", read(os.path.join(self.home, "logs", "refresh.log")))

    def test_a_plain_tick_while_a_build_runs_exits_0_quietly(self):
        self.hold_lock()
        p = self.run_refresh()
        self.assertEqual((p.returncode, p.stderr), (0, ""))

    def test_a_tick_without_changes_does_not_take_the_lock(self):
        # Otherwise it can take the lock just when the nightly --force starts, and --force is skipped.
        self.assertEqual(self.run_refresh().returncode, 0)
        os.remove(self.lock)
        self.assertEqual(self.run_refresh().returncode, 0)
        self.assertFalse(os.path.exists(self.lock))
        self.assertEqual(self.builds(), 1)

    def test_hubs_an_outside_route_table_and_the_version_are_inputs(self):
        self.run_refresh()
        self.assertEqual(self.builds(), 1)
        write(os.path.join(self.home, "kb.hubs.txt"), ".log()\n")
        self.run_refresh()
        self.assertEqual(self.builds(), 2)
        write(self.routes_file, '[{"uri": "x"}]\n')
        self.run_refresh()
        self.assertEqual(self.builds(), 3)
        write(os.path.join(self.pkg, "VERSION"), "1.0.1\n")
        self.run_refresh()
        self.assertEqual(self.builds(), 4)
        self.run_refresh()
        self.assertEqual(self.builds(), 4)


# --- 5. The file name stays out of the Controllers label -------------------------------------------
class ControllerLabel(unittest.TestCase):
    def test_a_controller_directly_in_controllers_has_no_file_name(self):
        d = label_namespaces.domain
        self.assertEqual(d({"source_file": "app/Http/Controllers/OrderController.php"}), "Http\\Controllers")
        self.assertEqual(d({"source_file": "app/Http/Controllers/V2/OrderController.php"}),
                         "Http\\Controllers\\V2")
        self.assertEqual(d({"source_file": "app/Http/Requests/StoreOrder.php"}), "Http\\Requests")
        self.assertEqual(d({"source_file": "app/Http/Kernel.php"}), "Http")


# --- 6. The dashboard counts real projects only ----------------------------------------------------
class DashboardProjects(unittest.TestCase):
    def test_db_and_domain_are_not_projects(self):
        g = {"nodes": [{"id": "a", "repo": "api"}, {"id": "b", "repo": "web"},
                       {"id": "c", "repo": "db"}, {"id": "e", "repo": "domain"}], "links": []}
        with tempfile.TemporaryDirectory() as d:
            gp = os.path.join(d, "graph.json")
            write(gp, json.dumps(g))
            with mock.patch.object(render_viz, "_qmd_collection_stats", return_value={}), \
                    mock.patch.object(render_viz, "_cli_version", return_value="x"), \
                    mock.patch.object(render_viz, "write_js") as wj:
                render_viz.write_status(d, ROOT, g, [], Counter(), gp)
        self.assertEqual(wj.call_args[0][2]["graph"]["projects"], 2)


# --- 9. link_http reads call sites only inside the calls folder ------------------------------------
class LinkHttpRoot(unittest.TestCase):
    def test_a_linked_file_outside_the_calls_folder_is_not_read(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "web", "src")
            outside = os.path.join(d, "secret", "api.ts")
            write(outside, "export function listOrders() {\n  return apiFetch(`/orders`);\n}\n")
            os.makedirs(src)
            os.symlink(outside, os.path.join(src, "api.ts"))
            g = {"directed": False, "multigraph": False, "graph": {}, "links": [], "nodes": [
                {"id": "api::index", "label": ".index()", "repo": "api", "type": "method",
                 "source_file": "app/Http/Controllers/OrderController.php", "source_location": "L14"},
                {"id": "api::ctl", "label": "OrderController", "repo": "api", "type": "class",
                 "source_file": "app/Http/Controllers/OrderController.php", "source_location": "L5"},
                {"id": "web::file", "label": "api.ts", "repo": "web", "type": "file",
                 "source_file": "src/api.ts", "source_location": "L1"}]}
            gp, rp = os.path.join(d, "graph.json"), os.path.join(d, "routes.json")
            write(gp, json.dumps(g))
            write(rp, json.dumps([{"method": "GET|HEAD", "uri": "orders",
                                   "action": "App\\Http\\Controllers\\OrderController@index"}]))
            p = subprocess.run([sys.executable, os.path.join(BIN, "link_http.py"), gp, rp, "api", src],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            edges = [e for e in json.loads(read(gp))["links"] if e.get("relation") == "http_request"]
        self.assertEqual(edges, [])


# --- 10. Extraction caps its output and its memory -------------------------------------------------
class ExtractionCaps(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name
        self.fake = os.path.join(self.d, "fakebin")
        self.doc = os.path.join(self.d, "a.docx")
        write(self.doc, "x")
        patcher = mock.patch.dict(os.environ, {"PATH": self.fake + os.pathsep + os.environ["PATH"]})
        patcher.start()
        self.addCleanup(patcher.stop)

    def pandoc(self, body: str) -> None:
        exe(os.path.join(self.fake, "pandoc"), f"#!{sys.executable}\nimport sys\n{body}\n")

    def test_the_output_is_cut_at_the_cap(self):
        self.pandoc("sys.stdout.write('word ' * 400000)")          # about 2 MB
        with mock.patch.dict(os.environ, {"KB_EXTRACT_MAX_OUTPUT_MB": "0.5"}):
            from pathlib import Path
            text = office.extract(Path(self.doc))
        self.assertLess(len(text), 600 * 1024)
        self.assertIn("KB_EXTRACT_MAX_OUTPUT_MB", text)

    def test_a_tool_over_the_memory_cap_fails_instead_of_filling_the_memory(self):
        # 300 MB under a 100 MB cap: the allocation fails, the file is reported, nothing grows.
        self.pandoc("b = bytearray(300 * 2**20)\nsys.stdout.write('word ' * 100)")
        with mock.patch.dict(os.environ, {"KB_EXTRACT_MAX_MEM_MB": "100"}):
            from pathlib import Path
            with self.assertRaises(parsers.ExtractError):
                office.extract(Path(self.doc))

    def test_a_normal_document_is_untouched(self):
        self.pandoc("sys.stdout.write('# Title\\n\\nbody\\n')")
        from pathlib import Path
        self.assertEqual(office.extract(Path(self.doc)), "# Title\n\nbody")


if __name__ == "__main__":
    unittest.main()
