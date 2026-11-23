#!/usr/bin/env python3
"""Fixes from the second review of the shared server and its sign-in code (2026-11-24).

One class per finding: the access plugin cannot be switched off from the browser, an admin cannot take
the own projects away, the password lock per name and address, the global sign-in limits, open
sign-ins under a flood, the add-on cache under threads, the sandbox service after a failed start, the
MCP messages of kb-api, the clients file, a forgot-password mail beside an invite, loopback requests,
batch limits, the admission of bearer tokens, the lifetime of an OAuth access token, the mode of
.mcp.json and the wording of the sign-in page.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import types
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from unittest import mock

from _kbtest import BIN  # noqa: F401

import addons
import admin
import kb_core
import mcp_rpc
import oauth
import plugins
from test_kb_login import kb_login
from test_oauth import kb_http
from test_users import users

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCESS_SRC = "NAME = 'x'\ndef login(body, core):\n    return 401, {}\n\ndef permissions(token, projects, core):\n    return 401, {}\n"


def serve(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def stop(*servers):
    for s in servers:
        s.shutdown()
        s.server_close()


class DataHome(unittest.TestCase):
    """A data folder of its own, with no KB_ADDONS: addons.json decides."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"KB_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("KB_ADDONS", None)
        addons._cache = {}

    def plugin(self, name: str, src: str = ACCESS_SRC) -> None:
        folder = os.path.join(self.tmp.name, "plugins", name)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "plugin.py"), "w", encoding="utf-8") as f:
            f.write(src)

    def file(self) -> list:
        with open(addons.state_file(), encoding="utf-8") as f:
            return json.load(f)

    def write(self, names) -> None:
        with open(addons.state_file(), "w", encoding="utf-8") as f:
            json.dump(names, f)


# ------------------------------------------------------------------------------------------ 1
class AccessPluginStaysOn(DataHome):
    def setUp(self):
        super().setUp()
        self.plugin("users")
        self.write(["users"])
        started = types.SimpleNamespace(NAME="users", TABS=[("Users", "/users", "admin")], PUBLIC=["/forgot"])
        admin.set_access(started)
        self.addCleanup(admin.set_access, admin.UNSET)

    def test_the_page_refuses_to_switch_off_the_active_access_plugin(self):
        status, note, done = admin.change_setting({"name": "users", "on": "0"})
        self.assertEqual((status, done), (409, ""))
        self.assertIn("kb addon off users", note)
        self.assertEqual(self.file(), ["users"])

    def test_the_page_refuses_a_second_access_plugin(self):
        self.plugin("ldap")
        status, note, done = admin.change_setting({"name": "ldap", "on": "1"})
        self.assertEqual((status, done), (409, ""))
        self.assertIn("kb addon on ldap", note)
        self.assertEqual(self.file(), ["users"])

    def test_other_add_ons_still_switch(self):
        self.assertEqual(admin.change_setting({"name": "metrics", "on": "1"})[0], 200)
        self.assertEqual(sorted(self.file()), ["metrics", "users"])

    def test_pages_show_the_access_plugin_the_server_started_with(self):
        with mock.patch.object(plugins, "access", lambda enabled: None):
            nav = admin.page("t", "", "", "", "csrf").decode()
            login = admin.login_page("").decode()
        self.assertIn("/admin/users", nav)
        self.assertIn("/admin/forgot", login)

    def test_a_plugin_missing_for_a_moment_stays_in_the_file(self):
        self.write(["users", "gone-for-a-moment"])
        addons.set_enabled("metrics", True)
        self.assertEqual(sorted(self.file()), ["gone-for-a-moment", "metrics", "users"])
        addons.set_enabled("metrics", False)
        self.assertEqual(sorted(self.file()), ["gone-for-a-moment", "users"])

    def test_start_without_an_access_source_says_what_to_do(self):
        env = {"KB_SERVER": "on"}
        self.assertIn("kb addon", kb_http.refuse_reason(env, access=None))
        own = {"KB_SERVER": "on", "KB_PERMISSION_URL": "http://127.0.0.1:8790/local/permissions"}
        self.assertIn("access plugin", kb_http.refuse_reason(own, access=None))
        self.assertIsNone(kb_http.refuse_reason(own, access=types.SimpleNamespace()))
        self.assertIsNone(kb_http.refuse_reason({"KB_SERVER": "on", "KB_PERMISSION_URL": "https://api.example/p"},
                                                access=None))


# ------------------------------------------------------------------------------------------ 2
class AdminKeepsAProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = users.Store(os.path.join(self.tmp.name, "users.json"))
        self.store.add("boss", "*", admin=True)
        self.store.add("other", "*", admin=True)
        p = mock.patch.object(users, "STORE", self.store)
        p.start()
        self.addCleanup(p.stop)
        self.core = types.SimpleNamespace(PROJECTS={"api": [], "web": []}, KB=self.tmp.name)

    def save(self, name, form):
        req = {"method": "POST", "path": "/users/save", "base": "", "csrf": "c", "user": "boss", "admin": True,
               "now": time.time(), "form": {"name": name, "pick": "1", "admin": "on", **form},
               "forget": lambda: None, "drop_sessions": lambda n: None, "audit": lambda *a: None}
        return users.page(req, self.core)

    def test_the_own_row_without_a_project_is_refused(self):
        _, _, body = self.save("boss", {})
        self.assertIn("at least one project", body)
        self.assertEqual(self.store.all()["boss"]["projects"], "*")

    def test_the_own_row_with_a_project_saves(self):
        self.save("boss", {"p:api": "on"})
        self.assertEqual(self.store.all()["boss"]["projects"], ["api"])

    def test_another_row_may_have_no_project(self):
        self.save("other", {})
        self.assertEqual(self.store.all()["other"]["projects"], [])


# ------------------------------------------------------------------------------------------ 3
class LockPerNameAndAddress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = users.Store(os.path.join(self.tmp.name, "users.json"))
        self.store.add("ann", ["api"], key=False)
        self.store.set_password("ann", "a long password 1")

    def test_a_stranger_does_not_lock_the_owner_out(self):
        for _ in range(users.LOCK_AFTER):
            with self.assertRaises(users.UserError):
                self.store.login("ann", "wrong", ip="6.6.6.6")
        with self.assertRaises(users.Locked):
            self.store.login("ann", "a long password 1", ip="6.6.6.6")
        self.assertTrue(self.store.login("ann", "a long password 1", ip="1.2.3.4").startswith(users.SIGNIN_PREFIX))

    def test_the_webhook_passes_the_address(self):
        p = mock.patch.object(users, "STORE", self.store)
        p.start()
        self.addCleanup(p.stop)
        for _ in range(users.LOCK_AFTER):
            users.login({"username": "ann", "password": "wrong", "ip": "6.6.6.6"}, None)
        self.assertEqual(users.login({"username": "ann", "password": "wrong", "ip": "6.6.6.6"}, None)[0], 429)
        self.assertEqual(users.login({"username": "ann", "password": "a long password 1", "ip": "1.2.3.4"},
                                     None)[0], 200)


# ------------------------------------------------------------------------------------------ 4
class GlobalLimitOnlyTightens(unittest.TestCase):
    def test_a_fresh_address_passes_while_all_addresses_are_over_the_cap(self):
        t = admin.Throttle(per_ip=20, total=10, window=300)
        for i in range(10):
            for _ in range(admin.Throttle.BUSY_PER_IP):
                t.hit(f"10.0.0.{i}")
        self.assertIsNotNone(t.hit("10.0.0.1"))         # a flooding address waits
        self.assertIsNone(t.hit("192.168.1.5"))          # a person at a fresh address signs in
        self.assertIsNone(t.hit("192.168.1.5"))          # ... and may type the code next

    def test_the_caps_are_raised(self):
        with mock.patch.dict(os.environ):
            for k in ("KB_LOGIN_LIMIT_ALL", "KB_OAUTH_REGISTER_LIMIT_ALL"):
                os.environ.pop(k, None)
            self.assertGreaterEqual(admin.login_throttle().total, 1000)
            self.assertGreaterEqual(admin.register_throttle().total, 1000)


# ------------------------------------------------------------------------------------------ 5
class SignInsInProgressUnderAFlood(unittest.TestCase):
    def setUp(self):
        self.srv = oauth.Server({"KB_LOGIN_URL": "http://127.0.0.1:1/x"}, lambda t: None, "")
        self.client = self.srv.register({"redirect_uris": ["http://127.0.0.1:5000/cb"]})
        self.flood = self.srv.register({"redirect_uris": ["http://127.0.0.1:5000/cb"]})

    def q(self, client):
        return {"client_id": client["client_id"], "redirect_uri": "http://127.0.0.1:5000/cb",
                "response_type": "code", "code_challenge": "x", "code_challenge_method": "S256"}

    def test_many_addresses_do_not_push_out_a_fresh_sign_in(self):
        with mock.patch.object(oauth, "MAX_TXNS", 20):
            mine = self.srv.start(self.q(self.client), "1.1.1.1")
            for i in range(200):
                try:
                    self.srv.start(self.q(self.flood), f"2.0.{i // 250}.{i % 250}")
                except oauth.OAuthError as e:
                    self.assertEqual(e.status, 503)
        self.assertIn(mine, self.srv.txns)

    def test_one_address_pushes_out_only_its_own(self):
        mine = self.srv.start(self.q(self.client), "1.1.1.1")
        for _ in range(100):
            self.srv.start(self.q(self.flood), "6.6.6.6")
        self.assertIn(mine, self.srv.txns)
        self.assertLessEqual(sum(1 for t in self.srv.txns.values() if t["ip"] == "6.6.6.6"), oauth.TXNS_PER_ADDRESS)


# ------------------------------------------------------------------------------------------ 6
class AddonCacheUnderThreads(DataHome):
    def test_reads_and_writes_at_once_never_fail(self):
        self.write(["metrics"])
        errors, done = [], threading.Event()

        def read():
            while not done.is_set():
                try:
                    addons.enabled()
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                    return

        threads = [threading.Thread(target=read) for _ in range(8)]
        for t in threads:
            t.start()
        for i in range(300):
            addons.set_enabled("audit", i % 2 == 0)
        done.set()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


# ------------------------------------------------------------------------------------------ 7
@unittest.skipUnless(shutil.which("node"), "needs Node")
class SandboxServiceSurvivesAFailedStart(unittest.TestCase):
    def test_a_failed_spawn_is_answered_and_the_service_lives(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        folder = os.path.join(tmp, "sandbox")
        os.makedirs(folder)
        shutil.copy(os.path.join(ROOT, "sandbox", "socket-server.mjs"), folder)
        sock_path = os.path.join(tmp, "s.sock")
        proc = subprocess.Popen(["node", os.path.join(folder, "socket-server.mjs")], stderr=subprocess.PIPE,
                                env={**os.environ, "KB_SANDBOX_SOCKET": sock_path})
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        for _ in range(100):
            if os.path.exists(sock_path):
                break
            time.sleep(0.05)
        shutil.rmtree(folder)   # the runner's folder is gone: the spawn fails (no cwd)
        for _ in range(2):
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(10)
            s.connect(sock_path)
            s.sendall(b'{"code":"1"}\n')
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            s.close()
            self.assertIn("error", json.loads(data.decode().strip().splitlines()[-1]))
        self.assertIsNone(proc.poll())


# ------------------------------------------------------------------------------------------ 8, 12
class LocalMcpMessages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import kb_api
        cls.calls = []

        def fake_call(name, args, allowed=None, caller=None):
            cls.calls.append(name)
            if args.get("symbol") == "boom":
                raise TypeError("broken")
            return "ok"
        cls.p = mock.patch.object(kb_core, "call_tool", fake_call)
        cls.p.start()
        cls.srv = serve(ThreadingHTTPServer(("127.0.0.1", 0), kb_api.make_handler("127.0.0.1", None)))
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()
        stop(cls.srv)

    def setUp(self):
        self.calls.clear()

    def post(self, body):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/mcp", body=json.dumps(body), headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data) if data else None

    def call(self, symbol="A", **extra):
        return {"jsonrpc": "2.0", "method": "tools/call",
                "params": {"name": "graph_explain", "arguments": {"symbol": symbol}}, **extra}

    def test_a_notification_runs_no_tool(self):
        status, body = self.post(self.call())
        self.assertEqual((status, body, self.calls), (202, None, []))

    def test_a_message_without_jsonrpc_is_invalid(self):
        status, body = self.post({"id": 1, "method": "ping"})
        self.assertEqual(body["error"]["code"], -32600)

    def test_a_bad_batch_item_fails_alone(self):
        status, body = self.post([5, {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
        self.assertEqual(status, 200)
        self.assertEqual([a.get("id") for a in body], [None, 2])

    def test_a_fault_of_a_tool_is_an_error_answer(self):
        status, body = self.post(self.call("boom", id=3))
        self.assertEqual(status, 200)
        self.assertTrue(body["result"]["isError"])

    def test_a_long_batch_is_refused_before_anything_runs(self):
        status, body = self.post([self.call(id=i) for i in range(mcp_rpc.MAX_BATCH + 1)])
        self.assertEqual(status, 400)
        self.assertEqual(self.calls, [])


class BatchLimits(unittest.TestCase):
    def test_the_answers_stop_at_the_size_cap(self):
        ran = []

        def one(msg):
            ran.append(msg["id"])
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {"text": "x" * 1000}}
        with mock.patch.object(mcp_rpc, "MAX_RESPONSE", 3000):
            status, answers = mcp_rpc.handle_payload([{"jsonrpc": "2.0", "id": i} for i in range(10)], one)
        self.assertEqual(status, 200)
        self.assertLess(len(ran), 10)
        self.assertEqual(answers[-1]["error"]["code"], -32000)
        self.assertEqual(len(answers), 10)

    def test_kb_http_refuses_a_long_batch(self):
        with mock.patch.object(kb_core, "tools", side_effect=AssertionError("ran")):
            status, body = mcp_rpc.handle_payload(
                [{"jsonrpc": "2.0", "id": i, "method": "tools/list"} for i in range(mcp_rpc.MAX_BATCH + 1)],
                lambda m: kb_http.handle_message(m, kb_http.Identity({"name": "ann"}, ["api"])))
        self.assertEqual(status, 400)


# ------------------------------------------------------------------------------------------ 9
class ClientsFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "oauth-clients.json")

    def test_a_save_does_not_use_a_fixed_temp_name(self):
        os.makedirs(f"{self.path}.tmp")   # another writer's fixed name is taken
        srv = oauth.Server({}, lambda t: None, self.path)
        client = srv.register({"redirect_uris": ["http://127.0.0.1:5000/cb"]})
        self.assertIn(client["client_id"], oauth.Server({}, lambda t: None, self.path).clients)

    def test_a_failed_save_is_a_503_and_keeps_nothing(self):
        srv = oauth.Server({}, lambda t: None, self.path)
        with mock.patch.object(oauth.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(oauth.OAuthError) as e:
                srv.register({"redirect_uris": ["http://127.0.0.1:5000/cb"]})
        self.assertEqual(e.exception.status, 503)
        self.assertEqual(srv.clients, {})
        self.assertEqual([n for n in os.listdir(self.tmp.name)], [])


# ------------------------------------------------------------------------------------------ 10
class ForgotKeepsTheInvite(unittest.TestCase):
    def test_the_invite_link_still_works_after_a_forgot_mail(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clock = [1000.0]
        store = users.Store(os.path.join(tmp.name, "users.json"), now=lambda: clock[0])
        store.add("ann@example.com", ["api"], key=False)
        invite = store.start_reset("ann@example.com", users.INVITE_TTL, invite=True)
        clock[0] += users.RESET_EVERY + 1
        reset = store.start_reset("ann@example.com", users.RESET_TTL)
        clock[0] += users.RESET_TTL + 1    # the forgot link ended; the invite works for days
        self.assertIsNone(store.reset_user(reset))
        self.assertEqual(store.reset_user(invite), "ann@example.com")
        store.finish_reset(invite, "a long password 1")
        self.assertIsNone(store.reset_user(invite))
        self.assertFalse(store.all()["ann@example.com"]["reset_pending"])


# ------------------------------------------------------------------------------------------ 3, 11
class LoopbackRequestsCount(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bodies = []

        def login(body, core):
            cls.bodies.append(body)
            return 401, {"message": "Wrong user name or password."}
        access = types.SimpleNamespace(login=login, permissions=lambda token, projects, core: (401, {}))
        cls.patches = [mock.patch.object(kb_core, "KB", cls.tmp.name),
                       mock.patch.object(kb_core, "PROJECTS", {"api": []}),
                       mock.patch.object(kb_core, "warm_qmd", lambda: None)]
        for p in cls.patches:
            p.start()
        auth = kb_http.Authenticator(url="http://127.0.0.1:1/unset", ttl=0)
        env = {"KB_LOGIN_URL": "http://127.0.0.1:1/unset"}
        cls.srv = kb_http.make_server("127.0.0.1", 0, auth, None, oauth_env=env, admins={"ann"}, access=access)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        auth.url = f"{cls.base}/local/permissions"
        env["KB_LOGIN_URL"] = f"{cls.base}/local/login"
        serve(cls.srv)

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        stop(cls.srv)
        cls.tmp.cleanup()
        admin.set_access(admin.UNSET)

    def setUp(self):
        self.srv.RequestHandlerClass.login_limit = admin.Throttle(3, 0, 300)
        self.bodies.clear()

    def post(self, path, form, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        ctype = "application/json" if path.startswith("/local/") else "application/x-www-form-urlencoded"
        body = json.dumps(form) if path.startswith("/local/") else urllib.parse.urlencode(form)
        c.request("POST", path, body=body, headers={"Content-Type": ctype, **(headers or {})})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_a_local_process_is_counted(self):
        got = [self.post("/local/login", {"username": "ann", "password": "x"}) for _ in range(4)]
        self.assertEqual(got[-1], 429)

    def test_the_servers_own_call_is_not_counted_twice(self):
        got = [self.post("/admin/login", {"username": "ann", "password": "x"}) for _ in range(3)]
        self.assertNotIn(429, got)
        self.assertEqual(len(self.bodies), 3)

    def test_the_plugin_gets_the_address_of_the_person(self):
        self.post("/admin/login", {"username": "ann", "password": "x"}, {"X-Forwarded-For": "9.9.9.9"})
        self.post("/local/login", {"username": "ann", "password": "x", "ip": "1.1.1.1"},
                  {"X-Forwarded-For": "8.8.8.8"})
        self.assertEqual([b["ip"] for b in self.bodies], ["9.9.9.9", "8.8.8.8"])

    def test_a_guessed_self_header_does_not_help(self):
        got = [self.post("/local/login", {"username": "ann", "password": "x"}, {"X-KB-Self": "guess"})
               for _ in range(4)]
        self.assertEqual(got[-1], 429)


# ------------------------------------------------------------------------------------------ 13
class TokenAdmission(unittest.TestCase):
    def test_checks_at_once_are_bounded(self):
        auth = kb_http.Authenticator(url="http://x", ttl=60, max_checks=2, check_wait=0.05)
        gate = threading.Event()

        def slow(token):
            gate.wait(5)
            return kb_http.AuthError(401, "no")
        with mock.patch.object(auth, "_ask", side_effect=slow):
            threads = [threading.Thread(target=lambda t=t: self.assertRaises(kb_http.AuthError, auth.check,
                                                                              f"Bearer tok{t}")) for t in range(2)]
            for t in threads:
                t.start()
            time.sleep(0.1)
            with self.assertRaises(kb_http.AuthError) as e:
                auth.check("Bearer third")
            gate.set()
            for t in threads:
                t.join()
        self.assertEqual(e.exception.status, 503)

    def test_a_malformed_token_never_reaches_the_webhook(self):
        auth = kb_http.Authenticator(url="http://x", ttl=60)
        with mock.patch.object(auth, "_ask", side_effect=AssertionError("asked")):
            for bad in ("x" * 5000, "abc\x01def", "abécd"):
                with self.assertRaises(kb_http.AuthError) as e:
                    auth.check(f"Bearer {bad}")
                self.assertEqual(e.exception.status, 401)

    def test_a_refusal_is_cached(self):
        auth = kb_http.Authenticator(url="http://x", ttl=60)
        with mock.patch.object(auth, "_ask", return_value=kb_http.AuthError(401, "no")) as ask:
            for _ in range(3):
                with self.assertRaises(kb_http.AuthError):
                    auth.check("Bearer random")
        self.assertEqual(ask.call_count, 1)


# ------------------------------------------------------------------------------------------ 14
class AccessTokenLifetime(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.srv = oauth.Server({}, lambda t: None, "", now=lambda: self.clock[0])
        self.client = self.srv.register({"redirect_uris": ["http://127.0.0.1:5000/cb"]})

    def test_the_access_token_ends_when_expires_in_says(self):
        tok = self.srv._issue("UPSTREAM", self.client["client_id"], "ann")
        self.assertNotEqual(tok["access_token"], "UPSTREAM")
        self.assertEqual(self.srv.resolve(tok["access_token"]), "UPSTREAM")
        self.clock[0] += tok["expires_in"] + 1
        self.assertIsNone(self.srv.resolve(tok["access_token"]))

    def test_a_refresh_gives_a_new_access_token(self):
        tok = self.srv._issue("UPSTREAM", self.client["client_id"], "ann")
        self.clock[0] += oauth.TOKEN_TTL + 1
        new = self.srv.token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        self.assertEqual(self.srv.resolve(new["access_token"]), "UPSTREAM")

    def test_a_revoked_client_ends_its_access_tokens(self):
        tok = self.srv._issue("UPSTREAM", self.client["client_id"], "ann")
        self.srv.revoke(self.client["client_id"])
        self.assertIsNone(self.srv.resolve(tok["access_token"]))

    def test_kb_http_checks_the_upstream_token_and_refuses_an_ended_one(self):
        auth = kb_http.Authenticator(url="http://x", ttl=0)
        auth.access_tokens = self.srv.resolve
        tok = self.srv._issue("UPSTREAM", self.client["client_id"], "ann")
        seen = []
        with mock.patch.object(auth, "_ask", side_effect=lambda t: seen.append(t) or
                               kb_http.Identity({"name": "ann"}, ["api"])):
            auth.check(f"Bearer {tok['access_token']}")
            self.clock[0] += oauth.TOKEN_TTL + 1
            with self.assertRaises(kb_http.AuthError) as e:
                auth.check(f"Bearer {tok['access_token']}")
        self.assertEqual((seen, e.exception.status), (["UPSTREAM"], 401))


# ------------------------------------------------------------------------------------------ 15
class McpJsonMode(unittest.TestCase):
    def test_a_replaced_file_is_owner_only_from_the_start(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, ".mcp.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        os.chmod(path, 0o644)
        modes = []
        real = os.replace

        def replace(src, dst):
            modes.append(stat.S_IMODE(os.stat(src).st_mode))
            return real(src, dst)
        with mock.patch.object(kb_login.os, "replace", side_effect=replace):
            kb_login.write_mcp_json(path, "secret", "https://kb.example/mcp")
        self.assertEqual(modes, [0o600])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(tmp.name)), [".mcp.json"])


# ------------------------------------------------------------------------------------------ 16
class Housekeeping(unittest.TestCase):
    def test_the_sign_in_page_names_no_api(self):
        self.assertNotIn(b"of the API", oauth.page("/authorize", "t", "password"))

    def test_gitignore_covers_the_temp_files(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        for want in ("/users.json.lock", "/.users.json.*.tmp", "/addons.json.tmp.*", "/settings.json.tmp.*"):
            self.assertIn(want, lines)
        self.assertNotIn("/users.json.tmp", lines)


if __name__ == "__main__":
    unittest.main()
