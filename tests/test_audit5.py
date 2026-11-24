#!/usr/bin/env python3
"""Review of the fourth round (2026-11-24): one test class per finding.

A contract doc found by its qmd path, which qmd normalises; the problem scanner that read call sites
without the checks of link_http; a review path of many segments; an extraction tool that failed but
whose output was kept; a sign-in token that lost its transaction; a Rebuild button that took a lock of
cron for its own build; and a route table that changed inside one second. Every input here is small:
a repro that reads a pipe or /dev/zero runs in a subprocess with a timeout and a memory cap.
"""
from __future__ import annotations

import json
import os
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _kbtest import BIN

import access
import admin
import contracts
import docs_grade
import kb_core
import oauth
import parsers
import settings
from parsers import office, pdf_ocr

CAP = 512 * 2**20   # the address space of a repro subprocess


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def capped() -> None:   # runs in a repro subprocess before it starts
    resource.setrlimit(resource.RLIMIT_AS, (CAP, CAP))


def run_capped(code: str, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True,
                          timeout=timeout, preexec_fn=capped, cwd=BIN)


# --- 1. Contract docs live in collections of their own -------------------------------------------
def _settings(test: unittest.TestCase, mode: str) -> None:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    p = mock.patch.object(settings, "state_file", lambda: os.path.join(tmp.name, "settings.json"))
    p.start()
    test.addCleanup(p.stop)
    settings._cache.clear()
    env = mock.patch.dict(os.environ, {"KB_CONTRACTS": mode})
    env.start()
    test.addCleanup(env.stop)


class ContractStage(unittest.TestCase):
    """Only the real contract files are copied into the contract stage, and the copy is replaced whole.
    qmd stores `contracts___private.md`, `contracts_/private.md` and `contracts/---/private.md` all as
    `contracts/private.md` (qmd 2.5 `handelize`): none of them may reach a contract collection."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.docs = os.path.join(self.tmp.name, "sw-docs")
        self.stage = os.path.join(self.tmp.name, "stage")

    def stage_all(self, **roots: str) -> None:
        p = subprocess.run([sys.executable, os.path.join(BIN, "contracts.py"), "stage-contracts", self.stage,
                            *(f"{k}={v}" for k, v in roots.items())],
                           capture_output=True, text=True, timeout=60, preexec_fn=capped)
        self.assertEqual(p.returncode, 0, p.stderr)

    def staged(self) -> dict[str, list[str]]:
        return {d: sorted(os.listdir(os.path.join(self.stage, d, "contracts"))) for d in sorted(os.listdir(self.stage))}

    def test_only_the_real_contract_files_are_staged(self):
        for rel in ("contracts/sw-web.md", "contracts/README.md", "contracts/drafts/x.md", "contracts___private.md",
                    "contracts_/private.md", "contracts/---/private.md", "contracts/---/x\U0001faeay.md",
                    "secret.md"):
            write(os.path.join(self.docs, rel), "PRIVATE" if "sw-web" not in rel else "the contract")
        write(os.path.join(self.tmp.name, "outside.md"), "PRIVATE outside")
        os.symlink(os.path.join(self.tmp.name, "outside.md"), os.path.join(self.docs, "contracts", "link.md"))
        os.mkfifo(os.path.join(self.docs, "contracts", "pipe.md"))
        self.stage_all(sw=self.docs, empty=os.path.join(self.tmp.name, "nothing"))
        self.assertEqual(self.staged(), {"empty": [], "sw": ["sw-web.md"]})
        self.assertEqual(Path(self.stage, "sw", "contracts", "sw-web.md").read_text(), "the contract")

    def test_a_removed_contract_is_gone_after_the_next_build(self):
        write(os.path.join(self.docs, "contracts", "old.md"), "old")
        self.stage_all(sw=self.docs)
        os.remove(os.path.join(self.docs, "contracts", "old.md"))
        write(os.path.join(self.docs, "contracts", "new.md"), "new")
        self.stage_all(sw=self.docs)
        self.assertEqual(self.staged(), {"sw": ["new.md"]})
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["stage", "sw-docs"])   # no .new or .old left
        self.stage_all()                                                           # the project left
        self.assertEqual(self.staged(), {})

    def test_the_build_makes_an_excluded_collection_per_project(self):
        home, fake = os.path.join(self.tmp.name, "kb"), os.path.join(self.tmp.name, "fakebin")
        log = os.path.join(self.tmp.name, "qmd.log")
        write(os.path.join(fake, "qmd"), f'#!/bin/sh\necho "$*" >> "{log}"\nexit 0\n')
        os.chmod(os.path.join(fake, "qmd"), 0o755)
        write(os.path.join(self.docs, "contracts", "sw-web.md"), "the contract")
        write(os.path.join(self.docs, "contracts___private.md"), "PRIVATE")
        write(os.path.join(home, "kb.projects.toml"), f'[[project]]\nname = "sw"\ndocs = "{self.docs}"\n')
        env = {**os.environ, "KB_HOME": home, "KB_ADDONS": "none", "KB_EXTRACT": "0",
               "PATH": fake + os.pathsep + os.environ["PATH"]}
        p = subprocess.run(["bash", os.path.join(BIN, "kb"), "kb"], env=env, capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout[-800:] + p.stderr[-800:])
        cstage = os.path.join(home, ".docs-contracts", "sw")
        self.assertEqual(os.listdir(os.path.join(cstage, "contracts")), ["sw-web.md"])
        calls = Path(log).read_text()
        self.assertIn(f"collection add {cstage} --name kb_contracts-sw", calls)
        self.assertIn("collection exclude kb_contracts-sw", calls)


class ContractCollectionAccess(unittest.TestCase):
    """A hidden project's doc collection is closed as a whole; its contract collection opens under `all`."""

    HITS = {"kb-web": [{"file": "qmd://kb-web/login.md", "line": 1, "docid": "#a", "title": "Login", "score": 0.9,
                        "snippet": "own"}],
            "kb-sw": [{"file": "qmd://kb-sw/contracts/private.md", "line": 1, "docid": "#p", "title": "x",
                       "score": 0.99, "snippet": "PRIVATE"}],
            "kb_contracts-sw": [{"file": "qmd://kb_contracts-sw/contracts/sw-web.md", "line": 1, "docid": "#c",
                                 "title": "Contract", "score": 0.8, "snippet": "the contract"}],
            "kb_contracts-web": [{"file": "qmd://kb_contracts-web/contracts/web-sw.md", "line": 1, "docid": "#w",
                                  "title": "Own contract", "score": 0.7, "snippet": "copy"}]}

    def setUp(self):
        for name, value in (("PROJECTS", {"sw": [], "web": []}), ("DOC_PROJECTS", {"sw", "web"}),
                            ("HOME_PROJECTS", [])):
            p = mock.patch.object(kb_core, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(kb_core.warm, "qmd", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def qmd(self, cmds: list):
        def run(cmd, **kw):
            cmds.append(cmd)
            cols = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-c"] or list(self.HITS)
            return json.dumps([h for c in cols for h in self.HITS.get(c, [])])
        return run

    def search(self, allowed, mode: str = "all") -> tuple[str, list]:
        _settings(self, mode)
        cmds = []
        with mock.patch.object(kb_core, "run", self.qmd(cmds)):
            out = kb_core.call_tool("search", {"q": "contract", "domain": "docs", "refuse": False}, allowed)
        return out, cmds

    def test_all_reads_the_contract_collections_of_the_others_only(self):
        out, cmds = self.search({"web"})
        self.assertIn("qmd://kb_contracts-sw/contracts/sw-web.md", out)
        self.assertNotIn("PRIVATE", out)
        self.assertNotIn("kb_contracts-web", out)        # its own copy: it reads kb-web already
        asked = [c for cmd in cmds for i, c in enumerate(cmd) if i and cmd[i - 1] == "-c"]
        self.assertEqual(sorted(asked), ["kb-web", "kb_contracts-sw"])

    def test_own_reads_no_collection_of_another_project(self):
        out, cmds = self.search({"web"}, "own")
        self.assertNotIn("kb_contracts-sw", out)
        self.assertNotIn("kb-sw", " ".join(" ".join(c) for c in cmds))

    def test_a_local_search_has_no_duplicates(self):
        out, _ = self.search(None)
        self.assertNotIn("kb_contracts-", out)
        self.assertIn("qmd://kb-sw/contracts/private.md", out)    # a local caller sees everything

    def test_doc_allowed_and_docs_get(self):
        _settings(self, "all")
        for path in ("qmd://kb-sw/contracts/private.md", "qmd://kb-sw/contracts/sw-web.md", "qmd://kb-sw/a.md"):
            self.assertFalse(access.doc_allowed(path, {"web"}), path)
        self.assertTrue(access.doc_allowed("qmd://kb_contracts-sw/contracts/sw-web.md:3", {"web"}))
        with mock.patch.object(kb_core, "run", lambda cmd, **kw: "qmd://kb-sw/contracts/private.md  #p\nPRIVATE"):
            self.assertIn("not found in your projects", kb_core.call_tool("docs_get", {"path": "#p"}, {"web"}))
        with mock.patch.dict(os.environ, {"KB_CONTRACTS": "own"}):
            settings._cache.clear()
            self.assertFalse(access.doc_allowed("qmd://kb_contracts-sw/contracts/sw-web.md", {"web"}))
            self.assertTrue(access.doc_allowed("qmd://kb_contracts-web/contracts/web-sw.md", {"web"}))

    def test_the_grade_counts_the_collections_the_caller_searches(self):
        seen = []
        with mock.patch.object(docs_grade, "open_index", lambda kb, *a, **k: seen.append((a, k))), \
                mock.patch.object(docs_grade, "first_hit", lambda out: "x"), \
                mock.patch.object(docs_grade, "grade", lambda q, out, index: mock.Mock(weak=False)):
            _settings(self, "all")
            kb_core._judge({}, "q", "qmd://kb-web/a.md", lambda extra: "", {"web"})
            kb_core._judge({}, "q", "qmd://kb-web/a.md", lambda extra: "", None)
        self.assertEqual(seen, [((["kb-web", "kb_contracts-sw"],), {}), ((), {"skip_prefix": "kb_contracts-"})])

    def test_a_contract_doc_link_points_into_its_contract_collection(self):
        self.assertEqual(contracts.doc_uri("sw", "contracts/sw-web.md"), "qmd://kb_contracts-sw/contracts/sw-web.md")


# --- 2. The problem scanner reads call sites like link_http -----------------------------------------
class ProblemScannerReads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = os.path.join(self.tmp.name, "fe", "src")
        os.makedirs(self.calls)
        self.routes = os.path.join(self.tmp.name, "routes.json")
        write(self.routes, json.dumps([{"method": "GET", "uri": "v1/known"}]))
        self.conn = os.path.join(self.tmp.name, "conn.json")
        write(self.conn, "[]")
        self.out = os.path.join(self.tmp.name, "problems.json")

    def problems(self) -> list[dict]:
        p = subprocess.run([sys.executable, os.path.join(BIN, "contracts.py"), "problems", self.conn, self.out,
                            f"routes:api={self.routes}", f"calls:fe=api={self.calls}"],
                           capture_output=True, text=True, timeout=20, preexec_fn=capped)
        self.assertEqual(p.returncode, 0, p.stderr[-500:])
        with open(self.out, encoding="utf-8") as f:
            return json.load(f)

    def test_a_pipe_and_a_device_do_not_stop_the_build(self):
        os.mkfifo(os.path.join(self.calls, "blocked.ts"))
        os.symlink("/dev/zero", os.path.join(self.calls, "large.ts"))
        write(os.path.join(self.calls, "ok.ts"), "http.get('v1/unknown')\n")
        rows = self.problems()   # the old code waits on the pipe for ever: the timeout fails the test
        self.assertEqual([r["where"] for r in rows if r["kind"] == "unknown-route"], ["src/ok.ts:L1"])

    def test_a_link_out_of_the_calls_folder_is_not_read(self):
        outside = os.path.join(self.tmp.name, "secret", "x.ts")
        write(outside, "http.get('v1/unknown')\n")
        os.symlink(outside, os.path.join(self.calls, "x.ts"))
        self.assertEqual([r for r in self.problems() if r["kind"] == "unknown-route"], [])


# --- 3. A review path of many segments costs its length ---------------------------------------------
class ReviewDeepPath(unittest.TestCase):
    def test_a_path_of_300000_segments_answers_at_once(self):
        code = ("import sys, time; sys.path.insert(0, '.'); import review\n"
                "g = {'nodes': [{'id': 'a', 'repo': 'api', 'source_file': 'app/X.php', 'type': 'class'}], 'links': []}\n"
                "t = time.monotonic(); out = review.analyze(g, {'x/' * 300000 + 'app/X.php': set()})\n"
                "print(round(time.monotonic() - t, 2)); print('X.php' in out)")
        p = run_capped(code, timeout=20)   # the old code needs hours: the timeout fails the test
        self.assertEqual(p.returncode, 0, p.stderr[-500:])
        seconds, found = p.stdout.split()
        self.assertLess(float(seconds), 5)


# --- 4. A failed extraction tool is an error, not a document ----------------------------------------
class ToolExitCode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def tool(self, name: str, body: str) -> None:
        path = os.path.join(self.tmp.name, "bin", name)
        write(path, "#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def test_output_before_a_crash_is_not_kept(self):
        self.tool("pandoc", "echo 'First paragraph.'; echo 'out of memory' >&2; exit 1")
        doc = Path(self.tmp.name, "a.docx")
        doc.write_bytes(b"x")
        with mock.patch.dict(os.environ, {"PATH": os.path.join(self.tmp.name, "bin") + os.pathsep + os.environ["PATH"]}):
            with self.assertRaises(parsers.ExtractError) as cm:
                office.extract(doc)
        self.assertNotIsInstance(cm.exception, parsers.TransientError)
        self.assertIn("out of memory", str(cm.exception))

    def test_the_cut_at_the_output_cap_keeps_its_note(self):
        self.tool("big", "yes 'line of text' | head -c 200000; exit 0")
        with mock.patch.dict(os.environ, {"KB_EXTRACT_MAX_OUTPUT_MB": "0.01"}):
            text, _ = parsers.run_tool([os.path.join(self.tmp.name, "bin", "big")], timeout=20)
        self.assertIn("output cut at", text)

    def test_ocr_ends_at_the_last_page_and_fails_on_another_error(self):
        doc = Path(self.tmp.name, "scan.pdf")
        doc.write_bytes(b"%PDF")

        def fake(errors: dict):
            def run(cmd, timeout):
                if cmd[0] == "tesseract":
                    return "page text", ""
                page = int(cmd[2])
                if page in errors:
                    raise parsers.ToolFailed("pdftoppm failed", 99, errors[page])
                Path(cmd[-1] + f"-{page}.png").write_bytes(b"png")
                return "", ""
            return run
        with mock.patch.object(pdf_ocr, "shutil_which", lambda name: "/bin/true"):
            with mock.patch.object(pdf_ocr, "run_tool", fake({3: "Wrong page range given: the first page (3) "
                                                                 "can not be after the last page (2)."})):
                self.assertEqual(pdf_ocr.extract(doc).count("page text"), 2)
            with mock.patch.object(pdf_ocr, "run_tool", fake({2: "Out of memory"})):
                with self.assertRaises(parsers.ExtractError):
                    pdf_ocr.extract(doc)


# --- 5. A token that lost its transaction is ended ---------------------------------------------------
class LostTransaction(unittest.TestCase):
    def test_the_second_submit_hands_its_token_to_on_drop(self):
        srv = oauth.Server({"KB_LOGIN_URL": "http://127.0.0.1:1/x"}, lambda t: None, "")
        dropped = []
        srv.on_drop = dropped.append
        client = srv.register({"redirect_uris": ["http://127.0.0.1:5000/cb"]})
        txn = srv.start({"client_id": client["client_id"], "redirect_uri": "http://127.0.0.1:5000/cb",
                         "response_type": "code", "code_challenge": "x", "code_challenge_method": "S256"})
        srv._finish(txn, "first-token")
        with self.assertRaises(oauth.OAuthError):
            srv._finish(txn, "second-token")
        self.assertEqual(dropped, ["second-token"])


# --- 6. The Rebuild button waits for its own child ---------------------------------------------------
class RebuildAck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "logs"))
        self.lock = os.path.join(self.tmp.name, "logs", "update.lock")
        p = mock.patch.object(kb_core, "KB", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(admin, "_rebuild_proc", None)
        p.start()
        self.addCleanup(p.stop)

    def script(self, body: str) -> str:
        path = os.path.join(self.tmp.name, "fake-refresh")
        write(path, "#!/usr/bin/env bash\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def test_a_lock_of_cron_is_not_the_start_of_this_build(self):
        # cron takes the lock after the check of the page; the child then finds it busy and exits 75
        msg = admin.rebuild(self.script(f'flock "{self.lock}" sleep 2 & sleep 0.5; exit 75'))
        self.assertIn("already running", msg)
        admin._rebuild_proc.wait(5)

    def test_the_child_that_takes_the_lock_confirms(self):
        msg = admin.rebuild(self.script(f'exec 8>"{self.lock}"; flock -n 8 || exit 75; '
                                        'echo $$ >"$KB_REFRESH_ACK"; sleep 1'))
        self.assertIn("Rebuild started", msg)
        admin._rebuild_proc.wait(5)
        self.assertEqual([f for f in os.listdir(os.path.join(self.tmp.name, "logs")) if "ack" in f], [])


# --- 6 and 7. kb-refresh: the ack and the route tables outside KB_ROUTES_DIR ------------------------
class KbRefreshInputs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = self._tmp.name
        self.bin = os.path.join(d, "pkg", "bin")
        os.makedirs(self.bin)
        for f in ("kb-refresh", "paths.py"):
            shutil.copy2(os.path.join(BIN, f), self.bin)
        write(os.path.join(d, "pkg", "VERSION"), "1.0.0\n")
        self.log = os.path.join(d, "kb.log")
        write(os.path.join(self.bin, "kb"), f'#!/bin/sh\necho "$*" >> "{self.log}"\n')
        os.chmod(os.path.join(self.bin, "kb"), 0o755)
        self.home = os.path.join(d, "kb")
        self.routes = os.path.join(d, "outside", "api.json")
        write(self.routes, '[{"uri": "a"}]\n')
        write(os.path.join(self.home, "kb.projects.toml"),
              f'[[project]]\nname = "one"\ncode = ["{d}"]\nroutes = "{self.routes}"\n')
        self.env = {**os.environ, "KB_HOME": self.home}
        self.env.pop("KB_ROUTES_DIR", None)

    def refresh(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", os.path.join(self.bin, "kb-refresh"), *args], env=env or self.env,
                              capture_output=True, text=True, timeout=60)

    def builds(self) -> int:
        return len(Path(self.log).read_text().splitlines()) if os.path.exists(self.log) else 0

    def test_a_same_size_version_in_the_same_second_is_a_change(self):
        self.refresh()
        self.assertEqual(self.builds(), 1)
        st = os.stat(self.routes)
        write(self.routes, '[{"uri": "b"}]\n')
        os.utime(self.routes, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.refresh()
        self.assertEqual(self.builds(), 2)
        self.refresh()
        self.assertEqual(self.builds(), 2)

    def test_force_writes_its_pid_to_the_ack_file(self):
        ack = os.path.join(self._tmp.name, "ack")
        p = subprocess.Popen(["bash", os.path.join(self.bin, "kb-refresh"), "--force"],
                             env={**self.env, "KB_REFRESH_ACK": ack})
        self.assertEqual(p.wait(60), 0)
        self.assertEqual(Path(ack).read_text().strip(), str(p.pid))


if __name__ == "__main__":
    unittest.main()


class ReadSourceRace(unittest.TestCase):
    """The path checked and the file opened must be one file: a link swapped in between is refused."""

    def test_a_link_swapped_in_after_the_check_is_not_read(self):
        import graph  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as d:
            root, outside = os.path.join(d, "docs"), os.path.join(d, "secret.md")
            os.makedirs(root)
            Path(outside).write_text("private")
            target = os.path.join(root, "contract.md")
            Path(target).write_text("public")
            real_open = os.open

            def swap_then_open(path, *a, **kw):   # the writer wins the race between check and open
                if path == target and not os.path.islink(target):
                    os.remove(target)
                    os.symlink(outside, target)
                return real_open(path, *a, **kw)

            with mock.patch.object(graph.os, "open", swap_then_open):
                self.assertIsNone(graph.read_source(target, root))


class OversizedContract(unittest.TestCase):
    def test_a_contract_over_the_limit_is_left_out_not_cut(self):
        import graph  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as d:
            docs = os.path.join(d, "docs", "contracts")
            os.makedirs(docs)
            front = "---\nfrom: web\nto: api\n---\n"
            Path(docs, "big.md").write_text(front + "x" * 200)
            Path(docs, "small.md").write_text(front + "ok")
            stage = os.path.join(d, "stage")
            with mock.patch.object(graph, "MAX_SOURCE_BYTES", 100), \
                    mock.patch("sys.stderr", new_callable=lambda: __import__("io").StringIO()) as err:
                contracts.stage_contracts(stage, {"web": os.path.join(d, "docs")})
            self.assertEqual(sorted(os.listdir(os.path.join(stage, "web", "contracts"))), ["small.md"])
            self.assertIn("big.md", err.getvalue())
