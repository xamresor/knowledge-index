#!/usr/bin/env python3
"""Tool usage counters, shared by every server (kb-http, kb-mcp, kb-api), in SQLite.

One row per tool call: when, which tool, who, which projects, ok or not, how long, the main argument.
The admin pages, `/metrics` and the `usage` tool read sums, per-day and per-hour counts, the most
frequent arguments and the percentiles from those rows. A local server counts as user "local".

Why SQLite (stdlib `sqlite3`, no dependency): several processes write the same counters — kb-api and
every Claude session's own kb-mcp. Each used to keep a JSON copy in memory and save it over the file,
so the last writer won and the others' calls were lost. SQLite in WAL mode takes appends from many
processes safely, and one row per call gives honest medians and p95 per tool and per project set.

The database sits next to the old JSON path (`usage-local.json` -> `usage-local.sqlite`). A JSON file
found there is imported once as totals (it has no per-call rows) and renamed `*.json.imported`.
Rows older than USAGE_DAYS are dropped.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time

USAGE_DAYS = 90       # rows older than this are dropped
USAGE_HOURS = 48      # per-hour counts for the sparklines
USAGE_TOP_ARGS = 200  # most frequent arguments kept per tool in a snapshot
RECENT = 200          # durations per tool for the percentiles

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  ts REAL NOT NULL, tool TEXT NOT NULL, user TEXT NOT NULL, project TEXT NOT NULL,
  ok INTEGER NOT NULL, seconds REAL NOT NULL, arg TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts);
CREATE INDEX IF NOT EXISTS calls_tool_ts ON calls (tool, ts);
CREATE TABLE IF NOT EXISTS legacy (snapshot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  ts REAL NOT NULL, user TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
"""


def _log(msg: str) -> None:
    sys.stderr.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    sys.stderr.flush()


def db_path(path: str) -> str:
    return re.sub(r"\.json$", "", path) + ".sqlite"


class Usage:
    """Counters of tool calls. `path` is the old JSON name; the database is `db_path(path)`."""

    def __init__(self, path: str, flush_every: float = 30.0):  # flush_every: kept for callers, unused
        self.path = db_path(path)
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(SCHEMA)
        self._import_json(path)
        self.db.execute("DELETE FROM calls WHERE ts < ?", (time.time() - USAGE_DAYS * 86400,))
        self.db.execute("DELETE FROM events WHERE ts < ?", (time.time() - USAGE_DAYS * 86400,))

    def _import_json(self, path: str) -> None:
        if not path.endswith(".json") or not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict) and old.get("tools"):
                self.db.execute("INSERT INTO legacy (snapshot) VALUES (?)", (json.dumps(old),))
            os.replace(path, path + ".imported")
        except (OSError, ValueError) as e:
            _log(f"usage: cannot import {path}: {e}")

    @staticmethod
    def arg_key(args: dict) -> str:
        """The main argument of a call, short: what people ask the KB about."""
        parts = [str(v) for k, v in sorted(args.items()) if isinstance(v, (str, int)) and k != "diff"]
        return " | ".join(" ".join(p.split()) for p in parts)[:120]

    def record(self, tool: str, user: str, args: dict, ok: bool, seconds: float) -> None:
        project = ",".join(p.strip() for p in str(args.get("project") or "all").split(",") if p.strip()) or "all"
        try:
            with self._lock:
                self.db.execute("INSERT INTO calls (ts, tool, user, project, ok, seconds, arg) VALUES (?,?,?,?,?,?,?)",
                                (time.time(), tool, user, project, 1 if ok else 0, round(seconds, 3),
                                 self.arg_key(args)))
        except sqlite3.Error as e:
            _log(f"usage: cannot record a call: {e}")

    def save(self) -> None:
        """Every call is written when it is recorded; kept so callers need not know."""

    # ------------------------------------------------------------------ audit
    def event(self, user: str, action: str, detail: str = "") -> None:
        """An audit event: a sign-in, a refused sign-in, an admin action (rebuild, revoke, sign-out).
        Nothing is written while the `audit` add-on is off."""
        try:
            import addons
            if not addons.on("audit"):
                return
        except Exception:  # noqa: BLE001
            return
        try:
            with self._lock:
                self.db.execute("INSERT INTO events (ts, user, action, detail) VALUES (?,?,?,?)",
                                (time.time(), str(user or "?")[:120], str(action)[:60], str(detail)[:300]))
        except sqlite3.Error as e:
            _log(f"usage: cannot record an event: {e}")

    def events(self, limit: int = 100, user: str = "") -> list[tuple]:
        """(when, user, action, detail), newest first."""
        sql, params = "SELECT ts, user, action, detail FROM events", []
        if user:
            sql += " WHERE user = ?"
            params.append(user)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return [(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), u, a, d)
                for ts, u, a, d in self._rows(sql, tuple(params))]

    def calls(self, limit: int = 200, user: str = "", tool: str = "", errors: bool = False) -> list[tuple]:
        """(when, user, tool, projects, ok, seconds, argument) of single calls, newest first."""
        where, params = [], []
        for col, val in (("user", user), ("tool", tool)):
            if val:
                where.append(f"{col} = ?")
                params.append(val)
        if errors:
            where.append("ok = 0")
        sql = "SELECT ts, user, tool, project, ok, seconds, arg FROM calls"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return [(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), u, t, p, bool(ok), s, a)
                for ts, u, t, p, ok, s, a in self._rows(sql, tuple(params))]

    # ------------------------------------------------------------------ reading
    def _rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self.db.execute(sql, params).fetchall()

    def p95(self, tool: str) -> float | None:
        r = sorted(s for (s,) in self._rows(
            "SELECT seconds FROM calls WHERE tool=? ORDER BY ts DESC LIMIT ?", (tool, RECENT)))
        if not r:
            r = sorted(self._legacy().get("recent", {}).get(tool) or [])
        return r[min(len(r) - 1, int(len(r) * 0.95))] if r else None

    def _legacy(self) -> dict:
        out: dict = {}
        for (snap,) in self._rows("SELECT snapshot FROM legacy"):
            try:
                out = _add(out, json.loads(snap))
            except ValueError:
                pass
        return out

    def snapshot(self) -> dict:
        """The counters in the shape the admin pages and /metrics read (JSON-compatible)."""
        d: dict = {"since": time.strftime("%Y-%m-%d"), "tools": {}, "days": {}, "users": {}, "args": {},
                   "hours": {}, "projects": {}, "recent": {}}
        rows = self._rows("SELECT ts, tool, user, project, ok, seconds, arg FROM calls ORDER BY ts")
        if rows:
            d["since"] = time.strftime("%Y-%m-%d", time.localtime(rows[0][0]))
        hour_cut = time.time() - USAGE_HOURS * 3600
        for ts, tool, user, project, ok, seconds, arg in rows:
            lt = time.localtime(ts)
            t = d["tools"].setdefault(tool, {"calls": 0, "errors": 0, "seconds": 0.0})
            t["calls"] += 1
            t["errors"] += 0 if ok else 1
            t["seconds"] += seconds
            day = d["days"].setdefault(time.strftime("%Y-%m-%d", lt), {})
            day[tool] = day.get(tool, 0) + 1
            if ts >= hour_cut:
                h = d["hours"].setdefault(time.strftime("%Y-%m-%d %H", lt), {})
                h[tool] = h.get(tool, 0) + 1
            u = d["users"].setdefault(user, {})
            u[tool] = u.get(tool, 0) + 1
            for p in project.split(","):
                pr = d["projects"].setdefault(p, {})
                pr[tool] = pr.get(tool, 0) + 1
            if arg:
                a = d["args"].setdefault(tool, {})
                a[arg] = a.get(arg, 0) + 1
            d["recent"].setdefault(tool, []).append(seconds)
        for t in d["tools"].values():
            t["seconds"] = round(t["seconds"], 1)
        for tool, r in d["recent"].items():
            d["recent"][tool] = r[-RECENT:]
        for tool, a in d["args"].items():
            d["args"][tool] = dict(sorted(a.items(), key=lambda kv: -kv[1])[:USAGE_TOP_ARGS])
        legacy = self._legacy()
        if legacy:
            since = min(d["since"], str(legacy.get("since") or d["since"]))
            d = _add(d, {k: v for k, v in legacy.items() if k not in ("hours", "recent", "since")})
            d["since"] = since
        return d

    @property
    def data(self) -> dict:
        return self.snapshot()

    def report(self, with_users: bool, days: int = 30, top: int = 15) -> str:
        d = self.snapshot()
        if not d["tools"]:
            return f"(no tool calls since {d['since']})"
        lines = [f"KB tool usage since {d['since']}", "", "Tool               calls  errors  avg s    p95 s"]
        for name, t in sorted(d["tools"].items(), key=lambda kv: -kv[1]["calls"]):
            avg = t["seconds"] / t["calls"] if t["calls"] else 0
            p95 = self.p95(name)
            lines.append(f"{name:<18} {t['calls']:>5}  {t['errors']:>6}  {avg:>5.1f}  "
                         + (f"{p95:>7.2f}" if p95 is not None else "      -"))
        recent = sorted(d["days"])[-days:]
        lines += ["", f"Last {len(recent)} day(s): "
                  + ", ".join(f"{day} {sum(d['days'][day].values())}" for day in recent)]
        for name, args in sorted(d["args"].items()):
            best = sorted(args.items(), key=lambda kv: -kv[1])[:top]
            lines += ["", f"Top {name} arguments:"] + [f"  {n:>4}  {a}" for a, n in best]
        if with_users:
            lines += ["", "Users:"]
            for user, tools in sorted(d["users"].items(), key=lambda kv: -sum(kv[1].values())):
                lines.append(f"  {user}: {sum(tools.values())} ("
                             + ", ".join(f"{k} {v}" for k, v in sorted(tools.items())) + ")")
        return "\n".join(lines)

    def slow(self, limit: int = 20, since_hours: float = 48) -> list[tuple]:
        """The slowest calls lately: (when, tool, user, project, seconds, argument)."""
        return [(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), tool, user, project, seconds, arg)
                for ts, tool, user, project, seconds, arg in self._rows(
                    "SELECT ts, tool, user, project, seconds, arg FROM calls WHERE ts >= ? "
                    "ORDER BY seconds DESC LIMIT ?", (time.time() - since_hours * 3600, limit))]


def _add(a, b):
    """Sum two counter trees (numbers add, dicts merge)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _add(out[k], v) if k in out else v
        return out
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        r = a + b
        return round(r, 1) if isinstance(r, float) else r
    return a if a is not None else b
