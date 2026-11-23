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
Rows older than USAGE_DAYS are dropped, at the start and once an hour.
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
PRUNE_EVERY = 3600    # seconds between two drops of old rows in a running server
SNAPSHOT_SECONDS = 5  # a snapshot is reused this long: /metrics scrapes and page loads do not each scan

# The main argument of each tool: the "question" a call asks. A tool not named here keys on its string
# arguments without the paging and option ones.
MAIN_ARGS = {"search": ("q",), "contracts_search": ("query",), "code_grep": ("pattern",),
             "graph_explain": ("symbol",), "graph_affected": ("symbol",), "graph_path": ("from", "to"),
             "graph_routes": ("from", "to"), "docs_get": ("path",), "connections": ("service",)}
NOT_THE_QUESTION = {"project", "page", "page_length", "cursor", "limit", "budget", "domain", "scope", "mode",
                    "min_score", "k", "glob", "diff", "files", "code", "calls"}

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
CREATE TABLE IF NOT EXISTS labels (
  tool TEXT NOT NULL, arg TEXT NOT NULL, project TEXT NOT NULL,
  expected TEXT NOT NULL DEFAULT '', no_answer INTEGER NOT NULL DEFAULT 0, skip INTEGER NOT NULL DEFAULT 0,
  by TEXT NOT NULL DEFAULT '', ts REAL NOT NULL,
  PRIMARY KEY (tool, arg, project));
"""

# The tools whose main argument is a question with one right answer: the calls of these tools are the
# questions an admin labels on the Questions page, and the labelled ones are the golden set from real use.
QUESTION_TOOLS = ("search", "contracts_search", "code_grep")


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
        self._snap: tuple[float, dict] | None = None
        self._pruned = 0.0
        self._prune()

    def _prune(self) -> None:
        """Drop the rows older than USAGE_DAYS: at the start and then once per PRUNE_EVERY seconds."""
        cut = time.time() - USAGE_DAYS * 86400
        try:
            with self._lock:
                self.db.execute("DELETE FROM calls WHERE ts < ?", (cut,))
                self.db.execute("DELETE FROM events WHERE ts < ?", (cut,))
        except sqlite3.Error as e:
            _log(f"usage: cannot drop old rows: {e}")
        self._pruned = time.monotonic()

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
    def arg_key(args: dict, tool: str = "") -> str:
        """The main argument of a call, short: what people ask the KB about. One question is one key
        whatever its page, limit or options: the project has its own column, and a call that only turns a
        page (a cursor) asks no new question, so it has no key. Old rows keep the old key (all string and
        number arguments joined by " | "): they stay readable and count apart."""
        if not isinstance(args, dict) or args.get("cursor"):
            return ""
        names = [k for k in MAIN_ARGS.get(tool, ()) if k in args] \
            or [k for k in ("q", "query", "pattern") if k in args][:1] \
            or sorted(k for k in args if k not in NOT_THE_QUESTION)
        parts = [args[k] for k in names if isinstance(args.get(k), (str, int)) and not isinstance(args.get(k), bool)]
        return " → ".join(" ".join(str(p).split()) for p in parts if str(p).strip())[:120]

    def record(self, tool: str, user: str, args: dict, ok: bool, seconds: float) -> None:
        project = ",".join(p.strip() for p in str(args.get("project") or "all").split(",") if p.strip()) or "all"
        try:
            with self._lock:
                self.db.execute("INSERT INTO calls (ts, tool, user, project, ok, seconds, arg) VALUES (?,?,?,?,?,?,?)",
                                (time.time(), tool, user, project, 1 if ok else 0, round(seconds, 3),
                                 self.arg_key(args, tool)))
                self._snap = None
        except sqlite3.Error as e:
            _log(f"usage: cannot record a call: {e}")
        if time.monotonic() - self._pruned > PRUNE_EVERY:
            self._prune()

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

    # ------------------------------------------------------------------ questions and their labels
    def questions(self, limit: int = 200) -> list[dict]:
        """The questions people asked (calls of QUESTION_TOOLS with an argument), one row per question:
        how often, when last, by whom, and the label an admin gave it. Not labelled first, then the newest.
        A labelled question stays in the list after its calls are deleted (calls are kept USAGE_DAYS)."""
        marks = ",".join("?" * len(QUESTION_TOOLS))
        asked = self._rows(
            f"SELECT tool, arg, project, COUNT(*), MAX(ts), GROUP_CONCAT(DISTINCT user) FROM calls "
            f"WHERE tool IN ({marks}) AND arg != '' GROUP BY tool, arg, project", QUESTION_TOOLS)
        labels = {(t, a, p): (e, bool(n), bool(k), by, ts) for t, a, p, e, n, k, by, ts in self._rows(
            "SELECT tool, arg, project, expected, no_answer, skip, by, ts FROM labels")}
        rows = {(t, a, p): {"tool": t, "arg": a, "project": p, "count": n, "last": ts, "users": u or ""}
                for t, a, p, n, ts, u in asked}
        for key, (expected, no_answer, skip, by, ts) in labels.items():
            row = rows.setdefault(key, {"tool": key[0], "arg": key[1], "project": key[2], "count": 0,
                                        "last": ts, "users": ""})
            row.update(expected=expected, no_answer=no_answer, skip=skip, by=by, labelled=True)
        out = sorted(rows.values(), key=lambda r: (bool(r.get("labelled")), -r["last"]))
        return out[:limit]

    def unlabelled(self) -> int:
        """How many asked questions have no label yet: the number beside the Questions tab."""
        marks = ",".join("?" * len(QUESTION_TOOLS))
        return self._rows(
            f"SELECT COUNT(*) FROM (SELECT DISTINCT tool, arg, project FROM calls WHERE tool IN ({marks}) "
            f"AND arg != '') c WHERE NOT EXISTS (SELECT 1 FROM labels l WHERE l.tool = c.tool AND l.arg = c.arg "
            f"AND l.project = c.project)", QUESTION_TOOLS)[0][0]

    def label(self, tool: str, arg: str, project: str, expected: str = "", no_answer: bool = False,
              skip: bool = False, by: str = "") -> bool:
        """Save the label of one question: the document or symbol that must be found, or "the corpus
        has no answer", or "not a question, skip". False when nothing of the three is given."""
        expected = " ".join(expected.split())[:300]
        if tool not in QUESTION_TOOLS or not arg or not (expected or no_answer or skip):
            return False
        with self._lock:
            self.db.execute(
                "INSERT INTO labels (tool, arg, project, expected, no_answer, skip, by, ts) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT (tool, arg, project) DO UPDATE SET expected = excluded.expected, "
                "no_answer = excluded.no_answer, skip = excluded.skip, by = excluded.by, ts = excluded.ts",
                (tool, arg, project or "all", "" if (no_answer or skip) else expected, 1 if no_answer else 0,
                 1 if skip else 0, by, time.time()))
        return True

    def unlabel(self, tool: str, arg: str, project: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM labels WHERE tool = ? AND arg = ? AND project = ?",
                            (tool, arg, project or "all"))

    def golden(self) -> list[dict]:
        """The labelled questions as a golden set: skipped ones are left out."""
        return [{"tool": t, "question": a, "project": p, "expected": e, "no_answer": bool(n)}
                for t, a, p, e, n in self._rows(
                    "SELECT tool, arg, project, expected, no_answer FROM labels WHERE skip = 0 ORDER BY ts")]

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
        """The counters in the shape the admin pages and /metrics read (JSON-compatible). SQLite sums
        the rows (GROUP BY); the result is reused for SNAPSHOT_SECONDS, and a call recorded by this process
        makes a new one."""
        with self._lock:
            snap = self._snap
        if snap is not None and time.monotonic() - snap[0] < SNAPSHOT_SECONDS:
            return json.loads(json.dumps(snap[1]))  # a copy: callers change what they get
        d = self._snapshot()
        with self._lock:
            self._snap = (time.monotonic(), d)
        return json.loads(json.dumps(d))

    def _snapshot(self) -> dict:
        d: dict = {"since": time.strftime("%Y-%m-%d"), "tools": {}, "days": {}, "users": {}, "args": {},
                   "hours": {}, "projects": {}, "recent": {}}
        first = self._rows("SELECT MIN(ts) FROM calls")[0][0]
        if first is not None:
            d["since"] = time.strftime("%Y-%m-%d", time.localtime(first))
        for tool, calls, errors, seconds in self._rows(
                "SELECT tool, COUNT(*), SUM(1 - ok), SUM(seconds) FROM calls GROUP BY tool"):
            d["tools"][tool] = {"calls": calls, "errors": errors, "seconds": round(seconds or 0.0, 1)}
        for day, tool, n in self._rows(
                "SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS day, tool, COUNT(*) FROM calls "
                "GROUP BY day, tool"):
            d["days"].setdefault(day, {})[tool] = n
        for hour, tool, n in self._rows(
                "SELECT strftime('%Y-%m-%d %H', ts, 'unixepoch', 'localtime') AS hour, tool, COUNT(*) FROM calls "
                "WHERE ts >= ? GROUP BY hour, tool", (time.time() - USAGE_HOURS * 3600,)):
            d["hours"].setdefault(hour, {})[tool] = n
        for user, tool, n in self._rows("SELECT user, tool, COUNT(*) FROM calls GROUP BY user, tool"):
            d["users"].setdefault(user, {})[tool] = n
        for project, tool, n in self._rows("SELECT project, tool, COUNT(*) FROM calls GROUP BY project, tool"):
            for p in project.split(","):
                pr = d["projects"].setdefault(p, {})
                pr[tool] = pr.get(tool, 0) + n
        for tool, arg, n in self._rows(
                "SELECT tool, arg, n FROM (SELECT tool, arg, COUNT(*) AS n, ROW_NUMBER() OVER "
                "(PARTITION BY tool ORDER BY COUNT(*) DESC) AS r FROM calls WHERE arg != '' GROUP BY tool, arg) "
                "WHERE r <= ? ORDER BY tool, n DESC", (USAGE_TOP_ARGS,)):
            d["args"].setdefault(tool, {})[arg] = n
        for tool, seconds in self._rows(
                "SELECT tool, seconds FROM (SELECT tool, seconds, ts, ROW_NUMBER() OVER "
                "(PARTITION BY tool ORDER BY ts DESC) AS r FROM calls) WHERE r <= ? ORDER BY tool, ts", (RECENT,)):
            d["recent"].setdefault(tool, []).append(seconds)
        legacy = self._legacy()
        if legacy:
            since = min(d["since"], str(legacy.get("since") or d["since"]))
            d = _add(d, {k: v for k, v in legacy.items() if k not in ("hours", "recent", "since")})
            d["since"] = since
        return d

    @property
    def data(self) -> dict:
        return self.snapshot()

    def own_args(self, user: str) -> dict:
        """{tool: {argument: count}} of one user's calls: what the MCP tool shows instead of everyone's."""
        out: dict = {}
        for tool, arg in self._rows("SELECT tool, arg FROM calls WHERE user = ? AND arg != ''", (user,)):
            a = out.setdefault(tool, {})
            a[arg] = a.get(arg, 0) + 1
        return out

    def report(self, with_users: bool, days: int = 30, top: int = 15, only_user: str | None = None) -> str:
        """with_users: the user list (the CLI). only_user: the arguments are that user's own, never anyone
        else's (the MCP tool: callers see the totals, but not what their colleagues asked)."""
        d = self.snapshot()
        if only_user is not None:
            d["args"] = self.own_args(only_user)
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
            title = f"Your top {name} arguments:" if only_user is not None else f"Top {name} arguments:"
            lines += ["", title] + [f"  {n:>4}  {a}" for a, n in best]
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
