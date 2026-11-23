#!/usr/bin/env python3
"""The admin pages (bin/admin.py through bin/kb-http): sign-in, who gets in, CSRF, revoke, charts."""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from _kbtest import BIN  # noqa: F401

import admin
import kb_core
from test_oauth import TOKEN, FakeApi, kb_http  # the same fake webhooks: password, then a code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def serve(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Admin(unittest.TestCase):
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
        cls.srv = serve(kb_http.make_server("127.0.0.1", 0, auth, None, oauth_env=env, admins={"ann"}))
        cls.other = serve(kb_http.make_server("127.0.0.1", 0, auth, None, oauth_env=env, admins={"someone"}))
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.other_base = f"http://127.0.0.1:{cls.other.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        for s in (cls.srv, cls.other, cls.api):
            s.shutdown()
            s.server_close()
        cls.tmp.cleanup()

    def client(self):
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)

    def req(self, opener, method, url, form=None):
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        if data:
            r.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with opener.open(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode()

    def sign_in(self, opener, base, code="123456"):
        self.req(opener, "POST", f"{base}/admin/login", {"username": "ann", "password": "pw"})
        status, _, page = self.req(opener, "POST", f"{base}/admin/login", {"username": "ann", "password": "pw"})
        lid = re.search(r'name="lid" value="([^"]+)"', page).group(1)
        return self.req(opener, "POST", f"{base}/admin/login", {"lid": lid, "code": code})

    def csrf(self, opener):
        _, _, page = self.req(opener, "GET", f"{self.base}/admin")
        return re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def test_pages_need_a_session(self):
        status, headers, _ = self.req(self.client(), "GET", f"{self.base}/admin/usage")
        self.assertEqual(status, 302)
        self.assertTrue(headers["Location"].endswith("/admin/login"))

    def test_admin_signs_in_and_sees_every_page(self):
        c = self.client()
        status, headers, _ = self.sign_in(c, self.base)
        self.assertEqual(status, 302)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        for path, word in (("", "Knowledge base"), ("/index", "Storage"), ("/usage", "Usage"),
                           ("/clients", "Sign-in clients"), ("/howto", "Connect an agent")):
            status, headers, page = self.req(c, "GET", f"{self.base}/admin{path}")
            self.assertEqual(status, 200, path)
            self.assertIn(word, page)
            self.assertNotIn("<script", page)

    def test_howto_page_gives_the_address_of_this_server(self):
        c = self.client()
        self.sign_in(c, self.base)
        _, _, page = self.req(c, "GET", f"{self.base}/admin/howto")
        self.assertIn(f"{self.base}/mcp", page)                 # the address to paste, not a placeholder
        self.assertIn("claude mcp add --transport http kb", page)
        self.assertIn("kb-login", page)
        self.assertIn('class="on">How to<', page)

    def test_wrong_code_and_non_admin(self):
        status, _, page = self.sign_in(self.client(), self.base, code="000000")
        self.assertIn("Wrong code", page)
        status, _, page = self.sign_in(self.client(), self.other_base)
        self.assertEqual(status, 403)
        self.assertIn("not a KB admin", page)

    def test_audit_records_sign_ins_and_actions(self):
        from usage import Usage
        u = Usage(os.path.join(self.tmp.name, "logs", "audit-test.json"))
        u.record("graph_explain", "ann", {"symbol": "Order"}, False, 0.4)
        with mock.patch.object(kb_http, "USAGE", u):
            self.sign_in(self.client(), self.base, code="000000")          # refused: wrong code
            c = self.client()
            self.sign_in(c, self.base)
            self.req(c, "POST", f"{self.base}/admin/clients/revoke", {"csrf": self.csrf(c), "client_id": "kb-nope"})
            status, _, page = self.req(c, "GET", f"{self.base}/admin/audit")
            _, _, errors = self.req(c, "GET", f"{self.base}/admin/audit?errors=1&tool=graph_explain")
        self.assertEqual(status, 200)
        for text in ("admin sign-in refused", "admin sign-in", "revoke client", "kb-nope"):
            self.assertIn(text, page)
        self.assertIn("Order", errors)
        self.assertIn("<b>error</b>", errors)
        actions = [a for _, _, a, _ in u.events()]
        self.assertIn("admin sign-in", actions)
        self.assertIn("admin sign-in refused", actions)

    def test_audit_off_records_nothing_and_has_no_page(self):
        from usage import Usage
        u = Usage(os.path.join(self.tmp.name, "logs", "audit-off.json"))
        with mock.patch.object(kb_http, "USAGE", u), \
                mock.patch.dict(os.environ, {"KB_ADDONS": "admin"}):
            c = self.client()
            self.sign_in(c, self.base)
            status, _, _ = self.req(c, "GET", f"{self.base}/admin/audit")
            _, _, home = self.req(c, "GET", f"{self.base}/admin")
        self.assertEqual(status, 404)
        self.assertNotIn("/admin/audit", home)
        self.assertEqual(u.events(), [])

    def test_post_needs_the_sessions_csrf_token(self):
        c = self.client()
        self.sign_in(c, self.base)
        status, _, page = self.req(c, "POST", f"{self.base}/admin/rebuild", {"csrf": "forged"})
        self.assertEqual(status, 403)
        status, _, page = self.req(c, "POST", f"{self.base}/admin/clients/revoke",
                                   {"csrf": self.csrf(c), "client_id": "kb-nope"})
        self.assertIn("No such client", page)

    def test_revoke_ends_a_client(self):
        server = self.srv.RequestHandlerClass.oauth
        cid = server.register({"redirect_uris": ["http://localhost:1/cb"]})["client_id"]
        server.refresh["rt"] = {"token": TOKEN, "client_id": cid}
        c = self.client()
        self.sign_in(c, self.base)
        _, _, page = self.req(c, "POST", f"{self.base}/admin/clients/revoke", {"csrf": self.csrf(c), "client_id": cid})
        self.assertIn("Revoked", page)
        self.assertNotIn(cid, server.clients)
        self.assertNotIn("rt", server.refresh)

    def test_logout(self):
        c = self.client()
        self.sign_in(c, self.base)
        self.req(c, "POST", f"{self.base}/admin/logout", {"csrf": self.csrf(c)})
        status, _, _ = self.req(c, "GET", f"{self.base}/admin")
        self.assertEqual(status, 302)

    def test_no_admins_no_pages(self):
        with mock.patch.dict(os.environ, {"KB_ADMINS": ""}):
            self.assertEqual(admin.admins(), set())
        self.assertTrue(admin.is_admin({"uname": "Ann"}, {"ann"}))
        self.assertFalse(admin.is_admin({"uname": "bob"}, {"ann"}))


class Questions(unittest.TestCase):
    """The Questions page: the questions people asked, the label an admin gives each one, the yellow
    mark beside the tab while some have no label, and the labelled ones as a golden set."""

    @classmethod
    def setUpClass(cls):
        cls.api = serve(ThreadingHTTPServer(("127.0.0.1", 0), FakeApi))
        api = f"http://127.0.0.1:{cls.api.server_address[1]}"
        cls.tmp = tempfile.TemporaryDirectory()
        env = {"KB_LOGIN_URL": f"{api}/login", "KB_HOOK_HEADERS": '{"app":"dev"}'}
        cls.patches = [mock.patch.object(kb_core, "KB", cls.tmp.name),
                       mock.patch.object(kb_core, "GRAPH", os.path.join(cls.tmp.name, "graph.json")),
                       mock.patch.object(kb_core, "PROJECTS", {"api": [], "web": []}),
                       mock.patch.object(kb_core, "warm_qmd", lambda: None),
                       mock.patch.dict(os.environ, {"KB_ADDONS": "admin,questions"})]  # the add-on is off by default
        for p in cls.patches:
            p.start()
        cls.usage = kb_http.Usage(os.path.join(cls.tmp.name, "usage.json"))
        auth = kb_http.Authenticator(url=f"{api}/permissions", ttl=0)
        cls.srv = serve(kb_http.make_server("127.0.0.1", 0, auth, cls.usage, oauth_env=env, admins={"ann"}))
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        kb_http.USAGE = None
        admin.set_badge_source(None)
        for p in cls.patches:
            p.stop()
        for s in (cls.srv, cls.api):
            s.shutdown()
            s.server_close()
        cls.tmp.cleanup()

    client, req, sign_in, csrf = Admin.client, Admin.req, Admin.sign_in, Admin.csrf

    def label(self, c, **form):
        return self.req(c, "POST", f"{self.base}/admin/questions/label", form)

    def test_questions_are_labelled_and_the_mark_goes(self):
        c = self.client()
        self.sign_in(c, self.base)
        _, _, page = self.req(c, "GET", f"{self.base}/admin")
        self.assertIn(">Questions</a>", page, "the tab is there, and nothing waits yet")
        self.assertNotIn('class="wait"', page)

        self.usage.record("search", "bob", {"query": "where is the payout job"}, True, 0.1)
        self.usage.record("search", "eve", {"query": "where is the payout job"}, True, 0.1)
        self.usage.record("search", "bob", {"query": "kafka topics"}, True, 0.1)
        self.usage.record("graph_explain", "bob", {"symbol": "Payout"}, True, 0.1)  # not a question tool
        _, _, page = self.req(c, "GET", f"{self.base}/admin")
        self.assertRegex(page, r'Questions <span class="wait"[^>]*>2</span>', "the mark shows on every page")
        _, _, page = self.req(c, "GET", f"{self.base}/admin/questions")
        self.assertIn("where is the payout job", page)
        self.assertIn("kafka topics", page)
        self.assertNotIn("Payout</td>", page)
        self.assertNotIn("<script", page)

        csrf = self.csrf(c)
        status, _, page = self.label(c, csrf="wrong", tool="search", arg="kafka topics", project="all", how="no_answer")
        self.assertEqual(status, 403, "a form without the CSRF token is refused")
        self.assertEqual(self.usage.unlabelled(), 2)
        status, _, page = self.label(c, csrf=csrf, tool="search", arg="kafka topics", project="all", how="expected")
        self.assertIn("Not saved", page, "an empty label is not a label")
        self.assertEqual(self.usage.unlabelled(), 2)

        status, _, page = self.label(c, csrf=csrf, tool="search", arg="where is the payout job", project="all",
                                     how="expected", expected="app/Jobs/Payout.php")
        self.assertEqual(status, 200)
        self.assertIn("Saved.", page)
        self.label(c, csrf=csrf, tool="search", arg="kafka topics", project="all", how="no_answer")
        _, _, page = self.req(c, "GET", f"{self.base}/admin")
        self.assertNotIn('class="wait"', page, "every question has a label: the mark is gone")

        status, _, body = self.req(c, "GET", f"{self.base}/admin/questions.json")
        golden = json.loads(body)["questions"]
        self.assertEqual([(q["question"], q["expected"], q["no_answer"]) for q in golden],
                         [("where is the payout job", "app/Jobs/Payout.php", False), ("kafka topics", "", True)])

        self.label(c, csrf=csrf, tool="search", arg="kafka topics", project="all", how="clear")
        self.assertEqual(self.usage.unlabelled(), 1, "a cleared label puts the question back")

    def test_without_the_addon_there_is_no_tab_and_no_page(self):
        c = self.client()
        self.sign_in(c, self.base)
        csrf = self.csrf(c)
        self.usage.record("search", "bob", {"query": "a question nobody labels"}, True, 0.1)
        with mock.patch.dict(os.environ, {"KB_ADDONS": "admin"}):
            _, _, page = self.req(c, "GET", f"{self.base}/admin")
            self.assertNotIn("Questions", page)
            self.assertNotIn('class="wait"', page)
            for path in ("/admin/questions", "/admin/questions.json"):
                self.assertEqual(self.req(c, "GET", f"{self.base}{path}")[0], 404, path)
            status, _, _ = self.label(c, csrf=csrf, tool="search", arg="a question nobody labels", project="all",
                                      how="skip")
            self.assertEqual(status, 404)
        self.usage.label("search", "a question nobody labels", "all", skip=True)  # leave nothing for the other test

    def test_the_page_needs_a_session(self):
        status, headers, _ = self.req(self.client(), "GET", f"{self.base}/admin/questions.json")
        self.assertEqual(status, 302)
        self.assertTrue(headers["Location"].endswith("/admin/login"))


if __name__ == "__main__":
    unittest.main()
