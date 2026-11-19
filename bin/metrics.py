#!/usr/bin/env python3
"""Prometheus metrics for the KB: GET /metrics, off unless KB_METRICS=on.

Why a pull endpoint and nothing more: Prometheus, Grafana Agent, VictoriaMetrics and PMM all scrape
the same text format, so one page serves every setup.
  * A team with PMM adds it as an external exporter, for example
    `pmm-admin add external --service-name=kb --scheme=https --metrics-path=/kb/metrics
     --listen-port=443 --external-hostname=api-test.example.com --username=kb --password=<token>`.
  * Someone running the KB alone does not need any of it: the admin pages draw the same numbers from
    the KB's own small history (logs/index-history.json, logs/usage.json).
No Prometheus inside, no client library, no push gateway: the module is plain Python, imported only
when it is switched on, and costs nothing when it is off.

Access: from loopback without a token (a local Prometheus), otherwise KB_METRICS_TOKEN as a bearer
token or as the password of HTTP basic auth (what pmm-admin sends). Behind a proxy a request counts as
remote (it carries X-Forwarded-For), so it needs the token.
"""
from __future__ import annotations

import base64
import hmac
import os
import re

import admin
import kb_core

ENABLED = os.environ.get("KB_METRICS", "off").lower() == "on"
TOKEN = os.environ.get("KB_METRICS_TOKEN", "")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def allowed(client_ip: str, headers) -> bool:
    remote = bool(headers.get("X-Forwarded-For")) or client_ip not in LOOPBACK
    if not remote:
        return True
    if not TOKEN:
        return False
    auth = headers.get("Authorization") or ""
    supplied = ""
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    elif auth.lower().startswith("basic "):
        try:
            supplied = base64.b64decode(auth[6:].strip()).decode().partition(":")[2]
        except (ValueError, UnicodeDecodeError):
            supplied = ""
    return bool(supplied) and hmac.compare_digest(supplied.encode(), TOKEN.encode())


def _label(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class _Out:
    def __init__(self):
        self.lines: list[str] = []
        self.seen: set[str] = set()

    def metric(self, name: str, kind: str, help_: str, value, labels: dict | None = None) -> None:
        if value is None:
            return
        if name not in self.seen:
            self.lines += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
            self.seen.add(name)
        lab = ""
        if labels:
            lab = "{" + ",".join(f'{k}="{_label(v)}"' for k, v in labels.items()) + "}"
        self.lines.append(f"{name}{lab} {float(value):g}")


def render(usage=None, oauth_server=None) -> str:
    """The metrics page (text format 0.0.4)."""
    out = _Out()
    out.metric("kb_info", "gauge", "Version of the knowledge base.", 1,
               {"version": kb_core.VERSION, "contract": kb_core.API_CONTRACT})
    g = admin.graph_facts()
    if isinstance(g.get("nodes"), int):
        out.metric("kb_graph_nodes", "gauge", "Nodes in the code graph.", g["nodes"])
        out.metric("kb_graph_edges", "gauge", "Edges in the code graph.", g["edges"])
        for repo, n in g.get("repos", {}).items():
            out.metric("kb_graph_project_nodes", "gauge", "Graph nodes per project.", n, {"project": repo})
    detail = admin.graph_detail()
    for rel, n in detail.get("relations", []):
        out.metric("kb_graph_relation_edges", "gauge", "Graph edges per relation (top ones).", n, {"relation": rel})
    docs = admin.docs_facts()
    for project, n in docs["docs"]:
        out.metric("kb_docs", "gauge", "Documents in the docs search per project.", n, {"project": project})
    for project, n in docs["chunks"]:
        out.metric("kb_doc_chunks", "gauge", "Embedded chunks per project.", n, {"project": project})
    out_dir = os.path.dirname(kb_core.GRAPH)
    for label, path in (("graph", kb_core.GRAPH), ("caller_views", os.path.join(out_dir, "filtered")),
                        ("docs_search", docs["path"] or ""), ("staged_code", os.path.join(kb_core.KB, "repos")),
                        ("dashboard", os.path.join(out_dir, "kb-graph-data"))):
        if path and os.path.exists(path):
            out.metric("kb_storage_bytes", "gauge", "Disk used per index.", admin._size(path), {"index": label})
    b = admin.builds()
    if b:
        when, secs, bad = b[-1]
        out.metric("kb_build_last_seconds", "gauge", "Duration of the last build.", secs)
        out.metric("kb_build_last_success", "gauge", "1 when the last build succeeded.", 0 if bad else 1)
        out.metric("kb_builds_failed_recent", "gauge", "Failed builds among the last 30.",
                   sum(1 for *_, f in b if f))
    if usage is not None:
        d = usage.snapshot()
        for tool, t in d.get("tools", {}).items():
            out.metric("kb_tool_calls_total", "counter", "Tool calls.", t.get("calls", 0), {"tool": tool})
            out.metric("kb_tool_errors_total", "counter", "Tool calls that failed.", t.get("errors", 0), {"tool": tool})
            out.metric("kb_tool_seconds_total", "counter", "Time spent answering, seconds.", t.get("seconds", 0),
                       {"tool": tool})
            out.metric("kb_tool_p95_seconds", "gauge", "95th percentile answer time over the last calls.",
                       usage.p95(tool), {"tool": tool})
    out.metric("kb_cursors", "gauge", "Answers kept behind a cursor.", len(kb_core.CURSORS.items))
    if oauth_server is not None:
        out.metric("kb_oauth_clients", "gauge", "Registered sign-in clients.", len(oauth_server.clients))
    for name, w in (("graph_worker", admin.warm._graph), ("qmd", admin.warm._qmd)):
        proc = getattr(w, "proc", None) if w else None
        if proc is not None and proc.poll() is None:
            rss = admin._rss_mb(proc.pid)
            if rss:
                out.metric("kb_process_resident_bytes", "gauge", "Memory of the warm helpers.", rss * 1024 * 1024,
                           {"process": name})
    rss = admin._rss_mb(os.getpid())
    if rss:
        out.metric("kb_process_resident_bytes", "gauge", "Memory of the warm helpers.", rss * 1024 * 1024,
                   {"process": "server"})
    return "\n".join(out.lines) + "\n"


VALID = re.compile(r"^(# (HELP|TYPE) [a-z_][a-z0-9_]* .+|[a-z_][a-z0-9_]*(\{[^}]*\})? -?[0-9.e+-]+|)$")
