#!/usr/bin/env python3
"""Fixes from the third review of the 2026-11-24 audit round.

One class per finding: the full-graph shortcut and a graph rebuilt in between, staging a whole app
folder, OAuth refresh against a revoked client, the access tokens a refresh leaves behind, the memory
of the password limit, the self-call header of kb-http, an upstream token that looks like a local
one, two processes that cut the same filtered copy, a document whose re-extraction fails, the access
plugin guard of the Settings page, and the deadline of the unused scan.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _kbtest import BIN

import access
import admin
import addons
import kb_core
import oauth
import plugins
import repomap
from test_build_review_round3 import SAFE_GRAPHIFY, FailingParser, exe, extract, write
from test_oauth import kb_http


def _projects(test, projects=("api", "web")):
    for name, value in (("PROJECTS", {p: [] for p in projects}), ("DOC_PROJECTS", set(projects)),
                        ("HOME_PROJECTS", [])):
        p = mock.patch.object(kb_core, name, value)
        p.start()
        test.addCleanup(p.stop)


def _graph_file(test, nodes) -> str:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = os.path.join(tmp.name, "graph.json")
    with open(path, "w") as f:
        json.dump({"directed": False, "multigraph": False, "graph": {}, "nodes": list(nodes), "links": []}, f)
    p = mock.patch.object(kb_core, "GRAPH", path)
    p.start()
    test.addCleanup(p.stop)
    kb_core._LABELS.clear()
    return path


# ------------------------------------------------------------------------------------------ 1
class LimitedCallerAlwaysFiltered(unittest.TestCase):
    """A caller with a list of projects gets that list as the view, also when it names every project
    of the graph now: the worker can load a newer graph with a new project before it answers."""

    def views(self, allowed) -> list:
        seen = []

        class Worker:
            def run(self, argv, view=None, _remember=True):
                seen.append(view)
                return "Node: X"
        with mock.patch.object(kb_core.warm, "graph", lambda: Worker()):
            kb_core.call_tool("graph_explain", {"symbol": "Zzz", "project": "all"}, allowed)
        return seen

    def setUp(self):
        _projects(self)
        _graph_file(self, [{"id": "a", "label": "A", "repo": "api"}, {"id": "w", "label": "W", "repo": "web"}])

    def test_a_list_of_every_project_still_sends_the_list(self):
        self.assertEqual(self.views({"api", "web"})[0]["allowed"], ["api", "web"])

    def test_an_all_projects_grant_uses_the_main_graph(self):
        self.assertIsNone(self.views(kb_core.AllProjects({"api", "web"}))[0]["allowed"])
        self.assertIsNone(self.views(None)[0]["allowed"])

    def test_graph_for_of_a_limited_caller_is_a_filtered_copy(self):
        with mock.patch.object(access, "graph_for", lambda allowed, known: ("cut", sorted(allowed), list(known))):
            self.assertEqual(kb_core.graph_for({"api", "web"}), ("cut", ["api", "web"], []))
        self.assertEqual(kb_core.graph_for(kb_core.AllProjects({"api"})), kb_core.GRAPH)

    def test_the_permission_webhook_star_is_an_all_projects_grant(self):
        class Answer:
            status = 200

            def __init__(self, body):
                self.body = json.dumps(body).encode()

            def read(self, _n=-1):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        auth = kb_http.Authenticator(url="https://hooks.example/p", ttl=0)
        with mock.patch.object(kb_http.urllib.request, "urlopen",
                               lambda req, timeout=None: Answer({"user": {"name": "ann"}, "projects": "*"})):
            star = auth.check("Bearer tok-a")
        with mock.patch.object(kb_http.urllib.request, "urlopen",
                               lambda req, timeout=None: Answer({"user": {"name": "bob"}, "projects": ["api", "web"]})):
            listed = auth.check("Bearer tok-b")
        self.assertIsInstance(star.projects, kb_core.AllProjects)
        self.assertNotIsInstance(listed.projects, kb_core.AllProjects)
        self.assertEqual(set(star.projects), {"api", "web"})


# ------------------------------------------------------------------------------------------ 2
class StagingAWholeAppFolder(unittest.TestCase):
    """The secret excludes match at any depth: a code folder given as `app` or `app/`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = self._tmp.name
        self.home, self.fake, self.app = os.path.join(d, "kb"), os.path.join(d, "fakebin"), os.path.join(d, "app")
        for rel in ("bootstrap/cache/config.php", ".env", ".env.local", "sub/.env.testing",
                    "sub/bootstrap/cache/services.php"):
            write(os.path.join(self.app, rel), "secret\n")
        write(os.path.join(self.app, "a.php"), "<?php\n")
        write(os.path.join(self.app, ".env.example"), "KEY=\n")
        os.makedirs(self.home)
        exe(os.path.join(self.fake, "graphify"), SAFE_GRAPHIFY)
        exe(os.path.join(self.fake, "qmd"), "#!/bin/sh\nexit 0\n")
        self.env = {**os.environ, "KB_HOME": self.home, "FAKE_LOG": os.path.join(d, "calls.log"),
                    "KB_ADDONS": "none", "PATH": self.fake + os.pathsep + os.environ["PATH"]}
        self.env.pop("KB_ROUTES_DIR", None)

    def staged(self, code: str) -> set:
        write(os.path.join(self.home, "kb.projects.toml"), f'[[project]]\nname = "one"\ncode = ["{code}"]\n')
        p = subprocess.run(["bash", os.path.join(BIN, "kb"), "graph"], env=self.env, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout[-800:] + p.stderr[-800:])
        root = os.path.join(self.home, "repos", "one", "app")
        return {os.path.relpath(os.path.join(dp, f), root) for dp, _, fs in os.walk(root) for f in fs}

    def test_without_a_trailing_slash(self):
        self.assertEqual(self.staged(self.app), {"a.php", ".env.example"})

    def test_with_a_trailing_slash(self):
        self.assertEqual(self.staged(self.app + "/"), {"a.php", ".env.example"})


# ------------------------------------------------------------------------------------------ 3 and 4
class OAuthClientRevokedMeanwhile(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.srv = oauth.Server({}, lambda t: None, "", now=lambda: self.clock[0])
        self.client = self.srv.register({"redirect_uris": ["http://127.0.0.1:5000/cb"]})
        self.cid = self.client["client_id"]

    def test_a_refresh_refused_when_the_client_is_revoked_during_the_check(self):
        tok = self.srv._issue("UPSTREAM", self.cid, "ann")
        self.srv.check_token = lambda t: self.srv.revoke(self.cid)
        with self.assertRaises(oauth.OAuthError) as e:
            self.srv.token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        # A revoked client: 401 invalid_client, so the MCP client registers again (third audit round).
        self.assertEqual((e.exception.status, e.exception.error), (401, "invalid_client"))
        self.assertEqual((self.srv.refresh, self.srv.access), ({}, {}))

    def test_a_token_of_a_removed_client_does_not_resolve(self):
        tok = self.srv._issue("UPSTREAM", self.cid, "ann")
        with self.srv.lock:
            del self.srv.clients[self.cid]     # gone without revoke(): the tokens must still stop
        self.assertIsNone(self.srv.resolve(tok["access_token"]))

    def test_revoke_drops_the_sign_ins_in_progress(self):
        q = {"client_id": self.cid, "redirect_uri": "http://127.0.0.1:5000/cb", "response_type": "code",
             "code_challenge_method": "S256", "code_challenge": "x"}
        txn = self.srv.start(q, "10.0.0.1")
        self.srv.revoke(self.cid)
        self.assertNotIn(txn, self.srv.txns)


class OAuthRefreshRetiresTheOldToken(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.srv = oauth.Server({}, lambda t: None, "", now=lambda: self.clock[0])
        self.cid = self.srv.register({"redirect_uris": ["http://127.0.0.1:5000/cb"]})["client_id"]

    def test_the_superseded_access_token_ends_after_a_short_overlap(self):
        old = self.srv._issue("UPSTREAM", self.cid, "ann")
        new = self.srv.token({"grant_type": "refresh_token", "refresh_token": old["refresh_token"]})
        self.assertEqual(self.srv.resolve(old["access_token"]), "UPSTREAM")   # the overlap
        self.clock[0] += oauth.ROTATE_OVERLAP + 1
        self.assertIsNone(self.srv.resolve(old["access_token"]))
        self.assertEqual(self.srv.resolve(new["access_token"]), "UPSTREAM")

    def test_many_refreshes_leave_one_live_token(self):
        tok = self.srv._issue("UPSTREAM", self.cid, "ann")
        for _ in range(50):
            tok = self.srv.token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        self.clock[0] += oauth.ROTATE_OVERLAP + 1
        live = [v for v in self.srv.access.values() if v["exp"] >= self.clock[0]]
        self.assertEqual(len(live), 1)
        self.assertEqual(len(self.srv.refresh), 1)

    def test_tokens_per_client_are_capped(self):
        for _ in range(oauth.TOKENS_PER_CLIENT + 10):
            self.srv._issue("UPSTREAM", self.cid, "ann")
        self.assertLessEqual(len(self.srv.access), oauth.TOKENS_PER_CLIENT)
        self.assertLessEqual(len(self.srv.refresh), oauth.TOKENS_PER_CLIENT)


# ------------------------------------------------------------------------------------------ 5
class ThrottleMemoryIsBounded(unittest.TestCase):
    def test_a_flood_of_addresses_keeps_a_bounded_map(self):
        t = admin.Throttle(5, 100, 300)
        t.MAX_IPS = 50
        for i in range(500):
            t.hit(f"10.0.{i // 256}.{i % 256}")
        self.assertLessEqual(len(t.ips), 50)
        self.assertLessEqual(len(t.all), 100)

    def test_an_address_list_never_grows_past_the_limit(self):
        t = admin.Throttle(0, 10 ** 9, 300)      # no per-address limit
        for _ in range(100):
            t.hit("10.0.0.1")
        self.assertLessEqual(len(t.ips["10.0.0.1"]), t.BUSY_PER_IP)
        self.assertLessEqual(len(t.all), 100)

    def test_expired_addresses_go_without_a_rebuild(self):
        clock = [0.0]
        t = admin.Throttle(5, 0, 10)
        with mock.patch.object(admin.time, "monotonic", lambda: clock[0]):
            for i in range(20):
                t.hit(f"a{i}")
            clock[0] = 100.0
            t.hit("late")
        self.assertEqual(list(t.ips), ["late"])

    def test_the_limits_still_hold(self):
        t = admin.Throttle(3, 0, 300)
        self.assertEqual([t.hit("x") is None for _ in range(4)], [True, True, True, False])


# ------------------------------------------------------------------------------------------ 6
class SelfCallMatchesTheBoundPort(unittest.TestCase):
    def test_another_loopback_port_is_not_this_server(self):
        other = f"http://127.0.0.1:{kb_http.PORT + 1}/local/permissions"
        self.assertEqual(kb_http.self_headers(other), {})
        self.assertIsNone(kb_http.refuse_reason({"KB_SERVER": "on", "KB_PERMISSION_URL": other}, access=None))

    def test_the_configured_and_the_bound_port_are_this_server(self):
        self.assertIn(kb_http.SELF_HEADER, kb_http.self_headers(f"http://127.0.0.1:{kb_http.PORT}/local/login"))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(kb_core, "KB", tmp.name):
            srv = kb_http.make_server("127.0.0.1", 0, kb_http.Authenticator(url="http://127.0.0.1:1/x", ttl=0),
                                      None, oauth_env={}, admins=set(), access=False)
        self.addCleanup(admin.set_access, admin.UNSET)
        self.addCleanup(srv.server_close)
        port = srv.server_address[1]
        self.assertIn(kb_http.SELF_HEADER, kb_http.self_headers(f"http://localhost:{port}/local/permissions"))


# ------------------------------------------------------------------------------------------ 7
class UpstreamTokenThatLooksLocal(unittest.TestCase):
    def test_refresh_checks_an_upstream_kbo_token_directly(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        auth = kb_http.Authenticator(url="http://127.0.0.1:1/x", ttl=0)
        with mock.patch.object(kb_core, "KB", tmp.name):
            srv = kb_http.make_server("127.0.0.1", 0, auth, None, oauth_env={"KB_LOGIN_URL": "http://127.0.0.1:1/l"},
                                      admins=set(), access=False)
        self.addCleanup(admin.set_access, admin.UNSET)
        self.addCleanup(srv.server_close)
        o = srv.RequestHandlerClass.oauth
        cid = o.register({"redirect_uris": ["http://127.0.0.1:5000/cb"]})["client_id"]
        tok = o._issue(oauth.ACCESS_PREFIX + "upstream", cid, "ann")
        seen = []
        with mock.patch.object(auth, "_ask", side_effect=lambda t: seen.append(t) or
                               kb_http.Identity({"name": "ann"}, ["api"])):
            new = o.token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
            self.assertEqual(auth.check(f"Bearer {new['access_token']}").name, "ann")
        self.assertEqual(seen[0], oauth.ACCESS_PREFIX + "upstream")


# ------------------------------------------------------------------------------------------ 8
class TwoCutsOfOneCopy(unittest.TestCase):
    def test_a_second_cut_during_the_first_does_not_break_it(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "graph.json")
            with open(src, "w") as f:
                f.write("{}")
            path = os.path.join(d, "filtered", "v-x", "graph.json")

            def argv(tmp):
                return [sys.executable, "-c", f"open({tmp!r}, 'w').write('{{}}')"]
            real, nested = access.json.dump, []

            def dump(obj, f, *a, **k):
                if not nested:      # another process cuts the same copy while this one writes
                    nested.append(1)
                    access._cut(argv, src, path, "test")
                return real(obj, f, *a, **k)
            with mock.patch.object(access.json, "dump", dump):
                access._cut(argv, src, path, "test")
            self.assertTrue(access._fresh(path, src))
            left = sorted(os.listdir(os.path.dirname(path)))
            self.assertEqual([n for n in left if n.endswith(".tmp")], [])


# ------------------------------------------------------------------------------------------ 9
class FailedReExtractionDropsTheOldText(unittest.TestCase):
    def test_the_stale_text_goes_when_the_changed_file_fails(self):
        class Good(FailingParser):
            __name__ = "parsers.failing"

            @classmethod
            def extract(cls, path: Path) -> str:
                return "---\nextracted_at: x\n---\nold text\n"
        with tempfile.TemporaryDirectory() as d:
            root, stage = Path(d, "docs"), Path(d, "stage")
            root.mkdir()
            pdf = root / "deck.pdf"
            pdf.write_bytes(b"%PDF one")

            def run(parser):
                with mock.patch("sys.stdin", io.StringIO(f"{pdf}\0")), mock.patch("sys.stdout", io.StringIO()), \
                        mock.patch("sys.stderr", io.StringIO()), \
                        mock.patch.object(extract.parsers, "runnable", return_value=[parser]), \
                        mock.patch.object(extract.parsers, "with_provenance", lambda text, p, m: text):
                    extract.main(["--into", str(stage), "--root", str(root), "--prune", "--from0"])
            run(Good)
            self.assertTrue((stage / "deck.pdf.md").is_file())
            pdf.write_bytes(b"%PDF two, longer")
            run(FailingParser)
            self.assertFalse((stage / "deck.pdf.md").exists())
            entry = json.loads((stage / extract.MANIFEST).read_text())["deck.pdf.md"]
            self.assertEqual((entry["size"], entry.get("failed")), (pdf.stat().st_size, "broken file"))


# ------------------------------------------------------------------------------------------ 10
class AccessPluginGuardByName(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"KB_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("KB_ADDONS", None)
        addons._cache = {}
        self.addCleanup(admin.set_access, admin.UNSET)

    def plugin(self, name: str, src: str, extra: dict | None = None) -> str:
        folder = os.path.join(self.tmp.name, "plugins", name)
        os.makedirs(folder, exist_ok=True)
        for fn, text in {"plugin.py": src, **(extra or {})}.items():
            with open(os.path.join(folder, fn), "w", encoding="utf-8") as f:
                f.write(text)
        return folder

    def enable(self, names) -> None:
        with open(addons.state_file(), "w", encoding="utf-8") as f:
            json.dump(names, f)

    def test_the_started_plugin_stays_on_whatever_its_source_says(self):
        impl = "def login(body, core):\n    return 401, {}\n\ndef permissions(token, projects, core):\n    return 401, {}\n"
        folder = self.plugin("sso", "NAME = 'single-sign-on'\nfrom sso_impl import login, permissions\n",
                             {"sso_impl.py": impl})
        self.enable(["sso"])
        mod = plugins.load("sso", folder)
        self.addCleanup(plugins._loaded.pop, "sso", None)
        admin.set_access(mod)
        status, note, _ = admin.change_setting({"name": "sso", "on": "0"})
        self.assertEqual(status, 409, note)
        with open(addons.state_file(), encoding="utf-8") as f:
            self.assertEqual(json.load(f), ["sso"])

    def test_explicit_metadata_wins_over_the_source_text(self):
        self.plugin("flagged", "ACCESS = True\nfrom elsewhere import login, permissions\n")
        self.plugin("helper", "ACCESS = False\ndef login(body, core):\n    pass\n\ndef permissions(t, p, c):\n    pass\n")
        self.assertTrue(plugins.is_access("flagged"))
        self.assertFalse(plugins.is_access("helper"))

    def test_a_flagged_second_access_plugin_is_refused(self):
        folder = self.plugin("users", "ACCESS = True\ndef login(b, c):\n    pass\n\ndef permissions(t, p, c):\n    pass\n")
        self.plugin("flagged", "ACCESS = True\nfrom elsewhere import login, permissions\n")
        self.enable(["users"])
        mod = plugins.load("users-r4", folder)
        self.addCleanup(plugins._loaded.pop, "users-r4", None)
        admin.set_access(mod)
        self.assertEqual(admin.change_setting({"name": "flagged", "on": "1"})[0], 409)


# ------------------------------------------------------------------------------------------ 11
class UnusedScanDeadlineInsideADirectory(unittest.TestCase):
    def test_one_flat_directory_stops_at_the_deadline(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(20):
                with open(os.path.join(d, f"f{i}.php"), "w") as f:
                    f.write("<?php\n")
            ticks = iter(range(10 ** 6))
            with mock.patch("time.monotonic", lambda: float(next(ticks))):
                _, complete = repomap.scan_names({"api:a.php": ["Lonely"]}, {"api": d}, 5.0)
            self.assertFalse(complete)

    def test_a_scan_in_time_is_complete(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "b.php"), "w") as f:
                f.write("<?php new Lonely();\n")
            left, complete = repomap.scan_names({"api:a.php": ["Lonely"]}, {"api": d}, time.monotonic() + 60)
        self.assertEqual((left, complete), ({}, True))


if __name__ == "__main__":
    unittest.main()
