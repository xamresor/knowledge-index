"""The KB's own users, rights and tokens: an access plugin (bin/plugins.py).

For a team that wants no second system behind the two webhooks of the access protocol
(docs/access-protocol.md). The core keeps no users; this plugin does.

- **Rights come from an admin.** On the Users tab an admin adds a person, chooses the projects the person
  may search (`*` for all) and the admin right, and gives the person a first key.
- **The password comes from the person.** A user name is an e-mail address. A new person gets a mail
  with a link and chooses a password; "Forgot the password?" on the sign-in page and "Reset password" of
  an admin send the same mail. The link works once and for a short time; a "forgot" link does not end a
  pending invite, both work until one is used. The store keeps a password as scrypt with its own salt. Five
  wrong passwords from one address lock the sign-in of that name from that address for 15 minutes: a
  stranger cannot lock the owner out, who signs in from another address.
- **An admin keeps a project.** An admin cannot save the own row without a project: the permission webhook
  would refuse every token of that admin, and the admin could not sign in to undo it.
- **Tokens come from the person.** Signed in, the person opens "My tokens": the own
  tokens, one per computer or agent. A token has the rights of its owner and none of its own. A person who
  is no admin reaches only that page and the how-to.
- A token is a long random string. It shows once; the store keeps only its SHA-256. It lives until its
  owner or an admin removes it, or an admin disables or removes the user. A change holds at once.
- The password also works at the browser sign-in of an agent. Each sign-in makes a sign-in token: it ends
  after 30 days without a request, and a new password ends all of them.

Mail: `<data>/smtp.env` (owner only) or the environment: KB_SMTP_HOST, KB_SMTP_PORT (587), KB_SMTP_USER,
KB_SMTP_PASSWORD, KB_SMTP_FROM, KB_SMTP_TLS (starttls | ssl | none) and KB_PUBLIC_URL, the address of this
server that goes into a mail (for example https://kb.example.com). Without KB_PUBLIC_URL there is no
"Forgot the password?": a link must never be built from the Host header of a stranger's request. Without
mail an admin gives a first key instead, as before.

Install: `kb addon install contrib/users`. The first admin, on the server:

  python3 contrib/users/plugin.py add ann --admin        add a user with every project, print the key
  python3 contrib/users/plugin.py add bob --projects api,web
  python3 contrib/users/plugin.py key bob                one more key for the user
  python3 contrib/users/plugin.py revoke bob             end every key of the user
  python3 contrib/users/plugin.py list

Store: `<data>/users.json`, readable by the owner only. Every change reads, changes and saves the file under
a flock on `<data>/users.json.lock`, so the command line and a running server never lose each other's
changes. Standard library only.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import smtplib
import ssl
import tempfile
import threading
import time
from email.message import EmailMessage

NAME = "users"
DESCRIPTION = "the KB's own users, rights and tokens, with the Users and My tokens pages"
ACCESS = True   # answers the access webhooks (bin/plugins.py is_access reads this line)
TOOLS: list = []
TOKENS = True  # people hold tokens made here: the how-to page explains the header and "My tokens"
TABS = [("Users", "/users", "admin"), ("My tokens", "/me", "member")]  # label, path under /admin, who may open
PUBLIC = ["/forgot", "/reset"]  # pages under /admin without a session
SIGN_IN_HINT = "your password, or a token of yours in the password field"

KEY_PREFIX = "kbk_"
NAME_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,80}$")
LABEL = re.compile(r"^[A-Za-z0-9 ._@+-]{1,60}$")
TOUCH_SECONDS = 300  # write "last used" at most this often per token
ONLINE_SECONDS = 900  # a user with a request in this time is "online"
MAX_TOKENS = 20
SIGNIN_PREFIX = "kbs_"
SIGNIN_KEEP = 10  # sign-in tokens per user: the oldest go
SIGNIN_IDLE = 30 * 86400  # a sign-in token ends after this time without a request
EMAIL = re.compile(r"^[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
PW_MIN, PW_MAX = 12, 200
RESET_TTL = 3600  # "forgot the password": the link works this long
INVITE_TTL = 3 * 86400  # the first mail to a new person
RESET_EVERY = 300  # one mail per user in this time
LOCK_AFTER, LOCK_SECONDS = 5, 900  # wrong passwords from one address, then that name is shut there this long


class UserError(Exception):
    """A refusal with a text that a person may read."""


class Locked(UserError):
    """Too many wrong passwords for a name."""

    def __init__(self, retry: int):
        super().__init__("Too many wrong passwords. Try again later, or use \"Forgot the password?\".")
        self.retry = retry


def _scrypt(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def parse_projects(text: str) -> list[str] | str:
    """The projects field of a form or of the command line: `*`, or names split by commas or spaces."""
    names = [p for p in re.split(r"[,\s]+", (text or "").strip()) if p]
    return "*" if "*" in names else sorted(set(names))


def projects_from_form(form: dict, known) -> list[str] | str:
    """The projects of an add or save form. The page sends one checkbox per project of the server
    (`p:<name>`), `p:*` for all, and `pick` to say that the checkboxes are there: an empty choice is then
    "no project". A name the server does not know drops. Without `pick` the text field `projects` counts."""
    if "pick" not in form:
        return parse_projects(str(form.get("projects", "")))
    if form.get("p:*") == "on":
        return "*"
    return sorted(n for n in known if form.get(f"p:{n}") == "on")


class Store:
    def __init__(self, path: str, now=time.time):
        self.path = path
        self.now = now
        self._lock = threading.RLock()
        self._stamp: tuple | None = None  # (inode, mtime in ns, size) of the file as last read
        self._users: dict[str, dict] = {}
        self._fails: dict[tuple[str, str], list[float]] = {}  # wrong passwords per (name, address), this process
        self._flock_fd: int | None = None
        self._flock_depth = 0

    # ------------------------------------------------------------------ file
    def _load(self) -> dict[str, dict]:
        try:
            st = os.stat(self.path)
        except OSError:
            self._users, self._stamp = {}, None
            return self._users
        stamp = (st.st_ino, st.st_mtime_ns, st.st_size)  # a save makes a new file: a new inode
        if stamp != self._stamp:
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                self._users = data.get("users", {}) if isinstance(data, dict) else {}
            except (OSError, ValueError):
                raise UserError("the users file cannot be read") from None
            for rec in self._users.values():  # a file of the first version: one key per user
                if "tokens" not in rec:
                    rec["tokens"] = {}
                    if rec.get("key_hash"):
                        rec["tokens"]["t1"] = {"hash": rec["key_hash"], "start": rec.get("key_start", ""),
                                               "label": "first key", "created": rec.get("key_issued"),
                                               "last_used": rec.get("last_used")}
                    for field in ("key_hash", "key_start", "key_issued"):
                        rec.pop(field, None)
            self._stamp = stamp
        return self._users

    @contextlib.contextmanager
    def _changing(self):
        """Read, change and save the file as one step, also against another process (the command line, a
        second server): the thread lock of this store, then a flock on `<file>.lock`, then a fresh read.
        Nested use takes the flock once."""
        with self._lock:
            if self._flock_depth == 0:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                fd = os.open(f"{self.path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                self._flock_fd = fd
            self._flock_depth += 1
            try:
                self._load()
                yield
            finally:
                self._flock_depth -= 1
                if self._flock_depth == 0 and self._flock_fd is not None:
                    fd, self._flock_fd = self._flock_fd, None
                    with contextlib.suppress(OSError):
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # A temporary file of its own for every save: another Store on the same file (the CLI, a second load
        # of the plugin) must not rename it away between our write and our rename.
        fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(self.path)}.", suffix=".tmp",
                                   dir=os.path.dirname(self.path) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"users": self._users}, f, indent=1, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        st = os.stat(self.path)
        self._stamp = (st.st_ino, st.st_mtime_ns, st.st_size)

    # ------------------------------------------------------------------ admin side
    def all(self) -> dict[str, dict]:
        """Every user, for the pages: no hash of a token, of the password or of a reset link."""
        with self._lock:
            now = self.now()
            return {name: {**{k: v for k, v in rec.items() if k not in ("pw", "reset", "invite")},
                           "has_password": bool(rec.get("pw")),
                           "reset_pending": any((rec.get(slot) or {}).get("expires", 0) > now
                                                for slot in ("reset", "invite")),
                           "tokens": {tid: {k: v for k, v in t.items() if k != "hash"}
                                      for tid, t in rec["tokens"].items()}}
                    for name, rec in self._load().items()}

    @staticmethod
    def _projects(projects) -> list[str] | str:
        if projects == "*":
            return "*"
        if not isinstance(projects, (list, tuple, set)) or not all(isinstance(p, str) and NAME_RE.match(p) for p in projects):
            raise UserError("a project name has letters, digits and . _ @ + - only")
        return sorted(set(projects))

    def _user(self, name: str) -> dict:
        rec = self._load().get((name or "").strip().lower())
        if rec is None:
            raise UserError("no such user")
        return rec

    def _new_token(self, rec: dict, label: str) -> str:
        if sum(1 for t in rec["tokens"].values() if t.get("kind") != "signin") >= MAX_TOKENS:
            raise UserError(f"a user has {MAX_TOKENS} tokens at most: remove one first")
        label = " ".join(str(label or "").split())[:60] or "token"
        if not LABEL.match(label):
            raise UserError("a token name has letters, digits, spaces and . _ @ + - only")
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        rec["tokens"][secrets.token_hex(4)] = {"hash": _hash(key), "start": key[:8], "label": label,
                                              "created": self.now(), "last_used": None}
        return key

    def add(self, name: str, projects, admin: bool = False, key: bool = True) -> str:
        """A new user. With `key` the user gets a first token, and the key comes back: the only time it
        exists in clear text. Without it the user has nothing yet: a mail with a link comes next."""
        name = (name or "").strip()
        if not NAME_RE.match(name):
            raise UserError("a user name has 1 to 80 letters, digits and . _ @ + - only")
        with self._changing():
            users = self._load()
            if name.lower() in users:
                raise UserError("this user exists already")
            rec = {"projects": self._projects(projects), "admin": bool(admin), "disabled": False,
                   "created": self.now(), "last_used": None, "tokens": {}}
            made = self._new_token(rec, "first key") if key else ""
            users[name.lower()] = rec
            self._save()
            return made

    def issue(self, name: str, label: str = "") -> str:
        """One more token for a user. The other tokens of the user stay."""
        with self._changing():
            key = self._new_token(self._user(name), label)
            self._save()
            return key

    def revoke(self, name: str) -> None:
        """End every token of a user."""
        with self._changing():
            self._user(name)["tokens"] = {}
            self._save()

    def remove_token(self, name: str, token_id: str, keep_one: bool = False) -> None:
        """End one token. `keep_one`: refuse the last token, for a person who removes the own tokens
        and would then have no way back in."""
        with self._changing():
            tokens = self._user(name)["tokens"]
            if token_id not in tokens:
                raise UserError("no such token")
            if keep_one and len(tokens) == 1 and not self._user(name).get("pw"):
                raise UserError("This is your last token: make a new one first, then remove this one.")
            del tokens[token_id]
            self._save()

    def update(self, name: str, projects=None, admin: bool | None = None, disabled: bool | None = None) -> None:
        with self._changing():
            rec = self._user(name)
            if projects is not None:
                rec["projects"] = self._projects(projects)
            if admin is not None:
                rec["admin"] = bool(admin)
            if disabled is not None:
                rec["disabled"] = bool(disabled)
            self._save()

    def remove(self, name: str) -> None:
        with self._changing():
            self._user(name)
            del self._users[name.strip().lower()]
            self._save()

    # ------------------------------------------------------------------ password and reset link
    def set_password(self, name: str, password: str, reset_value: str | None = None) -> None:
        """A new password. It ends every sign-in token of the user and any reset link. The hash takes
        about 30 ms: it is made before the lock, so token checks do not wait for it. With `reset_value`,
        the reset link is checked again under the lock: two uses of one link cannot both succeed."""
        password = password or ""
        if not PW_MIN <= len(password) <= PW_MAX:
            raise UserError(f"A password has {PW_MIN} characters or more.")
        if password.strip().lower() == (name or "").strip().lower():
            raise UserError("The password must not be the user name.")
        salt = secrets.token_bytes(16)
        hashed = _scrypt(password, salt)
        with self._changing():
            rec = self._user(name)
            if reset_value is not None and not self._reset_ok(rec, reset_value):
                raise UserError("This link does not work any more. Ask for a new mail.")
            rec["pw"] = {"salt": salt.hex(), "hash": hashed, "set": self.now()}
            rec.pop("reset", None)
            rec.pop("invite", None)
            rec["tokens"] = {tid: t for tid, t in rec["tokens"].items() if t.get("kind") != "signin"}
            lower = name.strip().lower()
            for key in [k for k in self._fails if k[0] == lower]:
                del self._fails[key]
            self._save()

    @staticmethod
    def _password_ok(pw: dict | None, usable: bool, password: str) -> bool:
        """The same work for a user without a password and for no user at all: the time tells nothing.
        `pw` is the stored password record; `usable` says that the user exists and is not disabled."""
        pw = pw or {"salt": "00" * 16, "hash": ""}
        return hmac.compare_digest(_scrypt(password or "", bytes.fromhex(pw["salt"])), pw["hash"] or "-") and usable

    def start_reset(self, name: str, ttl: float = RESET_TTL, invite: bool = False) -> str:
        """A one-time value for a "choose a password" link. Returns it: the store keeps its SHA-256.
        An invite and a "forgot" link have a slot each: a "forgot" mail does not end a pending invite of
        three days, and both work until one of them sets the password."""
        with self._changing():
            rec = self._user(name)
            if rec.get("disabled"):
                raise UserError("This user is disabled.")
            last = max((rec.get(slot) or {}).get("sent", 0) for slot in ("reset", "invite"))
            if self.now() - last < RESET_EVERY:
                raise UserError("A mail went out a moment ago. Wait a few minutes.")
            value = secrets.token_urlsafe(32)
            rec["invite" if invite else "reset"] = {"hash": _hash(value), "expires": self.now() + ttl,
                                                    "sent": self.now()}
            self._save()
            return value

    def _reset_ok(self, rec: dict, value: str) -> bool:
        ok = False
        for slot in ("reset", "invite"):   # both compared: the time does not tell which one matched
            link = rec.get(slot) or {}
            ok |= hmac.compare_digest(str(link.get("hash", "")), _hash(value or "")) \
                and link.get("expires", 0) > self.now()
        return ok and not rec.get("disabled")

    def reset_user(self, value: str) -> str | None:
        """The user of a reset value that still works, or None."""
        found = None
        with self._lock:
            for name, rec in self._load().items():
                if self._reset_ok(rec, value):
                    found = name
        return found

    def finish_reset(self, value: str, password: str) -> str:
        name = self.reset_user(value)
        if name is None:
            raise UserError("This link does not work any more. Ask for a new mail.")
        self.set_password(name, password, reset_value=value)  # checks and ends the link in one step
        return name

    def seen(self) -> list[tuple[str, float]]:
        """The active users with the time of their last request, newest first: "who is online"."""
        with self._lock:
            rows = [(name, rec["last_used"]) for name, rec in self._load().items()
                    if rec.get("last_used") and not rec.get("disabled")]
        return sorted(rows, key=lambda r: -r[1])

    # ------------------------------------------------------------------ webhook side
    def _by_key(self, key: str) -> tuple[str, dict, str] | None:
        """The active user and the token id of a key. Every token is compared, so the time does not
        tell which one matched."""
        want, found, now = _hash(key or ""), None, self.now()
        for name, rec in self._load().items():
            for tid, token in rec["tokens"].items():
                idle = token.get("kind") == "signin" and \
                    now - (token.get("last_used") or token.get("created") or 0) > SIGNIN_IDLE
                if hmac.compare_digest(str(token.get("hash", "")), want) and not rec.get("disabled") and not idle:
                    found = (name, rec, tid)
        return found

    def admin_by_key(self, key: str) -> bool:
        with self._lock:
            hit = self._by_key(key)
            return bool(hit and hit[1].get("admin"))

    def user_by_key(self, key: str) -> str | None:
        with self._lock:
            hit = self._by_key(key)
            return hit[0] if hit else None

    def login(self, username: str, password: str, ip: str = "") -> str:
        """The login webhook. The password field holds the password of the user, or a token of the user.
        -> the bearer token: a new sign-in token after a password, the token itself after a token.

        The password hash (scrypt, about 30 ms) runs outside the lock: a flood of wrong passwords must not
        make every token check wait. The record is copied under the lock, hashed without it, and the lock
        is taken again to save; a password changed in between counts as a wrong password.

        Wrong passwords count per name and client address (`ip`): five from one address shut that name for
        that address only. A stranger who knows a name cannot keep its owner out."""
        name = (username or "").strip().lower()
        who = (name, str(ip or ""))
        with self._lock:
            now = self.now()
            hit = self._by_key(password)
            if hit is not None and hit[0] == name:  # a token is 256 random bits: the lock is not for it
                return password
            fails = [t for t in self._fails.get(who, []) if now - t < LOCK_SECONDS]
            if len(fails) >= LOCK_AFTER:
                raise Locked(int(LOCK_SECONDS - (now - fails[0])) + 1)
            rec = self._load().get(name)
            pw = dict(rec["pw"]) if rec and rec.get("pw") else None
            usable = bool(rec) and not rec.get("disabled")
        ok = self._password_ok(pw, usable, password)
        if ok:
            with self._changing():
                rec = self._users.get(name)
                if rec is not None and not rec.get("disabled") and (rec.get("pw") or {}).get("hash") == pw["hash"]:
                    now = self.now()
                    self._fails.pop(who, None)
                    key = SIGNIN_PREFIX + secrets.token_urlsafe(32)
                    tokens = rec["tokens"]
                    tokens[secrets.token_hex(4)] = {"hash": _hash(key), "start": key[:8], "label": "sign-in",
                                                   "kind": "signin", "created": now, "last_used": now}
                    signins = sorted((tid for tid, t in tokens.items() if t.get("kind") == "signin"),
                                     key=lambda tid: tokens[tid].get("last_used") or tokens[tid].get("created") or 0)
                    for tid in signins:
                        stale = now - (tokens[tid].get("last_used") or tokens[tid].get("created") or 0) > SIGNIN_IDLE
                        if stale or len([1 for t in tokens.values() if t.get("kind") == "signin"]) > SIGNIN_KEEP:
                            del tokens[tid]
                    self._save()
                    return key
        with self._lock:
            now = self.now()
            self._fails[who] = [t for t in self._fails.get(who, []) if now - t < LOCK_SECONDS] + [now]
            if len(self._fails) > 5000:  # a flood of names: keep the map small
                self._fails = {k: v for k, v in self._fails.items() if v and now - v[-1] < LOCK_SECONDS}
        raise UserError("Wrong user name or password.")

    def permissions(self, token: str, asked: list[str]) -> tuple[int, dict]:
        """The permission webhook: the HTTP status and the body."""
        with self._lock:
            hit = self._by_key(token)
            if hit is None:
                return 401, {"message": "unknown key"}
            name, rec, tid = hit
            now = self.now()
            if now - (rec["tokens"][tid].get("last_used") or 0) >= TOUCH_SECONDS:
                with self._changing():  # a fresh read under the file lock, then the "last used" time
                    hit = self._by_key(token)
                    if hit is None:
                        return 401, {"message": "unknown key"}
                    name, rec, tid = hit
                    rec["tokens"][tid]["last_used"] = rec["last_used"] = now
                    self._save()
            if rec.get("projects") == "*":
                return 200, {"user": {"name": name}, "projects": "*"}
            allowed = [p for p in rec.get("projects") or [] if p in set(asked or [])]
            if not allowed:
                return 403, {"message": "no project of this server"}
            return 200, {"user": {"name": name}, "projects": allowed}


# ---------------------------------------------------------------------------------- pages
def _when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


CSS = """<style>
.ku{width:100%}
.ku td{vertical-align:middle;padding-top:10px;padding-bottom:10px}
.ku input[type=checkbox],.ku-row input[type=checkbox]{width:15px;height:15px;margin:0;padding:0;flex:none}
.ku .who{font-weight:600;white-space:nowrap}
.ku .who .m{display:block;font-weight:400;color:var(--ink3);font-size:12px;margin-top:2px}
.ku .c{text-align:center;width:1%;white-space:nowrap}
.pill{font-size:11px;font-weight:400;padding:1px 7px;border-radius:9px;background:var(--panel2);color:var(--ink2);margin-left:6px}
.pill.bad{color:var(--crit)}
.ku .tok{white-space:nowrap;color:var(--ink2)}
.ku .tok code{color:var(--ink)}
.ku .tok summary{cursor:pointer}
.ku .tok .list{display:grid;grid-template-columns:auto auto auto;gap:3px 12px;margin-top:6px}
.ku .tok .list .m{color:var(--ink3);font-size:12px}
.ku .act{width:1%}
.ku .act .btns{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;width:290px;margin-left:auto}
.ku .act form{display:inline}
.ku .act button{padding:4px 10px;white-space:nowrap}
button.danger{color:var(--crit)}
.ku tr.off td:not(.act){opacity:.55}
.kp{display:flex;flex-wrap:wrap;gap:6px}
.kp label{display:inline-flex;align-items:center;gap:6px;margin:0;padding:3px 9px;border:1px solid var(--line);
  border-radius:12px;font-size:12px;color:var(--ink);cursor:pointer;white-space:nowrap;background:var(--panel)}
.kp label:has(input:checked){border-color:var(--accent);background:var(--accent-soft)}
.kp label.all{font-weight:600}
.ku-row{display:flex;gap:14px 18px;align-items:flex-end;flex-wrap:wrap}
.ku-row label.t{display:block;margin:0 0 4px}
.ku-row .f{flex:0 1 320px}
.ku-row .chk{display:flex;align-items:center;gap:6px;padding-bottom:7px;font-size:13px;color:var(--ink);margin:0}
.ku-row button{margin-bottom:1px}
</style>"""


def project_boxes(projects: list[str], chosen, form_id: str = "", who: str = "") -> str:
    """One checkbox per project of the server, and "all" first. `chosen` is "*" or a list of names. A name
    the user has and the server no longer knows still shows, so that a look at the row tells the truth."""
    esc, _ = _ui()
    attr = f' form="{form_id}"' if form_id else ""
    names = list(projects) + sorted(set(chosen if isinstance(chosen, list) else []) - set(projects))
    every = chosen == "*"

    def box(value: str, text: str, on: bool, cls: str = "") -> str:
        return (f'<label{f" class={cls}" if cls else ""}><input type="checkbox" name="p:{esc(value)}"{attr}'
                f'{" checked" if on else ""} aria-label="{esc(who)} {esc(text)}">{esc(text)}</label>')

    return (f'<input type="hidden" name="pick" value="1"{attr}><div class="kp">'
            + box("*", "all", every, "all")
            + "".join(box(n, n, not every and n in (chosen or []), "") for n in names)
            + "</div>")


def online_panel(me: str, seen: list[tuple[str, float]], now: float, online_seconds: float) -> str:
    """Who is online: the people with a request in the last `online_seconds`, then the others by their
    last request. Only the Users tab shows it: an admin sees who uses the server, a member does not."""
    esc, panel = _ui()
    rows = "".join(
        f'<tr><td>{esc(name)}{" <small>(you)</small>" if name == me else ""}</td>'
        f'<td>{"<span class=ok>online</span>" if now - ts < online_seconds else "<span class=muted>away</span>"}</td>'
        f'<td>{esc(_when(ts))}</td></tr>' for name, ts in seen)
    return panel("Who is online", f'<p>A person is online after a request in the last {int(online_seconds // 60)} minutes.</p>'
                                  f'<table><tr><th>User</th><th></th><th>Last request</th></tr>'
                                  f'{rows or "<tr><td colspan=3>nobody yet</td></tr>"}</table>')


def users_page(base: str, csrf: str, users: dict, projects: list[str], me: str, note: str = "",
               key: tuple[str, str] | None = None, mail: bool = False, online: str = "") -> str:
    """The Users tab. `key` is (user, key) right after an admin issued one: the only page that ever
    shows it. One row is one person; the fields of a row belong to its Save form by the `form` attribute.
    `online` is the "Who is online" panel (online_panel)."""
    esc, panel = _ui()
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'

    def form(action: str, name: str, inner: str, fid: str = "") -> str:
        return (f'<form method="post" action="{base}/admin/users/{action}"{f" id={fid}" if fid else ""}>{hidden}'
                f'<input type="hidden" name="name" value="{esc(name)}">{inner}</form>')

    rows = ""
    for i, (name, rec) in enumerate(sorted(users.items())):
        fid = f"ku{i}"
        tokens = rec.get("tokens") or {}
        pills = ('<span class="pill">you</span>' if name == me else "") + \
                ('<span class="pill bad">disabled</span>' if rec.get("disabled") else "") + \
                ("" if rec.get("has_password") else
                 f'<span class="pill">{"mail sent, no password yet" if rec.get("reset_pending") else "no password"}</span>')
        ordered = sorted(tokens.values(), key=lambda t: -(t.get("created") or 0))
        listed = "".join(f'<span>{esc(t.get("label", ""))}</span><code>{esc(t.get("start", ""))}…</code>'
                         f'<span class="m">{esc(_when(t.get("created")))}</span>' for t in ordered)
        state = (f'<details class="tok"><summary>{len(tokens)} token{"" if len(tokens) == 1 else "s"}</summary>'
                 f'<div class="list">{listed}</div></details>') if tokens else '<span class="bad">no key</span>'
        rows += (
            f'<tr class="{"off" if rec.get("disabled") else ""}"><td class="who">{esc(name)}{pills}'
            f'<span class="m">last used {esc(_when(rec.get("last_used")))}</span></td>'
            f'<td>{project_boxes(projects, rec.get("projects"), fid, name)}</td>'
            f'<td class="c"><input type="checkbox" name="admin" form="{fid}"{" checked" if rec.get("admin") else ""} '
            f'aria-label="{esc(name)} is an admin"></td>'
            f'<td class="c"><input type="checkbox" name="disabled" form="{fid}"{" checked" if rec.get("disabled") else ""} '
            f'aria-label="{esc(name)} is disabled"></td>'
            f'<td class="tok">{state}</td><td class="act"><div class="btns">'
            + form("save", name, '<button class="primary">Save</button>', fid)
            + (form("reset", name, "<button>Reset password</button>") if mail and EMAIL.match(name) else "")
            + form("key", name, "<button>New key</button>")
            + ("" if name == me else
               (form("revoke", name, "<button>Revoke all</button>") if tokens else "")
               + form("remove", name, '<button class="danger">Remove</button>'))
            + "</div></td></tr>")
    shown = ""
    if key:
        shown = (f'<div class=note>The key of <b>{esc(key[0])}</b>. Copy it now: this page shows it once, and the '
                 f'server keeps only its hash.<pre>{esc(key[1])}</pre>The person signs in with the user name '
                 f'<code>{esc(key[0])}</code> and this key as the password ("How to" tab).</div>')
    add = panel("Add a user", f'<form method="post" action="{base}/admin/users/add" class="ku-row">{hidden}'
                              f'<div class="f"><label class="t" for="ku-n">User name: the e-mail of the person</label>'
                              f'<input id="ku-n" name="name" required></div>'
                              f'<div><label class="t">Projects</label>{project_boxes(projects, "*", "", "new user")}</div>'
                              f'<label class="chk"><input type="checkbox" name="admin"> admin</label>'
                              f'<button class="primary">{"Add and send the mail" if mail else "Add and show the key"}</button>'
                              f'</form>')
    users_panel = panel("Users", f'<p>The people who may use this knowledge base. A user name is the e-mail of the person. '
                                 + ('A new person gets a mail with a link and chooses a password; "Reset password" sends the '
                                    'mail again. ' if mail else 'This server sends no mail: give a new person the first key. ')
                                 + f'Signed in, the person makes the own tokens on "My tokens". "all" gives every project, '
                                 f'also a project the server gets later. A change takes effect at once.</p>'
                                 f'<div style="overflow-x:auto"><table class="ku"><tr><th>User</th><th>Projects</th>'
                                 f'<th class="c">Admin</th><th class="c">Disabled</th><th>Tokens</th><th></th></tr>'
                                 f'{rows or "<tr><td colspan=6>none</td></tr>"}</table></div>'
                                 f'<p class="muted">"New key" adds a key and keeps the other tokens; "Revoke all" ends '
                                 f'every token of the person.</p>')
    return (f'{CSS}<main>{f"<div class=note>{esc(note)}</div>" if note else ""}{shown}'
            + add + users_panel + online + "</main>")


def me_page(base: str, csrf: str, me: str, tokens: dict, note: str = "", key: str = "", mail: bool = False) -> str:
    """The page of every signed-in person on a server with its own users: the own tokens. `key` is a
    token made a moment ago: the only page that ever shows it."""
    esc, panel = _ui()
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
    rows = "".join(
        f'<tr><td>{esc(t.get("label", ""))}</td><td><code>{esc(t.get("start", ""))}…</code></td>'
        f'<td>{esc(_when(t.get("created")))}</td><td>{esc(_when(t.get("last_used")))}</td>'
        f'<td class="act"><form method="post" action="{base}/admin/me/remove">{hidden}'
        f'<input type="hidden" name="id" value="{esc(tid)}"><button class="danger">Remove</button></form></td></tr>'
        for tid, t in sorted(tokens.items(), key=lambda kv: -(kv[1].get("created") or 0)))
    shown = (f'<div class=note>Your new token. Copy it now: this page shows it once, and the server keeps only '
             f'its hash.<pre>{esc(key)}</pre>Put it into your agent as on the "How to" tab.</div>') if key else ""
    body = (f'{CSS}<main>{f"<div class=note>{esc(note)}</div>" if note else ""}{shown}'
            + panel(f"My tokens — {me}", f'<p>One token for each computer or agent. A token lives until you remove it. '
                                         f'Remove a token here when a computer is lost or a token leaked: it stops at once.</p>'
                                         f'<table class="ku"><tr><th>Name</th><th>Starts with</th><th>Made</th><th>Last used</th><th></th></tr>'
                                         f'{rows or "<tr><td colspan=5>none</td></tr>"}</table>')
            + panel("Make a token", f'<form method="post" action="{base}/admin/me/add" class="ku-row">{hidden}'
                                    f'<div class="f"><label for="ku-l">Name, for example the computer</label>'
                                    f'<input id="ku-l" name="label" maxlength="60" required></div>'
                                    f'<button class="primary">Make and show the token</button></form>')
            + (panel("Password", f'<form method="post" action="{base}/admin/me/reset" class="ku-row">{hidden}'
                                 f'<span>A mail with a link goes to {esc(me)}. A new password ends every sign-in '
                                 f'of yours; your tokens stay.</span>'
                                 f'<button>Send me a link to change the password</button></form>') if mail else "")
            + "</main>")
    return body


# ---------------------------------------------------------------------------------- the plugin
STORE: Store | None = None


def default_path() -> str:
    """`<data>/users.json`. The data folder comes from the core (bin/paths.py)."""
    try:
        import paths
    except ImportError:
        # A checkout: <repo>/contrib/users -> <repo>/bin. An installed package (the Docker image):
        # bin/ sits next to the knowledge_index package in site-packages.
        here = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "bin")
        try:
            import knowledge_index
            installed = os.path.join(os.path.dirname(os.path.dirname(knowledge_index.__file__)), "bin")
        except ImportError:
            installed = here
        for d in (here, installed):
            if os.path.isfile(os.path.join(d, "paths.py")):
                sys.path.insert(0, d)
                break
        import paths
    return os.path.join(str(paths.data_home()), "users.json")


_STORE_LOCK = threading.Lock()


def store(core=None) -> Store:
    """The one Store of this process. The lock keeps the first concurrent calls from making two."""
    global STORE
    if STORE is None:
        with _STORE_LOCK:
            if STORE is None:
                STORE = Store(os.path.join(core.KB, "users.json") if core is not None else default_path())
    return STORE


def _ui():
    import admin  # bin/admin.py of the core: the look of the pages
    return admin.esc, admin.panel


class MailError(Exception):
    pass


def mail_config(core=None) -> dict:
    """KB_SMTP_* and KB_PUBLIC_URL: from `<data>/smtp.env` (KEY=VALUE lines), then from the environment."""
    cfg: dict[str, str] = {}
    try:
        folder = core.KB if core is not None else os.path.dirname(default_path())
        with open(os.path.join(folder, "smtp.env"), encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    cfg[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    for key in ("KB_SMTP_HOST", "KB_SMTP_PORT", "KB_SMTP_USER", "KB_SMTP_PASSWORD", "KB_SMTP_FROM", "KB_SMTP_TLS",
                "KB_PUBLIC_URL"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


def can_mail(cfg: dict) -> bool:
    return bool(cfg.get("KB_SMTP_HOST") and cfg.get("KB_SMTP_FROM"))


def send_mail(to: str, subject: str, text: str, cfg: dict) -> None:
    """One plain-text mail. Raises MailError with a text for the admin; never the password of the server."""
    if not can_mail(cfg):
        raise MailError("mail is not set up on this server (KB_SMTP_HOST, KB_SMTP_FROM)")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["KB_SMTP_FROM"], to, subject
    msg.set_content(text)
    mode = cfg.get("KB_SMTP_TLS", "starttls").lower()
    port = int(cfg.get("KB_SMTP_PORT") or (465 if mode == "ssl" else 587))
    try:
        if mode == "ssl":
            smtp = smtplib.SMTP_SSL(cfg["KB_SMTP_HOST"], port, timeout=20, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(cfg["KB_SMTP_HOST"], port, timeout=20)
        with smtp:
            if mode == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            if cfg.get("KB_SMTP_USER"):
                smtp.login(cfg["KB_SMTP_USER"], cfg.get("KB_SMTP_PASSWORD", ""))
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        raise MailError(f"the mail server refused: {type(e).__name__}") from None


def send_link(s: Store, name: str, base: str, cfg: dict, invite: bool = False) -> None:
    """Mail a "choose a password" link to a user. `base` is the address of this server for the link."""
    name = (name or "").strip().lower()  # the name as the store keeps it: one spelling of the address
    if not EMAIL.match(name):
        raise UserError("The user name is no e-mail address: there is nowhere to send the mail.")
    if not can_mail(cfg):
        raise UserError("Mail is not set up on this server.")
    ttl = INVITE_TTL if invite else RESET_TTL
    value = s.start_reset(name, ttl, invite=invite)
    link = f"{base.rstrip('/')}/admin/reset?t={value}"
    hours = int(ttl // 3600)
    lead = ("You have an account on the knowledge base." if invite
            else "Somebody asked for a new password for your account on the knowledge base.")
    text = (f"{lead}\n\nUser name: {name}\n\nOpen this link and choose a password. It works once, for "
            f"{hours} hour{'s' if hours != 1 else ''}:\n\n{link}\n\n"
            + ("" if invite else "If you did not ask for it, do nothing: your password stays.\n"))
    try:
        send_mail(name, "Knowledge base: choose your password", text, cfg)
    except MailError as e:
        with s._changing():  # the mail did not leave: the link must not stay open, and a retry must not wait
            s._user(name).pop("invite" if invite else "reset", None)
            s._save()
        raise UserError(f"The mail did not go out: {e}.") from None


def login(body: dict, core) -> tuple:
    """The login webhook: the password of the user, or a token of the user in the password field."""
    try:
        return 200, {"token": store(core).login(str(body.get("username", "")), str(body.get("password", "")),
                                                str(body.get("ip", "")))}
    except Locked as e:
        return 429, {"message": str(e)}, {"Retry-After": str(e.retry)}
    except UserError as e:
        return 401, {"message": str(e)}


def permissions(token: str, projects: list[str], core) -> tuple[int, dict]:
    try:
        return store(core).permissions(token, projects)
    except UserError as e:  # the store itself is broken: an outage, not a refusal
        sys.stderr.write(f"users: {e}\n")
        return 503, {"message": "the users store is not available"}


def role(token: str, core) -> str | None:
    """Who may sign in to the pages with this token: "admin", "member" (the own tokens only) or nobody."""
    s = store(core)
    if s.user_by_key(token) is None:
        return None
    return "admin" if s.admin_by_key(token) else "member"


def public_page(req: dict, core) -> tuple[int, str, str]:
    """"Forgot the password?" and the page of a mail link. No session: the answer never tells whether a
    name exists, a link is built only from KB_PUBLIC_URL, and the link itself is the proof on /reset."""
    esc, panel = _ui()
    s, cfg, base = store(core), mail_config(core), req["base"]
    back = f'<p><a href="{base}/admin/login">Back to the sign-in</a></p>'

    def box(title: str, inner: str) -> tuple[int, str, str]:
        return 200, f"KB · {title.lower()}", f'<main class="login">{panel(title, inner)}</main>'

    if req["path"] == "/forgot":
        if not (can_mail(cfg) and cfg.get("KB_PUBLIC_URL")):
            return box("Forgot the password", f"<p>This server sends no mail. Ask an admin for a new key.</p>{back}")
        if req["method"] == "POST":
            name = str(req["form"].get("username", "")).strip().lower()
            try:
                send_link(s, name, cfg["KB_PUBLIC_URL"], cfg)
                req["audit"]("password mail", name[:80])
            except UserError:
                pass  # no such user, a mail a moment ago, a mail failure: the answer is the same
            return box("Forgot the password", "<p>If this account exists, a mail with a link is on its way. "
                                              f"The link works once, for one hour.</p>{back}")
        return box("Forgot the password",
                   f'<form method="post" action="{base}/admin/forgot"><label for="u">Your user name (e-mail)</label>'
                   f'<input id="u" name="username" autocomplete="username" autofocus required>'
                   f'<p><button class="primary">Send me a link</button></p></form>{back}')

    value = str(req["form"].get("t") or req["query"].get("t") or "")
    name = s.reset_user(value)
    if name is None:
        return box("Choose a password", f"<p>This link does not work any more. Ask for a new mail.</p>"
                                        f'<p><a href="{base}/admin/forgot">Forgot the password?</a></p>')
    error = ""
    if req["method"] == "POST":
        first, second = str(req["form"].get("password", "")), str(req["form"].get("again", ""))
        try:
            if first != second:
                raise UserError("The two passwords are not the same.")
            s.finish_reset(value, first)
            req["audit"]("password set", name)
            # The sign-in tokens of the old password are gone: the cached verdicts and the open
            # admin sessions that stand on them go too.
            if callable(req.get("forget")):
                req["forget"]()
            if callable(req.get("drop_sessions")):
                req["drop_sessions"](name)
            return box("Choose a password", f'<p>The password is set. Sign in with <b>{esc(name)}</b>.</p>'
                                            f'<p><a href="{base}/admin/login">Sign in</a></p>')
        except UserError as e:
            error = f'<p class="bad">{esc(str(e))}</p>'
    return box("Choose a password",
               f'{error}<p>Account <b>{esc(name)}</b>. {PW_MIN} characters or more.</p>'
               f'<form method="post" action="{base}/admin/reset"><input type="hidden" name="t" value="{esc(value)}">'
               f'<label for="p">New password</label><input id="p" name="password" type="password" '
               f'autocomplete="new-password" minlength="{PW_MIN}" autofocus required>'
               f'<label for="a">The same again</label><input id="a" name="again" type="password" '
               f'autocomplete="new-password" minlength="{PW_MIN}" required>'
               f'<p><button class="primary">Set the password</button></p></form>')


def page(req: dict, core) -> tuple:
    """A page under /admin. The core has checked the session, the form's csrf value and who may open
    the tab. -> (status, title, body) or ("redirect", "login")."""
    s, path, form, me = store(core), req["path"], req["form"], req["user"]
    post = req["method"] == "POST"
    action = path.rsplit("/", 1)[1] if path.count("/") > 1 else ""
    if action and not post:
        return 404, "KB admin", "<main><section>Not found.</section></main>"

    if path == "/users" or path.startswith("/users/"):
        # The Users tab. An admin cannot take the own access away: that is how the last admin locks
        # everybody out.
        name, note, key = str(form.get("name", "")).strip(), "", None
        try:
            cfg = mail_config(core)
            if action == "add":
                invite = can_mail(cfg) and bool(EMAIL.match(name))
                made = s.add(name, projects_from_form(form, sorted(core.PROJECTS)), form.get("admin") == "on",
                             key=not invite)
                if invite:
                    try:
                        send_link(s, name, cfg.get("KB_PUBLIC_URL") or req["base"], cfg, invite=True)
                        note = f"Added. A mail with a link went to {name.lower()}: the person chooses a password there."
                    except UserError as e:
                        note = f"Added, but: {e} Use \"Reset password\" to send the mail again."
                else:
                    key, note = (name.lower(), made), "Added. This server sends no mail to that name: give the key."
            elif action == "reset":
                send_link(s, name, cfg.get("KB_PUBLIC_URL") or req["base"], cfg)
                note = f"A mail with a link went to {name.lower()}."
            elif action == "key":
                key = (name.lower(), s.issue(name, "from admin"))
                note = 'One more key. The other tokens of the user stay: "Revoke all" ends them.'
            elif action in ("revoke", "remove", "save"):
                is_admin, disabled = form.get("admin") == "on", form.get("disabled") == "on"
                if name.lower() == me and (action != "save" or disabled or not is_admin):
                    raise UserError("This is your own account: ask another admin to do that.")
                if action == "revoke":
                    s.revoke(name)
                    note = "Revoked."
                elif action == "remove":
                    s.remove(name)
                    note = "Removed."
                else:
                    projects = projects_from_form(form, sorted(core.PROJECTS))
                    if name.lower() == me and not projects:
                        # No project: the permission webhook refuses every token of this admin, and the
                        # admin can no longer sign in to undo it.
                        raise UserError("Keep at least one project on your own account: without one you "
                                        "could not sign in any more.")
                    s.update(name, projects, is_admin, disabled)
                    note = "Saved."
            elif action:
                return 404, "KB admin", "<main><section>Not found.</section></main>"
        except UserError as e:
            note, key = str(e), None
        else:
            if action:
                if action != "reset":
                    req["forget"]()
                    if name.lower() != me:
                        req["drop_sessions"](name)
                req["audit"](f"user {action}", name[:80])
        return 200, "KB admin · users", users_page(req["base"], req["csrf"], s.all(), sorted(core.PROJECTS), me, note, key,
                                                   can_mail(mail_config(core)),
                                                   online_panel(me, s.seen(), req["now"], ONLINE_SECONDS))

    if path == "/me" or path.startswith("/me/"):
        # "My tokens". The user is the one of the session, never a name from the form.
        rec = s.all().get(me)
        if rec is None or rec.get("disabled"):
            return "redirect", "login"
        note, key = "", ""
        try:
            if action == "add":
                key = s.issue(me, str(form.get("label", "")))
                note = "Made."
            elif action == "remove":
                s.remove_token(me, str(form.get("id", "")), keep_one=True)
                req["forget"]()
                note = "Removed. The token stopped."
            elif action == "reset":
                cfg = mail_config(core)
                send_link(s, me, cfg.get("KB_PUBLIC_URL") or req["base"], cfg)
                note = f"A mail with a link went to {me}."
            elif action:
                return 404, "KB admin", "<main><section>Not found.</section></main>"
        except UserError as e:
            note, key = str(e), ""
        else:
            if action:
                req["audit"](f"token {action}")
        mail = can_mail(mail_config(core)) and bool(EMAIL.match(me))
        return 200, "KB · my tokens", me_page(req["base"], req["csrf"], me, s.all()[me]["tokens"], note, key, mail)

    return 404, "KB admin", "<main><section>Not found.</section></main>"


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="The KB's own users (plugin `users`).")
    ap.add_argument("--file", default=None, help="the users file (default <data>/users.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add a user and print the key")
    a.add_argument("name")
    a.add_argument("--projects", default="*", help="`*` (default) or names split by commas")
    a.add_argument("--admin", action="store_true")
    for cmd, text in (("key", "issue one more key"), ("revoke", "end every key"), ("remove", "remove the user")):
        sub.add_parser(cmd, help=text).add_argument("name")
    sub.add_parser("list", help="the users, without keys")
    args = ap.parse_args(argv)
    store = Store(args.file or default_path())
    try:
        if args.cmd == "add":
            print(store.add(args.name, parse_projects(args.projects), args.admin))
        elif args.cmd == "key":
            print(store.issue(args.name))
        elif args.cmd == "revoke":
            store.revoke(args.name)
        elif args.cmd == "remove":
            store.remove(args.name)
        else:
            for name, rec in sorted(store.all().items()):
                projects = rec["projects"] if rec["projects"] == "*" else ",".join(rec["projects"])
                flags = " ".join(f for f, on in (("admin", rec.get("admin")), ("disabled", rec.get("disabled")),
                                                 ("no-key", not rec.get("tokens"))) if on)
                print(f"{name}\t{projects}\t{flags}")
    except UserError as e:
        print(f"users: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
