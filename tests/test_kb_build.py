#!/usr/bin/env python3
"""bin/kb build safety: a build that fails half-way must leave the previous graph in place, and an
update skips what did not change but sees every change.

Runs the real bin/kb in a temp KB_HOME with a fake `graphify` on PATH (no real graphify, qmd or php).
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest

from _kbtest import BIN

FAKE_GRAPHIFY = r"""#!/bin/sh
# update <dir> --force : write a tiny valid graph; cluster-only : fail when told to
case "$1" in
  update) mkdir -p "$2/graphify-out"
          echo '{"directed": true, "multigraph": false, "graph": {}, "nodes": [{"id": "new::n", "label": "n"}], "links": []}' > "$2/graphify-out/graph.json" ;;
  cluster-only) [ -n "$FAKE_FAIL" ] && exit 1 ;;
esac
exit 0
"""


class FailedBuildKeepsTheGraph(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = self._tmp.name
        self.home = os.path.join(d, "kb")
        fake = os.path.join(d, "fakebin")
        src = os.path.join(d, "src")
        for p in (self.home, fake, src):
            os.makedirs(p)
        with open(os.path.join(src, "a.py"), "w") as f:
            f.write("x = 1\n")
        g = os.path.join(fake, "graphify")
        with open(g, "w") as f:
            f.write(FAKE_GRAPHIFY)
        os.chmod(g, os.stat(g).st_mode | stat.S_IEXEC)
        with open(os.path.join(self.home, "kb.projects.toml"), "w") as f:
            f.write(f'[[project]]\nname = "one"\ncode = ["{src}"]\n')
        self.env = {**os.environ, "KB_HOME": self.home, "PATH": fake + os.pathsep + os.environ["PATH"]}
        out = subprocess.run(["python3", os.path.join(BIN, "paths.py"), "--sh"], env=self.env,
                             capture_output=True, text=True, check=True).stdout
        self.out_dir = dict(line.split("=", 1) for line in out.splitlines())["KB_OUT"].strip('"')
        os.makedirs(self.out_dir, exist_ok=True)
        self.graph = os.path.join(self.out_dir, "graph.json")
        with open(self.graph, "w") as f:
            json.dump({"nodes": [{"id": "old::n", "label": "n"}], "links": []}, f)

    def build(self, fail: bool) -> subprocess.CompletedProcess:
        env = {**self.env, **({"FAKE_FAIL": "1"} if fail else {})}
        return subprocess.run(["bash", os.path.join(BIN, "kb"), "graph"], env=env,
                              capture_output=True, text=True, timeout=120)

    def ids(self) -> set[str]:
        with open(self.graph) as f:
            return {n["id"] for n in json.load(f)["nodes"]}

    def test_failed_build_restores_the_previous_graph(self):
        p = self.build(fail=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("kept the previous graph", p.stderr)
        self.assertEqual(self.ids(), {"old::n"})
        self.assertFalse(os.path.exists(self.graph + ".prev"))

    def test_good_build_replaces_the_graph(self):
        p = self.build(fail=False)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        self.assertIn("one::new::n", self.ids())   # a one-project graph gets the repo prefix
        self.assertNotIn("old::n", self.ids())
        self.assertFalse(os.path.exists(self.graph + ".prev"))


    def test_a_build_logs_the_seconds_of_each_step(self):
        p = self.build(fail=False)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        self.assertRegex(p.stdout, r"==> Merging into one cross-repo graph\n(.*\n)*?    \(\d+s\)")
        data = os.path.dirname(self.out_dir) if os.path.basename(self.out_dir) == "graphify-out" else self.home
        with open(os.path.join(data, "logs", "build-steps.log")) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"exit=0 graph \| \d+s Building per-project graphs \| \d+s Merging")

LOGGING_GRAPHIFY = r"""#!/bin/sh
# Logs every call. update without --force refuses (like graphify's shrink guard) when FAKE_REFUSE is set.
echo "$*" >> "$FAKE_LOG"
case "$1" in
  --version) echo "graphify 0.0.0-fake" ;;
  update) if [ -n "$FAKE_REFUSE" ] && [ "$3" != "--force" ]; then
            echo "[graphify] WARNING: new graph has 1 nodes but existing graph.json has 2. Refusing to overwrite." >&2
            exit 0
          fi
          mkdir -p "$2/graphify-out"
          echo '{"directed": true, "multigraph": false, "graph": {}, "nodes": [{"id": "new::n", "label": "n"}], "links": []}' > "$2/graphify-out/graph.json" ;;
esac
exit 0
"""


class IncrementalUpdate(unittest.TestCase):
    """`kb update` skips what did not change, and still sees every change."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = self._tmp.name
        self.home, fake, self.src = (os.path.join(d, x) for x in ("kb", "fakebin", "src"))
        for p in (self.home, fake, self.src):
            os.makedirs(p)
        self.write("a.py", "x = 1\n")
        g = os.path.join(fake, "graphify")
        with open(g, "w") as f:
            f.write(LOGGING_GRAPHIFY)
        os.chmod(g, os.stat(g).st_mode | stat.S_IEXEC)
        with open(os.path.join(self.home, "kb.projects.toml"), "w") as f:
            f.write(f'[[project]]\nname = "one"\ncode = ["{self.src}"]\n')
        self.log = os.path.join(d, "calls.log")
        self.env = {**os.environ, "KB_HOME": self.home, "FAKE_LOG": self.log,
                    "PATH": fake + os.pathsep + os.environ["PATH"]}

    def write(self, name, text):
        with open(os.path.join(self.src, name), "w") as f:
            f.write(text)

    def run_kb(self, **extra) -> tuple[str, list[str]]:
        open(self.log, "w").close()
        p = subprocess.run(["bash", os.path.join(BIN, "kb"), "graph"], env={**self.env, **extra},
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        with open(self.log) as f:
            calls = [c.strip() for c in f if not c.startswith("--version")]
        return p.stdout, calls

    def updates(self, calls):
        return [c.split()[-1] if c.endswith("--force") else "plain" for c in calls if c.startswith("update")]

    def test_unchanged_run_calls_nothing(self):
        self.run_kb()
        out, calls = self.run_kb()
        self.assertIn("unchanged — graph kept", out)
        self.assertIn("Inputs unchanged", out)
        self.assertEqual(calls, [])                     # no graphify update, no cluster-only

    def test_changed_file_rebuilds_incrementally(self):
        self.run_kb()
        self.write("a.py", "x = 2  # longer now\n")
        out, calls = self.run_kb()
        self.assertEqual(self.updates(calls), ["plain"])
        self.assertTrue(any(c.startswith("cluster-only") for c in calls))
        self.assertNotIn("Inputs unchanged", out)

    def test_new_file_and_deleted_file_are_seen(self):
        self.run_kb()
        self.write("b.py", "y = 1\n")
        self.assertEqual(self.updates(self.run_kb()[1]), ["plain"])
        os.remove(os.path.join(self.src, "b.py"))
        self.assertEqual(self.updates(self.run_kb()[1]), ["plain"])

    def test_refused_shrink_falls_back_to_a_full_pass(self):
        self.run_kb()
        self.write("a.py", "\n")
        out, calls = self.run_kb(FAKE_REFUSE="1")
        self.assertEqual(self.updates(calls), ["plain", "--force"])
        self.assertIn("full pass", out)

    def test_force_and_build_always_rebuild(self):
        self.run_kb()
        self.assertEqual(self.updates(self.run_kb(KB_FORCE="1")[1]), ["--force"])



SAFE_GRAPHIFY = r"""#!/bin/sh
# Logs every call. FAKE_UPDATE_FAIL: `update` fails and writes nothing (a crash, a full disk).
echo "$*" >> "$FAKE_LOG"
case "$1" in
  --version) echo "graphify 0.0.0-fake" ;;
  update) [ -n "$FAKE_UPDATE_FAIL" ] && exit 1
          mkdir -p "$2/graphify-out"
          echo '{"directed": true, "multigraph": false, "graph": {}, "nodes": [{"id": "new::n", "label": "n"}], "links": []}' > "$2/graphify-out/graph.json" ;;
  merge-graphs) first="$2"; while [ "$#" -gt 0 ] && [ "$1" != --out ]; do shift; done; cp "$first" "$2" ;;
esac
exit 0
"""


def _exe(path: str, text: str) -> None:
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


class BuildInputs(unittest.TestCase):
    """The build sees every input and says so loudly when one is broken (review 2026-11-23)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = d = self._tmp.name
        self.home, self.fake = os.path.join(d, "kb"), os.path.join(d, "fakebin")
        self.src = os.path.join(d, "app", "src")
        for p in (self.home, self.fake, self.src):
            os.makedirs(p)
        with open(os.path.join(self.src, "a.py"), "w") as f:
            f.write("x = 1\n")
        _exe(os.path.join(self.fake, "graphify"), SAFE_GRAPHIFY)
        _exe(os.path.join(self.fake, "qmd"), "#!/bin/sh\nexit 0\n")
        self.log = os.path.join(d, "calls.log")
        open(self.log, "w").close()
        self.env = {**os.environ, "KB_HOME": self.home, "FAKE_LOG": self.log, "KB_ADDONS": "none",
                    "PATH": self.fake + os.pathsep + os.environ["PATH"]}
        self.env.pop("KB_ROUTES_DIR", None)
        self.config(f'[[project]]\nname = "one"\ncode = ["{self.src}"]\n')

    def config(self, text: str) -> None:
        with open(os.path.join(self.home, "kb.projects.toml"), "w") as f:
            f.write(text)

    def kb(self, cmd: str = "graph", **extra) -> subprocess.CompletedProcess:
        open(self.log, "w").close()
        return subprocess.run(["bash", os.path.join(BIN, "kb"), cmd], env={**self.env, **extra},
                              capture_output=True, text=True, timeout=120)

    def calls(self) -> list[str]:
        with open(self.log) as f:
            return [c.strip() for c in f if not c.startswith("--version")]

    def project_graph(self, name: str = "one") -> str:
        return os.path.join(self.home, "repos", name, "graphify-out", "graph.json")

    def ok(self, p: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(p.returncode, 0, p.stdout[-800:] + p.stderr[-800:])
        return p

    # 1
    def test_failed_graphify_writes_no_manifest_and_the_next_run_retries(self):
        self.ok(self.kb())
        with open(os.path.join(self.src, "a.py"), "w") as f:
            f.write("x = 2  # changed\n")
        p = self.kb(FAKE_UPDATE_FAIL="1")
        self.assertIn("graphify failed", p.stderr)
        self.assertNotEqual(p.returncode, 0)                   # the build says it failed
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.project_graph()), ".kb-manifest")))
        p = self.ok(self.kb())
        self.assertNotIn("unchanged — graph kept", p.stdout)
        self.assertTrue(any(c.startswith("update") for c in self.calls()))

    # 2
    def test_missing_code_folder_fails_loudly_and_keeps_the_project_graph(self):
        self.ok(self.kb())
        with open(self.project_graph()) as f:
            before = f.read()
        self.config(f'[[project]]\nname = "one"\ncode = ["{self.src}", "{self.d}/gone"]\n')
        p = self.kb()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("gone", p.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.home, "repos", "one", "src", "a.py")))
        with open(self.project_graph()) as f:
            self.assertEqual(f.read(), before)

    def test_empty_code_folder_fails_loudly(self):
        empty = os.path.join(self.d, "empty", "src")
        os.makedirs(empty)
        self.config(f'[[project]]\nname = "one"\ncode = ["{empty}"]\n')
        p = self.kb()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no files", p.stderr)

    # 3
    def test_artisan_noise_keeps_the_last_good_route_table(self):
        app = os.path.join(self.d, "app")
        _exe(os.path.join(self.fake, "php"), "#!/bin/sh\necho 'PHP Notice: something'\necho '[]'\nexit 0\n")
        self.config(f'[[project]]\nname = "one"\ncode = ["{self.src}"]\nroutes = "{app}"\n')
        good = os.path.join(self.home, "routes.one.json")
        with open(good, "w") as f:
            f.write('[{"uri": "a"}]')
        p = self.kb()
        self.assertIn("previous route table", p.stderr)
        with open(good) as f:
            self.assertEqual(f.read(), '[{"uri": "a"}]')

    # 4
    def test_a_new_migration_reruns_the_merge(self):
        app = os.path.join(self.d, "app")
        mig = os.path.join(app, "database", "migrations")
        os.makedirs(mig)
        rdir = os.path.join(self.d, "routes")
        os.makedirs(rdir)
        with open(os.path.join(rdir, "one.json"), "w") as f:
            f.write("[]")
        self.config(f'[[project]]\nname = "one"\ncode = ["{self.src}"]\nroutes = "{app}"\n')
        self.ok(self.kb(KB_ROUTES_DIR=rdir))
        self.assertIn("Inputs unchanged", self.ok(self.kb(KB_ROUTES_DIR=rdir)).stdout)
        with open(os.path.join(mig, "2026_01_01_000000_create_x_table.php"), "w") as f:
            f.write("<?php\n")
        self.assertNotIn("Inputs unchanged", self.ok(self.kb(KB_ROUTES_DIR=rdir)).stdout)

    # 5
    def test_docs_only_kb_skips_the_graph(self):
        docs = os.path.join(self.d, "docs")
        os.makedirs(docs)
        self.config(f'[[project]]\nname = "manual"\ndocs = "{docs}"\n')
        p = self.ok(self.kb())
        self.assertIn("no code projects", p.stdout)

    # 6
    def test_scip_store_does_not_break_the_unchanged_check(self):
        with open(os.path.join(self.d, "app", "composer.json"), "w") as f:
            f.write("{}")
        tool = os.path.join(self.fake, "fake-scip-php")
        _exe(tool, f'#!/bin/sh\necho scip >> "{self.log}"\n: > index.scip\n')
        env = {"KB_ADDONS": "scip", "KB_SCIP_PHP": tool}
        self.ok(self.kb(**env))
        self.assertIn("scip", self.calls())
        p = self.ok(self.kb(**env))
        self.assertIn("Inputs unchanged", p.stdout)
        self.assertNotIn("scip", self.calls())

    # 7
    def test_two_code_folders_with_one_basename_fail_loudly(self):
        other = os.path.join(self.d, "other", "src")
        os.makedirs(other)
        with open(os.path.join(other, "b.py"), "w") as f:
            f.write("y = 1\n")
        self.config(f'[[project]]\nname = "one"\ncode = ["{self.src}", "{other}"]\n')
        p = self.kb()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("same folder name", p.stderr)

    # 8
    def test_config_is_read_as_toml(self):
        odd = os.path.join(self.d, "x#1", "src")
        os.makedirs(odd)
        with open(os.path.join(odd, "c.py"), "w") as f:
            f.write("z = 1\n")
        self.config(f"[[project]]\nname = 'one'   # comment\ncode = [\n  '{odd}',\n]\n")
        p = self.ok(self.kb())
        self.assertIn("[one] staged 1 source files", p.stdout)

    def test_broken_config_fails_loudly(self):
        self.config('[[project]]\nname = "one\n')
        p = self.kb()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("kb.projects.toml", p.stderr)

    def test_kb_passes_the_calling_project_and_the_migrations(self):
        # A wrapper `python3` logs the argv of each linker, then runs the real one.
        real = subprocess.run(["sh", "-c", "command -v python3"], capture_output=True, text=True).stdout.strip()
        argv_log = os.path.join(self.d, "argv.log")
        _exe(os.path.join(self.fake, "python3"),
             f'#!/bin/sh\ncase "$1" in *link_http.py|*link_data.py) echo "$*" >> "{argv_log}";; esac\n'
             f'case "$1" in *link_http.py) exit 0;; esac\nexec {real} "$@"\n')
        app = os.path.join(self.d, "app")
        os.makedirs(os.path.join(app, "database", "migrations"))
        rdir = os.path.join(self.d, "routes")
        os.makedirs(rdir)
        with open(os.path.join(rdir, "api.json"), "w") as f:
            f.write("[]")
        web = os.path.join(self.d, "web", "src")
        os.makedirs(web)
        with open(os.path.join(web, "w.js"), "w") as f:
            f.write("x()\n")
        self.config(f'[[project]]\nname = "api"\ncode = ["{self.src}"]\nroutes = "{app}"\n\n'
                    f'[[project]]\nname = "web"\ncode = ["{web}"]\ncalls = ["{web}"]\nserves = "api"\n')
        self.ok(self.kb(KB_ROUTES_DIR=rdir))
        with open(argv_log) as f:
            lines = f.read().splitlines()
        self.assertTrue(any("link_http.py --from web " in ln for ln in lines), lines)
        self.assertTrue(any(f"--migrations {app}/database/migrations" in ln for ln in lines if "link_data" in ln), lines)

    # 9 (the staging side; bin/extract has its own tests)
    def test_docs_staging_drops_outside_symlinks_and_keeps_extracted_files(self):
        docs = os.path.join(self.d, "docs")
        os.makedirs(docs)
        with open(os.path.join(docs, "guide.md"), "w") as f:
            f.write("# Guide\n")
        secret = os.path.join(self.d, "secret.json")
        with open(secret, "w") as f:
            f.write("{}")
        os.symlink(secret, os.path.join(docs, "report.md"))
        open(os.path.join(docs, "deck.pdf"), "wb").close()
        stage = os.path.join(self.home, ".docs", "manual")
        os.makedirs(stage)
        with open(os.path.join(stage, "deck.pdf.md"), "w") as f:
            f.write("extracted\n")
        self.config(f'[[project]]\nname = "manual"\ndocs = "{docs}"\n')
        self.ok(self.kb("kb", KB_EXTRACT="0"))
        self.assertTrue(os.path.isfile(os.path.join(stage, "guide.md")))
        self.assertFalse(os.path.lexists(os.path.join(stage, "report.md")))
        self.assertTrue(os.path.isfile(os.path.join(stage, "deck.pdf.md")))


if __name__ == "__main__":
    unittest.main()
