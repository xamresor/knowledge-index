#!/usr/bin/env python3
"""The admin pages of the shared server (bin/kb-http): /admin.

Who: the users named in KB_ADMINS (comma-separated; matched against the `uname`, `username`, `name`,
`id` or `uid` the identity endpoint returns). Nobody else sees anything, not even that the page exists
(404). No KB_ADMINS -> no admin pages.

Sign-in: the same login as the browser sign-in of MCP clients (user name, password, the authenticator
code, through the API's own login). The API token is checked like any tool call; then a session
cookie (random, HttpOnly, Secure behind TLS, SameSite=Strict, 8 hours) stands for it. Every form
carries the session's own CSRF token. Pages are plain HTML, no scripts.

What it shows and does:
  * status: version, the graph (built when, nodes, edges), the last builds and failures, the warm
    helpers and their memory, cursors and sign-in clients in use, problems the build found;
  * usage: calls per tool, per day, per user, the slowest tools;
  * sign-in clients: the registered MCP clients; revoking one ends its refresh tokens;
  * rebuild: starts `bin/kb-refresh --force` (once at a time).
Roles are not here on purpose: they live in the API (one place to keep them).
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import subprocess
import threading
import time

import kb_core
import warm

SESSION_TTL = 8 * 3600
COOKIE = "kb_admin"
ID_FIELDS = ("uname", "username", "name", "id", "uid")


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
    return any(str(user.get(k, "")).strip().lower() in names for k in ID_FIELDS if user.get(k))


class Sessions:
    def __init__(self, ttl: float = SESSION_TTL):
        self.ttl = ttl
        self.items: dict[str, dict] = {}
        self.lock = threading.Lock()

    def create(self, user: dict, token: str) -> str:
        sid = secrets.token_urlsafe(32)
        with self.lock:
            self._expire()
            self.items[sid] = {"user": user, "token": token, "csrf": secrets.token_urlsafe(24),
                               "exp": time.monotonic() + self.ttl}
        return sid

    def get(self, sid: str | None) -> dict | None:
        if not sid:
            return None
        with self.lock:
            self._expire()
            return self.items.get(sid)

    def drop(self, sid: str | None) -> None:
        with self.lock:
            self.items.pop(sid or "", None)

    def _expire(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self.items.items() if v["exp"] < now]:
            del self.items[k]


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


_rebuild_lock = threading.Lock()


def rebuild() -> str:
    """Start one forced rebuild in the background; refuse while one runs."""
    lock_file = os.path.join(kb_core.KB, "logs", "update.lock")
    if os.path.exists(lock_file) or not _rebuild_lock.acquire(blocking=False):
        return "A build is already running."
    try:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb-refresh")
        subprocess.Popen([script, "--force"], cwd=kb_core.KB, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return "Rebuild started. It takes a few minutes; the status page shows it in the build log."
    finally:
        _rebuild_lock.release()


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
        "building": os.path.exists(os.path.join(logs, "update.lock")),
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
STYLE = """:root{--surface:#fcfcfb;--plane:#f4f4f2;--ink:#0b0b0b;--ink2:#52514e;--grid:#e8e7e3;
--bar:#1c1917;--bar-ink:#e7e5e4;--s1:#2a78d6;--crit:#d03b3b;--good:#0ca30c;--btn:#1c1917;--btn-ink:#fff;
--btn2:#e8e7e3;--btn2-ink:#1c1917;--note:#ecfdf5}
@media (prefers-color-scheme: dark){:root{--surface:#1a1a19;--plane:#0d0d0d;--ink:#f0efec;--ink2:#c3c2b7;
--grid:#383835;--bar:#000;--bar-ink:#c3c2b7;--s1:#3987e5;--crit:#e66767;--good:#0ca30c;--btn:#f0efec;
--btn-ink:#0d0d0d;--btn2:#383835;--btn2-ink:#f0efec;--note:#12301f}}
body{font:15px system-ui,sans-serif;margin:0;background:var(--plane);color:var(--ink)}
header{background:var(--bar);color:#fff;padding:.8rem 1.5rem;display:flex;gap:1.5rem;align-items:center;flex-wrap:wrap}
header a{color:var(--bar-ink);text-decoration:none}header a.on{color:#fff;font-weight:600}
header form{margin-left:auto}main{max-width:72rem;margin:1.5rem auto;padding:0 1rem}
section{background:var(--surface);border-radius:10px;padding:1rem 1.3rem;margin-bottom:1rem;box-shadow:0 1px 2px #0002}
h2{font-size:1rem;margin:.2rem 0 .8rem}table{border-collapse:collapse;width:100%}
td,th{text-align:left;padding:.3rem .5rem;border-bottom:1px solid var(--grid);font-size:.9rem;vertical-align:top}
pre{white-space:pre-wrap;font-size:.82rem;margin:0}.bad{color:var(--crit)}.ok{color:var(--good)}
button{padding:.4rem .9rem;border:0;border-radius:6px;background:var(--btn);color:var(--btn-ink);cursor:pointer}
button.light{background:var(--btn2);color:var(--btn2-ink)}
.note{background:var(--note);border-left:3px solid var(--good);padding:.5rem .8rem;margin-bottom:1rem}
main.login{max-width:22rem;margin:12vh auto}label{display:block;font-size:.9rem;margin:.8rem 0 .3rem}
input{width:100%;box-sizing:border-box;padding:.55rem;border:1px solid var(--grid);border-radius:6px;font-size:1rem;
background:var(--surface);color:var(--ink)}"""


def esc(x) -> str:
    return html.escape(str(x))


def page(title: str, body: str, base: str = "", tab: str = "", csrf: str = "") -> bytes:
    nav = ""
    if csrf:
        links = "".join(f'<a href="{base}/admin{p}" class="{"on" if tab == t else ""}">{t}</a>'
                        for t, p in (("Status", ""), ("Index", "/index"), ("Usage", "/usage"),
                                             ("Clients", "/clients")))
        nav = (f'<header><b>KB admin</b>{links}<form method="post" action="{base}/admin/logout">'
               f'<input type="hidden" name="csrf" value="{esc(csrf)}"><button class="light">Sign out</button>'
               f'</form></header>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title>'
            f'<style>{STYLE}</style></head><body>{nav}{body}</body></html>').encode()


def login_page(base: str, step: str = "password", lid: str = "", error: str = "") -> bytes:
    err = f'<p class="bad">{esc(error)}</p>' if error else ""
    if step == "code":
        fields = (f'<input type="hidden" name="lid" value="{esc(lid)}"><label for="c">Authenticator code</label>'
                  '<input id="c" name="code" autocomplete="one-time-code" inputmode="numeric" autofocus required>')
    else:
        fields = ('<label for="u">User name</label><input id="u" name="username" autocomplete="username" autofocus required>'
                  '<label for="p">Password</label><input id="p" name="password" type="password" '
                  'autocomplete="current-password" required>')
    body = (f'<main class="login"><section><h2>KB admin</h2>{err}<form method="post" action="{base}/admin/login">'
            f'{fields}<p><button>Sign in</button></p></form></section></main>')
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
                   f'value="{esc(csrf)}"><button>Rebuild now</button></form>')
    body = (f'<main>{f"<div class=note>{esc(note)}</div>" if note else ""}'
            f'<section><h2>Knowledge base</h2><table>'
            f'<tr><th>Version</th><td>{esc(s["version"])} (tool contract {esc(s["contract"])})</td></tr>'
            f'<tr><th>Graph</th><td>built {esc(g.get("built"))}, {esc(g.get("nodes"))} nodes, '
            f'{esc(g.get("edges"))} edges, {esc(g.get("size_mb"))} MB</td></tr>'
            f'<tr><th>Projects</th><td>{esc(repos)}</td></tr>'
            f'<tr><th>Problems</th><td>{esc(s["problems"])}</td></tr>'
            f'<tr><th>In use</th><td>{s["cursors"]} cursors, {s["clients"]} sign-in clients, server '
            f'{esc(s["server_mb"])} MB</td></tr>'
            f'<tr><th>Signed in as</th><td>{esc(next((user.get(k) for k in ID_FIELDS if user.get(k)), "?"))}</td></tr>'
            f'</table></section>'
            f'<section><h2>Builds{" — <span class=bad>last failed</span>" if s["failed"] and s["builds"] and "exit=0" not in s["builds"][-1] else ""}</h2>'
            f'<table>{builds}</table><p></p>{rebuild_btn}</section>'
            f'<section><h2>Warm helpers</h2><table>{helpers}</table></section></main>')
    return page("KB admin", body, base, "Status", csrf)


def usage_page(base: str, csrf: str, report: str) -> bytes:
    return page("KB admin · usage", f"<main><section><h2>Usage</h2><pre>{esc(report)}</pre></section></main>",
                base, "Usage", csrf)


def clients_page(base: str, csrf: str, clients: dict, note: str = "") -> bytes:
    rows = "".join(
        f"<tr><td>{esc(c.get('client_name') or '—')}</td><td><code>{esc(cid)}</code></td>"
        f"<td>{esc(time.strftime('%Y-%m-%d %H:%M', time.localtime(c.get('client_id_issued_at', 0))))}</td>"
        f"<td>{esc(', '.join(c.get('redirect_uris', [])))}</td>"
        f'<td><form method="post" action="{base}/admin/clients/revoke"><input type="hidden" name="csrf" '
        f'value="{esc(csrf)}"><input type="hidden" name="client_id" value="{esc(cid)}">'
        f'<button class="light">Revoke</button></form></td></tr>'
        for cid, c in sorted(clients.items(), key=lambda kv: -kv[1].get("client_id_issued_at", 0)))
    body = (f'<main>{f"<div class=note>{esc(note)}</div>" if note else ""}<section><h2>Sign-in clients</h2>'
            f'<p>MCP clients that registered for the browser sign-in. Revoking one ends its refresh tokens: '
            f'it has to sign in again. A token it already holds lives until the API ends it.</p>'
            f'<table><tr><th>Name</th><th>Client id</th><th>Registered</th><th>Redirect</th><th></th></tr>'
            f'{rows or "<tr><td colspan=5>none</td></tr>"}</table></section></main>')
    return page("KB admin · clients", body, base, "Clients", csrf)


# ---------------------------------------------------------------------------------- index page
# Charts are server-rendered SVG: no scripts (the CSP forbids them), a native tooltip (<title>) on
# every mark, the numbers also as text. One series per chart, so one hue and no legend; a failed
# build carries the critical status color and the word "failed". Colors: the dataviz reference
# palette (series-1 blue, status critical), light and dark from the same ramps.
CHART_CSS = """.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));gap:.7rem;margin-bottom:1rem}
.tile{background:var(--surface);border-radius:10px;padding:.8rem 1rem;box-shadow:0 1px 2px #0002}
.tile b{display:block;font-size:1.5rem;color:var(--ink)}.tile span{font-size:.8rem;color:var(--ink2)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(30rem,100%),1fr));gap:1rem}
svg text{fill:var(--ink2);font:12px system-ui,sans-serif}svg .v{fill:var(--ink)}"""


def _hbar(rows: list[tuple[str, float]], unit: str = "", width: int = 520, fmt=lambda v: f"{v:,.0f}") -> str:
    """Horizontal bars, one series: label left, bar, value at the bar end."""
    if not rows:
        return "<p>no data</p>"
    top = max(v for _, v in rows) or 1
    lw, row_h, bar_h = 170, 24, 14
    plot = width - lw - 70
    h = row_h * len(rows) + 6
    parts = [f'<svg viewBox="0 0 {width} {h}" width="100%" role="img">']
    for i, (label, v) in enumerate(rows):
        y = i * row_h + 4
        w = max(2, plot * v / top)
        lab = label if len(label) <= 24 else label[:23] + "…"
        parts.append(f'<text x="{lw - 8}" y="{y + 11}" text-anchor="end">{esc(lab)}</text>'
                     f'<rect x="{lw}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="3" fill="var(--s1)">'
                     f'<title>{esc(label)}: {esc(fmt(v))}{esc(unit)}</title></rect>'
                     f'<text class="v" x="{lw + w + 6:.1f}" y="{y + 11}">{esc(fmt(v))}{esc(unit)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _vbar(rows: list[tuple[str, float, bool]], unit: str, width: int = 520, height: int = 190) -> str:
    """Vertical bars over time, one series; `bad` rows are marked critical with a text label."""
    if not rows:
        return "<p>no builds logged</p>"
    top = max(v for _, v, _ in rows) or 1
    left, bottom, top_pad = 40, 34, 16
    plot_w, plot_h = width - left - 8, height - bottom - top_pad
    step = plot_w / len(rows)
    bw = max(4, min(26, step - 4))
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">']
    for frac in (0, 0.5, 1):
        y = top_pad + plot_h * (1 - frac)
        parts.append(f'<line x1="{left}" x2="{width - 8}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--grid)"/>'
                     f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{top * frac:.0f}</text>')
    for i, (label, v, bad) in enumerate(rows):
        x = left + i * step + (step - bw) / 2
        bh = max(2, plot_h * v / top)
        y = top_pad + plot_h - bh
        color = "var(--crit)" if bad else "var(--s1)"
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="3" fill="{color}">'
                     f'<title>{esc(label)}: {v:.0f}{esc(unit)}{" — failed" if bad else ""}</title></rect>')
        if bad:
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{y - 4:.1f}" text-anchor="middle">failed</text>')
    first, last = rows[0][0], rows[-1][0]
    parts.append(f'<text x="{left}" y="{height - 10}">{esc(first)}</text>'
                 f'<text x="{width - 8}" y="{height - 10}" text-anchor="end">{esc(last)}</text></svg>')
    return "".join(parts)


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
    for line in _tail(os.path.join(kb_core.KB, "logs", "refresh.log"), 30):
        m = re.match(r"(\d{4}-\d\d-\d\d) (\d\d:\d\d):\d\d exit=(\d+) (\d+)s", line)
        if m:
            out.append((f"{m.group(1)[5:]} {m.group(2)}", float(m.group(4)), m.group(3) != "0"))
    return out


def index_page(base: str, csrf: str, usage) -> bytes:
    g = graph_facts()
    detail = graph_detail()
    docs = docs_facts()
    out_dir = os.path.dirname(kb_core.GRAPH)
    stores = [(label, _size(p) / 1e6) for label, p in (
        ("graph.json", kb_core.GRAPH),
        ("caller views on disk", os.path.join(out_dir, "filtered")),
        ("dashboard data", os.path.join(out_dir, "kb-graph-data")),
        ("contracts + problems", os.path.join(out_dir, "contracts.json")),
        ("graphify cache", os.path.join(out_dir, "cache")),
        ("docs search (qmd)", docs["path"] or ""),
        ("staged code", os.path.join(kb_core.KB, "repos")),
        ("staged docs", os.path.join(kb_core.KB, ".docs"))) if p and os.path.exists(p)]
    stores.sort(key=lambda r: -r[1])
    b = builds()
    ok = [s for _, s, bad in b if not bad]
    tools = {}
    days = []
    if usage is not None:
        with usage._lock:
            d = json.loads(json.dumps(usage.data))
        tools = d.get("tools", {})
        days = sorted(d.get("days", {}).items())[-14:]
    latency = sorted(((t, v["seconds"] / v["calls"]) for t, v in tools.items() if v.get("calls")),
                     key=lambda r: -r[1])
    calls = sum(v.get("calls", 0) for v in tools.values())
    avg = (sum(v.get("seconds", 0) for v in tools.values()) / calls) if calls else 0
    tile = lambda big, small: f'<div class="tile"><b>{esc(big)}</b><span>{esc(small)}</span></div>'  # noqa: E731
    tiles = "".join([
        tile(f"{g.get('nodes', '?'):,}" if isinstance(g.get("nodes"), int) else "?", "graph nodes"),
        tile(f"{g.get('edges', '?'):,}" if isinstance(g.get("edges"), int) else "?", "graph edges"),
        tile(f"{sum(n for _, n in docs['docs']):,}", "documents indexed"),
        tile(f"{sum(n for _, n in docs['chunks']):,}", "embedded chunks"),
        tile(f"{sum(v for _, v in stores):,.0f} MB", "on disk"),
        tile(f"{ok[-1]:.0f} s" if ok else "—", "last good build"),
        tile(f"{avg:.2f} s" if calls else "—", f"avg answer, {calls:,} calls"),
    ])
    sec = lambda title, inner: f"<section><h2>{esc(title)}</h2>{inner}</section>"  # noqa: E731
    body = (f'<main><div class="tiles">{tiles}</div><div class="grid2">'
            + sec("Graph nodes per project", _hbar(sorted(g.get("repos", {}).items(), key=lambda kv: -kv[1])))
            + sec("Graph nodes per type", _hbar(detail.get("types", [])))
            + sec("Graph edges per relation", _hbar(detail.get("relations", [])))
            + sec("Storage", _hbar(stores, " MB", fmt=lambda v: f"{v:,.1f}"))
            + sec("Documents per project (docs search)", _hbar(docs["docs"]))
            + sec("Embedded chunks per project", _hbar(docs["chunks"]))
            + sec("Build time, last 30 builds (s)", _vbar(b, " s"))
            + sec("Average answer time per tool (s)", _hbar(latency, " s", fmt=lambda v: f"{v:.2f}"))
            + sec("Calls per day, last 14 days", _vbar([(day[5:], float(sum(v.values())), False)
                                                        for day, v in days], " calls"))
            + "</div></main>")
    html_bytes = page("KB admin · index", body, base, "Index", csrf)
    return html_bytes.replace(b"</style>", CHART_CSS.encode() + b"</style>", 1)
