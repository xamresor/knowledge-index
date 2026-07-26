#!/usr/bin/env python3
"""Add semantic data-layer links to an enriched graphify graph.

graphify's AST misses framework-level relationships. This adds three edge kinds:

  * eloquent : model --belongsTo/hasMany/...--> model   (parsed from $this->belongsTo(X::class))
  * fk       : db_table --fk--> db_table                 (parsed from migration foreign keys)
  * sql      : class/file --sql--> db_table              (parsed from DB::table('t') / ->from('t'))

Missing db_table nodes (tables referenced by FK/SQL but with no model) are created on the fly.
Run AFTER enrich.py (which creates the db_table nodes + assigns domains).

Usage: link_data.py <graph.json> <repos_dir>
"""
from __future__ import annotations

import json
import os
import re
import sys

import graph
import lang
from lang import php_laravel

KB = None  # set in main

#: Re-exported: the migration-name parser now lives with the rest of the Laravel knowledge.
table_from_filename = php_laravel.table_from_filename


def project_migration_dirs() -> list[str]:
    """Laravel project roots from kb.projects.toml -> their database/migrations dirs."""
    cfg = os.path.join(KB, "kb.projects.toml")
    dirs = []
    if os.path.exists(cfg):
        with open(cfg, encoding="utf-8") as fh:
            config = fh.read()
        for m in re.finditer(r'(?m)^\s*routes\s*=\s*"([^"]+)"', config):
            d = php_laravel.migrations_dir(m.group(1))
            if os.path.isdir(d):
                dirs.append(d)
    return dirs


def main() -> int:
    global KB
    graph_path, repos_dir = sys.argv[1], sys.argv[2]
    KB = os.path.dirname(os.path.abspath(repos_dir))
    g = graph.load(graph_path)
    N, L = g["nodes"], g["links"]

    defined = {e["source"] for e in L if e.get("relation") in ("method", "contains")}
    # label -> canonical node id (prefer a definition node)
    label_node: dict[str, str] = {}
    file_node: dict[str, str] = {}
    table_node: dict[str, str] = {}
    table_domain: dict[str, str] = {}
    for n in N:
        if n.get("type") == "db_table":
            table_node[n["label"]] = n["id"]
            table_domain[n["label"]] = n.get("domain", "database")
        lab = n["label"]
        if not lab.endswith(("()",) + lang.PHP + (".js", ".vue", ".ts")):
            if lab not in label_node or n["id"] in defined:
                label_node[lab] = n["id"]
        sf = (n.get("source_file") or "").lstrip("/")
        if sf and (lab.endswith(".php")):
            file_node[sf] = n["id"]
    # source_file -> class def node (preferred attach point for SQL)
    classnode_by_file: dict[str, str] = {}
    for n in N:
        sf = (n.get("source_file") or "").lstrip("/")
        if sf and n["id"] in defined and not n["label"].endswith(lang.PHP + ("()",)):
            classnode_by_file.setdefault(sf, n["id"])

    def ensure_table(name: str, domain: str = "database") -> str:
        if name not in table_node:
            tid = f"db::{name}"
            table_node[name] = tid
            N.append({"id": tid, "label": name, "type": "db_table", "domain": domain,
                      "repo": "db", "source_file": ""})
            table_domain[name] = domain
        return table_node[name]

    new = []
    # 1) Eloquent model -> model
    eloq = 0
    for fp in php_laravel.walk(repos_dir):
        txt = php_laravel.read(fp)
        if txt is None:
            continue
        cls = php_laravel.class_name(txt)
        if not cls:
            continue
        src = label_node.get(cls)
        if not src:
            continue
        for rel, target in php_laravel.eloquent_relations(txt):
            dst = label_node.get(target)
            if dst and dst != src:
                # INFERRED: `X::class` is exact syntax, but the node is resolved by bare class
                # label — a name collision across repos would pick the wrong definition.
                new.append({"source": src, "target": dst, "relation": "eloquent",
                            "kind": rel, "confidence": "INFERRED", "confidence_score": 0.9,
                            "weight": 1.0})
                eloq += 1

    # 2) Foreign keys: db_table -> db_table (from migrations)
    fk = 0
    def add_fk(cur, refs, cur_from_filename=False):
        # ->on('t')/constrained('t') names the referenced table exactly, but the OWNING table is
        # only certain when Schema::create/table named it; a table guessed from the migration
        # FILENAME (table_from_filename) is honestly AMBIGUOUS.
        nonlocal fk
        conf = ("AMBIGUOUS", 0.6) if cur_from_filename else ("INFERRED", 0.9)
        for other in refs:
            if not cur or other == cur:
                continue
            a = ensure_table(cur, table_domain.get(cur, "database"))
            b = ensure_table(other, table_domain.get(other, "database"))
            new.append({"source": a, "target": b, "relation": "fk",
                        "confidence": conf[0], "confidence_score": conf[1], "weight": 1.0})
            fk += 1

    for mdir in project_migration_dirs():
        for fp in php_laravel.walk(mdir):
            txt = php_laravel.read(fp) or ""
            declared = php_laravel.schema_tables(txt)
            if len(declared) > 1:                       # multi-table file -> split per block
                for blk in php_laravel.schema_blocks(txt):
                    owner = php_laravel.schema_tables(blk)
                    if owner:
                        add_fk(owner[0], php_laravel.foreign_keys(blk))
            else:
                cur = declared[0] if declared else table_from_filename(os.path.basename(fp))
                add_fk(cur, php_laravel.foreign_keys(txt), cur_from_filename=not declared)

    # 3) raw SQL / query-builder table refs: class/file -> db_table
    sql = 0
    for fp in php_laravel.walk(repos_dir):
        txt = php_laravel.read(fp)
        if txt is None:
            continue
        tbls = php_laravel.table_refs(txt)
        if not tbls:
            continue
        rel = os.path.relpath(fp, os.path.join(repos_dir, os.path.basename(os.path.dirname(fp))))
        # resolve the attach node by matching the node source_file suffix
        attach = None
        for sf, nid in classnode_by_file.items():
            if fp.endswith(sf):
                attach = nid
                break
        if not attach:
            for sf, nid in file_node.items():
                if fp.endswith(sf):
                    attach = nid
                    break
        if not attach:
            continue
        for t in tbls:
            tid = ensure_table(t)
            # INFERRED: the table name is literal, but the attach node comes from a
            # source_file suffix match (fallback: the file node).
            new.append({"source": attach, "target": tid, "relation": "sql",
                        "confidence": "INFERRED", "confidence_score": 0.8, "weight": 1.0})
            sql += 1

    # dedup
    seen, out = set(), []
    for e in new:
        k = (e["source"], e["target"], e["relation"])
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    g["links"] = L + out
    graph.save(g, graph_path)
    print(f"data links: +{eloq} eloquent(model->model), +{fk} fk(table->table), "
          f"+{sql} sql(code->table); {len(out)} unique edges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
