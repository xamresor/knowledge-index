#!/usr/bin/env python3
"""Enrich a merged graphify graph with node TYPE, DOMAIN, db_table nodes and domain anchors.

Adds, per node:
  - type   : class | controller | model | service | repository | resource | request
             | interface | enum | trait | method | template | file | symbol
  - domain : entity/component/package the node belongs to (Vehicle, User, SmartCamera, ...)

Adds nodes/edges (rethought link logic, fixes orphans + builds domain clusters):
  - db_table nodes  : one per model $table, with model --defines_table--> table
  - domain anchors  : one hub per domain, with every member --in_domain--> anchor

Run AFTER graphify clustering/labeling so graphify never strips these custom fields.
Usage: enrich.py <graph.json> <repos_dir>
"""
from __future__ import annotations

import json
import os
import re
import sys

LAYER_SUFFIX = re.compile(
    r"(Controller|Resource|Request|Service|Repository|Policy|Observer|Factory|Seeder|"
    r"Cast|Enum|Type|Job|Listener|Event|Command|Middleware|Exception|Provider|Mixin|"
    r"Collection|Trait|Interface|Test|Handler|Builder|Manager|Action|Rule|Scope)$"
)
BARE_CLASS = re.compile(r"^[A-Z][A-Za-z0-9]*$")


def node_type(n: dict) -> str:
    lab, sf = n["label"], (n.get("source_file") or "")
    if lab.startswith(".") and lab.endswith("()"):
        return "method"
    if sf.endswith(".vue") or lab.endswith(".vue"):
        return "template"
    if lab.endswith((".php", ".js", ".ts")):
        return "file"
    if BARE_CLASS.match(lab):
        for suf, t in (("Controller", "controller"), ("Resource", "resource"),
                       ("Request", "request"), ("Service", "service"),
                       ("Repository", "repository"), ("Interface", "interface"),
                       ("Trait", "trait")):
            if lab.endswith(suf):
                return t
        if lab.endswith("Enum") or "/Enums/" in sf or "/Enum/" in sf:
            return "enum"
        if "/Models/" in sf or sf.startswith("app/Models") or "\\Models\\" in sf:
            return "model"
        return "class"
    return "symbol"


def entity(name: str) -> str:
    """VehicleController -> Vehicle ; UserResource -> User ; keep already-bare names."""
    prev = None
    while prev != name:
        prev = name
        name = LAYER_SUFFIX.sub("", name)
    return name or prev


def layer_bucket(sf: str, repo: str) -> str:
    """Coarse fallback domain for nodes without a strong entity/module signal."""
    p = sf.lstrip("/").split("/")
    if repo in ("webclient", "bo") or (p and p[0] in ("src", "assets-js")):
        area = p[1] if len(p) > 1 else "app"
        return f"{repo}:{area}"           # webclient:stores, bo:pages, ...
    if p and p[0] == "app" and len(p) > 1:
        return f"api:{p[1]}"              # api:Services, api:Http, api:Jobs, ...
    return f"{repo}:core"


def raw_domain(n: dict) -> str:
    sf = (n.get("source_file") or "").lstrip("/")
    p = sf.split("/")
    # packages/<Pkg> and app/Components/<Comp> are explicit module domains
    if p and p[0] in ("packages", "packages-dev") and len(p) > 1:
        return p[1]
    if len(p) > 2 and p[0] == "app" and p[1] == "Components":
        return p[2]
    # entity from the class name (Vehicle, User, Equipment, ...)
    lab = n["label"]
    base = None
    if BARE_CLASS.match(lab):
        base = entity(lab)
    elif lab.endswith((".php", ".vue", ".js", ".ts")):
        base = entity(os.path.splitext(lab)[0])
    elif lab.startswith(".") and "_" in n.get("id", ""):
        cls = n["id"].split("::")[-1].rsplit("_", 1)[0].split("_")[-1]
        base = cls.capitalize()
    if base and len(base) > 2:
        return base
    return layer_bucket(sf, n.get("repo", "misc"))


def find_tables(repos_dir: str) -> dict[str, str]:
    """class name -> table, scanned from staged model files ($table = '...')."""
    tbl = {}
    pat_t = re.compile(r"protected\s+\$table\s*=\s*'([^']+)'")
    pat_c = re.compile(r"class\s+(\w+)")
    for dp, _, files in os.walk(repos_dir):
        if "/Models" not in dp and "/Model" not in dp:
            continue
        for fn in files:
            if not fn.endswith(".php"):
                continue
            try:
                txt = open(os.path.join(dp, fn), encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            mt, mc = pat_t.search(txt), pat_c.search(txt)
            if mt and mc:
                tbl[mc.group(1)] = mt.group(1)
    return tbl


def main() -> int:
    graph_path, repos_dir = sys.argv[1], sys.argv[2]
    g = json.load(open(graph_path))
    N, L = g["nodes"], g["links"]
    by_id = {n["id"]: n for n in N}

    from collections import Counter
    MIN_DOMAIN = 5  # entity domains smaller than this fold into a coarse layer bucket

    for n in N:
        n["type"] = node_type(n)
        n["_raw_domain"] = raw_domain(n)

    # propagate domain down structural edges (file -> class -> method), so a method/member
    # inherits its container's domain instead of a noisy name-derived guess
    raw = {n["id"]: n["_raw_domain"] for n in N}
    pc = [(e["source"], e["target"]) for e in L
          if e.get("relation") in ("contains", "method")]
    for _ in range(4):
        for s, t in pc:
            if s in raw and t in raw:
                raw[t] = raw[s]
    for n in N:
        n["_raw_domain"] = raw[n["id"]]

    # consolidate: keep domains with >= MIN members; fold the long tail of singletons
    counts = Counter(n["_raw_domain"] for n in N)
    for n in N:
        rd = n["_raw_domain"]
        n["domain"] = rd if counts[rd] >= MIN_DOMAIN else layer_bucket(
            n.get("source_file", ""), n.get("repo", "misc"))
        del n["_raw_domain"]

    # db_table nodes + model->table edges
    tables = find_tables(repos_dir)
    table_nodes, new_links = {}, []
    for n in N:
        if n["type"] == "model" and n["label"] in tables:
            t = tables[n["label"]]
            tid = f"db::{t}"
            if tid not in table_nodes:
                table_nodes[tid] = {"id": tid, "label": t, "type": "db_table",
                                    "domain": n["domain"], "repo": "db",
                                    "source_file": "", "community": n.get("community")}
            new_links.append({"source": n["id"], "target": tid, "relation": "defines_table",
                              "confidence": "EXTRACTED", "weight": 1.0})

    # domain anchor nodes + membership edges (fixes orphans, builds clusters)
    from collections import Counter
    dom_count = Counter(n["domain"] for n in N)
    anchors = {}
    for n in N:
        d = n["domain"]
        if dom_count[d] < 2:        # singleton domains: no anchor (would just be a pair)
            continue
        aid = f"domain::{d}"
        if aid not in anchors:
            anchors[aid] = {"id": aid, "label": d, "type": "domain", "domain": d,
                            "repo": "domain", "source_file": "", "community": n.get("community")}
        new_links.append({"source": n["id"], "target": aid, "relation": "in_domain",
                          "confidence": "DERIVED", "weight": 0.3})

    g["nodes"] = N + list(table_nodes.values()) + list(anchors.values())
    g["links"] = L + new_links
    json.dump(g, open(graph_path, "w"))

    from collections import Counter as C
    print(f"enriched: +{len(table_nodes)} db_table nodes, +{len(anchors)} domain anchors, "
          f"+{len(new_links)} edges")
    print("  types:", C(n["type"] for n in g["nodes"]).most_common())
    print("  top domains:", C(n["domain"] for n in N).most_common(12))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
