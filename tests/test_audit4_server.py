"""Fourth audit round, the server side: KB_PUBLIC_URL from smtp.env and a refused start for a permission
URL that is not this server, a stable login id in usage and logs, the "forgot" mail off the request,
sign-in tokens of unfinished browser sign-ins, the reset page after a reload, a broken users file at
sign-in, a failed save that must not stay in memory, and the Rebuild button while a build runs."""
from __future__ import annotations

import http.cookiejar
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from _kbtest import BIN

import admin
import kb_core
import oauth
import plugins
from test_oauth import kb_http

_spec = importlib.util.spec_from_file_location(
    "kb_plugin_users_audit4", os.path.join(os.path.dirname(BIN), "contrib", "users", "plugin.py"))
users = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(users)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class StoreTmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = users.Store(os.path.join(self.tmp.name, "users.json"), now=self.clock)


class PublicUrl(unittest.TestCase):
    """Finding 1: KB_PUBLIC_URL in <data>/smtp.env names this server too, and a permission URL of the
    /local/ form that is not this server stops the start."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(kb_core, "KB", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def test_public_url_comes_from_smtp_env(self):
        with open(os.path.join(self.tmp.name, "smtp.env"), "w", encoding="utf-8") as f:
            f.write("# mail\nKB_SMTP_HOST=mail.example.com\nKB_PUBLIC_URL='https://kb.example.test/kb'\n")
        env: dict = {}
        kb_http.load_public_urls(env)
        self.assertEqual(env, {"KB_PUBLIC_URL": "https://kb.example.test/kb"})
        env = {"KB_PUBLIC_URL": "https://unit.example.test"}
        kb_http.load_public_urls(env)
        self.assertEqual(env["KB_PUBLIC_URL"], "https://unit.example.test")   # the environment wins

    def test_a_permission_url_that_is_not_this_server_refuses_the_start(self):
        env = {"KB_SERVER": "on", "KB_PERMISSION_URL": "https://kb.example.test/kb/local/permissions"}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KB_PUBLIC_URL", None)
            os.environ.pop("KB_PUBLIC_LOGIN_URL", None)
            self.assertIn("KB_PUBLIC_URL", kb_http.refuse_reason(env, access=object()) or "")
            os.environ["KB_PUBLIC_URL"] = "https://kb.example.test/kb"
            self.assertIsNone(kb_http.refuse_reason(env, access=object()))


class StableName(unittest.TestCase):
    """Finding 3: usage, the call log and the cursor owner use the stable login id, not a display name."""

    def test_login_id_before_display_name(self):
        self.assertEqual(kb_http.Identity({"name": "Roman", "uname": "rsereda"}, ["api"]).name, "rsereda")
        self.assertEqual(kb_http.Identity({"name": "Roman", "id": 7}, ["api"]).name, "7")
        self.assertEqual(kb_http.Identity({"name": "ann"}, ["api"]).name, "ann")   # the users plugin


class ForgotInBackground(StoreTmp):
    """Finding 2: the "forgot" answer does not wait for the mail server."""

    def test_the_answer_does_not_wait_for_the_mail(self):
        self.store.add("kate@example.com", ["api"], key=False)
        cfg = {"KB_SMTP_HOST": "h", "KB_SMTP_FROM": "f", "KB_PUBLIC_URL": "https://kb.test"}
        release, sent = threading.Event(), threading.Event()

        def slow_mail(to, subject, text, cfg):
            release.wait(5)
            sent.set()
        req = {"method": "POST", "path": "/forgot", "form": {"username": "kate@example.com"}, "query": {},
               "base": "", "audit": lambda *a: None}
        with mock.patch.object(users, "store", lambda core=None: self.store), \
                mock.patch.object(users, "mail_config", lambda core=None: cfg), \
                mock.patch.object(users, "send_mail", slow_mail):
            t0 = time.monotonic()
            _, _, body = users.public_page(req, None)
            took = time.monotonic() - t0
            self.assertIn("If this account exists", body)
            self.assertLess(took, 2)
            self.assertFalse(sent.is_set())
            release.set()
            self.assertTrue(sent.wait(5))


class UnfinishedSignIns(unittest.TestCase):
    """Finding 4: an upstream sign-in token that no code, refresh or access token holds any more ends."""

    def setUp(self):
        self.clock = Clock()
        self.ended: list[str] = []
        self.srv = oauth.Server({"KB_LOGIN_URL": "http://127.0.0.1:1/x"}, lambda t: None, now=self.clock)
        self.srv.on_drop = self.ended.append
        self.srv.login_post = lambda url, body, headers, ip: {"token": f"up-{body['username']}"}
        self.client = self.srv.register({"redirect_uris": ["http://127.0.0.1:5000/cb"]})

    def _code(self, user: str) -> str:
        q = {"client_id": self.client["client_id"], "redirect_uri": "http://127.0.0.1:5000/cb",
             "response_type": "code", "code_challenge": oauth.b64url_sha256("v" * 43),
             "code_challenge_method": "S256"}
        txn = self.srv.start(q, "1.1.1.1")
        _, url = self.srv.password_step(txn, user, "pw")
        return url.split("code=")[1].split("&")[0]

    def test_an_expired_code_ends_its_token(self):
        self._code("ann")
        self.clock.t += oauth.CODE_TTL + 1
        self.srv.start({"client_id": self.client["client_id"], "redirect_uri": "http://127.0.0.1:5000/cb",
                        "response_type": "code", "code_challenge": "x", "code_challenge_method": "S256"}, "2.2.2.2")
        self.assertEqual(self.ended, ["up-ann"])

    def test_a_used_code_keeps_its_token_and_a_revoke_ends_it(self):
        code = self._code("bob")
        self.srv.token({"grant_type": "authorization_code", "code": code, "code_verifier": "v" * 43,
                        "client_id": self.client["client_id"]})
        self.assertEqual(self.ended, [])
        self.srv.revoke(self.client["client_id"])
        self.assertEqual(self.ended, ["up-bob"])

    def test_a_failed_code_exchange_ends_its_token(self):
        code = self._code("eve")
        with self.assertRaises(oauth.OAuthError):
            self.srv.token({"grant_type": "authorization_code", "code": code, "code_verifier": "wrong" * 9,
                            "client_id": self.client["client_id"]})
        self.assertEqual(self.ended, ["up-eve"])


class ResetAfterReload(StoreTmp):
    """Finding 5: a POST without the link value says to open the link again, not that it is dead."""

    def test_empty_value_asks_to_open_the_link_again(self):
        req = {"method": "POST", "path": "/reset", "form": {"t": "", "password": "a", "again": "a"},
               "query": {}, "base": "", "audit": lambda *a: None}
        with mock.patch.object(users, "store", lambda core=None: self.store), \
                mock.patch.object(users, "mail_config", lambda core=None: {}):
            _, _, body = users.public_page(req, None)
        self.assertIn("Open the link from the mail again", body)
        self.assertNotIn("does not work any more", body)


class BrokenUsersFile(StoreTmp):
    """Finding 6: a users file that cannot be read is an outage (503) at sign-in, not wrong credentials."""

    def test_unreadable_file_is_503(self):
        with open(self.store.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        with mock.patch.object(users, "store", lambda core=None: self.store):
            status = users.login({"username": "ann", "password": "one long password"}, None)[0]
        self.assertEqual(status, 503)


class FailedSave(StoreTmp):
    """Finding 7: a change whose save failed does not stay in memory."""

    def test_a_revoke_that_was_not_saved_does_not_hold(self):
        key = self.store.add("ann", ["api"])
        with mock.patch.object(self.store, "_save", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                self.store.revoke("ann")
        # Memory and file agree: the token still works, and the admin sees that the revoke failed.
        self.assertEqual(self.store.permissions(key, ["api"])[0], 200)
        self.assertTrue(self.store.all()["ann"]["tokens"])

    def test_a_failed_touch_still_answers_200_and_waits(self):
        key = self.store.add("ann", ["api"])
        self.clock.t += users.TOUCH_SECONDS + 1
        with mock.patch.object(self.store, "_save", side_effect=OSError(28, "No space left on device")) as save:
            self.assertEqual(self.store.permissions(key, ["api"])[0], 200)
            self.assertEqual(self.store.permissions(key, ["api"])[0], 200)
        self.assertEqual(save.call_count, 1)   # the time stays in memory: no second write at once


class RebuildBusy(unittest.TestCase):
    """The Rebuild button: kb-refresh --force exits 75 when another build holds the lock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "logs"))
        p = mock.patch.object(kb_core, "KB", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        admin._rebuild_proc = None

    def _script(self, body: str) -> str:
        path = os.path.join(self.tmp.name, "fake-refresh")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def test_exit_75_says_a_build_is_running(self):
        msg = admin.rebuild(self._script("echo busy >&2; exit 75"))
        self.assertIn("already running", msg)

    def test_a_build_that_takes_the_lock_is_started(self):
        lock = os.path.join(self.tmp.name, "logs", "update.lock")
        msg = admin.rebuild(self._script(f'exec 8>"{lock}"; flock -n 8 || exit 75; echo $$ >"$KB_REFRESH_ACK"; sleep 1'))
        self.assertIn("Rebuild started", msg)
        admin._rebuild_proc.wait(5)


class AdminWithoutProject(StoreTmp):
    """Owner decision: an admin of the users plugin gets into the admin pages without any project. MCP
    still needs a project."""

    def test_the_store_answers_200_with_no_project_for_an_admin_only(self):
        boss = self.store.add("boss", ["gone"], admin=True)
        ann = self.store.add("ann", ["gone"])
        self.assertEqual(self.store.permissions(boss, ["api"]), (200, {"user": {"name": "boss"}, "projects": []}))
        self.assertEqual(self.store.permissions(ann, ["api"])[0], 403)

    def test_an_empty_grant_is_refused_for_mcp_and_allowed_for_the_admin_pages(self):
        auth = kb_http.Authenticator(url="http://127.0.0.1:1/x")
        auth.access = type("P", (), {"permissions": staticmethod(
            lambda token, known, core: (200, {"user": {"name": "boss"}, "projects": []}))})()
        with mock.patch.object(kb_http, "is_self_url", lambda url: True), \
                mock.patch.object(kb_core, "PROJECTS", {"api": []}):
            with self.assertRaises(kb_http.AuthError) as e:
                auth.check("Bearer tok")
            self.assertEqual(e.exception.status, 403)
            ident = auth.check("Bearer tok", admin_pages=True)   # the cached verdict, a second time
            self.assertEqual((ident.name, set(ident.projects)), ("boss", set()))
            with self.assertRaises(kb_http.AuthError):
                auth.check("Bearer tok")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


class AdminWithoutProjectHttp(unittest.TestCase):
    """The same over HTTP: the sign-in of the admin pages works, /mcp gets 403."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patches = [mock.patch.object(kb_core, "KB", cls.tmp.name),
                       mock.patch.object(kb_core, "GRAPH", os.path.join(cls.tmp.name, "graph.json")),
                       mock.patch.object(kb_core, "PROJECTS", {"api": []}),
                       mock.patch.object(kb_core, "warm_qmd", lambda: None),
                       mock.patch.dict(os.environ, {"KB_ADDONS": "none"}),
                       mock.patch.object(plugins, "access", lambda enabled: users),
                       mock.patch.object(users, "STORE", users.Store(os.path.join(cls.tmp.name, "users.json"))),
                       mock.patch.object(users, "mail_config", lambda core=None: {})]
        for p in cls.patches:
            p.start()
        cls.boss = users.STORE.add("boss", ["gone"], admin=True)
        cls.ann = users.STORE.add("ann", [])
        auth = kb_http.Authenticator(url="http://127.0.0.1:1/unset", ttl=60)
        env = {"KB_LOGIN_URL": "http://127.0.0.1:1/unset"}
        cls.srv = kb_http.make_server("127.0.0.1", 0, auth, None, oauth_env=env, admins=set())
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

    def req(self, opener, method, path, form=None, headers=None):
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        if data:
            r.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with opener.open(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode()

    def client(self):
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                           _NoRedirect)

    def test_an_admin_without_a_project_signs_in_and_opens_every_admin_page(self):
        c = self.client()
        status, headers, _ = self.req(c, "POST", "/admin/login", {"username": "boss", "password": self.boss})
        self.assertEqual((status, headers.get("Location", "").endswith("/admin")), (302, True))
        for path in ("/admin", "/admin/users", "/admin/settings", "/admin/usage"):
            self.assertEqual(self.req(c, "GET", path)[0], 200, path)

    def test_mcp_still_needs_a_project(self):
        r = urllib.request.Request(self.base + "/mcp", method="POST",
                                   data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode(),
                                   headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.boss}"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(r, timeout=10)
        self.assertEqual(e.exception.code, 403)

    def test_a_member_without_a_project_stays_out(self):
        c = self.client()
        status, headers, page = self.req(c, "POST", "/admin/login", {"username": "ann", "password": self.ann})
        self.assertNotEqual(status, 302)
        self.assertIn("access denied", page)   # the plugin still refuses a member without a project


class ResetPerDay(StoreTmp):
    """A cap of "forgot" mails per name and day (KB_RESET_PER_DAY, default 3), on top of RESET_EVERY."""

    def setUp(self):
        super().setUp()
        self.store.add("kate@example.com", ["api"], key=False)
        self.sent: list[str] = []
        cfg = {"KB_SMTP_HOST": "h", "KB_SMTP_FROM": "f", "KB_PUBLIC_URL": "https://kb.test"}
        for p in (mock.patch.object(users, "store", lambda core=None: self.store),
                  mock.patch.object(users, "mail_config", lambda core=None: cfg),
                  mock.patch.object(users, "send_mail", lambda to, subject, text, cfg: self.sent.append(to))):
            p.start()
            self.addCleanup(p.stop)

    def forgot(self, name="kate@example.com") -> str:
        req = {"method": "POST", "path": "/forgot", "form": {"username": name}, "query": {},
               "base": "", "audit": lambda *a: None}
        _, _, body = users.public_page(req, None)
        users._MAIL_QUEUE.join()   # the background worker has handled the request
        return body

    def test_the_fourth_mail_of_a_day_does_not_go(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KB_RESET_PER_DAY", None)
            answers = []
            for _ in range(4):
                answers.append(self.forgot())
                self.clock.t += users.RESET_EVERY + 1
            self.assertEqual(len(self.sent), 3)
            self.assertEqual(len(set(answers)), 1)            # the same answer: no hint
            self.assertEqual(answers[0], self.forgot("nobody@example.com"))
            self.clock.t += 24 * 3600
            self.forgot()
            self.assertEqual(len(self.sent), 4)                 # a new day: mail again

    def test_the_cap_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"KB_RESET_PER_DAY": "1"}):
            self.forgot()
            self.clock.t += users.RESET_EVERY + 1
            self.forgot()
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
