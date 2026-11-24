"""An access plugin: a plugin that answers the two webhooks of the access protocol itself, so kb-http
needs no separate webhook service. Covers the slot in the core (bin/plugins.py, bin/kb-http)."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from _kbtest import BIN  # noqa: F401

import kb_core
import plugins
from test_oauth import kb_http

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeAccess:
    """The smallest access plugin: one user, one token."""
    NAME = "fake-access"

    @staticmethod
    def login(body, core):
        if body.get("username") == "ann" and body.get("password") == "pw":
            return 200, {"token": "tok-ann"}
        if body.get("username") == "slow":
            return 429, {"message": "too many attempts"}, {"Retry-After": "7"}
        return 401, {"message": "refused"}

    @staticmethod
    def permissions(token, projects, core):
        if token == "boom":
            raise RuntimeError("the plugin broke")
        if token != "tok-ann":
            return 401, {"message": "unknown"}
        return 200, {"user": {"name": "ann"}, "projects": [p for p in projects if p == "api"]}


def post(url, body, token=None):
    r = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                               headers={"Content-Type": "application/json"})
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"null"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), dict(e.headers)


class Slot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patches = [mock.patch.object(kb_core, "KB", cls.tmp.name),
                       mock.patch.object(kb_core, "PROJECTS", {"api": [], "web": []}),
                       mock.patch.object(kb_core, "warm_qmd", lambda: None),
                       mock.patch.dict(os.environ, {"KB_ADDONS": "none"})]
        for p in cls.patches:
            p.start()
        auth = kb_http.Authenticator(url="http://127.0.0.1:1/unset", ttl=0)
        env = {"KB_LOGIN_URL": "http://127.0.0.1:1/unset"}
        cls.srv = kb_http.make_server("127.0.0.1", 0, auth, None, oauth_env=env, admins=set(), access=FakeAccess)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        auth.url = f"{cls.base}/local/permissions"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.plain = kb_http.make_server("127.0.0.1", 0, kb_http.Authenticator(url="http://127.0.0.1:1/x", ttl=0),
                                        None, oauth_env=env, admins=set(), access=False)
        cls.plain_base = f"http://127.0.0.1:{cls.plain.server_address[1]}"
        threading.Thread(target=cls.plain.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for s in (cls.srv, cls.plain):
            s.shutdown()
            s.server_close()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def test_the_plugin_answers_the_login_webhook(self):
        self.assertEqual(post(f"{self.base}/local/login", {"username": "ann", "password": "pw"})[:2],
                         (200, {"token": "tok-ann"}))
        self.assertEqual(post(f"{self.base}/local/login", {"username": "ann", "password": "no"})[0], 401)
        status, _, headers = post(f"{self.base}/local/login", {"username": "slow", "password": "x"})
        self.assertEqual((status, headers.get("Retry-After")), (429, "7"))

    def test_the_plugin_answers_the_permission_webhook_and_mcp_uses_it(self):
        # the permission webhook answers only this server; from the network it is closed
        self.assertEqual(post(f"{self.base}/local/permissions", {"projects": ["api", "web"]}, "tok-ann")[0], 403)
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        self.assertEqual(post(f"{self.base}/mcp", init, "tok-ann")[0], 200)
        self.assertEqual(post(f"{self.base}/mcp", init, "other")[0], 401)

    def test_a_broken_plugin_is_an_outage_not_an_open_door(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        self.assertEqual(post(f"{self.base}/mcp", init, "boom")[0], 503)   # an outage, still no door

    def test_without_a_plugin_and_without_own_users_the_addresses_do_not_exist(self):
        self.assertEqual(post(f"{self.plain_base}/local/login", {"username": "ann", "password": "pw"})[0], 404)

    def test_the_loader_finds_only_a_plugin_with_both_functions(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        for name, code in (("tools-only", "NAME='tools-only'\nTOOLS=[]\n"),
                           ("half", "NAME='half'\ndef login(body, core):\n    return 200, {}\n"),
                           ("door", "NAME='door'\ndef login(body, core):\n    return 200, {}\n"
                                    "def permissions(token, projects, core):\n    return 200, {}\n")):
            os.makedirs(os.path.join(home.name, name))
            with open(os.path.join(home.name, name, "plugin.py"), "w") as f:
                f.write(code)
        with mock.patch.object(plugins, "folder", lambda: home.name), mock.patch.dict(plugins._loaded, clear=True):
            self.assertIsNone(plugins.access({"tools-only", "half"}))
            self.assertIsNone(plugins.access(set()))
            self.assertEqual(plugins.access({"tools-only", "half", "door"}).NAME, "door")


if __name__ == "__main__":
    unittest.main()
