#!/usr/bin/env python3
"""Emit the viz **data** for the shell in `web/` — hierarchical, lazily loaded (vis-network).

The interface itself is no longer here. Until 0.5.0 this file carried the whole page as a `TEMPLATE`
string with four `__PLACEHOLDER__` substitutions and rendered it into git-ignored `graphify-out/`,
which meant the UI could not be diffed, linted or tested. Now the split is by role:

  * `web/` (versioned)  — the shell: markup, styling, behaviour. No build step.
  * this script         — data only, written next to the graph:

        kb-manifest.js                 kbManifest({domains, superNodes, fileMap, dataPath, nodeCount})
        kb-status.js                   kbStatus({graph, docs, aliases, quality, versions, takenAt})
        kb-graph-data/<domain>.js      kbRecv(domain, {nodes, edges})       loaded on demand
        kb-graph-data/_cross.js        kbCross([[from, fromDom, to, toDom, relation], …])
        kb-graph-data/_index.js        kbIndex([[label, id, domain], …])    loaded on first search

`.js` calls rather than JSON + fetch is deliberate: a page opened from `file://` cannot fetch a
sibling file but can load a sibling `<script>`, so the whole thing works with nothing running.

  node SHAPE = type        node COLOR = domain
  edge color = scope:      red = route (http_request) · blue = cross-domain · green = internal

Usage: render_viz.py <graph.json> [<out_dir>]        (out_dir defaults to the graph's directory)
"""
from __future__ import annotations

import colorsys
import datetime
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict

import graph as graph_io
import paths

# node type -> vis-network shape (the legend text lives in web/app.js, next to the rendering)
SHAPE = {
    "class": "dot", "controller": "hexagon", "service": "diamond", "repository": "diamond",
    "resource": "box", "request": "box", "model": "database",
    "db_table": "square", "method": "triangle", "function": "triangle",
    "interface": "diamond", "trait": "diamond", "enum": "star", "template": "star",
    "file": "dot", "symbol": "dot", "rationale": "text", "doc": "ellipse",
}
BIG = {"controller", "model", "db_table"}     # types drawn a bit larger
RED, BLUE, GREEN = "#e15759", "#4e79a7", "#59a14f"

DATA_DIR = "kb-graph-data"
MANIFEST = "kb-manifest.js"
STATUS = "kb-status.js"


def color_for(domain: str) -> str:
    h = (hash(domain) % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(h, 0.55, 0.65)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _js_call(fn: str, *args) -> str:
    """One generated file = one call into the shell. `</script` is escaped so a label containing it
    cannot end the script tag early."""
    payload = ",".join(json.dumps(a, ensure_ascii=False) for a in args)
    return f"{fn}({payload});\n".replace("</script", "<\\/script")


def write_js(path: str, fn: str, *args) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_js_call(fn, *args))


def build_members(real: list[dict], dom_of: dict, palette: dict) -> dict:
    """Per-domain vis-network node payloads."""
    members = defaultdict(list)
    for n in real:
        d = dom_of[n["id"]]
        typ = n.get("type", "symbol")
        col = palette[d]
        members[d].append({
            "id": n["id"], "label": n["label"], "shape": SHAPE.get(typ, "dot"), "group": d,
            "color": {"background": col, "border": col,
                      "highlight": {"background": "#fff", "border": col}},
            "size": 14 if typ in BIG else 9,
            "font": {"size": 11, "color": "#eee", "strokeWidth": 3, "strokeColor": "#111"},
            "title": f'{n["label"]} · type={typ} · domain={d} · repo={n.get("repo", "?")}',
        })
    return members


def split_edges(links: list[dict], by_id: dict, dom_of: dict) -> tuple[dict, list]:
    """Intra-domain edges (drawn with the domain) vs cross-domain edges (rerouted on expand)."""
    intra, cross = defaultdict(list), []
    for e in links:
        rel = e.get("relation", "")
        s, t = e["source"], e["target"]
        if rel == "in_domain" or s not in by_id or t not in by_id:
            continue
        ds, dt = dom_of[s], dom_of[t]
        if ds != dt:
            cross.append([s, ds, t, dt, rel])
            continue
        http = rel == "http_request"
        edge = {"from": s, "to": t,
                "color": {"color": RED if http else GREEN, "opacity": 0.6 if http else 0.4},
                "width": 3 if http else 1}
        if e.get("confidence") == "AMBIGUOUS":
            edge["dashes"] = True  # heuristic edge — visibly less trustworthy
        if http:
            edge["arrows"] = "to"
        intra[ds].append(edge)
    return intra, cross


def super_nodes_for(domains: list[str], counts: Counter, palette: dict) -> list[dict]:
    return [{"id": f"domain:{d}", "label": f"{d} ({counts[d]})", "shape": "dot", "domkey": d,
             "color": {"background": palette[d], "border": "#111"},
             "size": min(70, 14 + counts[d] / 4), "borderWidth": 3,
             "font": {"size": 18, "color": "#fff", "strokeWidth": 4, "strokeColor": "#111"},
             "title": f"{d} — {counts[d]} nodes (double-click to expand)"} for d in domains]


def _cli_version(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip().splitlines()[0] if out.stdout or out.stderr else "?"
    except Exception:  # noqa: BLE001
        return "not found"


def _qmd_collection_stats(collection: str) -> dict:
    """Files + freshness for our collection, parsed out of `qmd status`. Best-effort: a missing qmd
    must leave the panel empty rather than fail the build."""
    try:
        out = subprocess.run(["qmd", "status"], capture_output=True, text=True, timeout=60).stdout
    except Exception:  # noqa: BLE001
        return {}
    block = re.search(rf"^\s*{re.escape(collection)} \(qmd://.*?\n((?:\s{{4}}.*\n)+)",
                      out, re.MULTILINE)
    if not block:
        return {"collection": collection}
    files = re.search(r"Files:\s+(\d+)\s*(?:\(updated ([^)]+)\))?", block.group(1))
    stats = {"collection": collection}
    if files:
        stats["files"] = int(files.group(1))
        if files.group(2):
            stats["updated"] = files.group(2)
    return stats


def _aliases() -> list[list[str]]:
    """The alias table, for the status panel: renames are knowledge, not just configuration."""
    try:
        from alias_expand import load_aliases
        return [[src, dst] for src, dst in load_aliases()]
    except Exception:  # noqa: BLE001
        return []


def _quality(kb_root: str) -> dict:
    """Search-quality numbers, if a benchmark result was left next to the index."""
    path = os.path.join(kb_root, "graphify-out", "bench-latest.json")
    if not os.path.isfile(path):
        return {}
    try:
        data = graph_io.read_json(path)
        summary = data.get("summary", {})
        best = max(summary.items(), key=lambda kv: kv[1].get("avg_recall", 0), default=None)
        if not best:
            return {}
        name, metrics = best
        return {"measured_at": data.get("timestamp", "?"),
                "metrics": {f"{name} recall": round(metrics.get("avg_recall", 0), 3),
                            f"{name} MRR": round(metrics.get("avg_mrr", 0), 3)}}
    except Exception:  # noqa: BLE001
        return {}


def write_status(out_dir: str, kb_root: str, g: dict, domains: list[str], counts: Counter,
                 graph_path: str) -> None:
    """A build-time snapshot for the Status panel. Every block is stamped: a stale number that admits
    its age is useful, one that pretends to be live is not."""
    built = datetime.datetime.fromtimestamp(os.path.getmtime(graph_path)).strftime("%Y-%m-%d %H:%M")
    projects = len({n.get("repo") for n in g["nodes"] if n.get("repo")})
    version_file = os.path.join(kb_root, "VERSION")
    try:
        repo_version = open(version_file, encoding="utf-8").read().strip()
    except OSError:
        repo_version = "0+unknown"
    payload = {
        "graph": {"nodes": len(g["nodes"]), "edges": len(g["links"]),
                  "domains": len(domains), "projects": projects, "built_at": built},
        "docs": _qmd_collection_stats(os.environ.get("KB_COLLECTION", "kb")),
        "aliases": _aliases(),
        "quality": _quality(kb_root),
        "versions": {"knowledge-index": repo_version,
                     "graphify": _cli_version("graphify"), "qmd": _cli_version("qmd")},
        "takenAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    write_js(os.path.join(out_dir, STATUS), "kbStatus", payload)


def main() -> int:
    graph_path = sys.argv[1] if len(sys.argv) > 1 else str(paths.graph_path())
    out_dir = sys.argv[2] if len(sys.argv) > 2 else str(paths.out_dir())
    # The shell must sit next to the data (a file:// page loads siblings, it cannot fetch them).
    staged = paths.stage_web()
    kb_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(out_dir, DATA_DIR)
    os.makedirs(data_dir, exist_ok=True)
    g = graph_io.load(graph_path)

    real = [n for n in g["nodes"] if n.get("type") != "domain"]
    by_id = {n["id"]: n for n in real}
    dom_of = {n["id"]: n.get("domain", "misc") for n in real}
    domains = sorted(set(dom_of.values()))
    palette = {d: color_for(d) for d in domains}
    counts = Counter(dom_of.values())
    fname = {d: safe(d) for d in domains}

    members = build_members(real, dom_of, palette)
    intra, cross = split_edges(g["links"], by_id, dom_of)

    for d in domains:
        write_js(os.path.join(data_dir, fname[d] + ".js"), "kbRecv", d,
                 {"nodes": members[d], "edges": intra[d]})
    write_js(os.path.join(data_dir, "_cross.js"), "kbCross", cross)
    write_js(os.path.join(data_dir, "_index.js"), "kbIndex",
             [[n["label"], n["id"], dom_of[n["id"]]] for n in real])

    write_js(os.path.join(out_dir, MANIFEST), "kbManifest", {
        "domains": domains,
        "superNodes": super_nodes_for(domains, counts, palette),
        "fileMap": fname,
        "dataPath": f"../{os.path.basename(out_dir)}/{DATA_DIR}/",
        "nodeCount": len(real),
        "generatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    write_status(out_dir, kb_root, g, domains, counts, graph_path)

    print(f"viz data: {len(domains)} domain files + _cross ({len(cross)}) + _index "
          f"+ {MANIFEST} + {STATUS} -> {out_dir}/  (shell: {staged}/index.html)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
