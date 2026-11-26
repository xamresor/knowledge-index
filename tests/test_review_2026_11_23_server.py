#!/usr/bin/env python3
"""Fixes from the review of the shared server and its sign-in code (2026-11-23).

One class per finding: kb-http answers every message, the login helper turns every failure into a
refusal, the rebuild lock, the refresh token during an outage, the users store under load and between
processes, the auth cache, sessions that follow their token, limits of the OAuth registration, loopback
redirects, kb-api static files with a token, keep-alive after an early answer, bounded waits,
notifications, usage keys and the index history file.
"""
from __future__ import annotations

import fcntl
import http.client
import http.cookiejar
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from _kbtest import BIN  # noqa: F401

import admin
import kb_core
import oauth
import plugins
from test_oauth import STATE, FakeApi, kb_http
from test_users import users

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def serve(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def stop(*servers):
    for s in servers:
        s.shutdown()
        s.server_close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def ident(projects=("api",)):
    return kb_http.Identity({"name": "ann"}, list(projects))


# ------------------------------------------------------------------------------------------ 1, 13, 18
class MessagesAlwaysGetAnAnswer(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_call(name, args, allowed=None, caller=None):
            self.calls.append(name)
            if args.get("symbol") == "boom":
                raise subprocess.TimeoutExpired("graphify", 5)
            return "ok"
        for p in (mock.patch.object(kb_core, "call_tool", fake_call), mock.patch.object(kb_http, "USAGE", None)):
            p.start()
            self.addCleanup(p.stop)

    def call(self, params, mid=1):
        return kb_http.handle_message({"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": params},
                                      ident())

    def test_a_name_that_is_no_string_is_an_invalid_params_error(self):
        answer = self.call({"name": ["x"], "arguments": {}})
        self.assertEqual(answer["error"]["code"], -32602)

    def test_any_fault_of_a_tool_is_an_error_answer(self):
        answer = self.call({"name": "graph_explain", "arguments": {"symbol": "boom"}})
        self.assertTrue(answer["result"]["isError"])
        self.assertIn("TimeoutExpired", answer["result"]["content"][0]["text"])

    def test_a_fault_outside_the_tool_is_an_internal_error(self):
        with mock.patch.object(kb_core, "tools", side_effect=RuntimeError("broken")):
            answer = kb_http.handle_message({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}, ident())
        self.assertEqual(answer, {"jsonrpc": "2.0", "id": 7, "error": {"code": -32603, "message": "internal error"}})

    def test_a_notification_runs_nothing_and_gets_no_answer(self):
        for method in ("tools/call", "tools/list", "initialize", "ping"):
            msg = {"jsonrpc": "2.0", "method": method, "params": {"name": "graph_explain", "arguments": {"symbol": "A"}}}
            self.assertIsNone(kb_http.handle_message(msg, ident()), method)
        self.assertEqual(self.calls, [])

    def test_an_idle_connection_does_not_hold_a_thread_forever(self):
        self.assertTrue(kb_http.Handler.timeout and kb_http.Handler.timeout > 0)


class HttpAnswers(unittest.TestCase):
    """Over HTTP: a fault inside a tool, an empty batch, a POST answered before its body was read."""

    @classmethod
    def setUpClass(cls):
        cls.api = serve(ThreadingHTTPServer(("127.0.0.1", 0), FakeApi))
        api = f"http://127.0.0.1:{cls.api.server_address[1]}"
        cls.tmp = tempfile.TemporaryDirectory()

        def fake_call(name, args, allowed=None, caller=None):
            raise TypeError("unhashable type: 'list'")
        cls.patches = [mock.patch.object(kb_core, "KB", cls.tmp.name),
                       mock.patch.object(kb_core, "GRAPH", os.path.join(cls.tmp.name, "graph.json")),
                       mock.patch.object(kb_core, "call_tool", fake_call),
                       mock.patch.object(kb_core, "PROJECTS", {"api": [], "web": []}),
                       mock.patch.object(kb_core, "warm_qmd", lambda: None)]
        for p in cls.patches:
            p.start()
        env = {"KB_LOGIN_URL": f"{api}/login", "KB_HOOK_HEADERS": '{"app":"dev"}'}
        cls.srv = serve(kb_http.make_server("127.0.0.1", 0, kb_http.Authenticator(url=f"{api}/permissions", ttl=0),
                                            None, oauth_env=env, admins={"ann"}))
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        stop(cls.srv, cls.api)
        cls.tmp.cleanup()

    def setUp(self):
        STATE["valid"] = True

    def post(self, body):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/mcp", body=json.dumps(body), headers={"Authorization": "Bearer 42|devtoken",
                                                                 "Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, json.loads(raw) if raw else None

    def test_a_tool_that_breaks_still_gets_an_answer(self):
        status, body = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "graph_explain", "arguments": {"symbol": "A"}}})
        self.assertEqual(status, 200)
        self.assertTrue(body["result"]["isError"])

    def test_an_empty_batch_is_an_invalid_request(self):
        status, body = self.post([])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], -32600)

    def test_an_early_answer_to_a_post_ends_the_connection(self):
        # No session: the admin page redirects at once, before the form is read. The form must not be
        # read as the next request of the kept-alive connection.
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/admin/usage", body=b"GET /admin HTTP/1.1\r\nHost: x\r\n\r\n",
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Connection"), "close")
        c.close()


# ------------------------------------------------------------------------------------------ 2
class FlakyLogin(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/reset":
            self.connection.shutdown(socket.SHUT_RDWR)  # an answer that never comes
            return
        if self.path == "/slow":
            time.sleep(1.5)
        data = b"<html>not json</html>" if self.path == "/html" else json.dumps({"two_factor": "yes"}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass  # the client gave up (the timeout test)


class LoginFailuresAreRefusals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = serve(ThreadingHTTPServer(("127.0.0.1", 0), FlakyLogin))
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        stop(cls.srv)

    def test_every_failure_of_the_webhook_is_a_login_error(self):
        kb_login = oauth.kb_login
        with self.assertRaises(kb_login.LoginError):
            kb_login.post(f"{self.base}/html", {})
        with self.assertRaises(kb_login.LoginError):
            kb_login.post(f"{self.base}/reset", {})
        with self.assertRaises(kb_login.LoginError):
            kb_login.post(f"{self.base}/slow", {}, timeout=0.3)

    def test_two_factor_counts_only_as_an_object(self):
        with self.assertRaisesRegex(oauth.kb_login.LoginError, "without a token"):
            oauth.kb_login.login(f"{self.base}/tf", "ann", "pw", env={})

    def test_the_browser_sign_in_turns_a_strange_answer_into_a_refusal(self):
        srv = oauth.Server({"KB_LOGIN_URL": f"{self.base}/tf"}, lambda t: None)
        srv.clients["c"] = {"client_id": "c", "redirect_uris": ["http://localhost:1/cb"]}
        txn = srv.start({"client_id": "c", "redirect_uri": "http://localhost:1/cb", "response_type": "code",
                         "code_challenge": "x", "code_challenge_method": "S256"})
        with self.assertRaises(oauth.kb_login.LoginError):
            srv.password_step(txn, "ann", "pw")

    def test_the_login_timeout_has_its_own_setting(self):
        self.assertEqual(oauth.kb_login.login_timeout({"KB_LOGIN_TIMEOUT": "7"}), 7.0)
        self.assertEqual(oauth.kb_login.login_timeout({}), 30.0)


# ------------------------------------------------------------------------------------------ 3
class RebuildLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "logs"))
        self.lock = os.path.join(self.tmp.name, "logs", "update.lock")
        open(self.lock, "w").close()  # kb-refresh leaves the file behind after every build
        p = mock.patch.object(kb_core, "KB", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(admin, "_rebuild_proc", None)
        q.start()
        self.addCleanup(q.stop)

    def test_a_lock_file_without_a_flock_is_no_build(self):
        self.assertFalse(admin.building(self.lock))
        with mock.patch.object(admin.subprocess, "Popen") as popen, \
                mock.patch.object(admin, "REBUILD_WAIT", 0.1):
            popen.return_value.poll.return_value = None
            self.assertNotIn("already running", admin.rebuild())  # started (no ack from a mock)
            self.assertIn("already running", admin.rebuild())  # the first one runs: no second
        self.assertEqual(popen.call_count, 1)

    def test_a_held_flock_is_a_build(self):
        fd = os.open(self.lock, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertTrue(admin.building(self.lock))
        self.assertTrue(admin.status(None)["building"])
        with mock.patch.object(admin.subprocess, "Popen") as popen:
            self.assertIn("already running", admin.rebuild())
        popen.assert_not_called()


# ------------------------------------------------------------------------------------------ 4, 9, 10
class RefreshDuringAnOutage(unittest.TestCase):
    def test_a_503_keeps_the_refresh_token_and_a_401_ends_it(self):
        state = {"status": 503}

        def check(token):
            if state["status"] != 200:
                raise kb_http.AuthError(state["status"], "x")
        srv = oauth.Server({}, check)
        srv.clients["c"] = {"client_id": "c", "redirect_uris": ["http://localhost:1/cb"]}
        rt = srv._issue("tok", "c")["refresh_token"]
        with self.assertRaises(oauth.OAuthError) as cm:
            srv.token({"grant_type": "refresh_token", "refresh_token": rt})
        self.assertEqual((cm.exception.status, cm.exception.error), (503, "temporarily_unavailable"))
        state["status"] = 200
        rt2 = srv.token({"grant_type": "refresh_token", "refresh_token": rt})["refresh_token"]
        state["status"] = 401
        with self.assertRaises(oauth.OAuthError) as cm:
            srv.token({"grant_type": "refresh_token", "refresh_token": rt2})
        self.assertEqual((cm.exception.status, cm.exception.error), (400, "invalid_grant"))
        self.assertNotIn(rt2, srv.refresh)


class OAuthLimits(unittest.TestCase):
    def setUp(self):
        self.t = [1_000_000.0]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.srv = oauth.Server({}, lambda t: None, os.path.join(self.tmp.name, "clients.json"), now=lambda: self.t[0])

    def test_uris_per_client_and_their_length(self):
        with self.assertRaises(oauth.OAuthError):
            self.srv.register({"redirect_uris": [f"http://localhost:{p}/cb" for p in range(1, oauth.MAX_URIS + 2)]})
        with self.assertRaises(oauth.OAuthError):
            self.srv.register({"redirect_uris": ["http://localhost:1/" + "a" * oauth.MAX_URI_LENGTH]})

    def test_clients_have_a_cap_and_unused_ones_make_room(self):
        # a full list must not block new clients: the oldest client nobody signed in with goes
        with mock.patch.object(oauth, "MAX_CLIENTS", 2):
            used = self.srv.register({"redirect_uris": ["http://localhost:1/cb"]})["client_id"]
            self.srv._seen(used, "ann")
            old = self.srv.register({"redirect_uris": ["http://localhost:1/cb"]})["client_id"]
            self.t[0] += 1
            new = self.srv.register({"redirect_uris": ["http://localhost:1/cb"]})["client_id"]
            self.assertEqual(set(self.srv.clients), {used, new})
            self.assertNotIn(old, self.srv.clients)
            self.srv._seen(new, "bob")                   # every client in use: then the list is full
            with self.assertRaises(oauth.OAuthError) as cm:
                self.srv.register({"redirect_uris": ["http://localhost:1/cb"]})
            self.assertEqual(cm.exception.status, 503)
        with mock.patch.object(oauth, "MAX_CLIENTS", 10):
            gone = self.srv.register({"redirect_uris": ["http://localhost:1/cb"]})["client_id"]
            self.t[0] += (oauth.CLIENT_DAYS + 1) * 86400   # an unused client expires
            self.srv.register({"redirect_uris": ["http://localhost:1/cb"]})
            self.assertNotIn(gone, self.srv.clients)
        self.assertIn(used, self.srv.clients)

    def test_a_flood_of_sign_ins_does_not_block_a_new_one(self):
        self.srv.clients["c"] = {"client_id": "c", "redirect_uris": ["http://localhost:1/cb"]}
        self.srv.clients["d"] = {"client_id": "d", "redirect_uris": ["http://localhost:1/cb"]}
        q = {"client_id": "c", "redirect_uri": "http://localhost:1/cb", "response_type": "code",
             "code_challenge": "x", "code_challenge_method": "S256"}
        with mock.patch.object(oauth, "MAX_TXNS", 3), mock.patch.object(oauth, "TXNS_PER_CLIENT", 2):
            first = self.srv.start(q)
            for _ in range(5):
                self.t[0] += 1
                self.srv.start(q)
            self.assertLessEqual(sum(t["client_id"] == "c" for t in self.srv.txns.values()), 2)
            self.assertNotIn(first, self.srv.txns)
            mine = self.srv.start({**q, "client_id": "d"})      # another client still gets in
            self.assertIn(mine, self.srv.txns)
            self.assertLessEqual(len(self.srv.txns), 3)

    def test_a_refresh_token_expires_when_the_server_sets_days(self):
        self.srv.clients["c"] = {"client_id": "c", "redirect_uris": ["http://localhost:1/cb"]}
        with mock.patch.object(oauth, "REFRESH_DAYS", 30):
            rt = self.srv._issue("tok", "c")["refresh_token"]
            self.t[0] += 31 * 86400
            with self.assertRaises(oauth.OAuthError) as cm:
                self.srv.token({"grant_type": "refresh_token", "refresh_token": rt})
        self.assertEqual(cm.exception.error, "invalid_grant")

    def test_a_refresh_token_has_no_end_by_default(self):
        self.assertEqual(oauth.REFRESH_DAYS, 0)
        self.srv.clients["c"] = {"client_id": "c", "redirect_uris": ["http://localhost:1/cb"]}
        rt = self.srv._issue("tok", "c")["refresh_token"]
        self.t[0] += 5 * 365 * 86400
        new = self.srv.token({"grant_type": "refresh_token", "refresh_token": rt})
        self.assertEqual(self.srv.resolve(new["access_token"]), "tok")
        with self.assertRaises(oauth.OAuthError):          # still rotated: the old one works once
            self.srv.token({"grant_type": "refresh_token", "refresh_token": rt})

    def test_the_oldest_tokens_of_a_client_go_first_also_without_an_end(self):
        self.srv.clients["c"] = {"client_id": "c", "redirect_uris": ["http://localhost:1/cb"]}
        with mock.patch.object(oauth, "TOKENS_PER_CLIENT", 3):
            issued = []
            for i in range(5):
                self.t[0] += 10
                issued.append(self.srv._issue(f"tok{i}", "c")["refresh_token"])
        self.assertEqual([rt in self.srv.refresh for rt in issued], [False, False, True, True, True])


class TokensOverARestart(unittest.TestCase):
    """A restart or a deploy of the server asks nobody to sign in again: the tokens are in a file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clients = os.path.join(self.tmp.name, "logs", "oauth-clients.json")
        self.tokens = os.path.join(self.tmp.name, "logs", "oauth-tokens.json")
        self.t = [1000.0]
        self.valid = {"tok"}

    def server(self) -> "oauth.Server":
        def check(token):
            if token not in self.valid:
                err = Exception("ended")
                err.status = 401
                raise err
        return oauth.Server({"KB_LOGIN_URL": "http://127.0.0.1:1/x"}, check, self.clients,
                            now=lambda: self.t[0], tokens_file=self.tokens)

    def signed_in(self, srv) -> tuple[str, dict]:
        cid = srv.register({"redirect_uris": ["http://localhost:1/cb"]})["client_id"]
        srv._seen(cid, "ann")
        return cid, srv.token({"grant_type": "refresh_token", "client_id": cid,
                               "refresh_token": srv._issue("tok", cid, "ann")["refresh_token"]})

    def test_access_and_refresh_tokens_work_after_a_restart(self):
        cid, got = self.signed_in(self.server())
        self.assertEqual(os.stat(self.tokens).st_mode & 0o777, 0o600)     # the owner only
        again = self.server()                                              # the restart
        self.assertEqual(again.resolve(got["access_token"]), "tok")
        self.t[0] += oauth.TOKEN_TTL + 1                                    # the access token ended ...
        self.assertIsNone(again.resolve(got["access_token"]))
        new = again.token({"grant_type": "refresh_token", "client_id": cid,
                           "refresh_token": got["refresh_token"]})         # ... the client refreshes, no browser
        self.assertEqual(again.resolve(new["access_token"]), "tok")
        third = self.server()                                              # the rotation is on disk too
        self.assertEqual(third.resolve(new["access_token"]), "tok")
        with self.assertRaises(oauth.OAuthError):
            third.token({"grant_type": "refresh_token", "client_id": cid, "refresh_token": got["refresh_token"]})

    def test_a_revoked_client_stays_revoked_after_a_restart(self):
        srv = self.server()
        cid, got = self.signed_in(srv)
        self.assertTrue(srv.revoke(cid))
        again = self.server()
        self.assertIsNone(again.resolve(got["access_token"]))
        self.assertEqual((again.refresh, again.access), ({}, {}))

    def test_a_token_the_login_webhook_ended_asks_for_a_sign_in(self):
        cid, got = self.signed_in(self.server())
        self.valid.clear()                                 # an admin disabled the person, or a new password
        again = self.server()
        with self.assertRaises(oauth.OAuthError) as cm:
            again.token({"grant_type": "refresh_token", "client_id": cid, "refresh_token": got["refresh_token"]})
        self.assertEqual(cm.exception.error, "invalid_grant")
        self.assertNotIn(got["refresh_token"], self.server().refresh)

    def test_a_broken_or_foreign_file_gives_no_tokens(self):
        cid, got = self.signed_in(self.server())
        with open(self.tokens, encoding="utf-8") as f:
            data = json.load(f)
        data["refresh"]["x"] = {"token": "tok", "client_id": "no-such-client"}
        data["refresh"]["y"] = "not an entry"
        data["access"]["z"] = {"token": "tok", "client_id": cid}             # an access token with no end
        data["access"]["old"] = {"token": "tok", "client_id": cid, "exp": 1.0}
        with open(self.tokens, "w", encoding="utf-8") as f:
            json.dump(data, f)
        again = self.server()
        self.assertEqual(set(again.refresh), {got["refresh_token"]})
        self.assertFalse({"z", "old"} & set(again.access))
        with open(self.tokens, "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertEqual(self.server().refresh, {})

    def test_a_failed_save_does_not_break_the_sign_in(self):
        srv = self.server()
        cid = srv.register({"redirect_uris": ["http://localhost:1/cb"]})["client_id"]
        with mock.patch.object(oauth.tempfile, "mkstemp", side_effect=OSError("disk full")):
            got = srv.token({"grant_type": "refresh_token", "client_id": cid,
                             "refresh_token": srv._issue("tok", cid)["refresh_token"]})
        self.assertEqual(srv.resolve(got["access_token"]), "tok")


class LoopbackRedirects(unittest.TestCase):
    def test_only_the_plain_form_counts(self):
        good = ["http://localhost:33418/callback", "http://127.0.0.1/cb", "http://[::1]:8080/x?y=1"]
        bad = ["http://attacker.example\\@localhost/callback", "http://user@localhost/cb",
               "http://user:pw@127.0.0.1/cb", "http://localhost/a b", "http://localhost/\tcb",
               "http://localhost:99999/", "https://localhost/", "http://localhost/x#f",
               "http://localhost\\.evil.example/", "http://localhost/" + "a" * 600]
        for u in good:
            self.assertTrue(oauth.is_loopback_redirect(u), u)
        for u in bad:
            self.assertFalse(oauth.is_loopback_redirect(u), u)

    def test_a_stored_client_with_a_bad_uri_gets_no_code(self):
        srv = oauth.Server({}, lambda t: None)
        bad = "http://attacker.example\\@localhost/callback"
        srv.clients["c"] = {"client_id": "c", "redirect_uris": [bad]}
        with self.assertRaises(oauth.OAuthError):
            srv.start({"client_id": "c", "redirect_uri": bad, "response_type": "code",
                       "code_challenge": "x", "code_challenge_method": "S256"})


# ------------------------------------------------------------------------------------------ 6, 16, 17
class UsersStoreUnderLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "users.json")
        self.store = users.Store(self.path)
        self.key = self.store.add("ann", ["api"])
        self.store.set_password("ann", "a long password here")

    def test_the_plugin_name_is_a_name(self):
        self.assertEqual(users.NAME, "users")

    def test_a_password_check_does_not_hold_the_token_checks(self):
        started, go = threading.Event(), threading.Event()
        real = users._scrypt

        def slow(password, salt):
            started.set()
            go.wait(5)
            return real(password, salt)
        with mock.patch.object(users, "_scrypt", slow):
            t = threading.Thread(target=lambda: self.assertRaises(users.UserError, self.store.login, "ann", "wrong pw!!"))
            t.start()
            self.assertTrue(started.wait(5))
            done = []
            checker = threading.Thread(target=lambda: done.append(self.store.permissions(self.key, ["api"])[0]))
            checker.start()
            checker.join(1.0)
            early = list(done)  # the token check is done while the password hash still runs
            go.set()
            t.join(5)
            checker.join(5)
        self.assertEqual(early, [200])
        self.assertFalse(t.is_alive())

    def test_a_password_changed_during_the_check_does_not_sign_in(self):
        real = users._scrypt
        changed = []

        def hash_then_change(password, salt):
            out = real(password, salt)
            if not changed:
                changed.append(1)
                self.store.set_password("ann", "another long password")
            return out
        with mock.patch.object(users, "_scrypt", hash_then_change):
            with self.assertRaises(users.UserError):
                self.store.login("ann", "a long password here")

    def test_the_command_line_waits_for_a_save_of_the_server(self):
        self.store.add("bob", ["api"])
        script = os.path.join(ROOT, "contrib", "users", "plugin.py")
        with self.store._changing():
            cli = subprocess.Popen([sys.executable, script, "--file", self.path, "revoke", "bob"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(0.7)
            self.assertIsNone(cli.poll(), "the command line must wait for the file lock")
            self.store._users["ann"]["projects"] = ["api", "web"]  # the server's change, saved under the lock
            self.store._save()
        out, err = cli.communicate(timeout=20)
        self.assertEqual(cli.returncode, 0, err)
        with open(self.path) as f:
            data = json.load(f)["users"]
        self.assertEqual(data["ann"]["projects"], ["api", "web"])  # the server's change survived
        self.assertEqual(data["bob"]["tokens"], {})                # and the command line's too


class LoginThrottle(unittest.TestCase):
    def test_per_address_and_in_all(self):
        t = admin.Throttle(per_ip=2, total=3, window=60)
        self.assertIsNone(t.hit("a"))
        self.assertIsNone(t.hit("a"))
        self.assertIsNotNone(t.hit("a"))
        self.assertIsNone(t.hit("b"))
        # Three in all: the global cap tightens each address to Throttle.BUSY_PER_IP and never shuts the
        # page for a fresh address (review of 2026-11-24).
        self.assertIsNone(t.hit("c"))


# ------------------------------------------------------------------------------------------ 7
class AuthCacheKeepsNoException(unittest.TestCase):
    def test_a_cached_refusal_is_raised_new_each_time(self):
        a = kb_http.Authenticator(url="http://127.0.0.1:9/x", ttl=60)
        with mock.patch.object(a, "_ask", return_value=kb_http.AuthError(401, "invalid or expired token")):
            errors = []
            for _ in range(2):
                try:
                    a.check("Bearer t")
                except kb_http.AuthError as e:
                    errors.append(e)
        self.assertEqual([e.status for e in errors], [401, 401])
        self.assertIsNot(errors[0], errors[1])
        self.assertFalse(any(isinstance(v[1], BaseException) for v in a._cache.values()))


# ------------------------------------------------------------------------------------------ 6, 8 over HTTP
class SessionsFollowTheirToken(unittest.TestCase):
    """The users plugin on kb-http: a session ends when its token ends, an admin who lost the right
    loses the admin pages, and the password pages have an attempt limit."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patches = [mock.patch.object(kb_core, "KB", cls.tmp.name),
                       mock.patch.object(kb_core, "GRAPH", os.path.join(cls.tmp.name, "graph.json")),
                       mock.patch.object(kb_core, "PROJECTS", {"api": [], "web": []}),
                       mock.patch.object(kb_core, "warm_qmd", lambda: None),
                       mock.patch.dict(os.environ, {"KB_ADDONS": "none"}),
                       mock.patch.object(plugins, "access", lambda enabled: users),
                       mock.patch.object(users, "STORE", users.Store(os.path.join(cls.tmp.name, "users.json"))),
                       mock.patch.object(users, "mail_config", lambda core=None: {})]
        for p in cls.patches:
            p.start()
        cls.store = users.STORE
        cls.boss = cls.store.add("boss", "*", admin=True)
        cls.auth = kb_http.Authenticator(url="http://127.0.0.1:1/unset", ttl=0)
        env = {"KB_LOGIN_URL": "http://127.0.0.1:1/unset"}
        cls.srv = kb_http.make_server("127.0.0.1", 0, cls.auth, None, oauth_env=env, admins=set())
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.auth.url = f"{cls.base}/local/permissions"
        env["KB_LOGIN_URL"] = f"{cls.base}/local/login"
        serve(cls.srv)

    @classmethod
    def tearDownClass(cls):
        stop(cls.srv)
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def client(self):
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect)

    def req(self, opener, method, path, form=None, headers=None):
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with opener.open(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read().decode()

    def sign_in(self, name, key):
        c = self.client()
        status, _, _ = self.req(c, "POST", "/admin/login", {"username": name, "password": key})
        self.assertEqual(status, 302)
        return c

    def test_a_removed_token_ends_its_session(self):
        key = self.store.add("dan", "*", admin=True)
        c = self.sign_in("dan", key)
        self.assertEqual(self.req(c, "GET", "/admin/users")[0], 200)
        self.store.revoke("dan")
        status, headers, _ = self.req(c, "GET", "/admin/users")
        self.assertEqual(status, 302)
        self.assertTrue(headers["Location"].endswith("/admin/login"))

    def test_an_admin_who_lost_the_right_loses_the_admin_pages(self):
        key = self.store.add("eve", "*", admin=True)
        c = self.sign_in("eve", key)
        self.assertEqual(self.req(c, "GET", "/admin/users")[0], 200)
        self.store.update("eve", admin=False)
        status, _, page = self.req(c, "GET", "/admin/users")
        self.assertEqual(status, 403)
        self.assertEqual(self.req(c, "GET", "/admin/me")[0], 200)  # the own tokens stay

    def test_the_password_pages_have_an_attempt_limit(self):
        handler = self.srv.RequestHandlerClass
        with mock.patch.object(handler, "login_limit", admin.Throttle(per_ip=2, total=100, window=300)):
            far = {"X-Forwarded-For": "203.0.113.9"}
            for _ in range(2):
                self.assertEqual(self.req(self.client(), "POST", "/admin/login",
                                          {"username": "boss", "password": "wrong"}, far)[0], 200)
            status, headers, _ = self.req(self.client(), "POST", "/admin/login",
                                          {"username": "boss", "password": self.boss}, far)
            self.assertEqual(status, 429)
            self.assertIn("Retry-After", headers)
            # another address is not held up
            self.assertEqual(self.req(self.client(), "POST", "/admin/login",
                                      {"username": "boss", "password": self.boss},
                                      {"X-Forwarded-For": "198.51.100.7"})[0], 302)


class RegistrationLimit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patch = mock.patch.object(kb_core, "KB", cls.tmp.name)
        cls.patch.start()
        cls.srv = serve(kb_http.make_server("127.0.0.1", 0, kb_http.Authenticator(url="http://127.0.0.1:9/x"),
                                            None, oauth_env={"KB_LOGIN_URL": "http://127.0.0.1:9/x"}))
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        stop(cls.srv)
        cls.patch.stop()
        cls.tmp.cleanup()

    def test_registrations_per_address_have_a_limit(self):
        handler = self.srv.RequestHandlerClass
        with mock.patch.object(handler, "register_limit", admin.Throttle(per_ip=2, total=100, window=3600)):
            got = []
            for _ in range(3):
                r = urllib.request.Request(self.base + "/register", method="POST",
                                           data=json.dumps({"redirect_uris": ["http://localhost:1/cb"]}).encode(),
                                           headers={"Content-Type": "application/json",
                                                    "X-Forwarded-For": "203.0.113.5"})
                try:
                    with urllib.request.urlopen(r, timeout=10) as resp:
                        got.append(resp.status)
                except urllib.error.HTTPError as e:
                    got.append(e.code)
                    e.close()
        self.assertEqual(got, [201, 201, 429])


# ------------------------------------------------------------------------------------------ 11
class KbApiStaticFilesNeedTheToken(unittest.TestCase):
    def test_graph_data_needs_the_token(self):
        import pathlib
        import kb_api
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = pathlib.Path(tmp.name, "kb-manifest.js")
        data.write_text("kbManifest({})")
        with mock.patch.object(kb_api, "static_file", lambda path: data if path.startswith("/graphify-out/") else None):
            srv = serve(ThreadingHTTPServer(("127.0.0.1", 0), kb_api.make_handler("127.0.0.1", "sekret")))
            self.addCleanup(stop, srv)
            url = f"http://127.0.0.1:{srv.server_address[1]}/graphify-out/kb-manifest.js"
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(url, timeout=10)
            self.assertEqual(cm.exception.code, 401)
            cm.exception.close()
            r = urllib.request.Request(url, headers={"Authorization": "Bearer sekret"})
            with urllib.request.urlopen(r, timeout=10) as resp:
                self.assertEqual(resp.read(), b"kbManifest({})")


# ------------------------------------------------------------------------------------------ 15
class UsageKeysAndPruning(unittest.TestCase):
    def setUp(self):
        from usage import Usage
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.u = Usage(os.path.join(self.tmp.name, "usage.json"))

    def test_one_question_is_one_key(self):
        from usage import Usage
        k = Usage.arg_key
        self.assertEqual(k({"q": "payout job", "limit": 5, "page": 2, "explain": True, "project": "api"}, "search"),
                         "payout job")
        self.assertEqual(k({"q": "payout job"}, "search"), "payout job")
        self.assertEqual(k({"q": "payout job", "cursor": "c-123"}, "search"), "")  # a next page asks nothing new
        self.assertEqual(k({"pattern": "TODO", "glob": "*.php", "ignore_case": True}, "code_grep"), "TODO")
        self.assertEqual(k({"from": "A", "to": "B", "page_length": 10}, "graph_path"), "A → B")

    def test_old_rows_go_while_the_server_runs(self):
        import usage
        self.u.db.execute("INSERT INTO calls (ts, tool, user, project, ok, seconds, arg) VALUES (?,?,?,?,?,?,?)",
                          (time.time() - (usage.USAGE_DAYS + 5) * 86400, "search", "ann", "all", 1, 0.1, "old"))
        with mock.patch.object(usage, "PRUNE_EVERY", 0):
            self.u.record("search", "ann", {"q": "new"}, True, 0.1)
        self.assertEqual(self.u.snapshot()["args"]["search"], {"new": 1})

    def test_the_snapshot_is_reused_for_a_moment(self):
        self.u.record("search", "ann", {"q": "x"}, True, 0.1)
        self.u.snapshot()
        with mock.patch.object(self.u, "_rows", side_effect=AssertionError("no new scan")):
            self.assertEqual(self.u.snapshot()["tools"]["search"]["calls"], 1)


# ------------------------------------------------------------------------------------------ 19
class IndexHistoryFile(unittest.TestCase):
    def test_a_failed_write_keeps_the_old_history(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(kb_core, "KB", tmp.name):
            admin._history({"built": "2026-11-01 10:00", "nodes": 1})
            with mock.patch.object(admin.json, "dump", side_effect=OSError("disk full")):
                admin._history({"built": "2026-11-02 10:00", "nodes": 2})
            hist = admin._history({})
        self.assertEqual([h["nodes"] for h in hist], [1])
        self.assertEqual(os.listdir(os.path.join(tmp.name, "logs")), ["index-history.json"])


if __name__ == "__main__":
    unittest.main()


class TrustedProxies(unittest.TestCase):
    def test_loopback_and_the_configured_networks_are_trusted(self):
        # nginx outside a Docker container reaches kb-http from the bridge, not from loopback
        self.assertTrue(kb_http._trusted_proxy("127.0.0.1"))
        self.assertTrue(kb_http._trusted_proxy("::ffff:127.0.0.1"))
        self.assertFalse(kb_http._trusted_proxy("172.17.0.1"))
        with mock.patch.dict(os.environ, {"KB_TRUSTED_PROXIES": "10.0.0.5, 172.17.0.0/16"}):
            self.assertTrue(kb_http._trusted_proxy("172.17.0.1"))
            self.assertTrue(kb_http._trusted_proxy("10.0.0.5"))
            self.assertFalse(kb_http._trusted_proxy("203.0.113.9"))
        self.assertFalse(kb_http._trusted_proxy("not an address"))
