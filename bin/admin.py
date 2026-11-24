#!/usr/bin/env python3
"""The admin pages of the shared server (bin/kb-http): /admin.

Who: the users named in KB_ADMINS (comma-separated) or, without it, in `<data>/admins.txt` (one name
per line), matched against the `uname`, `username`, `name`, `id` or `uid` the permission webhook
returns. An access plugin with a `role()` function (contrib/users) names its own admins and members
instead. Nobody else sees anything, not even that the page exists (404). No admin names and no access
plugin with roles -> no admin pages.

Sign-in: the same login as the browser sign-in of MCP clients (user name, password, the authenticator
code, through the login webhook). The token is checked like any tool call; then a session cookie
(random, HttpOnly, Secure behind TLS, SameSite=Strict, 8 hours) stands for it. Every request checks
the token of the session again (cached KB_AUTH_CACHE_SECONDS): a removed token or a new password ends
the session, and a lost admin right ends the admin pages. Every form carries the session's own CSRF
token. Pages are plain HTML, no scripts. The password pages have an attempt limit (Throttle).

What it shows and does:
  * status: version, the graph (built when, nodes, edges), the last builds and failures, the warm
    helpers and their memory, cursors and sign-in clients in use, problems the build found;
  * usage: calls per tool, per day, per user, the slowest tools;
  * sign-in clients: the registered MCP clients; revoking one ends its refresh tokens;
  * rebuild: starts `bin/kb-refresh --force` (once at a time);
  * settings: the add-on switches (`<data>/addons.json`) and the settings of bin/settings.py.

The pages are part of the core, not an add-on: kb-http shows them when there are admins, kb-api on
loopback without a token.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.parse
import zipfile
from collections import OrderedDict, deque

import addons  # noqa: E402
import kb_core
import warm

SESSION_TTL = 8 * 3600
COOKIE = "kb_admin"
ID_FIELDS = ("uname", "username", "name", "id", "uid")   # to show and to find a user
# The fields that may make a user an admin (KB_ADMINS, admins.txt): stable, unique identifiers only. The
# display `name` is not one of them: a webhook that lets a person choose the own name ("roman") must not
# make that person an admin.
ADMIN_FIELDS = ("uname", "username", "id", "uid")

# The one script an admin page may run (the CSP allows it by its hash, kb-http): a "choose a password"
# link carries its one-time value in the fragment (#t=...), which no access log sees; this copies it into
# the form field `t` and takes it out of the address bar.
FRAGMENT_SCRIPT = ("<script>(function(){var m=/^#t=([A-Za-z0-9_-]+)$/.exec(location.hash);if(!m)return;"
                   "var f=document.querySelector('input[name=t]');if(f)f.value=m[1];"
                   "history.replaceState(null,'',location.pathname+location.search);})();</script>")


def script_hash(tag: str = FRAGMENT_SCRIPT) -> str:
    """The CSP source of a <script> element: 'sha256-<base64 of its body>'."""
    import base64
    import hashlib
    body = tag[len("<script>"):-len("</script>")]
    return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"


def admins(env: dict | None = None) -> set[str]:
    """KB_ADMINS (comma-separated), else `<data>/admins.txt` (one name per line, `#` comments): the
    file lets an operator name admins without editing the service unit."""
    env = os.environ if env is None else env
    names = [a for a in env.get("KB_ADMINS", "").split(",") if a.strip()]
    if not names:
        try:
            with open(os.path.join(kb_core.KB, "admins.txt"), encoding="utf-8") as f:
                names = [ln.split("#", 1)[0] for ln in f]
        except OSError:
            names = []
    return {a.strip().lower() for a in names if a.strip()}


def is_admin(user: dict, names: set[str]) -> bool:
    return any(str(user.get(k, "")).strip().lower() in names for k in ADMIN_FIELDS if user.get(k))


class Sessions:
    def __init__(self, ttl: float = SESSION_TTL):
        self.ttl = ttl
        self.items: dict[str, dict] = {}
        self.lock = threading.Lock()
        # (token) -> None, called when a session ends (sign-out, expiry, an admin change): kb-http ends the
        # sign-in token behind it in the access plugin. Called outside the lock; never raises.
        self.on_end = None

    def _ended(self, gone: list[dict]) -> None:
        for v in gone:
            if self.on_end is not None:
                try:
                    self.on_end(v["token"])
                except Exception:  # noqa: BLE001 — a sign-out never fails on the clean-up
                    pass

    def create(self, user: dict, token: str, admin: bool = True) -> str:
        """`admin` False: a person of the KB's own users who may open only the own tokens page."""
        sid = secrets.token_urlsafe(32)
        with self.lock:
            gone = self._expire()
            self.items[sid] = {"user": user, "token": token, "csrf": secrets.token_urlsafe(24), "admin": admin,
                               "exp": time.monotonic() + self.ttl}
        self._ended(gone)
        return sid

    def get(self, sid: str | None) -> dict | None:
        if not sid:
            return None
        with self.lock:
            gone = self._expire()
            found = self.items.get(sid)
        self._ended(gone)
        return found

    def drop(self, sid: str | None) -> None:
        with self.lock:
            v = self.items.pop(sid or "", None)
        self._ended([v] if v else [])

    def drop_user(self, name: str) -> None:
        """End every session of a user whose key or rights an admin has just changed."""
        name = name.strip().lower()
        with self.lock:
            gone = [self.items.pop(sid) for sid in [k for k, v in self.items.items()
                    if any(str(v["user"].get(f, "")).strip().lower() == name for f in ID_FIELDS)]]
        self._ended(gone)

    def _expire(self) -> list[dict]:
        """Drop the ended sessions; the caller holds the lock and calls _ended with the result after it."""
        now = time.monotonic()
        return [self.items.pop(k) for k in [k for k, v in self.items.items() if v["exp"] < now]]


class Logins:
    """Half-done sign-ins waiting for the authenticator code (5 minutes)."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.lock = threading.Lock()

    def put(self, challenge: str) -> str:
        lid = secrets.token_urlsafe(24)
        with self.lock:
            now = time.monotonic()
            for k in [k for k, v in self.items.items() if v["exp"] < now]:
                del self.items[k]
            self.items[lid] = {"challenge": challenge, "exp": now + 300}
        return lid

    def take(self, lid: str) -> str | None:
        with self.lock:
            v = self.items.pop(lid, None)
        return v["challenge"] if v and v["exp"] >= time.monotonic() else None


class Throttle:
    """A limit on attempts in a sliding window: per client address, and a tighter one while all addresses
    together pass `total`. The global cap never shuts the page for everybody: a flood from many addresses
    would then lock out every person. Over the cap, an address gets BUSY_PER_IP attempts in the window
    instead of `per_ip`: a person at a fresh address still signs in (password, then the code), a flooding
    address waits.

    Memory is bounded: at most MAX_IPS addresses, kept in the order of their last attempt. Expired ones
    leave from the front, a few per attempt; over MAX_IPS the address with the oldest attempt goes. An
    address keeps at most max(per_ip, BUSY_PER_IP) times, and the global list at most `total`."""

    BUSY_PER_IP = 5
    MAX_IPS = 10000

    def __init__(self, per_ip: int, total: int, window: float):
        self.per_ip, self.total, self.window = per_ip, total, window
        self.ips: "OrderedDict[str, list[float]]" = OrderedDict()
        self.all: deque[float] = deque()
        self.lock = threading.Lock()

    def hit(self, ip: str) -> int | None:
        """Count one attempt of `ip`. -> None when it may go on, else the seconds to wait."""
        if self.per_ip <= 0 and self.total <= 0:
            return None
        now = time.monotonic()
        cut = now - self.window
        with self.lock:
            while self.all and self.all[0] <= cut:
                self.all.popleft()
            while self.ips:
                first, times = next(iter(self.ips.items()))
                if times and times[-1] > cut:
                    break
                del self.ips[first]
            mine = [t for t in self.ips.get(ip, ()) if t > cut]
            if len(mine) >= self.per_ip > 0:
                return int(mine[0] - cut) + 1
            if len(self.all) >= self.total > 0 and len(mine) >= self.BUSY_PER_IP:
                return int(mine[0] - cut) + 1
            mine.append(now)
            del mine[:-max(self.per_ip, self.BUSY_PER_IP)]
            if self.total > 0:
                self.all.append(now)
                while len(self.all) > self.total:   # enough to know that the cap is reached
                    self.all.popleft()
            self.ips[ip] = mine
            self.ips.move_to_end(ip)
            while len(self.ips) > self.MAX_IPS:     # a flood of addresses: the least recent goes
                self.ips.popitem(last=False)
        return None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def login_throttle() -> Throttle:
    """KB_LOGIN_LIMIT attempts per client address (20) in KB_LOGIN_WINDOW seconds (300), at /admin/login,
    /authorize and /local/login. Over KB_LOGIN_LIMIT_ALL attempts of all addresses (2000) each address gets
    Throttle.BUSY_PER_IP. 0 = no limit."""
    return Throttle(_env_int("KB_LOGIN_LIMIT", 20), _env_int("KB_LOGIN_LIMIT_ALL", 2000),
                    _env_int("KB_LOGIN_WINDOW", 300))


def register_throttle() -> Throttle:
    """KB_OAUTH_REGISTER_LIMIT client registrations per address (20) per hour. Over
    KB_OAUTH_REGISTER_LIMIT_ALL of all addresses (2000) each address gets Throttle.BUSY_PER_IP. 0 = no limit."""
    return Throttle(_env_int("KB_OAUTH_REGISTER_LIMIT", 20), _env_int("KB_OAUTH_REGISTER_LIMIT_ALL", 2000), 3600)


_rebuild_lock = threading.Lock()
_rebuild_proc: subprocess.Popen | None = None


def building(lock_file: str | None = None) -> bool:
    """True while a build holds logs/update.lock. bin/kb-refresh keeps the file and takes a flock on it,
    so the file alone says nothing: a free flock means no build runs."""
    import fcntl
    lock_file = lock_file or os.path.join(kb_core.KB, "logs", "update.lock")
    try:
        fd = os.open(lock_file, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


REFRESH_BUSY = 75     # the exit code of `kb-refresh --force` when another build holds the lock
REBUILD_WAIT = 3.0    # seconds to wait for the started build to confirm that it took the lock


def _acked(ack: str, pid: int) -> bool:
    """True when kb-refresh wrote its own pid into the ack file: it holds the lock and builds."""
    try:
        with open(ack, encoding="utf-8") as f:
            return f.read().strip() == str(pid)
    except OSError:
        return False


def rebuild(script: str | None = None) -> str:
    """Start one forced rebuild in the background; refuse while one runs. The lock covers the check and
    the start, and the last started process counts as running until it ends: kb-refresh takes the flock
    a moment after it starts, and a second click in that moment must not start a second build.

    A build of cron can take the lock between the check and the start, so a held lock says nothing
    about this child. The child itself confirms: kb-refresh writes its pid into the file
    KB_REFRESH_ACK once it holds the lock and starts the build. The page waits up to REBUILD_WAIT
    seconds for that line, or for the child to exit (75: another build runs; another code: it stopped)."""
    global _rebuild_proc
    with _rebuild_lock:
        if building() or (_rebuild_proc is not None and _rebuild_proc.poll() is None):
            return "A build is already running."
        script = script or os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb-refresh")
        logs = os.path.join(kb_core.KB, "logs")
        fd, ack = tempfile.mkstemp(prefix=".rebuild-ack.", dir=logs if os.path.isdir(logs) else None)
        os.close(fd)
        try:
            proc = _rebuild_proc = subprocess.Popen(
                [script, "--force"], cwd=kb_core.KB, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env={**os.environ, "KB_REFRESH_ACK": ack})
            end = time.monotonic() + REBUILD_WAIT
            started = False
            while time.monotonic() < end and proc.poll() is None:
                if _acked(ack, proc.pid):
                    started = True
                    break
                time.sleep(0.05)
            started = started or _acked(ack, proc.pid)
        finally:
            try:
                os.unlink(ack)
            except OSError:
                pass
    code = proc.poll()
    if started:
        return "Rebuild started. It takes a few minutes; the status page shows it in the build log."
    if code == REFRESH_BUSY:
        return "A build is already running."
    if code is not None:
        return f"The rebuild stopped at once (exit code {code}). The status page shows the build log."
    return (f"The rebuild did not confirm its start within {REBUILD_WAIT:g} s. "
            "The status page shows whether it runs.")


# ---------------------------------------------------------------------------------- facts
def _tail(path: str, n: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


def _rss_mb(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


_graph_counts: dict = {}


def graph_facts() -> dict:
    g = kb_core.GRAPH
    try:
        st = os.stat(g)
    except OSError:
        return {"built": "not built"}
    key = (st.st_mtime, st.st_size)
    if _graph_counts.get("key") != key:
        try:
            with open(g, encoding="utf-8") as f:
                data = json.load(f)
            repos: dict[str, int] = {}
            for n in data.get("nodes", []):
                repos[n.get("repo") or "?"] = repos.get(n.get("repo") or "?", 0) + 1
            _graph_counts.update(key=key, nodes=len(data.get("nodes", [])),
                                 edges=len(data.get("links") or data.get("edges") or []), repos=repos)
        except (OSError, ValueError):
            _graph_counts.update(key=key, nodes="?", edges="?", repos={})
    return {"built": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "size_mb": round(st.st_size / 1e6, 1), "nodes": _graph_counts["nodes"],
            "edges": _graph_counts["edges"], "repos": _graph_counts["repos"]}


def status(oauth_server) -> dict:
    logs = os.path.join(kb_core.KB, "logs")
    builds = _tail(os.path.join(logs, "refresh.log"), 8)
    helpers = []
    for name, w in (("graph worker", warm._graph), ("qmd", warm._qmd)):
        proc = getattr(w, "proc", None) if w else None
        alive = proc is not None and proc.poll() is None
        helpers.append((name, "running" if alive else "not started", _rss_mb(proc.pid) if alive else None))
    return {
        "version": kb_core.VERSION, "contract": kb_core.API_CONTRACT, "graph": graph_facts(),
        "builds": builds, "failed": [b for b in builds if "exit=0" not in b],
        "building": building(os.path.join(logs, "update.lock")),
        "helpers": helpers, "server_mb": _rss_mb(os.getpid()),
        "cursors": len(kb_core.CURSORS.items),
        "clients": len(oauth_server.clients) if oauth_server else 0,
        "problems": _problems_line(),
    }


def _problems_line() -> str:
    try:
        return kb_core.call_tool("problems", {}).split("\n", 1)[0]
    except Exception:  # noqa: BLE001 - the page must render without the index
        return "(problems index not built)"


# ---------------------------------------------------------------------------------- pages
STYLE = """:root{--page:#f4f4f2;--panel:#fcfcfb;--panel2:#f0efec;--line:#dddcd6;--ink:#0b0b0b;--ink2:#52514e;
--ink3:#7a7975;--accent:#2a78d6;--accent-soft:#2a78d61f;--track:#e8e7e3;--crit:#d03b3b;--good:#0ca30c;--top:#ffffff;
--btn:#1c1917;--btn-ink:#fff}
@media (prefers-color-scheme: dark){:root{--page:#111110;--panel:#1a1a19;--panel2:#202020;--line:#30302e;--ink:#ecebe6;
--ink2:#b4b3ad;--ink3:#8a8984;--accent:#3987e5;--accent-soft:#3987e526;--track:#2c2c2a;--crit:#e05656;--good:#0ca30c;
--top:#161615;--btn:#ecebe6;--btn-ink:#111110}}
*{box-sizing:border-box}body{font:13px/1.45 Inter,system-ui,sans-serif;margin:0;background:var(--page);color:var(--ink);
font-variant-numeric:tabular-nums}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{background:var(--top);border-bottom:1px solid var(--line);height:52px;padding:0 24px;display:flex;align-items:center;gap:28px}
header .brand{font-weight:700;font-size:16px;color:var(--ink)}header nav{display:flex;gap:22px;height:100%}
header nav a{color:var(--ink2);display:flex;align-items:center;border-bottom:2px solid transparent;padding-top:2px}
header nav a.on{color:var(--ink);font-weight:600;border-bottom-color:var(--accent)}
.wait{display:inline-block;min-width:18px;padding:0 5px;margin-left:4px;border-radius:9px;background:#f2c200;color:#1c1917;
font-size:11px;font-weight:600;line-height:18px;text-align:center}header nav a:hover{text-decoration:none;color:var(--ink)}
header .right{margin-left:auto;display:flex;gap:8px;align-items:center}
main{padding:20px 24px 32px;max-width:1440px;margin:0 auto}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:3px;margin-bottom:16px;min-width:0}
.panel>h2{font-size:14px;font-weight:600;margin:0;padding:11px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px}
.panel>h2 small{margin-left:auto;font-weight:400;color:var(--ink3);font-size:12px}
.panel .body{padding:12px 16px}
.grid{display:grid;gap:16px}.g2{grid-template-columns:repeat(2,minmax(0,1fr))}.g3{grid-template-columns:repeat(3,minmax(0,1fr))}
@media (max-width:1100px){.g3{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (max-width:760px){.g2,.g3{grid-template-columns:1fr}}
.grid>.panel{margin-bottom:0}.stack{display:flex;flex-direction:column;gap:16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:3px;padding:10px 12px 8px}
.tile .k{font-size:12px;color:var(--ink2)}.tile .v{font-size:24px;font-weight:600;margin:2px 0 4px;letter-spacing:-.01em}
.tile .s{font-size:11px;color:var(--ink3)}
table{border-collapse:collapse;width:100%}th{font-weight:500;color:var(--ink2);font-size:12px;text-align:left;
padding:7px 10px;border-bottom:1px solid var(--line);background:var(--panel2);white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:middle}tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right;white-space:nowrap}tr.total td{font-weight:600}
.ratio{height:3px;background:var(--track);border-radius:2px;margin-top:4px}.ratio i{display:block;height:3px;background:var(--accent);border-radius:2px}
.ratio.bad i{background:var(--crit)}
.bad{color:var(--crit)}.ok{color:var(--good)}.muted{color:var(--ink3)}code{font-size:12px}
pre{background:var(--panel2);border:1px solid var(--line);border-radius:3px;padding:10px 12px;margin:8px 0;overflow-x:auto;font-size:12px}.steps{margin:8px 0;padding-left:20px}.steps li{margin:4px 0}
button,.btn{font:inherit;padding:6px 12px;border:1px solid var(--line);border-radius:3px;background:var(--panel);color:var(--ink);cursor:pointer}
button.primary{background:var(--btn);color:var(--btn-ink);border-color:var(--btn)}
.note{background:var(--accent-soft);border-left:3px solid var(--accent);padding:8px 12px;margin-bottom:16px}
.layout{display:grid;grid-template-columns:224px minmax(0,1fr);gap:16px}@media (max-width:900px){.layout{grid-template-columns:1fr}}
.filters h3{font-size:14px;margin:14px 0 6px;display:flex}.filters h3 span{margin-left:auto;color:var(--accent);font-weight:500}
.filters a{display:flex;gap:8px;align-items:center;color:var(--ink);padding:3px 0}.filters a:hover{text-decoration:none;color:var(--accent)}
.filters a .box{width:13px;height:13px;border:1px solid var(--ink3);border-radius:2px;flex:none}
.filters a.on .box{background:var(--accent);border-color:var(--accent)}.filters a .pct{margin-left:auto;color:var(--ink2)}
.filters hr{border:0;border-top:1px solid var(--line);margin:10px 0}
svg text{fill:var(--ink2);font:11px Inter,system-ui,sans-serif}svg .v{fill:var(--ink);font-size:12px}
svg .grid{stroke:var(--line)}svg .bar{fill:var(--accent)}svg .badbar{fill:none;stroke:var(--crit);stroke-width:1.5}
svg .badtxt{fill:var(--crit);font-weight:600}
main.login{max-width:360px;margin:12vh auto;padding:0}label{display:block;font-size:12px;color:var(--ink2);margin:12px 0 4px}
input{width:100%;padding:8px;border:1px solid var(--line);border-radius:3px;font:inherit;background:var(--page);color:var(--ink)}
footer.src{margin:24px 16px;font-size:12px;color:var(--ink2)}footer.src a{color:inherit}
form.inline{display:flex;gap:8px;align-items:center;margin:0}
select{padding:6px 8px;border:1px solid var(--line);border-radius:3px;font:inherit;background:var(--page);color:var(--ink)}"""


def esc(x) -> str:
    return html.escape(str(x))


# The access plugin the server started with (kb-http sets it once in make_server). The pages show its tabs
# and links, not the plugin that addons.json names now: a switch takes effect after a restart, and the
# pages must not change before the server does. Unset (kb-api, a test without a server): read addons.json.
UNSET = object()
_access = UNSET


def set_access(mod) -> None:
    global _access
    _access = mod


def access():
    if _access is not UNSET:
        return _access
    import plugins
    return plugins.access(addons.enabled())


# How many asked questions wait for a label: the yellow mark beside the Questions tab on every page.
# The server sets the source once (`set_badge_source`); without one there is no mark.
_badge_source = None


def set_badge_source(fn) -> None:
    global _badge_source
    _badge_source = fn


def _waiting() -> int:
    try:
        return int(_badge_source()) if _badge_source else 0
    except Exception:  # noqa: BLE001 - a broken counter must not break every admin page
        return 0


def page(title: str, body: str, base: str = "", tab: str = "", csrf: str = "", right: str = "",
         member: bool = False) -> bytes:
    """`member`: the signed-in person is no admin and sees only the own tokens and the how-to."""
    nav = ""
    if csrf:
        tabs = [("Status", ""), ("Index", "/index"), ("Usage", "/usage"), ("Questions", "/questions"),
                ("Audit", "/audit"), ("Clients", "/clients"), ("Settings", "/settings"), ("How to", "/howto")]
        if not addons.on("audit"):
            tabs.remove(("Audit", "/audit"))
        if not addons.on("questions"):
            tabs.remove(("Questions", "/questions"))
        extra = getattr(access(), "TABS", None) or []  # the access plugin's own pages
        for label, path, _who in extra:
            tabs.insert(-1, (label, path))
        if member:
            tabs = [(label, path) for label, path, who in extra if who == "member"] + [("How to", "/howto")]
        waiting = _waiting() if addons.on("questions") else 0
        mark = (f' <span class="wait" title="{waiting} question{"s" if waiting != 1 else ""} without a label">'
                f'{waiting}</span>') if waiting else ""
        links = "".join(f'<a href="{base}/admin{p}" class="{"on" if tab == t else ""}">{t}'
                        f'{mark if t == "Questions" else ""}</a>' for t, p in tabs)
        nav = (f'<header><span class="brand">KB admin</span><nav>{links}</nav><div class="right">{right}'
               f'<form method="post" action="{base}/admin/logout"><input type="hidden" name="csrf" '
               f'value="{esc(csrf)}"><button>Sign out</button></form></div></header>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title>'
            f'<style>{STYLE}</style></head><body>{nav}{body}<footer class="src"><a href="{esc(kb_core.SOURCE_URL)}">'
            f'Source</a> · AGPL-3.0-or-later</footer></body></html>').encode()


def panel(title: str, inner: str, note: str = "") -> str:
    small = f"<small>{esc(note)}</small>" if note else ""
    return f'<section class="panel"><h2>{esc(title)}{small}</h2><div class="body">{inner}</div></section>'


def login_page(base: str, step: str = "password", lid: str = "", error: str = "") -> bytes:
    err = f'<p class="bad">{esc(error)}</p>' if error else ""
    if step == "code":
        fields = (f'<input type="hidden" name="lid" value="{esc(lid)}"><label for="c">Authenticator code</label>'
                  '<input id="c" name="code" autocomplete="one-time-code" inputmode="numeric" autofocus required>')
    else:
        fields = ('<label for="u">User name</label><input id="u" name="username" autocomplete="username" autofocus required>'
                  '<label for="p">Password</label><input id="p" name="password" type="password" '
                  'autocomplete="current-password" required>')
    forgot = (f'<p><a href="{base}/admin/forgot">Forgot the password?</a></p>'
              if "/forgot" in (getattr(access(), "PUBLIC", None) or []) else "")
    body = (f'<main class="login">' + panel("KB admin · sign in", f'{err}<form method="post" action="{base}/admin/login">'
            f'{fields}<p><button class="primary">Sign in</button></p></form>{forgot}') + '</main>')
    return page("KB admin · sign in", body)


def status_page(base: str, csrf: str, s: dict, user: dict, note: str = "") -> bytes:
    g = s["graph"]
    repos = ", ".join(f"{k} {v}" for k, v in sorted(g.get("repos", {}).items(), key=lambda kv: -kv[1]))
    builds = "".join(f'<tr><td class="{"ok" if "exit=0" in b else "bad"}">{esc(b)}</td></tr>'
                     for b in reversed(s["builds"])) or "<tr><td>no builds logged</td></tr>"
    helpers = "".join(f"<tr><td>{esc(n)}</td><td>{esc(st)}</td><td>{esc(m) + ' MB' if m else ''}</td></tr>"
                      for n, st, m in s["helpers"])
    rebuild_btn = ("<p>A build is running.</p>" if s["building"] else
                   f'<form method="post" action="{base}/admin/rebuild"><input type="hidden" name="csrf" '
                   f'value="{esc(csrf)}"><button class="primary">Rebuild now</button></form>')
    body = (f'<main>{f"<div class=note>{esc(note)}</div>" if note else ""}'
            f'<section class=panel><h2>Knowledge base</h2><div class=body><table>'
            f'<tr><th>Version</th><td>{esc(s["version"])} (tool contract {esc(s["contract"])})</td></tr>'
            f'<tr><th>Graph</th><td>built {esc(g.get("built"))}, {esc(g.get("nodes"))} nodes, '
            f'{esc(g.get("edges"))} edges, {esc(g.get("size_mb"))} MB</td></tr>'
            f'<tr><th>Projects</th><td>{esc(repos)}</td></tr>'
            f'<tr><th>Problems</th><td>{esc(s["problems"])}</td></tr>'
            f'<tr><th>In use</th><td>{s["cursors"]} cursors, {s["clients"]} sign-in clients, server '
            f'{esc(s["server_mb"])} MB</td></tr>'
            f'<tr><th>Signed in as</th><td>{esc(next((user.get(k) for k in ID_FIELDS if user.get(k)), "?"))}</td></tr>'
            f'</table></div></section>'
            f'<section class=panel><h2>Builds{" — <span class=bad>last failed</span>" if s["failed"] and s["builds"] and "exit=0" not in s["builds"][-1] else ""}</h2>'
            f'<div class=body><table>{builds}</table><p></p>{rebuild_btn}</div></section>'
            f'<section class=panel><h2>Warm helpers</h2><div class=body><table>{helpers}</table></div></section></main>')
    return page("KB admin", body, base, "Status", csrf)


def audit_page(base: str, csrf: str, usage, query: dict | None = None) -> bytes:
    """Who did what: sign-ins and admin actions, then every tool call with its user, projects, argument,
    time and outcome. Filters by user, tool and errors are links (no scripts)."""
    query = query or {}
    if usage is None or not hasattr(usage, "calls"):
        return page("KB admin · audit", '<main>' + panel("Audit", '<p class="muted">usage counting is off</p>')
                    + '</main>', base, "Audit", csrf)
    user, tool = str(query.get("user", "")), str(query.get("tool", ""))
    errors = query.get("errors") in ("1", "true", "on")

    def link(**kw) -> str:
        q = {"user": user, "tool": tool, "errors": "1" if errors else "", **kw}
        q = {k: v for k, v in q.items() if v}
        return f"{base}/admin/audit" + ("?" + urllib.parse.urlencode(q) if q else "")

    ev = usage.events(100, user)
    ev_rows = "".join(f'<tr><td>{esc(w)}</td><td><a href="{esc(link(user=u))}">{esc(u)}</a></td>'
                      f'<td>{esc(a)}</td><td class="mono">{esc(d)}</td></tr>' for w, u, a, d in ev)
    calls = usage.calls(200, user, tool, errors)
    call_rows = "".join(
        f'<tr><td>{esc(w)}</td><td><a href="{esc(link(user=u))}">{esc(u)}</a></td>'
        f'<td><a href="{esc(link(tool=t))}">{esc(t)}</a></td><td>{esc(p)}</td>'
        f'<td class="mono">{esc(a[:80])}</td><td class="n">{s:.2f} s</td>'
        f'<td>{"ok" if ok else "<b>error</b>"}</td></tr>' for w, u, t, p, ok, s, a in calls)
    shown = " · ".join(x for x in (f"user {user}" if user else "", f"tool {tool}" if tool else "",
                                    "errors only" if errors else "") if x) or "everything"
    filters = (f'<p class="muted">Showing {esc(shown)}. <a href="{esc(link(errors="" if errors else "1"))}">'
               f'{"all calls" if errors else "errors only"}</a> · <a href="{base}/admin/audit">reset</a></p>')
    body = ('<main><div class="stack">' + filters
            + panel("Sign-ins and admin actions",
                    '<table><tr><th>When</th><th>User</th><th>Action</th><th>Detail</th></tr>'
                    + (ev_rows or '<tr><td colspan="4" class="muted">none yet</td></tr>') + '</table>',
                    "the last 100")
            + panel("Tool calls",
                    '<table><tr><th>When</th><th>User</th><th>Tool</th><th>Projects</th><th>Argument</th>'
                    '<th class="n">Time</th><th>Result</th></tr>'
                    + (call_rows or '<tr><td colspan="7" class="muted">none</td></tr>') + '</table>',
                    "the last 200, kept 90 days")
            + '</div></main>')
    return page("KB admin · audit", body, base, "Audit", csrf)


def client_users(c: dict) -> str:
    """Who signed in through a client, newest first, with the time of the last sign-in or refresh."""
    users = c.get("users") or {}
    if not users:
        return "—"
    return "<br>".join(f"{esc(u)} <small>({esc(time.strftime('%Y-%m-%d %H:%M', time.localtime(t)))})</small>"
                       for u, t in sorted(users.items(), key=lambda kv: -kv[1]))


# The files that the how-to page offers for download: the published name -> the source path under the
# root of the KB and the media type. A fixed list, so a name in the URL never reaches the file system.
HOWTO_FILES = {
    "kb.md": ("skills/kb/SKILL.md", "text/markdown; charset=utf-8"),
    "doc-organization.md": ("skills/doc-organization/SKILL.md", "text/markdown; charset=utf-8"),
    "kb-reminder.py": ("skills/kb/install/kb-reminder.py", "text/x-python; charset=utf-8"),
}


# One download with everything of "Set up Claude Code": the folders as ~/.claude (or .claude/ of a repository)
# holds them, and install.py that copies them and merges the hook and the CLAUDE.md rule.
HOWTO_BUNDLE = "kb-claude-setup.zip"
HOWTO_BUNDLE_FILES = {
    "skills/kb/SKILL.md": "kb.md",
    "skills/doc-organization/SKILL.md": "doc-organization.md",
    "hooks/kb-reminder.py": "kb-reminder.py",
}

CLAUDE_MD_RULE = ("## Code questions\n"
                  "For \"how does X work\", \"what connects to Y\" and \"what breaks if I change Z\" questions, "
                  "use the `kb` skill and its MCP tools before grep.\n"
                  "When you delegate such a question to a sub-agent, tell it to use the `kb` tools.\n"
                  "The index is as old as its last build: check a claim against the file before you rely on it.")

INSTALL_PY = r'''#!/usr/bin/env python3
"""Install the kb skills, the reminder hook and the CLAUDE.md rule for Claude Code.

  python3 install.py                      for yourself: ~/.claude, ~/.claude/CLAUDE.md
  python3 install.py --project <repo>     for a team: <repo>/.claude, <repo>/CLAUDE.md (commit them)

A repeat changes nothing new. Hooks and text that are already there stay.
"""
import argparse
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ["skills/kb/SKILL.md", "skills/doc-organization/SKILL.md", "hooks/kb-reminder.py"]
RULE = __RULE__
MARK = "kb-reminder.py"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", metavar="REPO", help="install into this repository instead of ~/.claude")
    args = ap.parse_args()
    if args.project:
        root = os.path.abspath(args.project)
        target, claude_md = os.path.join(root, ".claude"), os.path.join(root, "CLAUDE.md")
        hook_path = '"$CLAUDE_PROJECT_DIR/.claude/hooks/kb-reminder.py"'
    else:
        target = os.path.join(os.path.expanduser("~"), ".claude")
        claude_md = os.path.join(target, "CLAUDE.md")
        hook_path = '"$HOME/.claude/hooks/kb-reminder.py"'

    for rel in FILES:
        dest = os.path.join(target, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(HERE, rel), dest)
        print("copied  ", dest)

    settings_path = os.path.join(target, "settings.json")
    settings = {}
    if os.path.exists(settings_path):
        with open(settings_path, encoding="utf-8") as f:
            settings = json.load(f)
    groups = settings.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    if any(MARK in h.get("command", "") for g in groups for h in g.get("hooks", [])):
        print("kept    ", settings_path, "(the hook is there)")
    else:
        groups.append({"hooks": [{"type": "command", "command": f"python3 {hook_path} 2>/dev/null || true"}]})
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        print("updated ", settings_path)

    text = ""
    if os.path.exists(claude_md):
        with open(claude_md, encoding="utf-8") as f:
            text = f.read()
    if RULE.splitlines()[0] in text:
        print("kept    ", claude_md, "(the rule is there)")
    else:
        with open(claude_md, "a", encoding="utf-8") as f:
            f.write(("\n" if text and not text.endswith("\n") else "") + ("\n" if text else "") + RULE + "\n")
        print("updated ", claude_md)
    print("Done. Restart Claude Code, then check that /mcp shows kb as connected.")


if __name__ == "__main__":
    main()
'''


def howto_bundle() -> bytes | None:
    """The zip of HOWTO_BUNDLE, built from the files of this server; None when a file is missing."""
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, name in HOWTO_BUNDLE_FILES.items():
            found = howto_file(name)
            if found is None:
                return None
            z.writestr(arc, found[0])
        z.writestr("install.py", INSTALL_PY.replace("__RULE__", json.dumps(CLAUDE_MD_RULE)))
    return buf.getvalue()


def howto_file(name: str) -> tuple[bytes, str] | None:
    """One file of HOWTO_FILES as (content, media type), or None for an unknown name or a missing file."""
    if name == HOWTO_BUNDLE:
        bundle = howto_bundle()
        return None if bundle is None else (bundle, "application/zip")
    entry = HOWTO_FILES.get(name)
    if entry is None:
        return None
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, entry[0]), "rb") as f:
            return f.read(), entry[1]
    except OSError:
        return None


def _claude_setup(base: str) -> str:
    """The panel "Set up Claude Code": the skills, the reminder hook and the lines for CLAUDE.md."""
    files = f"{base}/admin/howto"

    def link(name: str) -> str:
        if howto_file(name) is None:
            return f"<code>{esc(name)}</code> <span class=muted>(not on this server)</span>"
        return f'<a href="{esc(files)}/{esc(name)}" download>{esc(name)}</a>'

    hook = json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{
        "type": "command",
        "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/kb-reminder.py" 2>/dev/null || true'}]}]}},
        indent=2)
    guideline = CLAUDE_MD_RULE
    return panel(
        "Set up Claude Code: skills, hook, CLAUDE.md",
        "<p>The server alone is not enough. Without these steps the agent often greps and never asks the "
        "knowledge base.</p>"
        f"<p><b>The short way.</b> Download {link(HOWTO_BUNDLE)} (the skills, the hook and an installer), then:"
        "<pre>cd ~/Downloads &amp;&amp; unzip -o kb-claude-setup.zip -d kb-claude-setup\n"
        "python3 kb-claude-setup/install.py                   # for yourself, every project\n"
        "python3 kb-claude-setup/install.py --project ~/my-repo   # or for a team: commit .claude/ and CLAUDE.md</pre>"
        "The installer copies the files, adds the hook to <code>settings.json</code> and the rule to "
        "<code>CLAUDE.md</code>, and keeps what is there. A repeat changes nothing. Then restart Claude Code "
        "(step 4).</p>"
        "<p><b>By hand,</b> the same in steps (once per person or once per repository):</p>"
        "<ol class=steps>"
        f"<li><b>Install the skills.</b> Download {link('kb.md')} and {link('doc-organization.md')}. "
        "Each file is a <code>SKILL.md</code>: put it in a folder with the name of the skill. For yourself, "
        "in every project:"
        "<pre>mkdir -p ~/.claude/skills/kb ~/.claude/skills/doc-organization\n"
        "mv ~/Downloads/kb.md ~/.claude/skills/kb/SKILL.md\n"
        "mv ~/Downloads/doc-organization.md ~/.claude/skills/doc-organization/SKILL.md</pre>"
        "For a whole team, put the same folders in <code>.claude/skills/</code> of the repository and commit "
        "them. <code>kb</code> tells the agent which tool answers which question. "
        "<code>doc-organization</code> keeps the docs in the shape that the index reads well.</li>"
        f"<li><b>Add the reminder hook.</b> Download {link('kb-reminder.py')} to "
        "<code>.claude/hooks/</code> of the repository (or <code>~/.claude/hooks/</code> and change the path). "
        "Merge this into <code>.claude/settings.json</code>; keep the hooks that are there:"
        f"<pre>{esc(hook)}</pre>"
        "The skill fires only when the agent decides so. With many MCP servers Claude Code also hides the "
        "<code>kb</code> tools behind its tool search. The hook adds a one-line reminder to every prompt "
        "that asks how code connects, so the agent does not fall back to grep.</li>"
        "<li><b>Add the rule to CLAUDE.md</b> of the repository (or to <code>~/.claude/CLAUDE.md</code>):"
        f"<pre>{esc(guideline)}</pre>"
        "Sub-agents do not see the skill choice of the main session: the rule tells the main session to pass "
        "it on.</li>"
        "<li><b>Restart Claude Code</b>, then check: <code>/mcp</code> shows <code>kb</code> as connected, "
        "and the question \"how does the sign-in work?\" starts with a <code>kb</code> tool, not with grep."
        "</li></ol>"
        "<p class=muted>A downloaded file is as new as this server. Download again after an update of the "
        "server.</p>")


def howto_page(base: str, csrf: str, login_url: str = "", member: bool = False) -> bytes:
    """How a person connects an agent to this server. The addresses come from the request, so the
    commands on the page work when pasted. `login_url` is the public address of the login webhook
    (KB_PUBLIC_LOGIN_URL): the server itself often reaches the webhook on a loopback address."""
    mcp = f"{base}/mcp"
    plugin = access()  # an access plugin answers the login at this server itself
    own = bool(getattr(plugin, "TOKENS", False))  # ... and people hold tokens that they make on its pages
    login_url = login_url or (f"{base}/local/login" if plugin is not None else "")
    secret = getattr(plugin, "SIGN_IN_HINT", "") or "the password"
    header = panel("Claude Code: a token in a header",
                   "<p>The shortest way on this server. Sign in to these pages, open \"My tokens\" and make a token "
                   "for this computer. A token lives until you remove it, so there is no sign-in to repeat. Keep "
                   "it out of a file that is in git.</p>"
                   f'<pre>claude mcp add --transport http kb {esc(mcp)} --scope user --header "Authorization: Bearer &lt;token&gt;"</pre>'
                   ) if own else ""
    entry = json.dumps({"mcpServers": {"kb": {"type": "http", "url": mcp}}}, indent=2)
    login = login_url or "<login address>"
    ask = "" if login_url else ("<p class=muted>Ask the administrator of this server for the login address: "
                                "KB_PUBLIC_LOGIN_URL is not set.</p>")
    body = (
        "<main>"
        + panel("Connect an agent", f"<p>The address of this knowledge base for an MCP client:</p><pre>{esc(mcp)}</pre>"
                "<p>Every person signs in with the own account. The server keeps no passwords: your system "
                "checks the account and says which projects the person may search.</p>")
        + panel("Claude Code: sign-in through the browser",
                "<ol class=steps><li>Register the server once. For yourself, in every project:"
                f"<pre>claude mcp add --transport http kb {esc(mcp)} --scope user</pre>"
                "Or for a whole team, in the <code>.mcp.json</code> of the repository:"
                f"<pre>{esc(entry)}</pre></li>"
                "<li>Start Claude Code, type <code>/mcp</code>, choose <code>kb</code>, then <b>Authenticate</b>. "
                "The browser opens the sign-in page of this server.</li>"
                f"<li>Enter the user name and {secret}, then the code when asked. The tools of the server "
                "appear in the session.</li></ol>"
                "<p>Start the sign-in from <code>/mcp</code>, not from a link in the chat. Claude Code waits for the "
                "answer on a local port for a short time only. If the browser ends on "
                "<code>http://localhost:…/callback</code> with \"This site can\u2019t be reached\", the wait is over: "
                "start again from <code>/mcp</code>.</p>")
        + header
        + panel("Claude Code: sign-in in the terminal, no browser",
                "<p><code>kb-login</code> from the source of this server asks for the user name, the password and the "
                "code, keeps the token in <code>~/.claude/kb/token</code> (owner only) and registers the server so "
                "that Claude Code sends the token by itself.</p>"
                f"<pre>bin/kb-login --login-url {esc(login)} --kb-url {esc(mcp)} --add-to-claude</pre>{ask}"
                "<p>When the token ends, run the same command without <code>--add-to-claude</code>.</p>")
        + _claude_setup(base)
        + panel("Other MCP clients",
                "<p>Any client that speaks MCP over HTTP works. A client with browser sign-in (OAuth) needs only "
                "the address above. A client without it sends the token of <code>kb-login</code> in the header "
                "<code>Authorization: Bearer &lt;token&gt;</code>.</p>")
        + panel("When it does not work",
                "<table><tr><th>What you see</th><th>Cause and fix</th></tr>"
                "<tr><td>401, or the client asks to sign in again</td><td>No token, or the token ended. Your system "
                "decides how long a token lives. Sign in again.</td></tr>"
                "<tr><td>403</td><td>The account may search no project of this server. Ask for access in your system.</td></tr>"
                "<tr><td>503</td><td>The server cannot reach your system to check the token. Try again later.</td></tr>"
                "<tr><td>Slow answers, a slow Status page</td><td>A build is running: see Status, \"Builds\". "
                "Wait until it ends.</td></tr>"
                "<tr><td>An answer that does not match the code</td><td>The index is as old as the last build on the "
                "Status page. Check the claim against the file.</td></tr></table>")
        + "</main>")
    return page("KB admin · how to", body, base, "How to", csrf, member=member)


def clients_page(base: str, csrf: str, clients: dict, note: str = "") -> bytes:
    rows = "".join(
        f"<tr><td>{esc(c.get('client_name') or '—')}</td><td>{client_users(c)}</td><td><code>{esc(cid)}</code></td>"
        f"<td>{esc(time.strftime('%Y-%m-%d %H:%M', time.localtime(c.get('client_id_issued_at', 0))))}</td>"
        f"<td>{esc(', '.join(c.get('redirect_uris', [])))}</td>"
        f'<td><form method="post" action="{base}/admin/clients/revoke"><input type="hidden" name="csrf" '
        f'value="{esc(csrf)}"><input type="hidden" name="client_id" value="{esc(cid)}">'
        f'<button>Revoke</button></form></td></tr>'
        for cid, c in sorted(clients.items(), key=lambda kv: -kv[1].get("client_id_issued_at", 0)))
    body = (f'<main>{f"<div class=note>{esc(note)}</div>" if note else ""}<section class=panel><h2>Sign-in clients</h2><div class=body>'
            f'<p>MCP clients that registered for the browser sign-in, with who signed in through each and when. Revoking one ends its refresh tokens: '
            f'it has to sign in again, and its access tokens stop at once.</p>'
            f'<table><tr><th>Name</th><th>Users</th><th>Client id</th><th>Registered</th><th>Redirect</th><th></th></tr>'
            f'{rows or "<tr><td colspan=6>none</td></tr>"}</table></div></section></main>')
    return page("KB admin · clients", body, base, "Clients", csrf)


def settings_page(base: str, csrf: str, note: str = "", bad: bool = False) -> bytes:
    """The add-on switches and the settings of bin/settings.py. A value that an env variable sets is
    shown locked: the page cannot change it."""
    import plugins
    import settings
    on = addons.enabled()
    env_addons = os.environ.get("KB_ADDONS")
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
    rows = []
    for name, what in addons.known().items():
        state = name in on
        notes = []
        if name in plugins.installed() and _is_access(name):
            notes.append("an access plugin: switched only on the server (kb addon on|off), then a restart")
        elif addons.needs_restart(name):
            notes.append("takes effect after a restart")
        lack = addons.missing(name) if state else None
        if lack:
            notes.append(f'<span class="bad">missing: {esc(lack)}</span>')
        if env_addons is not None:
            control = '<span class="muted">set by KB_ADDONS</span>'
        else:
            control = (f'<form method="post" action="{base}/admin/settings/addon" class="inline">{hidden}'
                       f'<input type="hidden" name="name" value="{esc(name)}">'
                       f'<input type="hidden" name="on" value="{"0" if state else "1"}">'
                       f'<button{"" if state else " class=primary"}>{"Switch off" if state else "Switch on"}</button></form>')
        rows.append(f'<tr><td><code>{esc(name)}</code></td><td>{esc(what)}</td>'
                    f'<td class="{"ok" if state else "muted"}">{"on" if state else "off"}</td><td>{control}</td>'
                    f'<td>{"<br>".join(n if n.startswith("<span") else esc(n) for n in notes)}</td></tr>')
    lock_note = ('<p class="muted">KB_ADDONS is set: it decides which add-ons are on, and these switches are '
                 'read-only. Change the variable and restart the server.</p>' if env_addons is not None else
                 '<p class="muted">A switch writes <code>addons.json</code> in the data folder. Most add-ons '
                 'follow at once; the ones marked below follow after a restart.</p>')
    srows = []
    for r in settings.rows():
        if r["locked"]:
            control = f'<span class="muted">set by {esc(r["env"])}</span>'
        else:
            opts = "".join(f'<option value="{esc(c)}"{" selected" if c == r["value"] else ""}>{esc(c)}</option>'
                           for c in r["choices"])
            control = (f'<form method="post" action="{base}/admin/settings/set" class="inline">{hidden}'
                       f'<input type="hidden" name="name" value="{esc(r["key"])}">'
                       f'<select name="value" aria-label="{esc(r["key"])}">{opts}</select><button>Save</button></form>')
        srows.append(f'<tr><td><code>{esc(r["key"])}</code></td><td>{esc(r["description"])}'
                     f'{" Takes effect after a restart." if r["restart"] else ""}</td>'
                     f'<td><b>{esc(r["value"])}</b><br><span class="muted">default {esc(r["default"])}, '
                     f'env {esc(r["env"])}</span></td><td>{control}</td></tr>')
    msg = f'<div class="note{" bad" if bad else ""}">{esc(note)}</div>' if note else ""
    body = (f'<main>{msg}'
            + panel("Add-ons", lock_note + '<table><tr><th>Add-on</th><th>What it gives</th><th>State</th><th></th>'
                    '<th>Note</th></tr>' + "".join(rows) + '</table>', "core add-ons and installed plugins")
            + panel("Settings", '<table><tr><th>Setting</th><th>What it does</th><th>Value</th><th></th></tr>'
                    + "".join(srows) + '</table>', "settings.json in the config folder; an env variable wins")
            + '</main>')
    return page("KB admin · settings", body, base, "Settings", csrf)


def _is_access(name: str) -> bool:
    """An installed plugin that answers the access webhooks (it is chosen once, at the start)."""
    import plugins
    return plugins.is_access(name)


def _access_refusal(name: str, turn_on: bool) -> str | None:
    """Why the page must not make this switch, or None. The access plugin decides who signs in: switched
    off from the browser, the server refuses to start after the next restart (or every token gets 403),
    and nobody can switch it back from the browser. A second one replaces the first at the next start.
    The command line on the server can still do both, on purpose."""
    import plugins
    mod = access()
    # The plugin the server runs now is known by its installed name first, whatever its source looks like
    # (it may import login and permissions from another file); NAME only when it was not loaded by name.
    started = (plugins.name_of(mod) or getattr(mod, "NAME", None)) if mod is not None else None
    if name != started and not plugins.is_access(name):
        return None
    active = {n for n in addons.enabled() if plugins.is_access(n)} | ({started} if started else set())
    if not turn_on and name in active:
        return (f"{name} answers the sign-in of this server: switched off here, nobody could sign in after a "
                f"restart. Switch it off on the server: kb addon off {name}, then restart.")
    if turn_on and active - {name}:
        return (f"{', '.join(sorted(active - {name}))} answers the sign-in already, and a server has one access "
                f"plugin. Change it on the server: kb addon on {name}, kb addon off "
                f"{sorted(active - {name})[0]}, then restart.")
    return None


def change_setting(form: dict) -> tuple[int, str, str]:
    """Apply one form of the Settings page -> (status, note for the page, audit text or "").
    The caller checked the session and the csrf value."""
    import settings
    name = str(form.get("name", ""))
    if "value" in form:
        value = str(form.get("value", ""))
        if name not in settings.REGISTRY:
            return 400, f"No such setting: {name}.", ""
        try:
            settings.set_value(name, value)
        except settings.Locked:
            return 409, f"{name} is set by {settings.REGISTRY[name].env}: change the variable instead.", ""
        except ValueError as e:
            return 400, str(e), ""
        return 200, f"Saved: {name} = {settings.get(name)}.", f"{name} = {settings.get(name)}"
    turn_on = str(form.get("on", "")) == "1"
    if name not in addons.known():
        return 400, f"No such add-on: {name}.", ""
    if os.environ.get("KB_ADDONS") is not None:
        return 409, "KB_ADDONS is set: change the variable instead.", ""
    refusal = _access_refusal(name, turn_on)
    if refusal:
        return 409, refusal, ""
    addons.set_enabled(name, turn_on)
    later = " It takes effect after a restart." if addons.needs_restart(name) else ""
    return 200, f"{name}: {'on' if turn_on else 'off'}.{later}", name


# ---------------------------------------------------------------------------------- charts
# Server-drawn SVG: no scripts (the CSP forbids them), a native tooltip (<title>) on every mark, the
# numbers also as text. One series per chart, so one hue and no legend; a failed build is drawn as a
# red outline with the word "failed" (status color + label, never color alone). Colors are CSS
# variables, so the same SVG follows the light and the dark theme.
def questions_page(base: str, csrf: str, usage, note: str = "") -> bytes:
    """The questions people asked, and what the right answer is. An admin gives each question a label:
    the document or symbol that must be found, "the corpus has no answer", or "skip". The labelled
    questions are the golden set from real use (`/admin/questions.json`)."""
    if usage is None or not hasattr(usage, "questions"):
        return page("KB admin · questions", '<main>' + panel("Questions", '<p class="muted">usage counting is off</p>')
                    + '</main>', base, "Questions", csrf)
    rows = usage.questions(300)
    todo = [r for r in rows if not r.get("labelled")]
    done = [r for r in rows if r.get("labelled")]

    def hidden(r: dict) -> str:
        return (f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
                f'<input type="hidden" name="tool" value="{esc(r["tool"])}">'
                f'<input type="hidden" name="arg" value="{esc(r["arg"])}">'
                f'<input type="hidden" name="project" value="{esc(r["project"])}">')

    def when(ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(ts))

    todo_rows = "".join(
        f'<tr><td>{esc(r["tool"])}</td><td class="mono">{esc(r["arg"])}</td><td>{esc(r["project"])}</td>'
        f'<td class="n">{r["count"]}</td><td>{when(r["last"])}</td><td>{esc(r["users"])}</td>'
        f'<td><form method="post" action="{base}/admin/questions/label" class="label">{hidden(r)}'
        f'<input name="expected" placeholder="path or symbol that must be found" size="34">'
        f'<button name="how" value="expected">Save</button>'
        f'<button name="how" value="no_answer">No answer in the corpus</button>'
        f'<button name="how" value="skip">Skip</button></form></td></tr>' for r in todo)
    done_rows = "".join(
        f'<tr><td>{esc(r["tool"])}</td><td class="mono">{esc(r["arg"])}</td><td>{esc(r["project"])}</td>'
        f'<td class="mono">{"<i>no answer in the corpus</i>" if r["no_answer"] else "<i>skipped</i>" if r["skip"] else esc(r["expected"])}</td>'
        f'<td>{esc(r["by"])}</td>'
        f'<td><form method="post" action="{base}/admin/questions/label">{hidden(r)}'
        f'<button name="how" value="clear">Clear</button></form></td></tr>' for r in done)
    head = (f'<p class="muted">{len(todo)} without a label, {len(done)} labelled. '
            f'<a href="{base}/admin/questions.json">Download the labelled questions</a> as a golden set.</p>'
            + (f'<p>{esc(note)}</p>' if note else ""))
    body = ('<main><div class="stack">' + head
            + panel("Without a label",
                    '<table><tr><th>Tool</th><th>Question</th><th>Projects</th><th class="n">Asked</th><th>Last</th>'
                    '<th>Users</th><th>The right answer</th></tr>'
                    + (todo_rows or '<tr><td colspan="7" class="muted">nothing waits</td></tr>') + '</table>',
                    "calls of search, contracts_search and code_grep; the argument is kept to 120 characters")
            + panel("Labelled",
                    '<table><tr><th>Tool</th><th>Question</th><th>Projects</th><th>The right answer</th><th>By</th>'
                    '<th></th></tr>'
                    + (done_rows or '<tr><td colspan="6" class="muted">none yet</td></tr>') + '</table>',
                    "kept after the calls themselves are deleted")
            + '</div></main>')
    return page("KB admin · questions", body, base, "Questions", csrf)


def nice(top: float) -> float:
    """A round axis maximum at or above `top` (1, 2, 2.5, 5 x 10^n)."""
    if top <= 0:
        return 1
    e = 10 ** math.floor(math.log10(top))
    for m in (1, 2, 2.5, 4, 5, 6, 8, 10):  # 4, 6, 8: 455 s gets a 600 axis, not 1,000
        if m * e >= top:
            return m * e
    return 10 * e


def hbars(rows: list[tuple[str, float]], unit: str = "", fmt=lambda v: f"{v:,.0f}", width: int = 440,
          label_w: int = 120, empty: str = "no data") -> str:
    """Horizontal bars: label, bar, value; a light axis with 0 / half / max underneath."""
    if not rows:
        return f'<p class="muted">{esc(empty)}</p>'
    top = nice(max(v for _, v in rows))
    vw = 64
    plot = width - label_w - vw - 8
    row_h = 22
    h = row_h * len(rows) + 22
    out = [f'<svg viewBox="0 0 {width} {h}" width="100%" role="img">']
    for frac in (0, .5, 1):
        x = label_w + plot * frac
        out.append(f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="0" y2="{h - 16}"/>'
                   f'<text x="{x:.1f}" y="{h - 3}" text-anchor="middle">{esc(fmt(top * frac))}</text>')
    for i, (label, v) in enumerate(rows):
        y = i * row_h + 4
        w = max(1.5, plot * v / top)
        lab = label if len(label) <= 18 else label[:17] + "…"
        out.append(f'<text x="0" y="{y + 11}">{esc(lab)}</text>'
                   f'<rect class="bar" x="{label_w}" y="{y + 1}" width="{w:.1f}" height="12" rx="1.5">'
                   f'<title>{esc(label)}: {esc(fmt(v))}{esc(unit)}</title></rect>'
                   f'<text class="v" x="{width}" y="{y + 11}" text-anchor="end">{esc(fmt(v))}{esc(unit)}</text>')
    out.append("</svg>")
    return "".join(out)


def vbars(rows: list[tuple[str, float, bool]], unit: str, width: int = 640, height: int = 210,
          every: int = 1, empty: str = "no data") -> str:
    """Vertical bars over time with a y axis; `bad` rows are a red outline labelled "failed"."""
    if not rows:
        return f'<p class="muted">{esc(empty)}</p>'
    top = nice(max(v for _, v, _ in rows) * 1.1)
    left, bottom, top_pad = 44, 26, 18
    pw, ph = width - left - 4, height - bottom - top_pad
    step = pw / len(rows)
    bw = max(3, min(28, step * 0.72))
    # Enough room for each x label (11 px text, about 6.5 units a character); the last bar always has its label.
    need = max(len(label) for label, _, _ in rows) * 6.5 + 8
    every = max(every, math.ceil(need / step))
    labelled = set(range(len(rows) - 1, -1, -every))
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">']
    for frac in (0, .25, .5, .75, 1):
        y = top_pad + ph * (1 - frac)
        out.append(f'<line class="grid" x1="{left}" x2="{width - 4}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{top * frac:,.0f}</text>')
    for i, (label, v, bad) in enumerate(rows):
        x = left + i * step + (step - bw) / 2
        bh = max(1.5, ph * v / top)
        y = top_pad + ph - bh
        tip = f"<title>{esc(label)}: {v:,.0f}{esc(unit)}{' — failed' if bad else ''}</title>"
        if bad:
            out.append(f'<rect class="badbar" x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}">{tip}</rect>'
                       f'<text class="badtxt" x="{x + bw / 2:.1f}" y="{y - 5:.1f}" text-anchor="middle">failed</text>')
        else:
            out.append(f'<rect class="bar" x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="1.5">{tip}</rect>')
        if i in labelled:
            cx = x + bw / 2
            if cx + len(label) * 3.25 > width - 2:  # a label at the right edge ends there instead of being cut
                out.append(f'<text x="{width - 2}" y="{height - 8}" text-anchor="end">{esc(label)}</text>')
            else:
                out.append(f'<text x="{cx:.1f}" y="{height - 8}" text-anchor="middle">{esc(label)}</text>')
    out.append("</svg>")
    return "".join(out)


def spark(values: list[float], width: int = 140, height: int = 18, scale: float | None = None) -> str:
    """A tiny bar sparkline; `scale` shares one maximum between rows (like PMM's table)."""
    if len(values) < 2 or not any(values):  # one point is no trend: a baseline, not a full bar
        return f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}"><line class="grid" x1="0" x2="{width}" y1="{height - .5}" y2="{height - .5}"/></svg>'
    top = scale or max(values)
    step = width / len(values)
    bars = "".join(f'<rect class="bar" x="{i * step:.1f}" y="{height - max(1, height * v / top):.1f}" '
                   f'width="{max(1, step - 1):.1f}" height="{max(1, height * v / top):.1f}"/>'
                   for i, v in enumerate(values) if v)
    return f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}">{bars}</svg>'


def ratio(frac: float, bad: bool = False) -> str:
    return f'<div class="ratio{" bad" if bad else ""}"><i style="width:{max(0, min(1, frac)) * 100:.1f}%"></i></div>'


def _size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for dp, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def _qmd_index() -> str | None:
    for p in (os.path.join(kb_core.KB, ".qmd", "index.sqlite"),
              os.path.expanduser("~/.cache/qmd/index.sqlite")):
        if os.path.isfile(p):
            return p
    return None


def docs_facts() -> dict:
    """Documents and embedded chunks per project, read-only from the qmd index."""
    import sqlite3
    path = _qmd_index()
    if not path:
        return {"path": None, "docs": [], "chunks": []}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        docs = con.execute("SELECT collection, COUNT(*) FROM documents WHERE active = 1 "
                           "GROUP BY collection").fetchall()
        chunks = con.execute("SELECT d.collection, COUNT(*) FROM documents d JOIN content_vectors v "
                             "ON v.hash = d.hash WHERE d.active = 1 GROUP BY d.collection").fetchall()
        con.close()
    except sqlite3.Error:
        return {"path": path, "docs": [], "chunks": []}
    prefix = f"{kb_core.COLLECTION}-"
    name = lambda c: c[len(prefix):] if c.startswith(prefix) else c  # noqa: E731
    return {"path": path, "docs": sorted(((name(c), n) for c, n in docs), key=lambda r: -r[1]),
            "chunks": sorted(((name(c), n) for c, n in chunks), key=lambda r: -r[1])}


_graph_detail: dict = {}


def graph_detail() -> dict:
    """Nodes per type and edges per relation (cached by the graph file's identity)."""
    try:
        st = os.stat(kb_core.GRAPH)
    except OSError:
        return {"types": [], "relations": []}
    key = (st.st_mtime, st.st_size)
    if _graph_detail.get("key") != key:
        try:
            with open(kb_core.GRAPH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {"types": [], "relations": []}
        types: dict[str, int] = {}
        for n in data.get("nodes", []):
            types[n.get("type") or "?"] = types.get(n.get("type") or "?", 0) + 1
        rels: dict[str, int] = {}
        for e in data.get("links") or data.get("edges") or []:
            rels[e.get("relation") or "?"] = rels.get(e.get("relation") or "?", 0) + 1
        _graph_detail.update(key=key, types=_top(types, 10), relations=_top(rels, 12))
    return _graph_detail


def _top(counts: dict, n: int) -> list[tuple[str, int]]:
    rows = sorted(counts.items(), key=lambda kv: -kv[1])
    if len(rows) > n:
        rows = rows[:n - 1] + [("other", sum(v for _, v in rows[n - 1:]))]
    return rows


def builds() -> list[tuple[str, float, bool]]:
    """(when, seconds, failed) of the last 30 builds, from logs/refresh.log."""
    import re
    out = []
    logs = os.path.join(kb_core.KB, "logs")
    # builds.log is written by bin/kb itself (every build, any trigger); refresh.log by the server's
    # kb-refresh only. Prefer the first, fall back to the second for builds made before it existed.
    source = os.path.join(logs, "builds.log")
    if not os.path.exists(source):
        source = os.path.join(logs, "refresh.log")
    for line in _tail(source, 30):
        m = re.match(r"(\d{4}-\d\d-\d\d) (\d\d:\d\d):\d\d exit=(\d+) (\d+)s", line)
        if m:
            out.append((f"{m.group(1)[5:]} {m.group(2)}", float(m.group(4)), m.group(3) != "0"))
    return out


PSEUDO_NAMES = {"domain": "(domain groups)", "db": "(database tables)"}


def _history(facts: dict) -> list[dict]:
    """The KB's own small history of its headline numbers, one point per graph build (kept in
    logs/index-history.json, last 60): the tiles' sparklines need no Prometheus."""
    import tempfile
    path = os.path.join(kb_core.KB, "logs", "index-history.json")
    with _history_lock:  # two page loads at once: one reads while the other writes
        try:
            with open(path, encoding="utf-8") as f:
                hist = json.load(f)
            if not isinstance(hist, list):
                hist = []
        except (OSError, ValueError):
            hist = []
        if facts.get("built") and (not hist or hist[-1].get("built") != facts["built"]):
            hist = (hist + [facts])[-60:]
            tmp = None
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".index-history.", suffix=".tmp", dir=os.path.dirname(path))
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(hist, f)
                os.replace(tmp, path)  # a reader sees the old file or the new one, never half of one
            except OSError:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
    return hist


_history_lock = threading.Lock()


def index_page(base: str, csrf: str, usage) -> bytes:
    g = graph_facts()
    detail = graph_detail()
    docs = docs_facts()
    out_dir = os.path.dirname(kb_core.GRAPH)
    stores = [(label, _size(p) / 1e6) for label, p in (
        ("staged code", os.path.join(kb_core.KB, "repos")),
        ("caller views", os.path.join(out_dir, "filtered")),
        ("graph.json", kb_core.GRAPH),
        ("docs search", docs["path"] or ""),
        ("dashboard data", os.path.join(out_dir, "kb-graph-data")),
        ("staged docs", os.path.join(kb_core.KB, ".docs")),
        ("graphify cache", os.path.join(out_dir, "cache")),
        ("contracts", os.path.join(out_dir, "contracts.json"))) if p and os.path.exists(p)]
    stores.sort(key=lambda r: -r[1])
    disk = sum(v for _, v in stores)
    b = builds()
    good = [s_ for _, s_, bad in b if not bad]
    d = usage.snapshot() if usage is not None else {"tools": {}, "days": {}, "hours": {}}
    tools = d.get("tools", {})
    calls = sum(v.get("calls", 0) for v in tools.values())
    avg = (sum(v.get("seconds", 0) for v in tools.values()) / calls) if calls else None
    n_docs, n_chunks = sum(n for _, n in docs["docs"]), sum(n for _, n in docs["chunks"])
    hist = _history({"built": g.get("built"), "nodes": g.get("nodes"), "edges": g.get("edges"),
                     "docs": n_docs, "chunks": n_chunks, "disk": round(disk, 1),
                     "build": good[-1] if good else None})
    series = lambda k: [float(h.get(k) or 0) for h in hist[-30:]]  # noqa: E731
    hours = [float(sum(v.values())) for _, v in sorted(d.get("hours", {}).items())][-24:]

    def tile(label, value, sub, values):
        return (f'<div class="tile"><div class="k">{esc(label)}</div><div class="v">{esc(value)}</div>'
                f'{spark(values, 160, 16)}<div class="s">{esc(sub)}</div></div>')
    fmt_n = lambda v: f"{v:,}" if isinstance(v, int) else "—"  # noqa: E731
    tiles = "".join([
        tile("Graph nodes", fmt_n(g.get("nodes")), f"built {g.get('built', '—')}", series("nodes")),
        tile("Graph edges", fmt_n(g.get("edges")), f"{len(g.get('repos', {}))} projects", series("edges")),
        tile("Documents", f"{n_docs:,}", "in docs search", series("docs")),
        tile("Embedded chunks", f"{n_chunks:,}", "vectors", series("chunks")),
        tile("Disk", f"{disk:,.0f} MB", "all indexes", series("disk")),
        tile("Last good build", f"{good[-1]:.0f} s" if good else "—",
             f"{sum(1 for *_, bad in b if bad)} failed of {len(b)}" if b else "no builds logged", [v for _, v, _ in b]),
        tile("Avg answer", f"{avg:.2f} s" if avg is not None else "—",
             f"{calls:,} calls" if calls else "no calls counted yet", hours),
    ])
    repos = sorted(((PSEUDO_NAMES.get(k, k), v) for k, v in g.get("repos", {}).items()), key=lambda kv: -kv[1])
    by_proj = {}
    for name, n in docs["docs"]:
        by_proj.setdefault(name, [0, 0])[0] = n
    for name, n in docs["chunks"]:
        by_proj.setdefault(name, [0, 0])[1] = n
    top_d = max([v[0] for v in by_proj.values()] or [1]) or 1
    top_c = max([v[1] for v in by_proj.values()] or [1]) or 1
    doc_rows = "".join(f'<tr><td>{esc(k)}</td><td class="n">{v[0]:,}{ratio(v[0] / top_d)}</td>'
                       f'<td class="n">{v[1]:,}{ratio(v[1] / top_c)}</td></tr>'
                       for k, v in sorted(by_proj.items(), key=lambda kv: -kv[1][1]))
    doc_table = (f'<table><tr><th>Project</th><th class="n">Documents</th><th class="n">Chunks</th></tr>{doc_rows}'
                 f'<tr class="total"><td>Total</td><td class="n">{n_docs:,}</td><td class="n">{n_chunks:,}</td></tr></table>'
                 if by_proj else '<p class="muted">docs search not built</p>')
    latency = sorted(((t, v["seconds"] / v["calls"]) for t, v in tools.items() if v.get("calls")),
                     key=lambda r: -r[1])
    days = sorted(d.get("days", {}).items())[-14:]
    body = ('<main><div class="tiles">' + tiles + '</div>'
            '<div class="grid g2">'
            + panel("Build time · last 30 builds", vbars(b, " s", every=max(1, len(b) // 10),
                                                         empty="no builds logged yet: the next build records its time"),
                    f"last {good[-1]:.0f} s" if good else "")
            + panel("Calls per day · last 14 days", vbars([(day[5:], float(sum(v.values())), False) for day, v in days],
                                                          " calls", empty="no calls counted yet"),
                    f"{calls:,} total" if calls else "")
            + '</div><div class="grid g3" style="margin-top:16px">'
            + panel("Graph nodes by project", hbars(repos))
            + panel("Graph nodes by type", hbars(detail.get("types", [])))
            + panel("Graph edges by relation", hbars(detail.get("relations", [])), f"{g.get('edges', 0):,} edges"
                    if isinstance(g.get("edges"), int) else "")
            + '</div><div class="grid g3" style="margin-top:16px">'
            + panel("Documents and chunks by project", doc_table, f"{n_docs:,} docs · {n_chunks:,} chunks")
            + panel("Storage per index", hbars(stores, " MB", fmt=lambda v: f"{v:,.0f}" if v >= 10 else f"{v:,.1f}"),
                    f"total {disk:,.0f} MB")
            + panel("Average answer time by tool", hbars(latency, " s", fmt=lambda v: f"{v:.2f}",
                                                         empty="no calls counted yet"), "mean")
            + '</div></main>')
    return page("KB admin · index", body, base, "Index", csrf)


def _rate(calls: float, hours: int) -> str:
    """Calls in the window, and per minute once there are enough of them to say something."""
    per_min = calls / (hours * 60)
    return f"{calls:,.0f} · {per_min:.2f}/min" if per_min >= 0.01 else f"{calls:,.0f} calls"


def usage_page(base: str, csrf: str, usage, query: dict | None = None, hours: int = 12) -> bytes:
    """PMM Query Analytics, KB edition: filters on the left (links, no scripts), one row per tool
    with its calls sparkline over the last `hours` hours, calls/min, mean and p95 time, errors."""
    query = query or {}
    if usage is None:
        return page("KB admin · usage", '<main>' + panel("Usage", '<p class="muted">usage counting is off</p>')
                    + '</main>', base, "Usage", csrf)
    d = usage.snapshot()
    tools = d.get("tools", {})
    pick_tool, pick_user, pick_proj = query.get("tool"), query.get("user"), query.get("project")
    allowed_tools = set(tools)
    if pick_user and pick_user in d.get("users", {}):
        allowed_tools &= set(d["users"][pick_user])
    if pick_proj and pick_proj in d.get("projects", {}):
        allowed_tools &= set(d["projects"][pick_proj])
    if pick_tool:
        allowed_tools &= {pick_tool}
    hour_keys = [time.strftime("%Y-%m-%d %H", time.localtime(time.time() - 3600 * k)) for k in range(hours - 1, -1, -1)]
    per_hour = {t: [float(d.get("hours", {}).get(h, {}).get(t, 0)) for h in hour_keys] for t in tools}
    window_calls = {t: sum(v) for t, v in per_hour.items()}
    scale = max([max(v) for v in per_hour.values()] or [1]) or 1
    rows = sorted((t for t in tools if t in allowed_tools), key=lambda t: -tools[t]["calls"])
    total_calls = sum(tools[t]["calls"] for t in rows) or 1
    max_avg = max([tools[t]["seconds"] / tools[t]["calls"] for t in rows if tools[t]["calls"]] or [1]) or 1
    max_err = max([tools[t]["errors"] for t in rows] or [1]) or 1
    p95s = {t: usage.p95(t) for t in rows}
    max_p95 = max([v for v in p95s.values() if v] or [1]) or 1

    def link(**kw) -> str:
        q = {k: v for k, v in {"tool": pick_tool, "user": pick_user, "project": pick_proj, **kw}.items() if v}
        return f"{base}/admin/usage" + ("?" + urllib.parse.urlencode(q) if q else "")

    def filt(title, key, counts, current):
        tot = sum(counts.values()) or 1
        items = "".join(f'<a class="{"on" if current == k else ""}" href="{esc(link(**{key: None if current == k else k}))}">'
                        f'<span class="box"></span>{esc(k)}<span class="pct">{v / tot:.0%}</span></a>'
                        for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:12])
        return f'<h3>{esc(title)}<span>{len(counts)}</span></h3>{items}'
    users = {u: sum(v.values()) for u, v in d.get("users", {}).items()}
    projects = {p: sum(v.values()) for p, v in d.get("projects", {}).items()}
    tool_counts = {t: v["calls"] for t, v in tools.items()}
    filters = ('<aside class="panel filters"><div class="body">'
               + filt("Project", "project", projects, pick_proj) + "<hr>" + filt("Tool", "tool", tool_counts, pick_tool)
               + "<hr>" + filt("User", "user", users, pick_user)
               + (f'<hr><a href="{base}/admin/usage">Reset all</a>' if (pick_tool or pick_user or pick_proj) else "")
               + '</div></aside>')
    trs = []
    tot_hour = [sum(per_hour[t][i] for t in rows) for i in range(hours)]
    trs.append(f'<tr class="total"><td>TOTAL</td><td>{spark(tot_hour, 300, 20)}</td>'
               f'<td class="n">{_rate(sum(window_calls[t] for t in rows), hours)}{ratio(1)}</td>'
               f'<td class="n">{sum(tools[t]["seconds"] for t in rows) / total_calls:.2f} s</td>'
               f'<td class="n">{sum(tools[t]["errors"] for t in rows)}</td><td class="n"></td></tr>')
    for t in rows:
        v = tools[t]
        avg = v["seconds"] / v["calls"] if v["calls"] else 0
        p95 = p95s[t]
        trs.append(f'<tr><td><a href="{esc(link(tool=t))}"><code>{esc(t)}</code></a></td>'
                   f'<td>{spark(per_hour[t], 300, 20, scale)}</td>'
                   f'<td class="n">{_rate(window_calls[t], hours)}{ratio(v["calls"] / total_calls)}</td>'
                   f'<td class="n">{avg:.2f} s{ratio(avg / max_avg)}</td>'
                   f'<td class="n{" bad" if v["errors"] else ""}">{v["errors"]}{ratio(v["errors"] / max_err, True) if v["errors"] else ""}</td>'
                   f'<td class="n">{f"{p95:.2f} s" if p95 is not None else "—"}{ratio((p95 or 0) / max_p95)}</td></tr>')
    table = ('<table><tr><th>Tool</th><th>Calls · last 12 hours</th><th class="n">Calls</th>'
             '<th class="n">Avg time</th><th class="n">Errors</th><th class="n">p95 time</th></tr>'
             + "".join(trs) + "</table>")
    args = []
    for t, a in d.get("args", {}).items():
        if t in allowed_tools:
            args += [(n, t, k) for k, n in a.items()]
    args.sort(key=lambda r: -r[0])
    top_n = args[0][0] if args else 1
    asked = "".join(f'<tr><td><code>{esc(t)}</code> · {esc(k)}</td><td class="n">{n:,}{ratio(n / top_n)}</td></tr>'
                    for n, t, k in args[:15]) or '<tr><td class="muted">no calls counted yet</td></tr>'
    sub = f"{len(rows)} tools · {len(users)} users · since {d.get('since', '—')}"
    body = ('<main><div class="layout">' + filters + '<div class="stack">'
            + panel("Tool usage", table, sub)
            + panel("Most asked", f'<table><tr><th>Argument</th><th class="n">Count</th></tr>{asked}</table>',
                    "the most frequent arguments")
            + _slow_panel(usage)
            + '</div></div></main>')
    return page("KB admin · usage", body, base, "Usage", csrf)


def _slow_panel(usage) -> str:
    """The slowest calls of the last 48 hours: which tool, which projects, how long (bin/usage.py)."""
    rows = usage.slow(15) if hasattr(usage, "slow") else []
    if not rows:
        return ""
    trs = "".join(f'<tr><td>{esc(when[5:16])}</td><td>{esc(tool)}</td><td>{esc(project)}</td>'
                  f'<td>{esc(user)}</td><td class="mono">{esc(arg[:60])}</td><td class="n">{secs:.2f} s</td></tr>'
                  for when, tool, user, project, secs, arg in rows)
    return panel("Slowest calls", '<table><tr><th>When</th><th>Tool</th><th>Projects</th><th>User</th>'
                 f'<th>Argument</th><th class="n">Time</th></tr>{trs}</table>', "the last 48 hours")
