"""The KB's own users, rights and tokens (the access plugin contrib/users): the store, the two webhooks that
kb-http answers from the plugin, the Users tab of an admin and the "My tokens" page of every person."""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from _kbtest import BIN  # noqa: F401

import importlib.util

import kb_core
import plugins
from test_oauth import kb_http

_spec = importlib.util.spec_from_file_location(
    "kb_plugin_users", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "contrib", "users", "plugin.py"))
users = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(users)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "users.json")
        self.clock = [1000.0]
        self.store = users.Store(self.path, now=lambda: self.clock[0])

    def test_a_key_is_shown_once_and_only_its_hash_is_kept(self):
        key = self.store.add("Ann", ["api"])
        self.assertTrue(key.startswith(users.KEY_PREFIX))
        self.assertGreaterEqual(len(key), 40)
        with open(self.path, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn(key, raw)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        (token,) = self.store.all()["ann"]["tokens"].values()
        self.assertEqual((token["start"], token["label"]), (key[:8], "first key"))
        self.assertNotIn("hash", token)                      # the pages never get the hash

    def test_login_needs_the_name_and_the_key_of_the_same_user(self):
        ann = self.store.add("ann", ["api"])
        bob = self.store.add("bob", ["web"])
        self.assertEqual(self.store.login("ANN", ann), ann)
        for name, key in (("ann", bob), ("ann", "kbk_wrong"), ("nobody", ann), ("ann", "")):
            with self.assertRaises(users.UserError):
                self.store.login(name, key)

    def test_permissions_answer_the_asked_subset(self):
        ann = self.store.add("ann", ["api", "gone"])
        boss = self.store.add("boss", "*", admin=True)
        self.assertEqual(self.store.permissions(ann, ["api", "web"]),
                         (200, {"user": {"name": "ann"}, "projects": ["api"]}))
        self.assertEqual(self.store.permissions(boss, ["api", "web"]),
                         (200, {"user": {"name": "boss"}, "projects": "*"}))
        self.assertEqual(self.store.permissions("kbk_unknown", ["api"])[0], 401)
        nothing = self.store.add("nothing", [])
        self.assertEqual(self.store.permissions(nothing, ["api"])[0], 403)

    def test_a_user_has_several_tokens_and_each_ends_alone(self):
        first = self.store.add("ann", ["api"])
        laptop = self.store.issue("ann", "laptop")
        self.assertNotEqual(first, laptop)
        self.assertEqual(self.store.permissions(first, ["api"])[0], 200)       # a new token ends no other
        self.assertEqual(self.store.permissions(laptop, ["api"])[0], 200)
        by_label = {t["label"]: tid for tid, t in self.store.all()["ann"]["tokens"].items()}
        self.assertEqual(sorted(by_label), ["first key", "laptop"])
        self.store.remove_token("ann", by_label["first key"], keep_one=True)
        self.assertEqual(self.store.permissions(first, ["api"])[0], 401)
        self.assertEqual(self.store.permissions(laptop, ["api"])[0], 200)
        with self.assertRaises(users.UserError):                               # the last one stays
            self.store.remove_token("ann", by_label["laptop"], keep_one=True)
        with self.assertRaises(users.UserError):
            self.store.remove_token("ann", "nope")
        self.store.remove_token("ann", by_label["laptop"])                     # an admin may end the last one
        self.assertEqual(self.store.permissions(laptop, ["api"])[0], 401)

    def test_revoke_disable_and_remove(self):
        one = self.store.add("ann", ["api"])
        two = self.store.issue("ann", "second")
        self.store.update("ann", disabled=True)
        self.assertEqual(self.store.permissions(one, ["api"])[0], 401)
        with self.assertRaises(users.UserError):
            self.store.login("ann", one)
        self.store.update("ann", disabled=False)
        self.assertEqual(self.store.permissions(two, ["api"])[0], 200)
        self.store.revoke("ann")
        self.assertEqual([self.store.permissions(k, ["api"])[0] for k in (one, two)], [401, 401])
        self.store.remove("ann")
        self.assertNotIn("ann", self.store.all())

    def test_bad_names_and_duplicates_are_refused(self):
        self.store.add("ann", ["api"])
        for name in ("ann", "ANN", "", "a b", "<x>", "x" * 81):
            with self.assertRaises(users.UserError):
                self.store.add(name, ["api"])
        with self.assertRaises(users.UserError):
            self.store.add("bob", ["a b"])
        with self.assertRaises(users.UserError):
            self.store.issue("nobody")
        with self.assertRaises(users.UserError):
            self.store.issue("ann", "<b>x</b>")
        for _ in range(users.MAX_TOKENS - 1):
            self.store.issue("ann", "more")
        with self.assertRaises(users.UserError):
            self.store.issue("ann", "one too many")

    def test_admin_by_key_last_use_and_who_is_online(self):
        boss = self.store.add("boss", "*", admin=True)
        ann = self.store.add("ann", ["api"])
        self.assertTrue(self.store.admin_by_key(boss))
        self.assertFalse(self.store.admin_by_key(ann))
        self.assertFalse(self.store.admin_by_key("kbk_unknown"))
        self.assertEqual(self.store.user_by_key(ann), "ann")
        self.assertEqual(self.store.seen(), [])
        self.clock[0] = 5000.0
        self.store.permissions(ann, ["api"])
        self.clock[0] = 6000.0
        self.store.permissions(boss, ["api"])
        self.assertEqual(self.store.seen(), [("boss", 6000.0), ("ann", 5000.0)])
        (token,) = self.store.all()["ann"]["tokens"].values()
        self.assertEqual(token["last_used"], 5000.0)

    def test_a_file_of_the_first_version_keeps_its_key(self):
        key = "kbk_" + "a" * 43
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"users": {"old": {"projects": "*", "admin": True, "disabled": False, "created": 1.0,
                                         "last_used": None, "key_hash": users._hash(key), "key_start": key[:8],
                                         "key_issued": 2.0}}}, f)
        self.assertTrue(self.store.admin_by_key(key))
        (token,) = self.store.all()["old"]["tokens"].values()
        self.assertEqual((token["start"], token["created"]), (key[:8], 2.0))

    def test_a_password_signs_in_and_makes_a_sign_in_token(self):
        self.store.add("ann@example.com", ["api"], key=False)
        self.assertEqual(self.store.all()["ann@example.com"]["tokens"], {})
        for bad in ("short", "ann@example.com", "x" * 201):
            with self.assertRaises(users.UserError):
                self.store.set_password("ann@example.com", bad)
        self.store.set_password("ann@example.com", "correct horse battery")
        token = self.store.login("Ann@Example.com", "correct horse battery")
        self.assertTrue(token.startswith(users.SIGNIN_PREFIX))
        self.assertEqual(self.store.permissions(token, ["api"])[0], 200)
        with self.assertRaises(users.UserError):
            self.store.login("ann@example.com", "wrong horse battery")
        with self.assertRaises(users.UserError):
            self.store.login("nobody@example.com", "correct horse battery")
        with open(self.path, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("correct horse", raw)
        self.assertNotIn(token, raw)
        shown = self.store.all()["ann@example.com"]
        self.assertTrue(shown["has_password"])
        self.assertNotIn("pw", shown)

    def test_a_new_password_ends_the_sign_ins_and_keeps_the_tokens(self):
        self.store.add("ann@example.com", ["api"], key=False)
        self.store.set_password("ann@example.com", "correct horse battery")
        signin = self.store.login("ann@example.com", "correct horse battery")
        laptop = self.store.issue("ann@example.com", "laptop")
        self.store.set_password("ann@example.com", "another long password")
        self.assertEqual(self.store.permissions(signin, ["api"])[0], 401)
        self.assertEqual(self.store.permissions(laptop, ["api"])[0], 200)
        (tid,) = self.store.all()["ann@example.com"]["tokens"]
        self.store.remove_token("ann@example.com", tid, keep_one=True)   # with a password the last token may go

    def test_sign_in_tokens_are_few_and_end_when_idle(self):
        self.store.add("ann@example.com", ["api"], key=False)
        self.store.set_password("ann@example.com", "correct horse battery")
        tokens = []
        for i in range(users.SIGNIN_KEEP + 3):
            self.clock[0] += 10
            tokens.append(self.store.login("ann@example.com", "correct horse battery"))
        self.assertEqual(len(self.store.all()["ann@example.com"]["tokens"]), users.SIGNIN_KEEP)
        self.assertEqual(self.store.permissions(tokens[0], ["api"])[0], 401)     # the oldest went
        self.assertEqual(self.store.permissions(tokens[-1], ["api"])[0], 200)
        self.clock[0] += 5 * 365 * 86400                                        # an agent token has no end ...
        self.assertEqual(users.SIGNIN_IDLE, 0)
        self.assertEqual(self.store.permissions(tokens[-1], ["api"])[0], 200)
        with mock.patch.object(users, "SIGNIN_IDLE", 30 * 86400):                # ... unless the server sets days
            self.clock[0] += 30 * 86400 + users.TOUCH_SECONDS + 1
            self.assertEqual(self.store.permissions(tokens[-1], ["api"])[0], 401)

    def test_a_page_session_ends_its_own_token_and_never_the_token_of_an_agent(self):
        self.store.add("ann@example.com", ["api"], key=False)
        self.store.set_password("ann@example.com", "correct horse battery")
        page = self.store.login("ann@example.com", "correct horse battery", purpose="page")
        agent = self.store.login("ann@example.com", "correct horse battery")
        self.assertEqual(self.store.login("ann@example.com", agent, purpose="page"), agent)   # typed as the password
        self.assertFalse(self.store.end_signin(agent, page_only=True))     # that session ends: the agent goes on
        self.assertEqual(self.store.permissions(agent, ["api"])[0], 200)
        self.assertTrue(self.store.end_signin(page, page_only=True))
        self.assertEqual(self.store.permissions(page, ["api"])[0], 401)
        self.assertTrue(self.store.end_signin(agent))                      # a revoked client still ends it

    def test_a_sign_in_token_ends_by_its_hash_and_a_key_does_not(self):
        key = self.store.add("ann@example.com", ["api"])
        self.store.set_password("ann@example.com", "correct horse battery")
        agent = self.store.login("ann@example.com", "correct horse battery")
        self.assertFalse(self.store.end_signin_hash(users._hash(key)))
        self.assertFalse(self.store.end_signin_hash("0" * 64))
        self.assertTrue(self.store.end_signin_hash(users._hash(agent)))
        self.assertEqual(self.store.permissions(agent, ["api"])[0], 401)
        self.assertEqual(self.store.permissions(key, ["api"])[0], 200)

    def test_a_page_sign_in_token_that_no_session_ended_goes_when_idle(self):
        self.store.add("ann@example.com", ["api"], key=False)
        self.store.set_password("ann@example.com", "correct horse battery")
        page = self.store.login("ann@example.com", "correct horse battery", purpose="page")
        agent = self.store.login("ann@example.com", "correct horse battery")
        self.clock[0] += users.PAGE_IDLE + users.TOUCH_SECONDS + 1
        self.assertEqual(self.store.permissions(agent, ["api"])[0], 200)
        self.assertEqual(self.store.permissions(page, ["api"])[0], 401)

    def test_five_wrong_passwords_lock_the_name_but_not_a_token(self):
        key = self.store.add("ann@example.com", ["api"])
        self.store.set_password("ann@example.com", "correct horse battery")
        for _ in range(users.LOCK_AFTER):
            with self.assertRaises(users.UserError):
                self.store.login("ann@example.com", "a wrong password")
        with self.assertRaises(users.Locked) as locked:
            self.store.login("ann@example.com", "correct horse battery")
        self.assertGreater(locked.exception.retry, 0)
        self.assertEqual(self.store.login("ann@example.com", key), key)
        self.clock[0] += users.LOCK_SECONDS + 1
        self.assertTrue(self.store.login("ann@example.com", "correct horse battery").startswith(users.SIGNIN_PREFIX))

    def test_two_uses_of_one_reset_link_cannot_both_set_the_password(self):
        # both requests pass the first check; the second must fail when it saves (the hash runs unlocked)
        self.store.add("ann@example.com", ["api"], key=False)
        value = self.store.start_reset("ann@example.com")
        self.assertEqual(self.store.reset_user(value), "ann@example.com")
        self.store.finish_reset(value, "correct horse battery")
        with self.assertRaises(users.UserError):
            self.store.set_password("ann@example.com", "the late request", reset_value=value)
        self.assertEqual(self.store.login("ann@example.com", "correct horse battery")[:len(users.SIGNIN_PREFIX)],
                         users.SIGNIN_PREFIX)

    def test_a_reset_link_works_once_and_for_a_short_time(self):
        self.store.add("ann@example.com", ["api"], key=False)
        value = self.store.start_reset("ann@example.com")
        with open(self.path, encoding="utf-8") as f:
            self.assertNotIn(value, f.read())
        self.assertTrue(self.store.all()["ann@example.com"]["reset_pending"])
        self.assertEqual(self.store.reset_user(value), "ann@example.com")
        self.assertIsNone(self.store.reset_user("something else"))
        with self.assertRaises(users.UserError):                       # one mail in five minutes
            self.store.start_reset("ann@example.com")
        with self.assertRaises(users.UserError):
            self.store.finish_reset(value, "short")
        self.assertEqual(self.store.finish_reset(value, "correct horse battery"), "ann@example.com")
        with self.assertRaises(users.UserError):                       # once
            self.store.finish_reset(value, "another long password")
        self.clock[0] += users.RESET_EVERY + 1
        late = self.store.start_reset("ann@example.com")
        self.clock[0] += users.RESET_TTL + 1
        self.assertIsNone(self.store.reset_user(late))
        self.store.update("ann@example.com", disabled=True)
        with self.assertRaises(users.UserError):
            self.store.start_reset("ann@example.com")

    def test_a_second_process_sees_the_change(self):
        key = self.store.add("ann", ["api"])
        other = users.Store(self.path)
        self.assertEqual(other.permissions(key, ["api"])[0], 200)
        other.revoke("ann")
        os.utime(self.path, (9e9, 9e9))  # a later mtime, whatever the clock resolution
        self.assertEqual(self.store.permissions(key, ["api"])[0], 401)


    def test_two_stores_that_save_at_once_do_not_break_each_other(self):
        # Two Store objects on one file (two loads of the plugin, or the CLI next to the server) must not share
        # a temporary file: B's save between A's write and A's rename used to end A with FileNotFoundError.
        self.store.add("ann", ["api"])
        other = users.Store(self.path)
        real_replace = os.replace
        nested = []

        def replace(src, dst):
            if not nested:
                nested.append(1)
                # The file lock now keeps B's whole change out of A's; B's bare save still must not share
                # A's temporary file.
                other._load()
                other._save()
            return real_replace(src, dst)

        with mock.patch.object(users.os, "replace", side_effect=replace):
            self.store.add("cid", ["api"])
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n not in ("users.json", "users.json.lock")],
                         [])  # no temporary file left

    def test_one_store_for_concurrent_first_calls(self):
        with mock.patch.object(users, "STORE", None), mock.patch.object(users, "Store") as cls:
            cls.side_effect = lambda path: (threading.Event().wait(0.05), object())[1]
            got = []
            threads = [threading.Thread(target=lambda: got.append(users.store())) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(cls.call_count, 1)
            self.assertEqual(len({id(g) for g in got}), 1)


class HttpTest(unittest.TestCase):
    """kb-http answers both webhooks itself and shows the Users tab to an admin of the store."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patches = [mock.patch.object(kb_core, "KB", cls.tmp.name),
                       mock.patch.object(kb_core, "GRAPH", os.path.join(cls.tmp.name, "graph.json")),
                       mock.patch.object(kb_core, "PROJECTS", {"api": [], "web": []}),
                       mock.patch.object(kb_core, "warm_qmd", lambda: None),
                       mock.patch.dict(os.environ, {"KB_ADDONS": "none"}),
                       mock.patch.object(plugins, "access", lambda enabled: users),   # the plugin is installed and on
                       mock.patch.object(users, "STORE", users.Store(os.path.join(cls.tmp.name, "users.json"))),
                       mock.patch.object(users, "mail_config", lambda core=None: dict(cls.cfg)),
                       mock.patch.object(users, "send_mail", lambda to, subject, text, cfg: cls.mails.append((to, text))),
                       mock.patch.object(users, "RESET_EVERY", 0)]
        cls.mails = []
        cls.cfg = {}                     # no mail, as on a server without SMTP: a test switches it on
        for p in cls.patches:
            p.start()
        cls.store = users.STORE
        cls.boss = cls.store.add("boss", "*", admin=True)
        cls.ann = cls.store.add("ann", ["api"])
        # The server asks itself: bind first, then point the two webhooks at the bound port.
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

    def setUp(self):
        # Requests of this machine count against the password limit: a fresh limit per test.
        self.srv.RequestHandlerClass.login_limit = kb_http.admin.login_throttle()

    def post_json(self, path, body, token=None):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                   headers={"Content-Type": "application/json"})
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def client(self):
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect)

    def req(self, opener, method, path, form=None):
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            r.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with opener.open(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode()

    def admin(self, name="boss", key=None):
        c = self.client()
        status, _, page = self.req(c, "POST", "/admin/login", {"username": name, "password": key or self.boss})
        return c, status, page

    def csrf(self, c):
        _, _, page = self.req(c, "GET", "/admin/users")
        return re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def with_mail(self):
        cfg = {"KB_SMTP_HOST": "smtp.test", "KB_SMTP_FROM": "kb@test", "KB_PUBLIC_URL": "https://kb.public.test/kb"}
        type(self).cfg = cfg
        self.addCleanup(lambda: setattr(type(self), "cfg", {}))
        del self.mails[:]

    def wait_for_mail(self):
        """"Forgot" mails go from a background worker: wait until it handled every request."""
        if users._MAIL_QUEUE is not None:
            users._MAIL_QUEUE.join()

    def link(self, to):
        self.wait_for_mail()
        (text,) = [t for name, t in self.mails if name == to][-1:]
        # The value is in the fragment: a browser never sends it, the page script puts it into the form.
        url = re.search(r"https://kb\.public\.test/kb(/admin/reset#t=[A-Za-z0-9_-]+)", text).group(1)
        value = url.split("t=")[1]
        return f"/admin/reset?t={value}", value    # the form of an older link: the server still reads it

    def test_a_new_person_gets_a_mail_and_chooses_a_password(self):
        self.with_mail()
        c, _, _ = self.admin()
        _, _, page = self.req(c, "POST", "/admin/users/add",
                              {"csrf": self.csrf(c), "name": "Kate@Example.com", "projects": "api"})
        self.assertIn("A mail with a link went to kate@example.com", page)
        self.assertNotIn("kbk_", page.split("Users</h2>")[0])            # no key to hand over
        url, value = self.link("kate@example.com")
        self.assertNotIn(value, page)
        anon = self.client()
        status, _, page = self.req(anon, "GET", url)
        self.assertEqual(status, 200)
        self.assertIn("kate@example.com", page)
        _, _, page = self.req(anon, "POST", "/admin/reset", {"t": value, "password": "one long password", "again": "other"})
        self.assertIn("not the same", page)
        _, _, page = self.req(anon, "POST", "/admin/reset", {"t": value, "password": "short", "again": "short"})
        self.assertIn("characters or more", page)
        _, _, page = self.req(anon, "POST", "/admin/reset",
                              {"t": value, "password": "one long password", "again": "one long password"})
        self.assertIn("The password is set", page)
        _, _, page = self.req(anon, "GET", url)                           # the link worked once
        self.assertIn("does not work any more", page)
        status, headers, _ = self.req(anon, "POST", "/admin/login",
                                      {"username": "kate@example.com", "password": "one long password"})
        self.assertEqual((status, headers["Location"].endswith("/admin/me")), (302, True))
        status, body = self.post_json("/local/login", {"username": "kate@example.com", "password": "one long password"})
        self.assertEqual(status, 200)
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        self.assertEqual(self.post_json("/mcp", init, body["token"])[0], 200)   # the password works for an agent too

    def test_forgot_tells_nothing_and_builds_the_link_from_the_set_address(self):
        self.with_mail()
        self.store.add("lena@example.com", ["api"], key=False)
        anon = self.client()
        status, _, page = self.req(anon, "GET", "/admin/login")
        self.assertIn("/admin/forgot", page)
        answers = []
        for name in ("lena@example.com", "nobody@example.com"):
            r = urllib.request.Request(self.base + "/admin/forgot", method="POST",
                                       data=urllib.parse.urlencode({"username": name}).encode(),
                                       headers={"Host": "evil.test", "X-Forwarded-Host": "evil.test",
                                                "Content-Type": "application/x-www-form-urlencoded"})
            with anon.open(r, timeout=10) as resp:
                answers.append(re.sub(r"\s+", " ", resp.read().decode()))
        self.assertEqual(answers[0], answers[1])                           # the same page for both names
        self.wait_for_mail()
        self.assertEqual([to for to, _ in self.mails], ["lena@example.com"])
        self.assertNotIn("evil.test", self.mails[0][1])
        self.assertIn("https://kb.public.test/kb/admin/reset#t=", self.mails[0][1])
        self.assertNotIn("reset?t=", self.mails[0][1])      # nothing in a query that an access log keeps

    def test_without_mail_there_is_no_forgot_and_an_admin_gives_a_key(self):
        del self.mails[:]
        anon = self.client()
        _, _, page = self.req(anon, "POST", "/admin/forgot", {"username": "ann"})
        self.assertIn("sends no mail", page)
        self.assertEqual(self.mails, [])
        type(self).cfg = {"KB_SMTP_HOST": "smtp.test", "KB_SMTP_FROM": "kb@test"}       # mail, but no public address
        self.addCleanup(lambda: setattr(type(self), "cfg", {}))
        _, _, page = self.req(anon, "POST", "/admin/forgot", {"username": "ann"})
        self.assertIn("sends no mail", page)

    def test_an_admin_and_the_person_send_the_reset_mail(self):
        self.with_mail()
        self.store.add("mona@example.com", ["api"], key=False)
        self.store.set_password("mona@example.com", "one long password")
        c, _, _ = self.admin()
        _, _, page = self.req(c, "POST", "/admin/users/reset", {"csrf": self.csrf(c), "name": "mona@example.com"})
        self.assertIn("A mail with a link went to mona@example.com", page)
        m = self.client()
        self.req(m, "POST", "/admin/login", {"username": "mona@example.com", "password": "one long password"})
        _, _, page = self.req(m, "POST", "/admin/me/reset", {"csrf": self.me_csrf(m)})
        self.assertIn("A mail with a link went to mona@example.com", page)
        self.assertEqual([to for to, _ in self.mails], ["mona@example.com", "mona@example.com"])
        status, _, _ = self.req(m, "POST", "/admin/users/reset", {"csrf": self.me_csrf(m), "name": "boss"})
        self.assertEqual(status, 403)                                      # a member resets nobody else

    def test_wrong_passwords_lock_the_sign_in_and_the_link_stays_out_of_the_log(self):
        self.store.add("nina@example.com", ["api"], key=False)
        self.store.set_password("nina@example.com", "one long password")
        for _ in range(users.LOCK_AFTER):
            self.assertEqual(self.post_json("/local/login", {"username": "nina@example.com", "password": "nope nope nope"})[0], 401)
        self.assertEqual(self.post_json("/local/login", {"username": "nina@example.com", "password": "one long password"})[0], 429)
        lines = []
        with mock.patch.object(kb_http, "log", lines.append):
            self.req(self.client(), "GET", "/admin/reset?t=SECRETVALUE123")
        self.assertTrue(lines)
        self.assertNotIn("SECRETVALUE123", " ".join(lines))

    def test_login_webhook(self):
        self.assertEqual(self.post_json("/local/login", {"protocol": 1, "username": "ann", "password": self.ann}),
                         (200, {"token": self.ann}))
        status, body = self.post_json("/local/login", {"protocol": 1, "username": "ann", "password": "kbk_no"})
        self.assertEqual(status, 401)
        self.assertNotIn("kbk_no", json.dumps(body))

    def test_permission_webhook_and_the_mcp_endpoint(self):
        # The permission webhook answers this server only (in-process); from the network it is closed.
        self.assertEqual(self.post_json("/local/permissions", {"protocol": 1, "projects": ["api", "web"]}, self.ann)[0],
                         403)
        self.assertEqual(users.permissions(self.ann, ["api", "web"], kb_core),
                         (200, {"user": {"name": "ann"}, "projects": ["api"]}))
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        self.assertEqual(self.post_json("/mcp", init, self.ann)[0], 200)
        self.assertEqual(self.post_json("/mcp", init, "kbk_no")[0], 401)

    def member(self, name="ann", key=None):
        c = self.client()
        status, headers, _ = self.req(c, "POST", "/admin/login", {"username": name, "password": key or self.ann})
        return c, status, headers

    def me_csrf(self, c):
        _, _, page = self.req(c, "GET", "/admin/me")
        return re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def test_a_person_who_is_no_admin_reaches_only_the_own_page(self):
        c, status, headers = self.member()
        self.assertEqual(status, 302)
        self.assertTrue(headers["Location"].endswith("/admin/me"))
        status, headers, _ = self.req(c, "GET", "/admin")
        self.assertEqual((status, headers["Location"].endswith("/admin/me")), (302, True))
        csrf = self.me_csrf(c)
        for path in ("/admin/users", "/admin/usage", "/admin/clients", "/admin/index"):
            status, _, page = self.req(c, "GET", path)
            self.assertEqual(status, 403, path)
            self.assertNotIn("boss", page)
        for path, form in (("/admin/users/add", {"name": "mallory", "projects": "*", "admin": "on"}),
                           ("/admin/users/save", {"name": "ann", "projects": "*", "admin": "on"}),
                           ("/admin/users/key", {"name": "boss"}), ("/admin/users/revoke", {"name": "boss"}),
                           ("/admin/rebuild", {}), ("/admin/clients/revoke", {"client_id": "x"})):
            status, _, _ = self.req(c, "POST", path, {"csrf": csrf, **form})
            self.assertEqual(status, 403, path)
        self.assertNotIn("mallory", self.store.all())
        self.assertFalse(self.store.all()["ann"]["admin"])
        self.assertTrue(self.store.admin_by_key(self.boss))
        status, _, page = self.req(c, "GET", "/admin/howto")
        self.assertEqual(status, 200)
        self.assertNotIn(">Users<", page)                       # the tabs of a member: no admin tab
        self.assertNotIn(">Usage<", page)
        self.assertIn(">My tokens<", page)
        self.assertIn("/local/login", page)                     # kb-login gets the address of this server
        status, _, _ = self.req(c, "GET", "/admin/howto/kb.md")
        self.assertEqual(status, 200)                           # a member downloads the skills too

    def test_a_person_makes_and_removes_the_own_tokens(self):
        key = self.store.add("gina", ["api"])
        c, _, _ = self.member("gina", key)
        status, _, page = self.req(c, "GET", "/admin/me")
        self.assertEqual(status, 200)
        for word in ("My tokens", "first key"):
            self.assertIn(word, page)
        self.assertNotIn("Who is online", page)                 # the online list is for admins only
        self.assertNotIn("<script", page)
        _, _, page = self.req(c, "POST", "/admin/me/add", {"csrf": self.me_csrf(c), "label": "laptop"})
        token = re.search(r"kbk_[A-Za-z0-9_-]{30,}", page).group(0)
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        self.assertEqual(self.post_json("/mcp", init, token)[0], 200)
        _, _, again = self.req(c, "GET", "/admin/me")
        self.assertNotIn(token, again)
        self.assertIn("laptop", again)
        ids = {t["label"]: tid for tid, t in self.store.all()["gina"]["tokens"].items()}
        _, _, page = self.req(c, "POST", "/admin/me/remove", {"csrf": self.me_csrf(c), "id": ids["laptop"]})
        self.assertIn("Removed", page)
        self.assertEqual(self.post_json("/mcp", init, token)[0], 401)          # at once, not after the cache
        _, _, page = self.req(c, "POST", "/admin/me/remove", {"csrf": self.me_csrf(c), "id": ids["first key"]})
        self.assertIn("last token", page)
        self.assertEqual(len(self.store.all()["gina"]["tokens"]), 1)

    def test_a_person_cannot_touch_the_token_of_another(self):
        key = self.store.add("hank", ["api"])
        c, _, _ = self.member("hank", key)
        (boss_token,) = [tid for tid, t in self.store.all()["boss"]["tokens"].items() if t["label"] == "first key"]
        _, _, page = self.req(c, "POST", "/admin/me/remove", {"csrf": self.me_csrf(c), "id": boss_token,
                                                               "name": "boss"})
        self.assertIn("no such token", page)
        self.assertTrue(self.store.admin_by_key(self.boss))
        status, _, _ = self.req(c, "POST", "/admin/me/add", {"label": "no csrf"})
        self.assertEqual(status, 403)

    def test_the_online_list_and_a_disabled_person(self):
        key = self.store.add("ivy", ["api"])
        self.post_json("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, key)
        admin, _, _ = self.admin()
        _, _, page = self.req(admin, "GET", "/admin/users")
        self.assertIn("Who is online", page)
        self.assertRegex(page, r"ivy.*online")
        c, _, _ = self.member("ivy", key)
        _, _, page = self.req(c, "GET", "/admin/me")
        self.assertNotIn("Who is online", page)
        self.store.update("ivy", disabled=True)
        status, headers, _ = self.req(c, "GET", "/admin/me")
        self.assertEqual((status, headers["Location"].endswith("/admin/login")), (302, True))

    def test_an_admin_opens_every_page(self):
        c, status, _ = self.admin()
        self.assertEqual(status, 302)
        for path, word in (("/admin/users", "ann"), ("/admin/me", "My tokens"), ("/admin", "Knowledge base")):
            status, _, page = self.req(c, "GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn(word, page)
            self.assertIn(">Users<", page)                      # the plugin's tabs join the admin tabs
            self.assertIn(">My tokens<", page)
            self.assertNotIn(self.ann, page)
            self.assertNotIn("<script", page)

    def test_admin_adds_a_user_and_the_key_shows_once(self):
        c, _, _ = self.admin()
        status, _, page = self.req(c, "POST", "/admin/users/add",
                                   {"csrf": self.csrf(c), "name": "carl", "projects": "api, web"})
        self.assertEqual(status, 200)
        key = re.search(r"kbk_[A-Za-z0-9_-]{30,}", page).group(0)
        self.assertEqual(self.store.permissions(key, ["api", "web"])[1]["projects"], ["api", "web"])
        _, _, again = self.req(c, "GET", "/admin/users")
        self.assertNotIn(key, again)
        self.assertIn("carl", again)

    def test_a_revoke_ends_the_access_at_once(self):
        key = self.store.add("dora", ["api"])
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        self.assertEqual(self.post_json("/mcp", init, key)[0], 200)      # the verdict is cached for 60 s
        c, _, _ = self.admin()
        self.req(c, "POST", "/admin/users/revoke", {"csrf": self.csrf(c), "name": "dora"})
        self.assertEqual(self.post_json("/mcp", init, key)[0], 401)

    def test_an_admin_cannot_lock_the_own_account(self):
        c, _, _ = self.admin()
        for path, form in (("/admin/users/remove", {}), ("/admin/users/revoke", {}),
                           ("/admin/users/save", {"projects": "*", "disabled": "on", "admin": "on"}),
                           ("/admin/users/save", {"projects": "*"})):
            _, _, page = self.req(c, "POST", path, {"csrf": self.csrf(c), "name": "boss", **form})
            self.assertIn("own account", page, path)
        self.assertTrue(self.store.admin_by_key(self.boss))

    def test_a_form_without_the_csrf_value_changes_nothing(self):
        c, _, _ = self.admin()
        status, _, _ = self.req(c, "POST", "/admin/users/add", {"name": "eve", "projects": "*"})
        self.assertEqual(status, 403)
        self.assertNotIn("eve", self.store.all())

    def test_save_changes_projects_and_flags(self):
        self.store.add("fred", ["api"])
        c, _, _ = self.admin()
        self.req(c, "POST", "/admin/users/save", {"csrf": self.csrf(c), "name": "fred", "projects": "web",
                                                   "admin": "on"})
        rec = self.store.all()["fred"]
        self.assertEqual((rec["projects"], rec["admin"], rec["disabled"]), (["web"], True, False))

    def test_save_takes_the_project_checkboxes(self):
        self.store.add("gus", ["api"])
        c, _, _ = self.admin()
        _, _, page = self.req(c, "GET", "/admin/users")
        self.assertIn('name="p:web"', page)                     # one checkbox per project of the server
        self.assertIn('name="p:*"', page)
        self.req(c, "POST", "/admin/users/save", {"csrf": self.csrf(c), "name": "gus", "pick": "1",
                                                   "p:api": "on", "p:web": "on"})
        self.assertEqual(self.store.all()["gus"]["projects"], ["api", "web"])
        self.req(c, "POST", "/admin/users/save", {"csrf": self.csrf(c), "name": "gus", "pick": "1",
                                                   "p:*": "on", "p:api": "on"})
        self.assertEqual(self.store.all()["gus"]["projects"], "*")          # "all" wins
        self.req(c, "POST", "/admin/users/add", {"csrf": self.csrf(c), "name": "hal", "pick": "1", "p:web": "on",
                                                  "p:nope": "on"})
        self.assertEqual(self.store.all()["hal"]["projects"], ["web"])      # a name the server does not know drops


class ProjectsFromForm(unittest.TestCase):
    def test_checkboxes_text_and_unknown_names(self):
        known = ["api", "web"]
        self.assertEqual(users.projects_from_form({"pick": "1", "p:web": "on", "p:api": "on"}, known), ["api", "web"])
        self.assertEqual(users.projects_from_form({"pick": "1", "p:*": "on"}, known), "*")
        self.assertEqual(users.projects_from_form({"pick": "1"}, known), [])
        self.assertEqual(users.projects_from_form({"projects": "api, web"}, known), ["api", "web"])


if __name__ == "__main__":
    unittest.main()
