#!/usr/bin/env python3
"""The Settings page of /admin (bin/admin.py, bin/kb-http, bin/kb_api.py): add-on switches and the
settings of bin/settings.py. Also: the admin pages are part of the core, not an add-on."""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import subprocess
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from _kbtest import BIN

import addons
import kb_api
import kb_core
import plugins
import settings
from test_oauth import FakeApi, kb_http  # the fake webhooks: user ann, password, then a code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def serve(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def member_plugin():
    """An access plugin that makes everybody a member with one own page."""
    return types.SimpleNamespace(NAME="fake", TABS=[("Mine", "/me", "member")],
                                 role=lambda token, core: "member",
                                 page=lambda req, core: (200, "Mine", "<main>mine</main>"))


class SettingsPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = serve(ThreadingHTTPServer(("127.0.0.1", 0), FakeApi))
        api = f"http://127.0.0.1:{cls.api.server_address[1]}"
        cls.tmp = tempfile.TemporaryDirectory()
        env = {"KB_LOGIN_URL": f"{api}/login", "KB_HOOK_HEADERS": '{"app":"dev"}'}
        cls.patches = [mock.patch.object(kb_core, "KB", cls.tmp.name),
                       mock.patch.object(kb_core, "GRAPH", os.path.join(cls.tmp.name, "graph.json")),
                       mock.patch.object(kb_core, "PROJECTS", {"api": [], "web": []}),
                       mock.patch.object(kb_core, "warm_qmd", lambda: None)]
        for p in cls.patches:
            p.start()
        auth = kb_http.Authenticator(url=f"{api}/permissions", ttl=0)
        cls.srv = serve(kb_http.make_server("127.0.0.1", 0, auth, None, oauth_env=env, admins={"ann"},
                                            access=False))
        cls.members = serve(kb_http.make_server("127.0.0.1", 0, auth, None, oauth_env=env, admins=set(),
                                                access=member_plugin()))
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.member_base = f"http://127.0.0.1:{cls.members.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        for s in (cls.srv, cls.members, cls.api):
            s.shutdown()
            s.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.addons_file = os.path.join(self.dir.name, "addons.json")
        self.settings_file = os.path.join(self.dir.name, "settings.json")
        for p in (mock.patch.object(addons, "state_file", lambda: self.addons_file),
                  mock.patch.object(settings, "state_file", lambda: self.settings_file),
                  mock.patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)
        for k in ("KB_ADDONS", "KB_CONTRACTS"):
            os.environ.pop(k, None)
        addons._cache.clear()
        settings._cache.clear()
        self.audit = []
        p = mock.patch.object(kb_http, "_audit", lambda user, action, detail="": self.audit.append((user, action, detail)))
        p.start()
        self.addCleanup(p.stop)

    # ---------------------------------------------------------------- helpers
    def client(self):
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect)

    def req(self, opener, method, url, form=None):
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        if data:
            r.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with opener.open(r, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def sign_in(self, base):
        c = self.client()
        _, page = self.req(c, "POST", f"{base}/admin/login", {"username": "ann", "password": "pw"})
        lid = re.search(r'name="lid" value="([^"]+)"', page).group(1)
        self.req(c, "POST", f"{base}/admin/login", {"lid": lid, "code": "123456"})
        return c

    def csrf(self, c):
        _, page = self.req(c, "GET", f"{self.base}/admin/settings")
        return re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def addons_on_file(self):
        with open(self.addons_file) as f:
            return set(json.load(f))

    # ---------------------------------------------------------------- tests
    def test_admin_sees_the_page(self):
        c = self.sign_in(self.base)
        status, page = self.req(c, "GET", f"{self.base}/admin/settings")
        self.assertEqual(status, 200)
        for word in ("Add-ons", "code-mode", "tool-multicall", "audit", "contracts", "KB_CONTRACTS",
                     'class="on">Settings<'):
            self.assertIn(word, page)
        self.assertNotIn("<script", page)
        self.assertNotIn(">admin<", page)                       # admin is no add-on any more
        _, home = self.req(c, "GET", f"{self.base}/admin")
        self.assertIn("/admin/settings", home)                  # a tab on every page

    def test_member_gets_403(self):
        c = self.sign_in(self.member_base)
        status, page = self.req(c, "GET", f"{self.member_base}/admin/settings")
        self.assertEqual(status, 403)
        self.assertNotIn("code-mode", page)
        _, mine = self.req(c, "GET", f"{self.member_base}/admin/me")
        token = re.search(r'name="csrf" value="([^"]+)"', mine).group(1)
        status, _ = self.req(c, "POST", f"{self.member_base}/admin/settings/addon",
                             {"csrf": token, "name": "metrics", "on": "1"})
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(self.addons_file))

    def test_a_form_needs_the_csrf_token(self):
        c = self.sign_in(self.base)
        status, _ = self.req(c, "POST", f"{self.base}/admin/settings/addon", {"csrf": "forged", "name": "metrics", "on": "1"})
        self.assertEqual(status, 403)
        status, _ = self.req(c, "POST", f"{self.base}/admin/settings/set", {"name": "contracts", "value": "own"})
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(self.addons_file))
        self.assertFalse(os.path.exists(self.settings_file))

    def test_a_switch_writes_addons_json(self):
        c = self.sign_in(self.base)
        token = self.csrf(c)
        status, page = self.req(c, "POST", f"{self.base}/admin/settings/addon", {"csrf": token, "name": "metrics", "on": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(self.addons_on_file(), {"metrics"})
        self.assertIn(("ann", "add-on on", "metrics"), self.audit)
        self.req(c, "POST", f"{self.base}/admin/settings/addon", {"csrf": token, "name": "metrics", "on": "0"})
        self.assertEqual(self.addons_on_file(), set())
        status, _ = self.req(c, "POST", f"{self.base}/admin/settings/addon", {"csrf": token, "name": "nope", "on": "1"})
        self.assertEqual(status, 400)
        self.assertEqual(self.addons_on_file(), set())

    def test_a_setting_is_saved(self):
        c = self.sign_in(self.base)
        status, page = self.req(c, "POST", f"{self.base}/admin/settings/set",
                                {"csrf": self.csrf(c), "name": "contracts", "value": "own"})
        self.assertEqual(status, 200)
        self.assertEqual(settings.get("contracts"), "own")
        self.assertIn(("ann", "setting", "contracts = own"), self.audit)
        status, _ = self.req(c, "POST", f"{self.base}/admin/settings/set",
                             {"csrf": self.csrf(c), "name": "contracts", "value": "bogus"})
        self.assertEqual(status, 400)
        self.assertEqual(settings.get("contracts"), "own")

    def test_env_locked_values_cannot_be_changed(self):
        os.environ["KB_CONTRACTS"] = "all"
        os.environ["KB_ADDONS"] = "metrics"
        c = self.sign_in(self.base)
        _, page = self.req(c, "GET", f"{self.base}/admin/settings")
        self.assertIn("set by KB_CONTRACTS", page)
        self.assertIn("KB_ADDONS", page)
        token = self.csrf(c)
        status, _ = self.req(c, "POST", f"{self.base}/admin/settings/set", {"csrf": token, "name": "contracts", "value": "own"})
        self.assertEqual(status, 409)
        status, _ = self.req(c, "POST", f"{self.base}/admin/settings/addon", {"csrf": token, "name": "audit", "on": "1"})
        self.assertEqual(status, 409)
        self.assertFalse(os.path.exists(self.settings_file))
        self.assertFalse(os.path.exists(self.addons_file))
        self.assertEqual(settings.get("contracts"), "all")

    def test_admin_pages_are_core(self):
        os.environ["KB_ADDONS"] = "none"
        c = self.sign_in(self.base)
        status, _ = self.req(c, "GET", f"{self.base}/admin")
        self.assertEqual(status, 200)

    def test_an_old_addons_json_with_admin_still_works(self):
        with open(self.addons_file, "w") as f:
            json.dump(["admin", "metrics"], f)
        self.assertEqual(addons.enabled(), {"metrics"})
        self.assertNotIn("admin", addons.known())
        c = self.sign_in(self.base)
        self.assertEqual(self.req(c, "GET", f"{self.base}/admin")[0], 200)


class RestartAndPieces(unittest.TestCase):
    def test_which_switches_need_a_restart(self):
        mod = types.SimpleNamespace(login=lambda *a: None, permissions=lambda *a: None)
        with mock.patch.object(plugins, "installed", lambda: {"users": "/x/users"}), \
                mock.patch.object(plugins, "load", lambda name, path: mod):
            self.assertTrue(addons.needs_restart("users"))
            self.assertIn("users", addons.known())
        self.assertTrue(addons.needs_restart("fast-routes"))
        self.assertFalse(addons.needs_restart("metrics"))
        self.assertFalse(addons.needs_restart("audit"))

    def test_the_page_marks_the_access_plugin(self):
        import admin
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "users"))
            with open(os.path.join(d, "users", "plugin.py"), "w") as f:
                f.write("raise SystemExit('the page must not load me')\n"
                        "def login(body, core):\n    pass\ndef permissions(token, projects, core):\n    pass\n")
            with mock.patch.object(plugins, "installed", lambda: {"users": os.path.join(d, "users")}), \
                    mock.patch.dict(os.environ, {"KB_ADDONS": "none"}):
                page = admin.settings_page("", "tok").decode()
        self.assertIn("<code>users</code>", page)
        self.assertIn("an access plugin: switched only on the server (kb addon on|off), then a restart", page)
        self.assertIn("takes effect after a restart", page.split("<code>fast-routes</code>")[1].split("</tr>")[0])

    def test_code_mode_without_its_sandbox_misses_a_piece(self):
        with mock.patch.dict(os.environ, {"KB_SANDBOX_SOCKET": ""}), \
                mock.patch.object(addons, "_sandbox_dirs", lambda: ["/nonexistent"]):
            self.assertIn("sandbox", addons.missing("code-mode"))
        with mock.patch.dict(os.environ, {"KB_SANDBOX_SOCKET": "/run/sb.sock"}):
            self.assertIsNone(addons.missing("code-mode"))
        self.assertIsNone(addons.missing("metrics"))


class LocalPages(unittest.TestCase):
    """kb-api: the admin pages need no add-on; loopback without a token is still the rule."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for p in (mock.patch.dict(os.environ, {"KB_ADDONS": "none"}),
                  mock.patch.object(settings, "state_file", lambda: os.path.join(self.dir.name, "settings.json"))):
            p.start()
            self.addCleanup(p.stop)
        settings._cache.clear()

    def get(self, srv, path, data=None):
        url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
        r = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_local_admin_without_the_addon(self):
        srv = serve(ThreadingHTTPServer(("127.0.0.1", 0), kb_api.make_handler("127.0.0.1", None)))
        try:
            self.assertEqual(self.get(srv, "/admin")[0], 200)
            status, page = self.get(srv, "/admin/settings")
            self.assertEqual(status, 200)
            self.assertIn("contracts", page)
            form = urllib.parse.urlencode({"csrf": kb_api.CSRF, "name": "contracts", "value": "own"}).encode()
            self.assertEqual(self.get(srv, "/admin/settings/set", form)[0], 200)
            self.assertEqual(settings.get("contracts"), "own")
        finally:
            srv.shutdown()
            srv.server_close()
        srv = serve(ThreadingHTTPServer(("127.0.0.1", 0), kb_api.make_handler("127.0.0.1", "sekret")))
        try:
            self.assertEqual(self.get(srv, "/admin")[0], 404)       # with a token: no local admin pages
        finally:
            srv.shutdown()
            srv.server_close()


class AddonCommand(unittest.TestCase):
    def test_install_and_remove_admin_say_it_is_core(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "KB_HOME": home}
            env.pop("KB_ADDONS", None)
            for action in ("install", "remove"):
                p = subprocess.run(["bash", os.path.join(BIN, "kb"), "addon", action, "admin"],
                                   capture_output=True, text=True, env=env, timeout=60)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("part of the core", p.stdout + p.stderr)
            self.assertFalse(os.path.exists(os.path.join(home, "addons.json")))


if __name__ == "__main__":
    unittest.main()
