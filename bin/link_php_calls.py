#!/usr/bin/env python3
"""Link PHP method calls made through a typed variable: `$rental->chargeNowAmount()`.

graphify's AST links a call when it sees the class (`Rental::x()`, `new Rental`, `$this->x()`), not
when the receiver is a variable whose type comes from somewhere else. In a Laravel app that is most
calls: a controller or service gets its collaborators as typed parameters or constructor-promoted
properties and calls their methods. A retrieval benchmark (bench/graph_vs_grep.py, 2026-11-20) missed
exactly these, and a SCIP index of the same app found three times as many cross-file method calls.

The rule is local and cheap, no type inference:
  * a parameter `Rental $rental` (also `?Rental`, `\\App\\Models\\Rental`) types `$rental` inside that
    method;
  * a property `private OwnerPayouts $payouts` (declared or constructor-promoted) types
    `$this->payouts` in the whole class;
  * `$rental->chargeNowAmount(` and `$this->payouts->book(` then become `calls` edges from the
    enclosing method to the method of that class (or interface) when the graph has it.
Edges are INFERRED (the type is the declared one; a subclass or a reassigned variable is not seen).

Usage: link_php_calls.py <graph.json> <repos_dir>
"""
from __future__ import annotations

import os
import re
import sys

import graph

TYPE = r"\??\\?([A-Z][\w\\]*)"
PARAM_RE = re.compile(TYPE + r"\s+&?(?:\.\.\.)?\$(\w+)")
PROP_RE = re.compile(r"\b(?:private|protected|public)\s+(?:readonly\s+)?" + TYPE + r"\s+\$(\w+)")
VAR_CALL_RE = re.compile(r"\$(\w+)\s*(?:\?->|->)\s*(\w+)\s*\(")
PROP_CALL_RE = re.compile(r"\$this\s*->\s*(\w+)\s*(?:\?->|->)\s*(\w+)\s*\(")
FUNC_RE = re.compile(r"\bfunction\s+&?(\w+)\s*\(")
SCALARS = {"int", "float", "string", "bool", "array", "callable", "iterable", "object", "mixed", "void",
           "self", "static", "null", "false", "true", "never", "Closure"}
CONTAINERS = {"class", "model", "controller", "interface", "trait", "service", "request", "resource", "enum"}


def _short(t: str) -> str:
    return t.rsplit("\\", 1)[-1]


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _signature(text: str, start: int) -> str:
    """The parameter list of the function whose `(` is at `start` (balanced parentheses)."""
    depth = 0
    for i in range(start, min(len(text), start + 4000)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    return ""


def link(g: dict, repos_dir: str) -> int:
    nodes = graph.nodes(g)
    links = g["links"]
    by_id = {n["id"]: n for n in nodes}
    # class name -> [(repo, file)] ; (repo, file, method name lower) -> node id ; methods per file by line
    classes: dict[str, list[tuple[str, str]]] = {}
    methods: dict[tuple[str, str, str], str] = {}
    per_file: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for n in nodes:
        f, repo = n.get("source_file") or "", n.get("repo") or ""
        if not f.endswith(".php"):
            continue
        label = str(n.get("label") or "")
        if n.get("type") in CONTAINERS and "." not in label and "(" not in label:
            classes.setdefault(label, []).append((repo, f))
        elif label.endswith("()"):
            name = label.strip(".").removesuffix("()").lower()
            methods.setdefault((repo, f, name), n["id"])
            loc = str(n.get("source_location") or "")
            if loc[1:].isdigit():
                per_file.setdefault((repo, f), []).append((int(loc[1:]), n["id"]))
    existing = {(e.get("source"), e.get("target")) for e in links} | {(e.get("target"), e.get("source")) for e in links}

    def target(type_name: str, repo: str, method: str) -> str | None:
        # The same project only: two projects are separate services, and a class of the same name in
        # another project is not the one this code calls.
        homes = [h for h in classes.get(type_name, []) if h[0] == repo]
        for r, f in homes:
            hit = methods.get((r, f, method.lower()))
            if hit:
                return hit
        return None

    added = 0
    for (repo, f), starts in per_file.items():
        path = os.path.join(repos_dir, repo, f)
        text = graph.read_source(path, os.path.join(repos_dir, repo))
        if text is None:
            continue
        starts.sort()
        props = {name: _short(t) for t, name in PROP_RE.findall(text) if _short(t) not in SCALARS}
        # every function of the file: its parameter types and the span of its body
        funcs = []
        for m in FUNC_RE.finditer(text):
            sig = _signature(text, m.end() - 1)
            params = {name: _short(t) for t, name in PARAM_RE.findall(sig) if _short(t) not in SCALARS}
            props.update({name: _short(t) for t, name in PROP_RE.findall(sig) if _short(t) not in SCALARS})
            funcs.append((m.start(), params))
        for idx, (pos, params) in enumerate(funcs):
            end = funcs[idx + 1][0] if idx + 1 < len(funcs) else len(text)
            body = text[pos:end]
            line0 = _line_of(text, pos)
            owner = None
            for ln, nid in starts:              # the graph's method node for this function
                if ln <= line0 + 1:
                    owner = nid
            if owner is None:
                continue
            calls = [(params.get(v), meth, mm.start()) for mm in VAR_CALL_RE.finditer(body)
                     for v, meth in [mm.groups()] if v != "this"]
            calls += [(props.get(p), meth, mm.start()) for mm in PROP_CALL_RE.finditer(body)
                      for p, meth in [mm.groups()]]
            for type_name, meth, off in calls:
                if not type_name:
                    continue
                tgt = target(type_name, repo, meth)
                if not tgt or tgt == owner or (owner, tgt) in existing:
                    continue
                existing.add((owner, tgt))
                existing.add((tgt, owner))
                links.append({"relation": "calls", "confidence": "INFERRED", "confidence_score": 0.8,
                              "weight": 1.0, "_origin": "kb-php-types", "source": owner, "target": tgt,
                              "source_file": f, "source_location": f"L{line0 + body.count(chr(10), 0, off)}"})
                added += 1
    return added


def main() -> int:
    graph_path, repos_dir = sys.argv[1], sys.argv[2]
    g = graph.load(graph_path)
    n = link(g, repos_dir)
    graph.save(g, graph_path)
    print(f"  typed PHP calls: {n} edges")
    return 0


if __name__ == "__main__":
    sys.exit(main())
