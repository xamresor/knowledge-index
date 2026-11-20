#!/usr/bin/env python3
"""A map of a project that fits a token budget: the first thing an agent reads in a strange repository.

Idea from Aider's repo map: not every file, but the parts everything else leans on. Here "leans on" is
counted in the graph: the number of distinct symbols that depend on a class (calls, references,
imports, inheritance, HTTP requests from a frontend), the dependents of its methods included. The map
lists the domains with their size, then the most depended-on classes with their busiest methods and
the routes they serve, until the budget (about 4 characters per token) is spent.

`build(graph, routes, projects, budget)` is pure: the caller's view of the graph (node-link JSON) and
the contract index rows. It runs in the graph worker like bin/review.py.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from review import CONTAINER_TYPES, DEPENDS, SYMBOL_TYPES, _orient

FRONTEND = (".ts", ".tsx", ".js", ".jsx", ".vue", ".mjs")
FRAMEWORK = {"Controller", "Model", "Request", "Resource", "JsonResource", "ServiceProvider", "Command",
             "Job", "Event", "Listener", "Mailable", "Notification", "Policy", "Middleware", "Factory",
             "Seeder", "Migration", "Exception", "TestCase", "Facade"}


# Laravel wires these by convention (a route, the scheduler, an event map, a config list, discovery):
# no edge points at them and they are still alive, so `unused` never lists them.
WIRED = re.compile(r"(^|/)(Console|Commands|Jobs|Listeners|Observers|Policies|Providers|Middleware|Filament|"
                   r"Mail|Notifications|Events|Controllers|Requests|Resources|Rules|Casts|Exceptions|Http/Kernel|"
                   r"database|migrations|seeders|factories|routes|config|tests?|Livewire|View/Components)(/|$)")


# frontend entry points: file-system routes (Next, Nuxt, SvelteKit), package entries, configs, type files
ENTRY = re.compile(r"(^|/)(pages|app|routes)/|(^|/)(index|main)\.[jt]sx?$|\.config\.|\.d\.ts$|(^|/)sitemap\.")


def unused(graph: dict, projects: list[str] | None, limit: int = 60, roots: dict[str, str] | None = None) -> str:
    """Classes and frontend modules nothing in the graph depends on, minus what is wired by convention."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    edges = graph.get("links") if "links" in graph else graph.get("edges", [])
    want = set(projects) if projects else {n.get("repo") for n in nodes.values() if n.get("repo")}
    used_files: set[tuple[str, str]] = set()
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s in nodes and t in nodes and e.get("relation") in DEPENDS:
            a, b = _orient(e, nodes[s], nodes[t])
            if nodes[a].get("source_file") != nodes[b].get("source_file"):
                used_files.add((nodes[b].get("repo", ""), nodes[b].get("source_file", "")))
    found: dict[str, list[str]] = defaultdict(list)
    for n in nodes.values():
        f, repo = n.get("source_file") or "", n.get("repo", "")
        if (repo not in want or not f or WIRED.search(f) or (f.endswith(FRONTEND) and ENTRY.search(f))
                or (repo, f) in used_files):
            continue
        if n.get("type") in CONTAINER_TYPES or (n.get("type") == "file" and f.endswith(FRONTEND)):
            found[f"{repo}:{f}"].append(str(n.get("label")))
    if roots:
        found = _not_named_elsewhere(found, roots)
    rows = [f"- {f}  ({', '.join(sorted(set(labels))[:4])}{' …' if len(set(labels)) > 4 else ''})"
            for f, labels in sorted(found.items())]
    if not rows:
        return "(nothing unused found)"
    head = (f"Possibly unused ({len(rows)}): nothing in the graph depends on these files"
            + (" and their names appear in no other indexed file" if roots else "")
            + ". Code loaded by a computed name, config outside the indexed folders or a framework convention "
              "can still be alive: check before deleting.")
    more = [f"… +{len(rows) - limit} more"] if len(rows) > limit else []
    return "\n".join([head, *rows[:limit], *more])


def build(graph: dict, routes: list[dict] | None, projects: list[str] | None, budget: int = 1500) -> str:
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    edges = graph.get("links") if "links" in graph else graph.get("edges", [])
    want = set(projects) if projects else {n.get("repo") for n in nodes.values() if n.get("repo")}
    dependents: dict[str, set[str]] = defaultdict(set)
    owner: dict[str, str] = {}
    for e in edges:
        s, t, rel = e.get("source"), e.get("target"), e.get("relation")
        if s not in nodes or t not in nodes:
            continue
        if rel in DEPENDS:
            a, b = _orient(e, nodes[s], nodes[t])
            if a != b:
                dependents[b].add(a)
        elif rel in ("contains", "method"):
            if "file" in (nodes[s].get("type"), nodes[t].get("type")):
                continue            # a file contains its class: not a member
            if nodes[t].get("type") in CONTAINER_TYPES and nodes[s].get("type") not in CONTAINER_TYPES:
                s, t = t, s
            if nodes[s].get("type") in CONTAINER_TYPES:
                owner[t] = s
    members: dict[str, list[str]] = defaultdict(list)
    for m, c in owner.items():
        members[c].append(m)

    served: dict[tuple[str, str], int] = Counter()   # (project, controller) -> routes
    for r in routes or []:
        cls = str(r.get("action") or "").partition("@")[0].rsplit("\\", 1)[-1]
        if cls:
            served[(r.get("project"), cls)] += 1

    present = sorted(p for p in want if any(n.get("repo") == p for n in nodes.values()))
    share = max(400, budget * 4 // max(1, len(present)))     # each project gets its part of the budget
    out: list[str] = []
    used = 0

    def add(line: str) -> bool:
        nonlocal used
        if used + len(line) + 1 > share:
            return False
        out.append(line)
        used += len(line) + 1
        return True

    for project in present:
        used = 0
        mine = [n for n in nodes.values() if n.get("repo") == project]
        files = sum(1 for n in mine if n.get("type") == "file")
        add(f"## {project}: {files} files, {sum(1 for n in mine if n.get('type') in SYMBOL_TYPES)} symbols")
        domains = Counter(str(n.get("domain")) for n in mine if n.get("domain") and n.get("type") != "domain")
        if domains:
            add("domains: " + ", ".join(f"{d.split(':', 1)[-1]} {c}" for d, c in domains.most_common(12)))
        ranked = []
        by_file: dict[str, list[str]] = defaultdict(list)
        for n in mine:
            if n.get("source_file"):
                by_file[n["source_file"]].append(n["id"])
        for n in mine:
            if not n.get("source_file") or n.get("label") in FRAMEWORK:
                continue
            frontend_module = n.get("type") == "file" and n["source_file"].endswith(FRONTEND)
            if n.get("type") not in CONTAINER_TYPES and not frontend_module:
                continue
            # a frontend module is a file of functions: everything defined in it counts as its parts
            parts = by_file[n["source_file"]] if frontend_module else members.get(n["id"], [])
            deps = set(dependents.get(n["id"], ()))
            for m in parts:
                deps |= dependents.get(m, set())
            deps -= {n["id"], *parts}
            if deps:
                ranked.append((len(deps), n))
        ranked.sort(key=lambda x: (-x[0], str(x[1].get("label"))))
        if not ranked:
            continue
        add("most depended on:")
        for count, n in ranked:
            label = str(n.get("label"))
            parts = (by_file[n["source_file"]] if n.get("type") == "file" else members.get(n["id"], []))
            busy = sorted(((len(dependents.get(m, ())), nodes[m]) for m in parts
                           if m != n["id"] and dependents.get(m)), key=lambda x: -x[0])[:4]
            meth = ", ".join(f"{str(m.get('label')).lstrip('.')} {k}" for k, m in busy)
            route_n = served.get((project, label), 0)
            line = (f"- {label} ({n.get('source_file')}) — {count} dependents"
                    + (f"; serves {route_n} routes" if route_n else "") + (f"; {meth}" if meth else ""))
            if not add(line):
                out.append(f"… (+{len(ranked) - ranked.index((count, n))} more: raise 'budget')")
                break
        out.append("")
    return "\n".join(out).strip() or "(nothing to map for these projects)"


def _not_named_elsewhere(found: dict[str, list[str]], roots: dict[str, str]) -> dict[str, list[str]]:
    """Drop candidates whose name appears in another file of the indexed code: the graph misses
    `new X(...)` inside an array, `X::class` in a relation, a string in a registry."""
    import os
    names: dict[str, str] = {}                      # name -> "repo:file" that defines it
    for key, labels in found.items():
        for label in labels:
            name = re.sub(r"\.(php|tsx?|jsx?|vue|mjs)$", "", str(label).rsplit("/", 1)[-1]).strip(".()")
            if len(name) >= 4:
                names[name] = key
    if not names:
        return found
    pattern = re.compile(r"\b(" + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + r")\b")
    alive: set[str] = set()
    for repo, root in roots.items():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("graphify-out", "node_modules", "vendor", ".git")]
            for fn in filenames:
                if not fn.endswith((".php", *FRONTEND)):
                    continue
                path = os.path.join(dirpath, fn)
                rel = f"{repo}:{os.path.relpath(path, root)}"
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        text = fh.read(2_000_000)
                except OSError:
                    continue
                for m in set(pattern.findall(text)):
                    if not names[m].endswith(rel.split(":", 1)[1]) or not names[m].startswith(repo + ":"):
                        alive.add(names[m])
    return {k: v for k, v in found.items() if k not in alive}
