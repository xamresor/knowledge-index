"""Third audit round, the server side: the users store under a broken disk, OAuth after a revoke, sign-in
quotas, `kb addon on|off`, in-process self-calls, a closed /local/permissions, admin pages without OAuth,
the scrypt cap, forget() against an answer in flight, client snapshots, "*" on an empty server, sessions
that a save keeps, reset links out of the access log, file locks, admin fields and the settings generation."""
from __future__ import annotations

import http.cookiejar
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from _kbtest import BIN

import admin
import addons
import kb_api
import kb_core
import oauth
import settings
from test_oauth import kb_http

_spec = importlib.util.spec_from_file_location(
    "kb_plugin_users_audit3", os.path.join(os.path.dirname(BIN), "contrib", "users", "plugin.py"))
users = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(users)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeAccess:
    NAME = "fake-access"
    calls: list = []

    @classmethod
    def login(cls, body, core):
        cls.calls.append(("login", body.get("ip"), body.get("purpose")))
        if body.get("username") == "ann" and body.get("password") == "pw":
            return 200, {"token": "tok-ann"}
        return 401, {"message": "refused"}

    @classmethod
    def permissions(cls, token, projects, core):
        cls.calls.append(("permissions", token))
        if token == "tok-all":
            return 200, {"user": {"name": "all"}, "projects": "*"}
        if token != "tok-ann":
            return 401, {"message": "unknown"}
        return 200, {"user": {"uname": "ann"}, "projects": [p for p in projects if p == "api"]}


def post(url, body, token=None, headers=None):
    r = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                               headers={"Content-Type": "application/json", **(headers or {})})
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


class StoreTmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = users.Store(os.path.join(self.tmp.name, "users.json"), now=self.clock)


class TouchOnBrokenDisk(StoreTmp):
    """Finding 1: a "last used" write that fails must not refuse every token."""

    def test_a_failed_touch_still_answers_200(self):
        key = self.store.add("ann", ["api"])
        self.clock.t += users.TOUCH_SECONDS + 1
        with mock.patch.object(self.store, "_save", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(self.store.permissions(key, ["api"])[0], 200)
            self.assertEqual(self.store.permissions(key, ["api"])[0], 200)

    def test_a_failed_sign_in_save_is_a_clear_503(self):
        self.store.add("ann", ["api"], key=False)
        self.store.set_password("ann", "one long password")
        with mock.patch.object(users, "store", lambda core=None: self.store), \
                mock.patch.object(self.store, "_save", side_effect=OSError(30, "Read-only file system")):
            status, body = users.login({"username": "ann", "password": "one long password"}, None)[:2]
        self.assertEqual(status, 503)
        self.assertIn("cannot be saved", body["message"])


class SignInQuotas(StoreTmp):
    """Finding 3: page sign-ins have a quota of their own and end with their session."""

    def test_page_sign_ins_do_not_push_out_an_agent(self):
        self.store.add("ann", ["api"], key=False)
        self.store.set_password("ann", "one long password")
        agent = self.store.login("ann", "one long password", "1.1.1.1")
        for _ in range(users.PAGE_KEEP + 5):
            self.clock.t += 1
            self.store.login("ann", "one long password", "1.1.1.1", purpose="page")
        self.assertEqual(self.store.permissions(agent, ["api"])[0], 200)

    def test_end_signin_ends_a_sign_in_token_but_never_a_key(self):
        key = self.store.add("ann", ["api"])
        self.store.set_password("ann", "one long password")
        page = self.store.login("ann", "one long password", "1.1.1.1", purpose="page")
        self.assertTrue(self.store.end_signin(page))
        self.assertEqual(self.store.permissions(page, ["api"])[0], 401)
        self.assertFalse(self.store.end_signin(key))
        self.assertEqual(self.store.permissions(key, ["api"])[0], 200)

    def test_a_session_that_ends_calls_on_end(self):
        ended = []
        s = admin.Sessions(ttl=0.0)
        s.on_end = ended.append
        sid = s.create({"name": "ann"}, "tok-1")
        s.drop(sid)
        s.create({"name": "ann"}, "tok-2")
        s.get("nothing")   # expires tok-2 (ttl 0)
        self.assertEqual(ended, ["tok-1", "tok-2"])


class ScryptCap(StoreTmp):
    """Finding 9: at most a few scrypt hashes at once; one more is a 503, not more memory."""

    def test_no_free_hash_slot_is_503(self):
        self.store.add("ann", ["api"], key=False)
        with mock.patch.object(users, "HASH_SLOTS", threading.BoundedSemaphore(1)), \
                mock.patch.object(users, "HASH_WAIT", 0.01), \
                mock.patch.object(users, "store", lambda core=None: self.store):
            users.HASH_SLOTS.acquire()
            out = users.login({"username": "ann", "password": "one long password"}, None)
        self.assertEqual(out[0], 503)


class SaveKeepsSessions(StoreTmp):
    """Finding 13: "New key" and a save that takes nothing away keep the person signed in."""

    def req(self, path, form):
        dropped = []
        r = {"method": "POST", "path": path, "form": form, "base": "", "csrf": "c", "user": "boss",
             "admin": True, "now": 0, "forget": lambda: None, "drop_sessions": dropped.append,
             "audit": lambda *a: None}
        core = mock.Mock(PROJECTS={"api": [], "web": []}, KB=self.tmp.name)
        with mock.patch.object(users, "store", lambda core=None: self.store), \
                mock.patch.object(users, "mail_config", lambda core=None: {}):
            users.page(r, core)
        return dropped

    def test_only_a_loss_ends_the_sessions(self):
        self.store.add("boss", "*", admin=True)
        self.store.add("ann", ["api", "web"])
        self.assertEqual(self.req("/users/key", {"name": "ann"}), [])
        same = {"name": "ann", "pick": "1", "p:api": "on", "p:web": "on"}
        self.assertEqual(self.req("/users/save", same), [])
        self.assertEqual(self.req("/users/save", {**same, "p:web": ""}), ["ann"])   # a project gone
        self.assertEqual(self.req("/users/save", {"name": "ann", "pick": "1", "p:api": "on", "disabled": "on"}),
                         ["ann"])


class ResetLinkFragment(StoreTmp):
    """Finding 14: the one-time value travels in the fragment, which no access log sees."""

    def test_the_link_has_the_value_in_the_fragment_and_the_page_reads_it(self):
        self.store.add("kate@example.com", ["api"], key=False)
        sent = []
        cfg = {"KB_SMTP_HOST": "h", "KB_SMTP_FROM": "f"}
        with mock.patch.object(users, "send_mail", lambda to, subj, text, cfg: sent.append(text)):
            users.send_link(self.store, "kate@example.com", "https://kb.test", cfg)
        self.assertIn("https://kb.test/admin/reset#t=", sent[0])
        self.assertNotIn("?t=", sent[0])
        value = sent[0].split("#t=")[1].split()[0]
        req = {"method": "GET", "path": "/reset", "form": {}, "query": {}, "base": "", "audit": lambda *a: None}
        with mock.patch.object(users, "store", lambda core=None: self.store), \
                mock.patch.object(users, "mail_config", lambda core=None: cfg):
            _, _, body = users.public_page(req, None)
            self.assertIn(admin.FRAGMENT_SCRIPT, body)
            post_req = {**req, "method": "POST", "form": {"t": value, "password": "one long password",
                                                           "again": "one long password"}}
            _, _, body = users.public_page(post_req, None)
        self.assertIn("The password is set", body)

    def test_the_csp_allows_exactly_that_script(self):
        self.assertTrue(admin.script_hash().startswith("'sha256-"))


class OAuthRevoked(unittest.TestCase):
    """Finding 2: a revoked (or evicted) client gets 401 invalid_client, so the MCP SDK registers again."""

    def test_revoke_then_sign_in_again(self):
        srv = oauth.Server({"KB_LOGIN_URL": "x"}, lambda t: None, "")
        client = srv.register({"redirect_uris": ["http://localhost:1/cb"]})
        tok = srv._issue("api-token", client["client_id"], "ann")
        self.assertTrue(srv.revoke(client["client_id"]))
        with self.assertRaises(oauth.OAuthError) as e:
            srv.token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                       "client_id": client["client_id"]})
        self.assertEqual((e.exception.status, e.exception.error), (401, "invalid_client"))
        with self.assertRaises(oauth.OAuthError) as e:
            srv.token({"grant_type": "authorization_code", "code": "x", "client_id": client["client_id"]})
        self.assertEqual(e.exception.error, "invalid_client")
        again = srv.register({"redirect_uris": ["http://localhost:1/cb"]})   # the SDK registers again
        self.assertIn("refresh_token", srv._issue("api-token", again["client_id"], "ann"))

    def test_clients_snapshot_is_a_copy(self):
        srv = oauth.Server({"KB_LOGIN_URL": "x"}, lambda t: None, "")
        c = srv.register({"redirect_uris": ["http://localhost:1/cb"]})
        snap = srv.clients_snapshot()
        snap[c["client_id"]]["users"] = {"x": 1}
        self.assertNotIn("users", srv.clients[c["client_id"]])


class Authenticator(unittest.TestCase):
    def setUp(self):
        FakeAccess.calls = []
        p = mock.patch.object(kb_core, "PROJECTS", {"api": [], "web": []})
        p.start()
        self.addCleanup(p.stop)

    def test_a_public_url_of_this_server_is_called_in_process(self):
        """Finding 5: no HTTP call, so no shared IP of the server and no second connection."""
        with mock.patch.dict(os.environ, {"KB_PUBLIC_URL": "https://kb.example.com/kb"}):
            self.assertTrue(kb_http.is_self_url("https://kb.example.com/kb/local/permissions"))
            self.assertFalse(kb_http.is_self_url("https://other.example.com/kb/local/permissions"))
            auth = kb_http.Authenticator(url="https://kb.example.com/kb/local/permissions", ttl=0)
            auth.access = FakeAccess
            with mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("no HTTP")):
                self.assertEqual(auth.check_upstream("tok-ann").projects, {"api"})
                data = kb_http.webhook_login(FakeAccess, "https://kb.example.com/kb/local/login",
                                             {"username": "ann", "password": "pw"}, {}, "9.9.9.9", "page")
        self.assertEqual(data, {"token": "tok-ann"})
        self.assertIn(("login", "9.9.9.9", "page"), FakeAccess.calls)
        with self.assertRaises(oauth.kb_login.LoginError):
            kb_http.plugin_login(FakeAccess, {"username": "ann", "password": "no"}, "1.1.1.1")

    def test_a_forget_during_a_check_keeps_the_answer_out_of_the_cache(self):
        """Finding 10."""
        auth = kb_http.Authenticator(url="http://127.0.0.1:1/x", ttl=60)

        def ask(token):
            auth.forget()   # an admin revoked the token while the webhook answered
            return kb_http.Identity({"name": "ann"}, ["api"])
        with mock.patch.object(auth, "_ask", side_effect=ask) as asked:
            auth.check_upstream("tok-ann")
            auth.check_upstream("tok-ann")
        self.assertEqual(asked.call_count, 2)

    def test_every_project_on_a_server_without_projects_still_signs_in(self):
        """Finding 12."""
        auth = kb_http.Authenticator(url="http://127.0.0.1:1/local/permissions", ttl=0)
        auth.access = FakeAccess
        with mock.patch.object(kb_core, "PROJECTS", {}), mock.patch.object(kb_http, "is_self_url", lambda u: True):
            ident = auth.check_upstream("tok-all")
        self.assertIsInstance(ident.projects, kb_core.AllProjects)


class Server(unittest.TestCase):
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
        cls.srv = kb_http.make_server("127.0.0.1", 0, auth, None, oauth_env=env, admins={"ann"}, access=FakeAccess)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        auth.url = f"{cls.base}/local/permissions"
        env["KB_LOGIN_URL"] = f"{cls.base}/local/login"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def test_permissions_webhook_is_closed_to_the_network(self):
        """Finding 6: only this server may ask it; /local/login stays public."""
        self.assertEqual(post(f"{self.base}/local/permissions", {"projects": ["api"]}, "tok-ann")[0], 403)
        self.assertEqual(post(f"{self.base}/local/permissions", {"projects": ["api"]}, "tok-ann",
                              {kb_http.SELF_HEADER: kb_http.SELF_SECRET})[0], 200)
        self.assertEqual(post(f"{self.base}/local/login", {"username": "ann", "password": "pw"})[0], 200)
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        self.assertEqual(post(f"{self.base}/mcp", init, "tok-ann")[0], 200)   # in-process check

    def test_admin_sign_in_runs_in_process_with_the_page_purpose(self):
        FakeAccess.calls = []
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        r = urllib.request.Request(self.base + "/admin/login", method="POST",
                                   data=urllib.parse.urlencode({"username": "ann", "password": "pw"}).encode())
        with opener.open(r, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
        self.assertIn(("login", "127.0.0.1", "page"), FakeAccess.calls)

    def test_listen_backlog_is_not_five(self):
        """Finding 8."""
        self.assertGreaterEqual(self.srv.request_queue_size, 64)


class AdminWithoutOAuth(unittest.TestCase):
    """Finding 7: KB_OAUTH=off ends the browser sign-in of MCP clients, not the admin pages."""

    def test_admin_login_page_stays(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(kb_core, "KB", tmp), mock.patch.object(kb_core, "warm_qmd", lambda: None), \
                mock.patch.dict(os.environ, {"KB_OAUTH": "off", "KB_LOGIN_URL": "http://127.0.0.1:1/login",
                                             "KB_ADDONS": "none"}):
            srv = kb_http.make_server("127.0.0.1", 0, kb_http.Authenticator(url="http://127.0.0.1:1/p"), None,
                                      admins={"ann"}, access=False)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{srv.server_address[1]}"
                with urllib.request.urlopen(base + "/admin/login", timeout=10) as r:
                    self.assertEqual(r.status, 200)
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(base + "/.well-known/oauth-authorization-server", timeout=10)
                self.assertEqual(e.exception.code, 404)
            finally:
                srv.shutdown()
                srv.server_close()


class AdminFields(unittest.TestCase):
    def test_a_display_name_never_makes_an_admin(self):
        """Finding 16."""
        self.assertFalse(admin.is_admin({"name": "roman"}, {"roman"}))
        self.assertTrue(admin.is_admin({"uname": "Roman", "name": "x"}, {"roman"}))
        self.assertTrue(admin.is_admin({"id": 7}, {"7"}))


class FilesAndTokens(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        p = mock.patch.dict(os.environ, {"KB_HOME": self.home.name})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("KB_ADDONS", None)
        os.environ.pop("KB_CONTRACTS", None)

    def test_addons_and_settings_lock_the_file_across_processes(self):
        """Finding 15."""
        locked = []
        real = addons.fcntl.flock
        with mock.patch.object(addons.fcntl, "flock", side_effect=lambda fd, op: (locked.append(op), real(fd, op))):
            addons.set_enabled("metrics", True)
        self.assertIn(addons.fcntl.LOCK_EX, locked)
        self.assertTrue(os.path.exists(addons.state_file() + ".lock"))
        settings.set_value("contracts", "own")
        self.assertTrue(os.path.exists(settings.state_file() + ".lock"))

    def test_settings_generation_changes_on_a_save(self):
        """Finding 17."""
        before = settings.generation()
        settings.set_value("contracts", "own")
        self.assertNotEqual(settings.generation(), before)

    def test_kb_api_compares_the_token_in_constant_time(self):
        with mock.patch.object(kb_api.hmac, "compare_digest", wraps=kb_api.hmac.compare_digest) as cmp:
            self.assertTrue(kb_api.authorized("10.0.0.1", "sekret", "Bearer sekret"))
            self.assertFalse(kb_api.authorized("10.0.0.1", "sekret", "Bearer other"))
        self.assertEqual(cmp.call_count, 2)

    def test_kb_addon_on_and_off(self):
        """Finding 4: the command every recovery message names."""
        env = {**os.environ, "KB_HOME": self.home.name}
        for action, want in (("on", True), ("off", False)):
            p = subprocess.run(["bash", os.path.join(BIN, "kb"), "addon", action, "metrics"],
                               capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(os.path.join(self.home.name, "addons.json")) as f:
                self.assertEqual("metrics" in json.load(f), want)


if __name__ == "__main__":
    unittest.main()
