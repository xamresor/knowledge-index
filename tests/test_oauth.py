#!/usr/bin/env python3
"""bin/oauth.py through bin/kb-http: the browser sign-in an MCP client runs by itself.

A fake API answers both webhooks of access protocol v1 (login: password, then a code; permissions); the test
walks the client's side of the flow: discovery, registration, the login page, the code, the token,
a tool call with it, a refresh, and the ways it must refuse.
"""
from __future__ import annotations

import base64
import hashlib
import html
import importlib.machinery
import importlib.util
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from _kbtest import BIN

import kb_core
import oauth

loader = importlib.machinery.SourceFileLoader("kb_http_oauth", os.path.join(BIN, "kb-http"))
spec = importlib.util.spec_from_loader(loader.name, loader)
kb_http = importlib.util.module_from_spec(spec)
loader.exec_module(kb_http)

TOKEN = "42|devtoken"
STATE = {"valid": True}


class FakeApi(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("protocol") != 1:
            self._json(400, {"message": "protocol 1 expected"})
        elif self.path == "/permissions":    # the permission webhook
            ok = self.headers.get("Authorization") == f"Bearer {TOKEN}" and STATE["valid"]
            self._json(200, {"user": {"uname": "ann"}, "projects": ["api"]}) if ok \
                else self._json(401, {"message": "Unauthenticated."})
        elif self.headers.get("app") != "dev":
            self._json(403, {"message": "wrong app"})
        elif self.path == "/login":          # the login webhook, both steps
            if body == {"protocol": 1, "username": "ann", "password": "pw"}:
                self._json(200, {"two_factor": {"challenge": "ch1"}})
            elif body == {"protocol": 1, "challenge": "ch1", "code": "123456"}:
                self._json(200, {"token": TOKEN})
            elif "challenge" in body:
                self._json(422, {"message": "Wrong code."})
            else:
                self._json(422, {"message": "Wrong user name or password."})
        else:
            self._json(404, {"message": "not found"})


def serve(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def pkce():
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    return verifier, oauth.b64url_sha256(verifier)


class OAuthFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = serve(ThreadingHTTPServer(("127.0.0.1", 0), FakeApi))
        api = f"http://127.0.0.1:{cls.api.server_address[1]}"
        cls.tmp = tempfile.TemporaryDirectory()
        env = {"KB_LOGIN_URL": f"{api}/login", "KB_HOOK_HEADERS": '{"app":"dev"}'}
        cls.patches = [mock.patch.object(kb_core, "KB", cls.tmp.name),
                       mock.patch.object(kb_core, "call_tool", lambda n, a, allowed=None, caller=None: "ok"),
                       mock.patch.object(kb_core, "PROJECTS", {"api": [], "web": []}),
                       mock.patch.object(kb_core, "warm_qmd", lambda: None)]
        for p in cls.patches:
            p.start()
        cls.srv = serve(kb_http.make_server("127.0.0.1", 0, kb_http.Authenticator(url=f"{api}/permissions", ttl=0),
                                            None, oauth_env=env))
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        for s in (cls.srv, cls.api):
            s.shutdown()
            s.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        STATE["valid"] = True

    # ---------------------------------------------------------------- helpers
    def call(self, method, path, data=None, form=False, headers=None):
        body = None
        hdrs = dict(headers or {})
        if data is not None:
            if form:
                body = urllib.parse.urlencode(data).encode()
                hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                body = json.dumps(data).encode()
                hdrs["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=body, method=method, headers=hdrs)
        try:
            with OPENER.open(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode()

    def register(self, uris=("http://localhost:33418/callback",)):
        status, _, text = self.call("POST", "/register", {"redirect_uris": list(uris),
                                                          "client_name": "Claude Code"})
        return status, json.loads(text)

    def sign_in(self, client, verifier_challenge, code="123456", state="s1"):
        verifier, challenge = verifier_challenge
        q = urllib.parse.urlencode({"response_type": "code", "client_id": client["client_id"],
                                    "redirect_uri": client["redirect_uris"][0], "state": state,
                                    "code_challenge": challenge, "code_challenge_method": "S256"})
        status, _, page = self.call("GET", f"/authorize?{q}")
        self.assertEqual(status, 200, page)
        txn = html.unescape(re.search(r'name="txn" value="([^"]+)"', page).group(1))
        status, _, page = self.call("POST", "/authorize", {"txn": txn, "step": "password",
                                                           "username": "ann", "password": "pw"}, form=True)
        self.assertIn('name="code"', page)
        status, headers, _ = self.call("POST", "/authorize", {"txn": txn, "step": "code", "code": code},
                                       form=True)
        return status, headers

    # ---------------------------------------------------------------- the flow
    def test_discovery_points_to_this_server(self):
        status, headers, _ = self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                       headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "kb.example",
                                                "X-Forwarded-Prefix": "/kb"})
        self.assertEqual(status, 401)
        self.assertIn('resource_metadata="https://kb.example/kb/.well-known/oauth-protected-resource"',
                      headers["WWW-Authenticate"])
        _, _, text = self.call("GET", "/.well-known/oauth-protected-resource",
                               headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "kb.example",
                                        "X-Forwarded-Prefix": "/kb"})
        meta = json.loads(text)
        self.assertEqual(meta["resource"], "https://kb.example/kb/mcp")
        self.assertEqual(meta["authorization_servers"], ["https://kb.example/kb"])
        _, _, text = self.call("GET", "/.well-known/oauth-authorization-server")
        self.assertEqual(json.loads(text)["code_challenge_methods_supported"], ["S256"])

    def test_full_sign_in_then_tool_call_then_refresh(self):
        _, client = self.register()
        v = pkce()
        status, headers = self.sign_in(client, v)
        self.assertEqual(status, 302)
        loc = urllib.parse.urlsplit(headers["Location"])
        _, page_headers, _ = self.call("GET", "/authorize?client_id=x")
        self.assertIn("http://localhost:*", page_headers["Content-Security-Policy"])  # the 302 may land
        q = dict(urllib.parse.parse_qsl(loc.query))
        self.assertEqual(q["state"], "s1")
        status, _, text = self.call("POST", "/token", {
            "grant_type": "authorization_code", "code": q["code"], "code_verifier": v[0],
            "redirect_uri": client["redirect_uris"][0], "client_id": client["client_id"]}, form=True)
        self.assertEqual(status, 200, text)
        tok = json.loads(text)
        # An access token of this server that stands for the API token and ends after expires_in.
        self.assertTrue(tok["access_token"].startswith(oauth.ACCESS_PREFIX))
        self.assertEqual(tok["expires_in"], oauth.TOKEN_TTL)
        status, _, _ = self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                 headers={"Authorization": f"Bearer {tok['access_token']}"})
        self.assertEqual(status, 200)
        # the code is single use
        status, _, _ = self.call("POST", "/token", {"grant_type": "authorization_code", "code": q["code"],
                                                    "code_verifier": v[0]}, form=True)
        self.assertEqual(status, 400)
        # refresh while the API token is alive, rotated
        status, _, text = self.call("POST", "/token", {"grant_type": "refresh_token",
                                                       "refresh_token": tok["refresh_token"]}, form=True)
        self.assertEqual(status, 200)
        new = json.loads(text)
        self.assertTrue(new["access_token"].startswith(oauth.ACCESS_PREFIX))
        self.assertNotEqual(new["access_token"], tok["access_token"])
        status, _, _ = self.call("POST", "/token", {"grant_type": "refresh_token",
                                                    "refresh_token": tok["refresh_token"]}, form=True)
        self.assertEqual(status, 400)  # the old refresh token is gone
        # an expired API token cannot be refreshed: sign in again
        STATE["valid"] = False
        status, _, text = self.call("POST", "/token", {"grant_type": "refresh_token",
                                                       "refresh_token": new["refresh_token"]}, form=True)
        self.assertEqual((status, json.loads(text)["error"]), (400, "invalid_grant"))

    def test_wrong_pkce_verifier_is_refused(self):
        _, client = self.register()
        status, headers = self.sign_in(client, pkce())
        code = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(headers["Location"]).query))["code"]
        status, _, text = self.call("POST", "/token", {"grant_type": "authorization_code", "code": code,
                                                       "code_verifier": "wrong"}, form=True)
        self.assertEqual((status, json.loads(text)["error"]), (400, "invalid_grant"))

    def test_wrong_code_shows_the_form_again(self):
        _, client = self.register()
        status, headers = self.sign_in(client, pkce(), code="000000")
        self.assertEqual(status, 200)
        self.assertNotIn("Location", headers)

    def test_only_loopback_redirects(self):
        status, body = self.register(["https://evil.example/cb"])
        self.assertEqual((status, body["error"]), (400, "invalid_redirect_uri"))

    def test_unregistered_redirect_is_refused(self):
        _, client = self.register()
        v = pkce()
        q = urllib.parse.urlencode({"response_type": "code", "client_id": client["client_id"],
                                    "redirect_uri": "http://localhost:1/other", "code_challenge": v[1],
                                    "code_challenge_method": "S256"})
        status, _, _ = self.call("GET", f"/authorize?{q}")
        self.assertEqual(status, 400)

    def test_plain_pkce_is_refused(self):
        _, client = self.register()
        q = urllib.parse.urlencode({"response_type": "code", "client_id": client["client_id"],
                                    "redirect_uri": client["redirect_uris"][0], "code_challenge": "x",
                                    "code_challenge_method": "plain"})
        status, _, _ = self.call("GET", f"/authorize?{q}")
        self.assertEqual(status, 400)

    def test_clients_survive_a_restart(self):
        _, client = self.register()
        again = oauth.Server({}, lambda t: None, os.path.join(self.tmp.name, "logs", "oauth-clients.json"))
        self.assertIn(client["client_id"], again.clients)


class Off(unittest.TestCase):
    def test_off_without_login_settings(self):
        with mock.patch.object(kb_core, "KB", tempfile.gettempdir()):
            self.assertEqual(kb_http.login_env({}), {})
            self.assertEqual(kb_http.login_env({"KB_LOGIN_URL": "https://a", "KB_OAUTH": "off"}), {})
            self.assertEqual(kb_http.login_env({"KB_LOGIN_URL": "https://a"})["KB_LOGIN_URL"], "https://a")


if __name__ == "__main__":
    unittest.main()


class SignInAudit(unittest.TestCase):
    """Browser sign-ins and refusals reach the audit log through Server.on_event."""

    def test_refused_and_finished_sign_ins_are_reported(self):
        seen = []
        srv = oauth.Server({"KB_LOGIN_URL": "http://hooks/login"}, lambda t: None)
        srv.on_event = lambda user, action, detail: seen.append((user, action))
        srv.clients["c1"] = {"client_name": "Claude Code"}
        srv.txns["t1"] = {"client_id": "c1", "redirect_uri": "http://127.0.0.1:9/cb", "challenge": "x",
                          "state": "", "exp": srv.now() + 600}
        with mock.patch.object(oauth.kb_login, "post", side_effect=oauth.kb_login.LoginError("HTTP 401: wrong")):
            with self.assertRaises(oauth.kb_login.LoginError):
                srv.password_step("t1", "ann", "bad")
        with mock.patch.object(oauth.kb_login, "post", return_value={"token": "tok"}):
            srv.password_step("t1", "ann", "pw")
        self.assertEqual(seen, [("ann", "sign-in refused"), ("ann", "browser sign-in")])


class ClientUsers(unittest.TestCase):
    """The admin clients page shows who signed in through each client."""

    def test_sign_in_and_refresh_note_the_user(self):
        import tempfile as tf
        d = tf.mkdtemp()
        clock = [1000.0]
        path = os.path.join(d, "clients.json")
        srv = oauth.Server({"KB_LOGIN_URL": "http://hooks/login"}, lambda t: None, path, now=lambda: clock[0])
        srv.clients["c1"] = {"client_name": "Claude Code"}
        srv.txns["t1"] = {"client_id": "c1", "redirect_uri": "http://127.0.0.1:9/cb", "challenge": "x",
                          "state": "", "exp": clock[0] + 600}
        with mock.patch.object(oauth.kb_login, "post", return_value={"token": "tok"}):
            srv.password_step("t1", "ann", "pw")
        self.assertEqual(srv.clients["c1"]["users"], {"ann": 1000})
        code = next(iter(srv.codes))
        verifier = "v" * 43
        srv.codes[code]["challenge"] = oauth.b64url_sha256(verifier)
        rt = srv.token({"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                        "redirect_uri": "http://127.0.0.1:9/cb"})["refresh_token"]
        clock[0] = 2000.0
        srv.token({"grant_type": "refresh_token", "refresh_token": rt})
        self.assertEqual(srv.clients["c1"]["users"], {"ann": 2000})
        again = oauth.Server({}, lambda t: None, path)
        self.assertEqual(again.clients["c1"]["users"], {"ann": 2000})
        import admin
        self.assertIn("ann", admin.client_users(again.clients["c1"]))
        self.assertEqual(admin.client_users({}), "—")
