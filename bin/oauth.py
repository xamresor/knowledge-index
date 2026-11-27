#!/usr/bin/env python3
"""MCP OAuth for bin/kb-http: an MCP client (Claude Code) signs in through the browser by itself.

Without it a developer runs bin/kb-login in a terminal and a client that tries OAuth after a 401
fails with "could not connect". With it, `/mcp` -> `kb` -> Authenticate opens a login page; the
person types the user name, password and code; the login webhook (access protocol v1) checks them and
gives the token, which the client keeps as its access token. kb-http checks it with the permission
webhook, so access rules do not change.

What it implements (MCP authorization spec 2025-06-18):

  GET  /.well-known/oauth-protected-resource      RFC 9728: which authorization server to ask
  GET  /.well-known/oauth-authorization-server    RFC 8414: the endpoints below
  POST /register                                  RFC 7591: dynamic client registration
  GET  /authorize, POST /authorize                the login page (password, then the 2FA code)
  POST /token                                     authorization_code (PKCE S256) and refresh_token

Deliberate limits:
  * Public clients only, PKCE S256 required, redirect URIs on loopback only (a desktop client's
    local callback). A page elsewhere cannot get a code sent to it.
  * The access token is a token of this server (`kbo_…`) that stands for the token of the login webhook
    and ends after KB_OAUTH_TOKEN_TTL (8 hours), the `expires_in` the client is told. kb-http swaps it for
    the token behind it and checks that with the permission webhook. A refresh gives a new access token
    while the permission webhook still accepts the token behind it; once that ended, the client sends the
    person through the browser again. The access token a refresh replaces ends a minute later, a client
    keeps at most KB_OAUTH_TOKENS_PER_CLIENT (50) access and refresh tokens, and the tokens, codes and
    sign-ins of a revoked client stop at once.
  * Codes and login transactions live in memory. Access and refresh tokens are kept in a file that only
    the server user can read (`tokens_file`, mode 0600), so a restart or a deploy asks nobody to sign in
    again. The file is no credential: an entry is found by the SHA-256 of the client's token, and the token
    of the login webhook in it is sealed with that client token (`_seal`). A reader of the file or of a
    backup has no token of a client, so the reader can open no entry. Registered clients are kept in a
    second file: they hold no secret.
  * A change is on disk before the client gets its answer. When the write fails, the client gets 503,
    the old refresh token still works, and nothing is half done. A revoke that cannot be saved is undone
    and reported.
  * A refresh token works once. The server remembers a used one for KB_OAUTH_SPENT_DAYS (90). When a used
    token comes again, somebody else holds a copy: every token of that sign-in ends and the person signs
    in again (KB_OAUTH_REUSE=log only writes the audit line). Two sessions of one client that refresh at
    the same moment are no theft: for ROTATE_OVERLAP seconds the used token gives a pair of its own, at
    most OVERLAP_PAIRS times.
  * What the seal does not cover: a person who holds an old token of a client and an old copy of the file
    from the time of that token can open that entry. KB_OAUTH_SEAL_KEY (a value of the service
    environment, not in the data folder) closes that: without the key no copy of the file opens.
  * /register needs no sign-in, so everything a stranger can make has a limit (environment, defaults):
    KB_OAUTH_MAX_CLIENTS (1000) clients, KB_OAUTH_MAX_URIS (10) redirect URIs per client of at most
    KB_OAUTH_MAX_URI_LENGTH (512) characters, KB_OAUTH_MAX_TXNS (1000) sign-ins in progress, of them
    KB_OAUTH_TXNS_PER_ADDRESS (10) of one client address. A full list pushes out the oldest sign-in of the
    address that has the most, or one older than a minute, never a fresh sign-in of another person; a client
    that nobody signed in with goes after KB_OAUTH_CLIENT_DAYS (1), and first when the list is full; a refresh token has
    no end of its own (KB_OAUTH_REFRESH_DAYS, 0 = none): it ends when an admin revokes the client or the login
    webhook's token behind it ends. kb-http limits registrations per address (admin.register_throttle).
  * Enrolling a new authenticator is not done here (it shows a secret): the page says to use the app
    or bin/kb-login once.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import importlib.machinery
import importlib.util
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_TTL = 300          # authorization codes and login transactions, seconds


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


TOKEN_TTL = _env_int("KB_OAUTH_TOKEN_TTL", 8 * 3600)    # an access token ends after this; expires_in says so
ACCESS_PREFIX = "kbo_"                                    # the access tokens of this server
# Limits: /register needs no sign-in, so nothing a stranger sends may grow without end.
MAX_CLIENTS = _env_int("KB_OAUTH_MAX_CLIENTS", 1000)     # registered clients in all
MAX_URIS = _env_int("KB_OAUTH_MAX_URIS", 10)             # redirect URIs of one client
MAX_URI_LENGTH = _env_int("KB_OAUTH_MAX_URI_LENGTH", 512)
MAX_TXNS = _env_int("KB_OAUTH_MAX_TXNS", 1000)           # sign-ins in progress (GET /authorize)
CLIENT_DAYS = _env_int("KB_OAUTH_CLIENT_DAYS", 1)        # a client nobody signed in with goes after this
TXNS_PER_CLIENT = _env_int("KB_OAUTH_TXNS_PER_CLIENT", 20)  # sign-ins in progress of one client
TXNS_PER_ADDRESS = _env_int("KB_OAUTH_TXNS_PER_ADDRESS", 10)  # sign-ins in progress of one client address
TXN_FRESH = 60       # a sign-in younger than this never gives way to another address
REFRESH_DAYS = _env_int("KB_OAUTH_REFRESH_DAYS", 0)      # a refresh token ends after this time; 0 = no end
ROTATE_OVERLAP = 60  # seconds the access token a refresh replaced still works (requests in flight)
TOKENS_PER_CLIENT = _env_int("KB_OAUTH_TOKENS_PER_CLIENT", 50)  # live access and refresh tokens of one client
SPENT_DAYS = _env_int("KB_OAUTH_SPENT_DAYS", 90)         # a used refresh token is remembered this long
# What a used refresh token that comes again does: "revoke" ends every token of that sign-in, "log" only
# writes the audit line.
REUSE = (os.environ.get("KB_OAUTH_REUSE") or "revoke").strip().lower()
OVERLAP_PAIRS = 3    # token pairs one used refresh token gives inside ROTATE_OVERLAP (sessions at the same moment)
TAKING_WAIT = 15.0   # seconds a /token call waits for another call that holds the same refresh token
# Optional second key of the seal, kept outside the token file and its backups (an environment variable of
# the service). With it, an old backup together with an old token of a client opens nothing. Set it once:
# a change of the value ends every browser sign-in.
SEAL_KEY = os.environ.get("KB_OAUTH_SEAL_KEY") or ""
LOOPBACK = ("localhost", "127.0.0.1", "[::1]", "::1")


def _load_kb_login():
    loader = importlib.machinery.SourceFileLoader("kb_login_for_oauth", os.path.join(HERE, "kb-login"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


kb_login = _load_kb_login()


class OAuthError(Exception):
    def __init__(self, status: int, error: str, description: str = ""):
        super().__init__(description or error)
        self.status, self.error, self.description = status, error, description


def b64url_sha256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def is_loopback_redirect(uri: str) -> bool:
    """An http URI on this computer, in its plain form. A backslash, a user part, a control character or
    a space is refused: Python and a browser can read such a URI as two different hosts
    (`http://evil.example\\@localhost/` is localhost for Python and evil.example for a browser)."""
    if not isinstance(uri, str) or not uri or len(uri) > MAX_URI_LENGTH:
        return False
    if "\\" in uri or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in uri) or not uri.isascii():
        return False
    try:
        u = urllib.parse.urlsplit(uri)
        port = u.port
    except ValueError:
        return False
    if u.scheme != "http" or u.fragment or "@" in u.netloc or u.username is not None:
        return False
    host = (u.hostname or "").lower()
    if host not in LOOPBACK:
        return False
    # The authority must be exactly host[:port]: nothing else may hide in it.
    shown = f"[{host}]" if ":" in host else host
    return u.netloc.lower() == (shown if port is None and not u.netloc.endswith(":") else f"{shown}:{port}")


class Server:
    """State and logic; the HTTP adapter in kb-http turns requests into calls to `handle`."""

    def __init__(self, login_env: dict, check_token, clients_file: str = "", now=time.time,
                 tokens_file: str = ""):
        self.env = login_env                 # KB_LOGIN_URL, KB_HOOK_HEADERS (docs/access-protocol.md)
        self.check_token = check_token       # token -> raises when the API no longer accepts it
        self.clients_file = clients_file
        self.now = now
        self.on_event = None                 # (user, action, detail) -> None: the audit log, set by kb-http
        self.lock = threading.Lock()
        self._released = threading.Condition(self.lock)   # a refresh token left _taking
        self.clients: dict[str, dict] = self._load_clients()
        self.txns: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        # SHA-256 of a refresh / access token -> its entry: the token behind it sealed with the client's
        # token ("sealed"), the SHA-256 of the token behind it ("tid": one sign-in), client, end.
        self.refresh: dict[str, dict] = {}
        self.access: dict[str, dict] = {}
        self.spent: dict[str, dict] = {}     # SHA-256 of a used refresh token -> its sign-in ("tid"), when
        self._plain: dict[str, str] = {}     # tid -> the token behind it; memory only, filled when a client shows a token
        # A refresh token between its pop and the new pair (the permission webhook is asked meanwhile). It
        # is still written with the others: a save by another call, then a crash, must not lose it.
        self._taking: dict[str, dict] = {}
        self.tokens_file = tokens_file       # refresh, access and spent, kept over a restart; "" = memory only
        self._dirty = False                  # they changed since the last save
        self.headers: dict[str, str] = {}    # extra headers for the login webhook (kb-http: its self-call header)
        # (url, body, headers, ip) -> answer dict, or LoginError: the call of the login webhook. kb-http sets
        # it to call its access plugin in-process when the webhook is this server; else kb_login.post.
        self.login_post = None
        # token -> None: an upstream sign-in token that no code, refresh or access token of this server
        # holds any more (a code nobody exchanged, a revoked client, an ended refresh token). kb-http ends
        # it in the access plugin, so unfinished sign-ins do not push out the real tokens of the person.
        self.on_drop = None
        # tid -> None: the same for a token this process never saw in the open (its entries came from the
        # disk). `tid` is the SHA-256 of the token, which is what the users plugin keeps of it.
        self.on_drop_tid = None
        self._dropped: list[str] = []          # tids of removed entries, checked by _end_dropped
        self._inflight: dict[str, int] = {}    # tids of a /token call between pop and issue
        self._load_tokens()
        with self.lock:
            legacy = self._dirty
            self._save_tokens()                # entries of the format before sealing leave the disk now
            if legacy and self._dirty:
                print("oauth: the token file still holds tokens in the open: it could not be rewritten "
                      f"({self.tokens_file}). Check the disk; the next change tries again.", flush=True)

    def clients_snapshot(self) -> dict:
        """A deep copy of the registered clients, taken under the lock: a page that iterates it never meets
        a client (or a user of one) added or removed in the middle."""
        import copy
        with self.lock:
            return copy.deepcopy(self.clients)

    # ------------------------------------------------------------------ storage
    def _load_clients(self) -> dict:
        try:
            with open(self.clients_file, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_clients(self) -> None:
        if not self.clients_file:
            return
        folder = os.path.dirname(self.clients_file) or "."
        os.makedirs(folder, exist_ok=True)
        # A temporary file of its own: a second process (or a test) on the same file must not rename ours away.
        fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(self.clients_file)}.", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.clients, f)
            os.replace(tmp, self.clients_file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _load_tokens(self) -> None:
        """Read the tokens of the last run. An entry that ended, that has no client any more or that is not
        in the right form is left out. A missing or broken file means no tokens."""
        if os.environ.get("KB_DATABASE_URL", "").strip() and hasattr(self, "_load_tokens_pg"):
            data = self._load_tokens_pg()
            if data is None:
                return
        else:
            if not self.tokens_file:
                return
            try:
                with open(self.tokens_file, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                return
        t = self.now()
        for name, table in (("refresh", self.refresh), ("access", self.access), ("spent", self.spent)):
            rows = data.get(name) if isinstance(data, dict) else None
            for k, v in (rows.items() if isinstance(rows, dict) else []):
                if isinstance(v, dict) and "token" in v:
                    self._dirty = True        # the format before sealing: it leaves the disk, kept or not
                if not (isinstance(v, dict) and v.get("client_id") in self.clients):
                    continue
                exp = v.get("exp")
                if exp is not None and (not isinstance(exp, (int, float)) or exp < t):
                    continue
                if name == "access" and exp is None:
                    continue                  # an access token always has an end
                if "sealed" not in v and isinstance(v.get("token"), str) and v["token"]:
                    # The format before sealing: the token behind in the open. A refresh entry had the
                    # refresh token as its key, so it can be sealed now. An access entry cannot (its key
                    # was a hash already): it is dropped, and the client refreshes without a browser.
                    self._dirty = True
                    if name != "refresh":
                        continue
                    v = {**{x: y for x, y in v.items() if x != "token"},
                         "sealed": _seal(v["token"], k), "tid": _sha(v["token"])}
                    k = _sha(k)
                if not isinstance(v.get("tid"), str) or (name != "spent" and not isinstance(v.get("sealed"), str)):
                    continue
                if name == "spent" and not isinstance(v.get("when"), (int, float)):
                    continue
                table[k] = v

    def _save_tokens(self, strict: bool = False) -> None:
        """Write the tokens when they changed. The caller holds the lock. `strict`: a failed write raises
        OSError, and the caller undoes its change (a token the client gets must be on disk). Without it a
        failed write is kept for the next change: used for clean-up that no client waits for."""
        if not self._dirty:
            return
        try:
            if os.environ.get("KB_DATABASE_URL", "").strip() and hasattr(self, "_save_tokens_pg"):
                self._save_tokens_pg()
            elif self.tokens_file:
                self._write_tokens_file()
            self._dirty = False
        except OSError:
            if strict:
                raise

    def _write_tokens_file(self) -> None:
        folder = os.path.dirname(self.tokens_file) or "."
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(self.tokens_file)}.", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:   # mkstemp: readable by the owner only
                json.dump({"refresh": {**self.refresh, **self._taking}, "access": self.access,
                           "spent": self.spent}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.tokens_file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:                               # the rename itself must survive a power loss
            d = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(d)
            finally:
                os.close(d)
        except OSError:
            pass                           # a file system without it: the data is written all the same

    def _expire(self) -> None:
        t = self.now()
        for table in (self.txns, self.codes, self.refresh, self.access):
            for k in [k for k, v in table.items() if v.get("exp", float("inf")) < t]:
                if table[k].get("tid"):
                    self._dropped.append(table[k]["tid"])
                del table[k]
                self._dirty = self._dirty or table is self.refresh or table is self.access
        for k in [k for k, v in self.spent.items() if v["when"] < t - SPENT_DAYS * 86400]:
            del self.spent[k]
            self._dirty = True
        for v in self.spent.values():      # past the overlap a used token opens nothing any more
            if "sealed" in v and v["when"] < t - ROTATE_OVERLAP:
                del v["sealed"]
                self._dirty = True

    def _end_dropped(self) -> None:
        """Hand the tokens of the login webhook that nothing holds any more to on_drop. Takes the lock
        itself; the callback runs without it. A token this process never saw in the open (its entries
        came from the disk and no client showed a token since) is handed over as its `tid` to
        on_drop_tid."""
        with self.lock:
            self._save_tokens()
            dropped, self._dropped = set(self._dropped), []
            if not dropped:
                return
            live = {v["tid"] for table in (self.codes, self.refresh, self.access) for v in table.values()}
            gone = [t for t in dropped if t not in live and not self._inflight.get(t)]
            tokens = [(t, self._plain.pop(t, None)) for t in gone]
        for tid, token in tokens:
            try:
                if token and self.on_drop is not None:
                    self.on_drop(token)
                elif not token and self.on_drop_tid is not None:
                    self.on_drop_tid(tid)
            except Exception:  # noqa: BLE001 — cleanup never breaks a sign-in
                pass

    def _prune_clients(self) -> bool:
        """Drop the clients that registered more than CLIENT_DAYS ago and never had a sign-in. -> changed"""
        cut = self.now() - CLIENT_DAYS * 86400
        old = [cid for cid, c in self.clients.items()
               if not c.get("users") and (c.get("client_id_issued_at") or 0) < cut]
        for cid in old:
            del self.clients[cid]
        return bool(old)

    # ------------------------------------------------------------------ metadata
    @staticmethod
    def resource_metadata(base: str) -> dict:
        return {"resource": f"{base}/mcp", "authorization_servers": [base],
                "bearer_methods_supported": ["header"]}

    @staticmethod
    def server_metadata(base: str) -> dict:
        return {"issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"]}

    # ------------------------------------------------------------------ registration
    def register(self, body: dict) -> dict:
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
            raise OAuthError(400, "invalid_redirect_uri", "redirect_uris is required")
        if len(uris) > MAX_URIS:
            raise OAuthError(400, "invalid_redirect_uri", f"{MAX_URIS} redirect URIs at most")
        if not all(is_loopback_redirect(u) for u in uris):
            raise OAuthError(400, "invalid_redirect_uri", "only plain http loopback redirect URIs are allowed "
                                                          f"(at most {MAX_URI_LENGTH} characters)")
        client_id = "kb-" + secrets.token_urlsafe(16)
        client = {"client_id": client_id, "redirect_uris": uris,
                  "client_name": str(body.get("client_name") or "")[:100],
                  "client_id_issued_at": int(self.now()),
                  "token_endpoint_auth_method": "none",
                  "grant_types": ["authorization_code", "refresh_token"],
                  "response_types": ["code"]}
        with self.lock:
            changed = self._prune_clients()
            if len(self.clients) >= MAX_CLIENTS:   # full: the oldest client nobody signed in with goes
                unused = sorted((c.get("client_id_issued_at") or 0, cid) for cid, c in self.clients.items()
                                if not c.get("users"))
                if unused:
                    del self.clients[unused[0][1]]
                    changed = True
            if len(self.clients) >= MAX_CLIENTS:
                if changed:
                    self._save_clients()
                raise OAuthError(503, "temporarily_unavailable", "too many registered clients: ask an admin")
            self.clients[client_id] = client
            try:
                self._save_clients()
            except OSError:
                del self.clients[client_id]   # not on disk: not registered (kb-http answers 503)
                raise OAuthError(503, "temporarily_unavailable", "the client could not be saved: try again later") \
                    from None
        return client

    # ------------------------------------------------------------------ authorize
    def start(self, q: dict, ip: str = "") -> str:
        """Check the authorize request; returns a transaction id for the login page. `ip` is the client
        address: a flood from one address or many must not push out a person's sign-in."""
        client = self.clients.get(q.get("client_id", ""))
        if not client:
            raise OAuthError(400, "invalid_client", "unknown client_id: register first")
        redirect = q.get("redirect_uri", "")
        if not is_loopback_redirect(redirect) or not (
                redirect in client["redirect_uris"] or _same_app(redirect, client["redirect_uris"])):
            raise OAuthError(400, "invalid_request", "redirect_uri is not registered for this client")
        if q.get("response_type") != "code":
            raise OAuthError(400, "unsupported_response_type", "response_type must be code")
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            raise OAuthError(400, "invalid_request", "PKCE with S256 is required")
        txn = secrets.token_urlsafe(24)
        with self.lock:
            self._expire()
            # Full: room is made only at the cost of the flood. An address over its own limit loses its own
            # oldest sign-in; a full client or a full list loses one of the busiest address, or one older
            # than TXN_FRESH. A fresh sign-in of another person never goes: then this request is refused.
            own = sorted((t["created"], k) for k, t in self.txns.items() if t.get("ip") == ip)
            if len(own) >= TXNS_PER_ADDRESS > 0:
                del self.txns[own[0][1]]
            same = [k for k, t in self.txns.items() if t["client_id"] == client["client_id"]]
            if (len(same) >= TXNS_PER_CLIENT and not self._make_room(same, ip)) or \
                    (len(self.txns) >= MAX_TXNS and not self._make_room(list(self.txns), ip)):
                raise OAuthError(503, "temporarily_unavailable", "too many sign-ins in progress: try again in a minute")
            now = self.now()
            self.txns[txn] = {"client_id": client["client_id"], "redirect_uri": redirect,
                              "state": q.get("state", ""), "challenge": q["code_challenge"], "ip": ip,
                              "created": now, "exp": now + CODE_TTL, "api_challenge": None}
        self._end_dropped()
        return txn

    def _make_room(self, keys: list[str], ip: str) -> bool:
        """Drop one sign-in of `keys`: the oldest of this address, else the oldest of the address with the
        most (two or more: more than one person makes), else the oldest one older than TXN_FRESH. False
        when every one is the fresh sign-in of another address. The caller holds the lock."""
        rows = sorted((self.txns[k]["created"], k, self.txns[k].get("ip", "")) for k in keys)
        if not rows:
            return False
        mine = [k for _, k, a in rows if a == ip]
        if not mine:
            count: dict[str, int] = {}
            for _, _, a in rows:
                count[a] = count.get(a, 0) + 1
            busiest = max(count, key=count.get)
            if count[busiest] >= 2:
                mine = [k for _, k, a in rows if a == busiest]
            else:
                mine = [k for created, k, _ in rows if created < self.now() - TXN_FRESH]
        if not mine:
            return False
        del self.txns[mine[0]]
        return True

    def password_step(self, txn_id: str, username: str, password: str) -> tuple[str, str | None]:
        """-> ("code", None) when a code is needed next, ("done", redirect URL) when signed in."""
        txn = self._txn(txn_id)
        txn["user"] = username
        try:
            data = self._post(txn, {"username": username, "password": password})
        except kb_login.LoginError as e:
            self._event(username, "sign-in refused", str(e)[:200])
            raise
        challenge = kb_login.challenge_of(data)
        if challenge:
            txn["api_challenge"] = challenge
            return "code", None
        return "done", self._finish(txn_id, data.get("token"))

    def code_step(self, txn_id: str, code: str) -> str:
        txn = self._txn(txn_id)
        if not txn.get("api_challenge"):
            raise OAuthError(400, "invalid_request", "no second step pending")
        try:
            data = self._post(txn, {"challenge": txn["api_challenge"], "code": code.strip()})
        except kb_login.LoginError as e:
            self._event(txn.get("user", "?"), "sign-in refused", f"code: {str(e)[:180]}")
            raise
        return self._finish(txn_id, data.get("token"))

    def _post(self, txn: dict, body: dict) -> dict:
        if self.login_post is not None:
            return self.login_post(self.env["KB_LOGIN_URL"], body, self._hook_headers(txn), txn.get("ip", ""))
        return kb_login.post(self.env["KB_LOGIN_URL"], body, headers=self._hook_headers(txn))

    def _hook_headers(self, txn: dict) -> dict:
        """KB_HOOK_HEADERS, and the headers kb-http adds when the login webhook is kb-http itself: its
        self-call header and the address of the person, so that the webhook counts that address."""
        out = {**kb_login.hook_headers(self.env), **self.headers}
        if self.headers and txn.get("ip"):
            out["X-Forwarded-For"] = txn["ip"]
        return out

    def _txn(self, txn_id: str) -> dict:
        with self.lock:
            self._expire()
            txn = self.txns.get(txn_id)
        self._end_dropped()
        if not txn:
            raise OAuthError(400, "invalid_request", "the sign-in took too long: start it again")
        return txn

    def _finish(self, txn_id: str, token: str | None) -> str:
        if not isinstance(token, str) or not token:
            raise kb_login.LoginError("the API answered without a token")
        with self.lock:
            txn = self.txns.pop(txn_id, None)
            if txn:
                code = secrets.token_urlsafe(32)
                self._plain[_sha(token)] = token
                self.codes[code] = {"token": token, "tid": _sha(token), "client_id": txn["client_id"],
                                    "redirect_uri": txn["redirect_uri"], "challenge": txn["challenge"],
                                    "exp": self.now() + CODE_TTL, "user": txn.get("user", "")}
            else:
                # The transaction ended (it expired, or a second submit of the same form took it), but
                # the API already gave this sign-in a token. Nothing holds it: end it upstream.
                self._plain[_sha(token)] = token
                self._dropped.append(_sha(token))
        if not txn:
            self._end_dropped()
            raise OAuthError(400, "invalid_request", "the sign-in took too long: start it again")
        self._seen(txn["client_id"], txn.get("user", ""))
        client = self.clients.get(txn["client_id"], {})
        self._event(txn.get("user", "?"), "browser sign-in", str(client.get("client_name") or txn["client_id"])[:80])
        query = {"code": code}
        if txn["state"]:
            query["state"] = txn["state"]
        sep = "&" if "?" in txn["redirect_uri"] else "?"
        return txn["redirect_uri"] + sep + urllib.parse.urlencode(query)

    def _seen(self, client_id: str, user: str) -> None:
        """Note who signed in through a client and when (shown on the admin clients page). A client
        registers before anyone signs in, so its record has no user until this."""
        if not user:
            return
        with self.lock:
            client = self.clients.get(client_id)
            if client is None:
                return
            client.setdefault("users", {})[user] = int(self.now())
            try:
                self._save_clients()
            except OSError:
                pass

    def _event(self, user: str, action: str, detail: str = "") -> None:
        if self.on_event:
            try:
                self.on_event(user, action, detail)
            except Exception:  # noqa: BLE001 — the audit log never breaks a sign-in
                pass

    def revoke(self, client_id: str) -> bool:
        """Forget a client, its sign-ins in progress, codes and tokens (admin page). False when there is no
        such client. Raises OSError when the change cannot be saved: nothing is revoked then, in memory or
        on disk, so that a restart never brings a revoked client back."""
        with self.lock:
            if client_id not in self.clients:
                return False
            client = self.clients.pop(client_id)
            txns = {t: self.txns.pop(t) for t in [t for t, v in self.txns.items() if v["client_id"] == client_id]}
            taken = [(table, k, table.pop(k))
                     for table in (self.refresh, self._taking, self.codes, self.access, self.spent)
                     for k in [k for k, v in table.items() if v["client_id"] == client_id]]
            self._dirty = True
            try:
                self._save_tokens(strict=True)   # the tokens first: a half-done revoke leaves no token
                self._save_clients()
            except OSError:
                self.clients[client_id] = client
                self.txns.update(txns)
                for table, k, v in taken:
                    table[k] = v
                self._dirty = True
                self._save_tokens()   # the token file may be written already: put the tokens back there too
                raise
            self._released.notify_all()
            self._dropped.extend(v["tid"] for table, _, v in taken if table is not self.spent)
        self._end_dropped()
        return True

    # ------------------------------------------------------------------ token
    def _known_client(self, form: dict) -> None:
        """A client_id that this server does not know (revoked by an admin, or pushed out by
        KB_OAUTH_MAX_CLIENTS) is 401 invalid_client: an MCP client registers again only on that error. With
        400 invalid_grant it would retry the same client forever."""
        cid = form.get("client_id")
        if cid:
            with self.lock:
                known = cid in self.clients
            if not known:
                raise OAuthError(401, "invalid_client", "unknown client_id: register again")

    def token(self, form: dict) -> dict:
        held: list[str] = []
        taking: list[str] = []
        try:
            return self._token(form, held, taking)
        finally:
            with self.lock:
                # Still reserved by this call: it refused the token, so the token is consumed. A token that
                # was put back or claimed left `taking`: another call may hold it by now.
                for k in taking:
                    if self._taking.pop(k, None) is not None:
                        self._dirty = True
                self._released.notify_all()
                for t in held:   # issued, put back, or dropped: _end_dropped tells which
                    self._inflight[t] -= 1
                    if not self._inflight[t]:
                        del self._inflight[t]
                    self._dropped.append(t)
            self._end_dropped()

    def _hold(self, entry: dict | None, held: list[str]) -> None:
        """Count the sign-in of a popped code or refresh token as in use until token() ends. The caller
        holds the lock."""
        if entry and entry.get("tid"):
            self._inflight[entry["tid"]] = self._inflight.get(entry["tid"], 0) + 1
            held.append(entry["tid"])

    def _token(self, form: dict, held: list[str], taking: list[str]) -> dict:
        grant = form.get("grant_type")
        if grant in ("authorization_code", "refresh_token"):
            self._known_client(form)
        if grant == "authorization_code":
            with self.lock:
                self._expire()
                entry = self.codes.pop(form.get("code", ""), None)  # single use, even on failure
                self._hold(entry, held)
            if not entry:
                raise OAuthError(400, "invalid_grant", "unknown or expired code")
            if form.get("client_id") and form["client_id"] != entry["client_id"]:
                raise OAuthError(400, "invalid_grant", "code was issued to another client")
            if form.get("redirect_uri", entry["redirect_uri"]) != entry["redirect_uri"]:
                raise OAuthError(400, "invalid_grant", "redirect_uri does not match")
            if b64url_sha256(form.get("code_verifier", "")) != entry["challenge"]:
                raise OAuthError(400, "invalid_grant", "PKCE verification failed")
            try:
                return self._issue(entry["token"], entry["client_id"], entry.get("user", ""))
            except OAuthError as e:
                if e.status == 503:   # only the save failed: the code is good for another try
                    with self.lock:
                        if entry["client_id"] in self.clients and entry["exp"] >= self.now():
                            self.codes[form.get("code", "")] = entry
                raise
        if grant == "refresh_token":
            rt = form.get("refresh_token", "")
            key = _sha(rt)
            with self.lock:
                self._expire()
                # Another call holds this token and asks the permission webhook: wait for its end, then
                # this call is the second session of the overlap (or takes the token that call put back).
                deadline = time.monotonic() + TAKING_WAIT
                while key in self._taking and time.monotonic() < deadline:
                    self._released.wait(max(0.01, deadline - time.monotonic()))
                if key in self._taking:
                    raise OAuthError(503, "temporarily_unavailable",
                                     "another refresh of this token is in progress: try again")
                entry = self.refresh.pop(key, None)  # rotated on use
                if entry is not None:
                    self._taking[key] = entry         # on disk as before until the new pair is
                    taking.append(key)
                self._hold(entry, held)
                used = None if entry else self.spent.get(key)
                self._hold(used, held)
            if not entry:
                if used:
                    return self._used_again(key, dict(used), rt, form)
                raise OAuthError(400, "invalid_grant", "unknown refresh token")
            if form.get("client_id") and form["client_id"] != entry["client_id"]:
                raise OAuthError(400, "invalid_grant", "refresh token was issued to another client")
            token = _unseal(entry["sealed"], rt)
            if token is None:
                raise OAuthError(400, "invalid_grant", "the refresh token cannot be read: sign in again")
            with self.lock:
                self._plain[entry["tid"]] = token
            try:
                self.check_token(token)
            except Exception as e:  # noqa: BLE001
                if getattr(e, "status", None) in (401, 403):  # expired or revoked: sign in again
                    raise OAuthError(400, "invalid_grant", "the API token expired: sign in again") from None
                # The permission webhook is down (503) or broke: the refresh token stays for the next try.
                with self.lock:
                    taking.remove(key)
                    if self._taking.pop(key, None) is not None and entry["client_id"] in self.clients:
                        self.refresh[key] = entry
                    self._released.notify_all()
                raise OAuthError(503, "temporarily_unavailable",
                                 "the sign-in service is not available: try again later") from None
            self._seen(entry["client_id"], entry.get("user", ""))
            return self._issue(token, entry["client_id"], entry.get("user", ""), replaces=entry.get("at"),
                               used=(key, entry), owner=taking)
        raise OAuthError(400, "unsupported_grant_type", "use authorization_code or refresh_token")

    def _used_again(self, key: str, used: dict, rt: str, form: dict) -> dict:
        """A refresh token that was used before comes again. Inside ROTATE_OVERLAP it is a second session
        of the same client that read the stored token a moment before the first one replaced it: it gets
        a pair of its own, at most OVERLAP_PAIRS times, after the same check of the token behind it.
        Later, or from another client, or for a sign-in that ended, somebody holds a copy of an old token
        (the client, or a thief who took it): every token of that sign-in ends, and the person signs in
        again. The end is on disk before the answer; when it cannot be saved, it is undone (503)."""
        same_client = not form.get("client_id") or form["client_id"] == used["client_id"]
        if (same_client and used.get("sealed") and not used.get("ended")
                and self.now() - used["when"] <= ROTATE_OVERLAP):
            token = _unseal(used["sealed"], rt)
            if token is not None:
                with self.lock:
                    live = self.spent.get(key)
                    pairs = (live or {}).get("pairs", 0)
                    if live is None or live.get("ended") or pairs >= OVERLAP_PAIRS:
                        raise OAuthError(400, "invalid_grant", "this refresh token was used before: sign in again")
                    live["pairs"] = pairs + 1
                    self._dirty = True
                try:
                    self.check_token(token)
                except Exception as e:  # noqa: BLE001
                    if getattr(e, "status", None) in (401, 403):
                        raise OAuthError(400, "invalid_grant", "the API token expired: sign in again") from None
                    raise OAuthError(503, "temporarily_unavailable",
                                     "the sign-in service is not available: try again later") from None
                return self._issue(token, used["client_id"], used.get("user", ""), overlap=key)
        client = self.clients.get(used["client_id"], {})
        if REUSE != "log":
            with self.lock:
                taken = [(table, k, table.pop(k)) for table in (self.refresh, self._taking, self.access)
                         for k in [k for k, v in table.items() if v["tid"] == used["tid"]]]
                marks = [(v, v.pop("sealed", None)) for v in self.spent.values()
                         if v["tid"] == used["tid"] and not v.get("ended")]
                for v, _ in marks:
                    v["ended"] = True          # no overlap pair for this sign-in any more
                self._dirty = True
                try:
                    self._save_tokens(strict=True)
                except OSError:
                    for table, k, v in taken:
                        table[k] = v
                    for v, sealed in marks:
                        del v["ended"]
                        if sealed is not None:
                            v["sealed"] = sealed
                    raise OAuthError(503, "temporarily_unavailable",
                                     "the change cannot be saved on the server: try again later") from None
                self._released.notify_all()
                self._dropped.extend(v["tid"] for _, _, v in taken)
                self._dropped.append(used["tid"])
        self._event(used.get("user") or "?", "refresh token used twice",
                    f"{str(client.get('client_name') or used['client_id'])[:60]}: "
                    + ("only logged" if REUSE == "log" else "the sign-in ended"))
        raise OAuthError(400, "invalid_grant", "this refresh token was used before: sign in again")

    def _issue(self, token: str, client_id: str, user: str = "", replaces: str | None = None,
               used: tuple[str, dict] | None = None, owner: list[str] | None = None,
               overlap: str | None = None) -> dict:
        """A new access token that ends after TOKEN_TTL, and a refresh token (no end when REFRESH_DAYS
        is 0). An entry is kept under the SHA-256 of its token, with the token behind it sealed. The client
        must still exist under the lock: one revoked while the refresh asked the permission webhook gets
        nothing. `replaces` is the SHA-256 of the access token a refresh supersedes: it ends after
        ROTATE_OVERLAP. `used` is the refresh token this call consumed (its key and entry): it goes to
        `spent`; `owner` is the caller's list of reservations in `_taking`, which loses that key here.
        `overlap` is the key of the used token an overlap pair is made for: its sign-in must not have
        ended while the permission webhook was asked. A client keeps at most TOKENS_PER_CLIENT access and refresh tokens; the oldest go first.
        The change is saved before the answer; when the save fails, it is undone and the client gets 503."""
        rt = secrets.token_urlsafe(32)
        at = ACCESS_PREFIX + secrets.token_urlsafe(32)
        rkey, akey, tid = _sha(rt), _sha(at), _sha(token)
        with self.lock:
            self._expire()
            if client_id not in self.clients:
                raise OAuthError(401, "invalid_client", "the client was revoked: register again")
            if overlap is not None and (self.spent.get(overlap) or {"ended": True}).get("ended"):
                raise OAuthError(400, "invalid_grant", "this sign-in ended: sign in again")
            taken = self._taking.pop(used[0], None) if used is not None else None
            if used is not None:
                if owner is not None and used[0] in owner:
                    owner.remove(used[0])
                self._released.notify_all()
                if taken is None:   # ended while the permission webhook was asked: nothing is changed
                    raise OAuthError(400, "invalid_grant", "this sign-in ended: sign in again")
            now = self.now()
            old = self.access.get(replaces or "")
            old_exp = old["exp"] if old is not None else None
            if old is not None:
                old["exp"] = min(old["exp"], now + ROTATE_OVERLAP)
            pruned = []
            for table in (self.access, self.refresh):
                mine = sorted((v.get("created", v.get("exp", 0)), k) for k, v in table.items()
                              if v["client_id"] == client_id)
                for _, k in mine[:max(0, len(mine) - TOKENS_PER_CLIENT + 1)]:
                    pruned.append((table, k, table.pop(k)))
            self.refresh[rkey] = {"sealed": _seal(token, rt), "tid": tid, "client_id": client_id, "user": user,
                                  "at": akey, "created": now}
            if REFRESH_DAYS > 0:
                self.refresh[rkey]["exp"] = now + REFRESH_DAYS * 86400
            self.access[akey] = {"sealed": _seal(token, at), "tid": tid, "client_id": client_id, "created": now,
                                 "exp": now + TOKEN_TTL}
            if used is not None:
                self.spent[used[0]] = {"tid": tid, "client_id": client_id, "user": user, "when": now,
                                       "sealed": used[1]["sealed"]}
            self._plain[tid] = token
            self._dirty = True
            try:
                self._save_tokens(strict=True)
            except OSError:
                del self.refresh[rkey], self.access[akey]
                for table, k, v in pruned:
                    table[k] = v
                if old is not None:
                    old["exp"] = old_exp
                if used is not None:              # the client can try again with the token it has
                    self.spent.pop(used[0], None)
                    self.refresh[used[0]] = used[1]
                raise OAuthError(503, "temporarily_unavailable",
                                 "the sign-in cannot be saved on the server: try again later") from None
            self._dropped.extend(v["tid"] for _, _, v in pruned)
        return {"access_token": at, "token_type": "Bearer", "expires_in": TOKEN_TTL,
                "refresh_token": rt}

    def resolve(self, access_token: str) -> str | None:
        """The login webhook's token behind an access token of this server, or None when it is unknown,
        ended or its client was revoked."""
        with self.lock:
            entry = self.access.get(_sha(access_token))
            if entry is None or entry["exp"] < self.now() or entry["client_id"] not in self.clients:
                return None
            token = self._plain.get(entry["tid"])
            if token is None:
                token = _unseal(entry["sealed"], access_token)
                if token is not None:
                    self._plain[entry["tid"]] = token
            return token


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _stream(secret: str, size: int) -> bytes:
    """32 bytes of MAC key, then `size` bytes of key stream, from the client's token and SEAL_KEY."""
    seed = b"kb-oauth-seal\0" + secret.encode() + b"\0" + hashlib.sha256(SEAL_KEY.encode()).digest()
    return hashlib.shake_256(seed).digest(32 + size)


def _seal(token: str, secret: str) -> str:
    """The token of the login webhook, readable only with `secret`: the refresh or access token that the
    client holds and this server never stores. `secret` is 256 random bits and seals one token, so a key
    stream derived from it is used once: SHAKE-256 gives 32 bytes of MAC key and the stream, the token is
    XORed with the stream, and an HMAC-SHA256 tag (16 bytes) tells a wrong secret or a changed entry.
    SEAL_KEY, when set, is a second key that is not in the file."""
    data = token.encode()
    stream = _stream(secret, len(data))
    body = bytes(x ^ y for x, y in zip(data, stream[32:]))
    return (hmac.new(stream[:32], body, hashlib.sha256).digest()[:16] + body).hex()


def _unseal(sealed: str, secret: str) -> str | None:
    """The token that _seal(token, secret) hid, or None for another secret or a broken entry."""
    try:
        raw = bytes.fromhex(sealed)
    except (TypeError, ValueError):
        return None
    tag, body = raw[:16], raw[16:]
    stream = _stream(secret, len(body))
    if len(tag) != 16 or not hmac.compare_digest(tag, hmac.new(stream[:32], body, hashlib.sha256).digest()[:16]):
        return None
    try:
        return bytes(x ^ y for x, y in zip(body, stream[32:])).decode()
    except UnicodeDecodeError:
        return None


def _same_app(redirect: str, registered: list[str]) -> bool:
    """A loopback client may pick a new port each run (RFC 8252 §7.3): same host and path count."""
    r = urllib.parse.urlsplit(redirect)
    for u in registered:
        g = urllib.parse.urlsplit(u)
        if (g.hostname, g.path) == (r.hostname, r.path):
            return True
    return False


# ---------------------------------------------------------------------- the login page
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in to the KB</title>
<style>body{{font:16px system-ui,sans-serif;background:#f5f5f4;color:#1c1917;margin:0}}
main{{max-width:22rem;margin:12vh auto;padding:2rem;background:#fff;border-radius:12px;
box-shadow:0 1px 3px #0002}}h1{{font-size:1.2rem;margin:0 0 .3rem}}p{{color:#57534e;margin:.2rem 0 1.2rem}}
label{{display:block;font-size:.9rem;margin:.8rem 0 .3rem}}input{{width:100%;box-sizing:border-box;
padding:.6rem;border:1px solid #d6d3d1;border-radius:8px;font-size:1rem}}button{{margin-top:1.2rem;
width:100%;padding:.7rem;border:0;border-radius:8px;background:#1c1917;color:#fff;font-size:1rem}}
.err{{color:#b91c1c;font-size:.9rem}}</style></head><body><main>
<h1>Code knowledge base</h1><p>{lead}</p>{error}
<form method="post" action="{action}"><input type="hidden" name="txn" value="{txn}">{fields}
<button type="submit">{button}</button></form></main></body></html>"""


def page(action: str, txn: str, step: str, error: str = "") -> bytes:
    if step == "code":
        lead = "Enter the code from your authenticator app (or a recovery code)."
        fields = ('<input type="hidden" name="step" value="code"><label for="c">Code</label>'
                  '<input id="c" name="code" autocomplete="one-time-code" inputmode="numeric" autofocus required>')
        button = "Confirm"
    else:
        lead = "Sign in with your user name and password."
        fields = ('<input type="hidden" name="step" value="password"><label for="u">User name</label>'
                  '<input id="u" name="username" autocomplete="username" autofocus required>'
                  '<label for="p">Password</label>'
                  '<input id="p" name="password" type="password" autocomplete="current-password" required>')
        button = "Sign in"
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return PAGE.format(lead=lead, error=err, action=html.escape(action), txn=html.escape(txn),
                       fields=fields, button=button).encode()
