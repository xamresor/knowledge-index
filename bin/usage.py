#!/usr/bin/env python3
"""Tool usage counters, shared by every server (kb-http, kb-mcp, kb-api).

Per tool: calls, errors, total seconds and the last RECENT durations (for p95). Per day and per hour
(the last 48 hours, for the sparklines of the admin pages): calls per tool. Per user and per project:
calls per tool. The most frequent arguments per tool. Kept in memory, saved to a JSON file at most every
`flush_every` seconds. A local server counts as user "local".
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

USAGE_DAYS = 90       # per-day counters older than this are dropped
USAGE_HOURS = 48      # per-hour counters kept for the sparklines
USAGE_TOP_ARGS = 200  # most frequent arguments kept per tool
RECENT = 200          # durations kept per tool for the percentiles


def _log(msg: str) -> None:
    sys.stderr.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    sys.stderr.flush()


class Usage:
    """Counters of tool calls: per tool, per day, per user, and the most frequent arguments.
    Kept in memory and saved to a JSON file at most every `flush_every` seconds."""

    def __init__(self, path: str, flush_every: float = 30.0):
        self.path = path
        self.flush_every = flush_every
        self._lock = threading.Lock()
        self._saved_at = time.monotonic()
        self.data = {"since": time.strftime("%Y-%m-%d"), "tools": {}, "days": {}, "users": {},
                     "args": {}, "hours": {}, "projects": {}, "recent": {}}
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update({k: v for k, v in loaded.items() if k in self.data})
        except (OSError, ValueError):
            pass

    @staticmethod
    def arg_key(args: dict) -> str:
        """The main argument of a call, short: what people ask the KB about."""
        parts = [str(v) for k, v in sorted(args.items()) if isinstance(v, (str, int))]
        return " | ".join(" ".join(p.split()) for p in parts)[:120]

    def record(self, tool: str, user: str, args: dict, ok: bool, seconds: float) -> None:
        day = time.strftime("%Y-%m-%d")
        hour = time.strftime("%Y-%m-%d %H")
        with self._lock:
            d = self.data
            d["hours"].setdefault(hour, {})
            d["hours"][hour][tool] = d["hours"][hour].get(tool, 0) + 1
            for old in sorted(d["hours"])[:-USAGE_HOURS]:
                del d["hours"][old]
            r = d["recent"].setdefault(tool, [])
            r.append(round(seconds, 3))
            del r[:-RECENT]
            for project in str(args.get("project") or "all").split(","):
                pr = d["projects"].setdefault(project.strip() or "all", {})
                pr[tool] = pr.get(tool, 0) + 1
            t = d["tools"].setdefault(tool, {"calls": 0, "errors": 0, "seconds": 0.0})
            t["calls"] += 1
            t["errors"] += 0 if ok else 1
            t["seconds"] = round(t["seconds"] + seconds, 1)
            d["days"].setdefault(day, {})
            d["days"][day][tool] = d["days"][day].get(tool, 0) + 1
            u = d["users"].setdefault(user, {})
            u[tool] = u.get(tool, 0) + 1
            key = self.arg_key(args)
            if key:
                a = d["args"].setdefault(tool, {})
                a[key] = a.get(key, 0) + 1
                if len(a) > USAGE_TOP_ARGS * 2:  # keep the most frequent ones
                    d["args"][tool] = dict(sorted(a.items(), key=lambda kv: -kv[1])[:USAGE_TOP_ARGS])
            if len(d["days"]) > USAGE_DAYS:
                for old in sorted(d["days"])[:-USAGE_DAYS]:
                    del d["days"][old]
            due = time.monotonic() - self._saved_at >= self.flush_every
        if due:
            self.save()

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self.data)
            self._saved_at = time.monotonic()
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self.path)
        except OSError as e:
            _log(f"usage: cannot save {self.path}: {e}")

    def p95(self, tool: str) -> float | None:
        with self._lock:
            r = sorted(self.data["recent"].get(tool, []))
        return r[min(len(r) - 1, int(len(r) * 0.95))] if r else None

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.data))

    def report(self, with_users: bool, days: int = 30, top: int = 15) -> str:
        with self._lock:
            d = json.loads(json.dumps(self.data))
        if not d["tools"]:
            return f"(no tool calls since {d['since']})"
        lines = [f"KB tool usage since {d['since']}", "", "Tool               calls  errors  avg s"]
        for name, t in sorted(d["tools"].items(), key=lambda kv: -kv[1]["calls"]):
            avg = t["seconds"] / t["calls"] if t["calls"] else 0
            lines.append(f"{name:<18} {t['calls']:>5}  {t['errors']:>6}  {avg:>5.1f}")
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
