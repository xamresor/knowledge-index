#!/usr/bin/env python3
"""Contracts between the indexed services: the route index, the service map, and the gaps.

Why this module exists
----------------------
The graph answers "what calls what" inside code. A team working on several services also asks
three questions the graph alone answers badly:

  * **contracts_search** — "who calls `POST v2/users`, and what guards it?" One row per route of
    every route table, joined with the frontend call sites found by `link_http.py`.
  * **connections** — "which services talk to which, over what, and why?" Built from contract docs
    (`<project docs>/contracts/<from>-<to>.md`, front matter `from`, `to`, `protocol`, `summary`)
    plus one generated HTTP line per (frontend, API) pair the code shows.
  * **problems** — "what is missing?" A project without contract docs, a contract doc with
    incomplete front matter, a connection the code shows but no doc describes, a frontend call to a
    route that does not exist.

All three are built once per `kb update` into JSON files next to the graph (`bin/kb` runs this
file's CLI) and only read at query time, so a query never walks a checkout.

CLI (used by bin/kb):
    contracts.py contracts   <graph.json> <out.json> <project>=<routes.json>...
    contracts.py connections <contracts.json> <out.json> <project>=<docs dir>...
    contracts.py problems    <connections.json> <out.json> docs:<p>=<dir> routes:<p>=<routes.json>
                             calls:<p>=<serves>=<dir,...>...
    contracts.py stage-contracts <stage dir> <project>=<docs dir>...
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph  # noqa: E402
import paths  # noqa: E402

OUT = str(paths.out_dir())
CONTRACTS = f"{OUT}/contracts.json"
CONNECTIONS = f"{OUT}/connections.json"
PROBLEMS = f"{OUT}/problems.json"
COLLECTION = os.environ.get("KB_COLLECTION", "kb")
# The contract docs of each project are also a qmd collection of their own, `<COLLECTION>_contracts-<p>`
# (bin/kb build_kb, stage_contracts): what a caller limited to some projects may read of the others.
CONTRACT_COLLECTION = f"{COLLECTION}_contracts-"
# Contract docs live in <project docs>/contracts/*.md, one connection per file.
CONTRACT_DOCS_DIR = "contracts"
CONTRACT_KEYS = ("from", "to", "protocol", "summary")
CONTRACTS_RESULTS = 25
MAX_OUT = 200_000   # kb_core pages the answer (PAGE_CHARS)


class NotBuilt(LookupError):
    """An index file is missing (not built yet). The core turns it into a ToolError."""


def contract_collection(project: str) -> str:
    """The qmd collection that holds only the contract docs of `project`."""
    return f"{CONTRACT_COLLECTION}{project}"


def doc_uri(project: str, rel: str) -> str:
    """qmd path of a contract doc: in the contract collection of its project, which every caller who
    may see the contract can read (bin/access.py doc_allowed)."""
    return f"qmd://{contract_collection(project)}/{rel}"


# --------------------------------------------------------------------------- shared helpers
def norm_uri(p: str) -> str:
    """Normalise a route URI or a call-site path so the two compare equal.

    Every parameter form (`${id}`, `{id}`, `{id?}`, `:id`) becomes `{}`. Parameters are collapsed
    before the query string is cut, so an optional Laravel parameter `{id?}` is not cut in half.
    """
    p = p.strip()
    p = re.sub(r"\$\{[^}]*\}", "{}", p)
    p = re.sub(r"\{[^}]*\}", "{}", p)
    p = re.sub(r":\w+", "{}", p)
    p = p.split("?", 1)[0].strip().strip("/")
    p = re.sub(r"//+", "/", p)
    return p.lower()


def route_variants(uri: str) -> list[str]:
    """Normalised forms of a route URI with and without its trailing optional parameters
    (`v2/x/{id}/generate/{date?}` also answers `v2/x/{id}/generate`)."""
    segs = uri.strip("/").split("/")
    out = [norm_uri(uri)]
    while segs and re.fullmatch(r"\{[^}]*\?\}", segs[-1]):
        segs = segs[:-1]
        out.append(norm_uri("/".join(segs)))
    return out


def uri_matches(call: str, route: str, prefix: bool = False) -> bool:
    """Segment by segment: a route parameter `{}` takes any segment of the call, and a variable in
    the call (`{}`, also inside a segment like `{}-verification`) takes any text.

    `prefix=True` also accepts a route with extra leading segments: the frontend often omits a prefix
    its base URL supplies (Laravel's `api/`, an API version) — the same tolerance as link_http.py."""
    a, b = call.split("/"), route.split("/")
    if prefix and len(b) > len(a):
        b = b[len(b) - len(a):]
        # Anchor the shortened route on a literal first segment: otherwise any short call would
        # "match" the parameter tail of a long route (`v2/nope` vs `.../{env}/{kind}`).
        if a[0] != b[0]:
            return False
    if len(a) != len(b):
        return False
    for c, r in zip(a, b):
        if c == r or r == "{}":
            continue
        if "{}" not in c or not re.fullmatch(re.escape(c).replace(re.escape("{}"), ".+"), r):
            return False
    return True


def frontmatter(text: str) -> dict[str, str]:
    """`key: value` lines between the leading `---` fences (a flat YAML subset, no dependency)."""
    m = re.match(r"\A---\s*\n(.*?)\n---\s*(\n|\Z)", text, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip() and not k.startswith((" ", "#")):
            out[k.strip().lower()] = v.strip().strip("'\"")
    return out


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _contract_files(root: str):
    """(file name, path) of every contract doc candidate under <root>/contracts/.

    A file (or the contracts folder) that is a symlink out of the project's docs root is skipped:
    `contracts/copied.md -> <other project>/docs/contracts/private.md` would show another project's
    contract as this project's own, also under `contracts: own`. The path returned is the resolved
    one, so the file read is the file checked."""
    real_root = os.path.realpath(root)
    cdir = os.path.join(root, CONTRACT_DOCS_DIR)
    if not os.path.isdir(cdir) or not _inside(os.path.realpath(cdir), real_root):
        return
    for fn in sorted(os.listdir(cdir)):
        if fn.endswith(".md") and fn.lower() != "readme.md":
            real = os.path.realpath(os.path.join(cdir, fn))
            if _inside(real, real_root) and os.path.isfile(real):
                yield fn, real


# --------------------------------------------------------------------------- contract docs in qmd
def stage_contracts(stage: str, roots: dict[str, str]) -> int:
    """Copy the contract docs of every project (`_contract_files`) into `<stage>/<project>/contracts/`.

    Each project's copy is its own qmd collection (`contract_collection`), and nothing else is in it:
    a caller limited to some projects reads the contract docs of the others only there, never in their
    doc collections. So no file name can make another doc look like a contract doc. A folder for every
    project, also one without contract docs: kb_core can name the collection of every docs project.

    The new copy is built next to the old one and swapped in whole, so a removed or replaced contract
    doc does not stay. Read like a source file (graph.read_source): a regular file inside the docs root,
    at most graph.MAX_SOURCE_BYTES (a longer doc is left out). -> the number of docs copied."""
    import shutil  # noqa: PLC0415
    new, old = f"{stage}.new", f"{stage}.old"
    for d in (new, old):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(new)
    copied = 0
    for project, root in sorted(roots.items()):
        dest = os.path.join(new, project, CONTRACT_DOCS_DIR)
        os.makedirs(dest)
        for fn, fp in _contract_files(root) if os.path.isdir(root) else ():
            # One byte over the limit tells a long doc from one that fits: a cut copy would read as
            # the whole contract, so a doc over the limit is left out, with a warning.
            text = graph.read_source(fp, root, graph.MAX_SOURCE_BYTES + 1)
            if text is None:
                continue
            if len(text.encode("utf-8")) > graph.MAX_SOURCE_BYTES:
                print(f"  warning: {fp} is over {graph.MAX_SOURCE_BYTES} bytes; "
                      "it is not shared as a contract doc", file=sys.stderr)
                continue
            with open(os.path.join(dest, fn), "w", encoding="utf-8") as f:
                f.write(text)
            copied += 1
    if os.path.isdir(stage):
        os.rename(stage, old)
    os.rename(new, stage)
    shutil.rmtree(old, ignore_errors=True)
    return copied


_JSON_CACHE: dict[str, tuple[float, list[dict]]] = {}
_json_lock = threading.Lock()


def load_index(path: str) -> list[dict]:
    """A JSON index of the build, cached by mtime (a rebuild replaces the file atomically)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        raise NotBuilt(f"{os.path.basename(path)} is not built yet (run `kb update`)") from None
    with _json_lock:
        hit = _JSON_CACHE.get(path)
        if hit is None or hit[0] != mtime:
            with open(path, encoding="utf-8") as f:
                hit = _JSON_CACHE[path] = (mtime, json.load(f))
        return hit[1]


def _write_atomic(obj, dst: str) -> None:
    tmp = f"{dst}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, dst)  # readers never see a half-written index


# --------------------------------------------------------------------------- contracts
def build_contracts(g: dict, routes: dict[str, list[dict]]) -> list[dict]:
    """One entry per route of every route table, with the frontend call sites taken from the
    `http_request` edges of graph `g`. `routes` maps a project name to its `route:list` rows."""
    ek = "links" if "links" in g else "edges"
    repo = {n["id"]: n.get("repo", "") for n in g.get("nodes", [])}
    callers: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for e in g.get(ek, []):
        if e.get("relation") != "http_request":
            continue
        key = (str(e.get("http_method", "")).upper(), norm_uri(str(e.get("http_uri", ""))))
        src = e.get("source", "")
        where = f"{e.get('source_file', '')}:{e.get('source_location', '')}".rstrip(":")
        callers.setdefault(key, set()).add((repo.get(src) or src.split("::")[0], where))
    # Attach each call site to one route per verb: the exact URI, else the shortest route that ends
    # with it (the base URL supplied the prefix) — the rule link_http.py used to draw the edge.
    by_verb: dict[str, list[str]] = {}
    for rows in routes.values():
        for r in rows:
            for v in str(r.get("method") or "").split("|"):
                by_verb.setdefault(v.upper(), []).append(norm_uri(str(r.get("uri") or "")))
    target: dict[tuple[str, str], str] = {}
    for verb, call in callers:
        uris = by_verb.get(verb, [])
        if call in uris:
            target[(verb, call)] = call
        else:
            cands = sorted((u for u in uris if call and u.endswith("/" + call)), key=len)
            if cands:
                target[(verb, call)] = cands[0]
    attached: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for key, uri in target.items():
        attached.setdefault((key[0], uri), set()).update(callers[key])
    out = []
    for project, rows in sorted(routes.items()):
        for r in rows:
            verbs = [v for v in str(r.get("method") or "").split("|") if v and v != "HEAD"]
            uri = norm_uri(str(r.get("uri") or ""))
            seen: set[tuple[str, str]] = set()
            for v in verbs:
                seen |= attached.get((v.upper(), uri), set())
            out.append({
                "project": project,
                "method": "|".join(verbs),
                "uri": str(r.get("uri") or "").strip("/"),
                "name": r.get("name") or "",
                "action": r.get("action") or "",
                "middleware": [str(m) for m in (r.get("middleware") or [])],
                "defined": r.get("path") or "",
                "callers": [{"repo": a, "at": b} for a, b in sorted(seen)],
            })
    return out


def declared_callers(c: dict, conn: list[dict]) -> list[str]:
    """`<from> — <doc>` of each contract doc to this route's project that names this route."""
    uri = norm_uri(c["uri"])
    return [f"{r['from']} — {r['doc']}" for r in conn
            if r["to"] == c["project"] and r.get("doc")
            and any(u == uri or uri_matches(u, uri, prefix=True) for u in r.get("routes", []))]


def contracts_search(q: str, path: str | None = None, limit: int = CONTRACTS_RESULTS,
                     conn_path: str | None = None, visible: set[str] | None = None) -> str:
    """Routes whose method, URI, name, action or middleware hold every word of `q`.

    `visible`: the projects whose contracts the caller may see (the setting `contracts: own`), None =
    every contract. With it, a route of a visible project shows in full with the call sites of visible
    projects only. A route of a hidden project shows only when a visible project calls it, and then as
    the bare endpoint (method and URI): no name, action, middleware, file or project. Its words match
    only that endpoint, so a search by a hidden controller name finds nothing."""
    words = [w.lower() for w in q.split()]
    if not words:
        raise ValueError("'query' must not be empty")
    try:
        conn = load_index(conn_path or CONNECTIONS)
    except NotBuilt:
        conn = []  # the service map is optional here
    if visible is not None:
        conn = [r for r in conn if r.get("owner") in visible]   # declared callers: readable docs only
    hits = []
    for c in load_index(path or CONTRACTS):
        if visible is not None:
            callers = [x for x in c["callers"] if x["repo"] in visible]
            if c["project"] not in visible:
                # match the bare endpoint first: the declared callers cost a walk over the service
                # map, so they are found only for the routes that match
                bare_hay = " ".join([c["method"], c["uri"], norm_uri(c["uri"])]).lower()
                if not all(w.strip("/") in bare_hay for w in words):
                    continue
                declared = declared_callers(c, conn)
                if not callers and not declared:
                    continue
                c = {"project": "", "method": c["method"], "uri": c["uri"], "name": "", "action": "",
                     "middleware": [], "defined": "", "callers": callers, "bare": declared}
            else:
                c = {**c, "callers": callers}
        hay = " ".join([c["method"], c["uri"], norm_uri(c["uri"]), c["name"], c["action"],
                        *c["middleware"]]).lower()
        if all(w.strip("/") in hay for w in words):
            hits.append(c)
    if not hits:
        return "(no contracts match)"
    hits.sort(key=lambda c: (-len(c["callers"]), c["uri"], c["method"]))
    parts = []
    for c in hits[:limit]:
        if "bare" in c:
            lines = [f"{c['method']} /{c['uri']}  (a project you cannot see: the endpoint only)"]
            declared = c["bare"]
        else:
            lines = [f"{c['method']} /{c['uri']}" + (f"  [{c['name']}]" if c["name"] else "")
                     + f"  ({c['project']})",
                     f"  action: {c['action']}" + (f"  @ {c['defined']}" if c["defined"] else "")]
            if c["middleware"]:
                # `Vendor\Middleware\Authorize:x` -> `Authorize:x`; the search matches the full name.
                short = [m.split(":", 1)[0].rsplit("\\", 1)[-1] + (":" + m.split(":", 1)[1] if ":" in m else "")
                         for m in c["middleware"]]
                lines.append(f"  middleware: {', '.join(short)}")
            declared = declared_callers(c, conn)
        if c["callers"]:
            lines.append("  callers: " + "; ".join(f"{x['repo']} {x['at']}" for x in c["callers"]))
        if declared:
            lines.append("  callers (contracts): " + "; ".join(declared))
        if not c["callers"] and not declared:
            lines.append("  callers: none found (no frontend call site, no contract doc)"
                         + (" in your projects" if visible is not None else ""))
        parts.append("\n".join(lines))
    more = f"\n\n(+{len(hits) - limit} more — add words to narrow)" if len(hits) > limit else ""
    return "\n\n".join(parts)[:MAX_OUT] + more


# --------------------------------------------------------------------------- connections
def build_connections(docs: dict[str, str], contract_rows: list[dict]) -> list[dict]:
    """Connections from the contract docs (`docs` maps a project to its docs dir) plus one
    generated HTTP connection per (frontend repo, route project) pair of the contract index."""
    out = []
    for project, root in sorted(docs.items()):
        for fn, fp in _contract_files(root):
            try:
                with open(fp, encoding="utf-8", errors="replace") as f:
                    text = f.read(65536)
            except OSError:
                continue
            fm = frontmatter(text)
            if not fm.get("from") or not fm.get("to"):
                continue  # not a contract doc: no from/to
            # API routes the doc names (`v1/hooks/x`, by the usual version prefix): contracts_search
            # then lists the `from` service as a caller of each of them.
            routes = sorted({norm_uri(m) for m in re.findall(r"\bv\d+/[\w{}./*?-]+", text)})
            out.append({"from": fm["from"].lower(), "to": fm["to"].lower(),
                        "protocol": fm.get("protocol", ""), "summary": fm.get("summary", ""),
                        "doc": doc_uri(project, f"{CONTRACT_DOCS_DIR}/{fn}"),
                        "owner": project, "routes": routes})
    calls: dict[tuple[str, str], int] = {}
    for c in contract_rows:
        for repo in {x["repo"] for x in c.get("callers", [])}:
            calls[(repo, c["project"])] = calls.get((repo, c["project"]), 0) + 1
    for (src, dst), n in sorted(calls.items()):
        out.append({"from": src, "to": dst, "protocol": "http",
                    "summary": f"{n} endpoints called in code (contracts_search for details)",
                    "doc": "", "owner": "generated"})
    return out


HIDDEN = "(hidden)"   # an end of a connection that the caller may not see (`contracts: own`)


def visible_connections(rows: list[dict], visible: set[str]) -> list[dict]:
    """The connection rows a caller limited to `visible` may see (the setting `contracts: own`).

    A row whose contract doc belongs to a visible project stays as it is: the caller can read that doc.
    Any other row stays only when one of its ends is a visible project; its other end becomes
    "(hidden)", and a contract doc of a hidden project loses its summary and its doc path."""
    out, seen = [], set()
    for r in rows:
        if r.get("doc") and r.get("owner") in visible:
            row = r
        elif r["from"] in visible or r["to"] in visible:
            row = {**r,
                   "from": r["from"] if r["from"] in visible else HIDDEN,
                   "to": r["to"] if r["to"] in visible else HIDDEN,
                   "summary": r["summary"] if not r.get("doc") else "(summary in a contract doc you cannot see)",
                   "doc": ""}
        else:
            continue
        key = (row["from"], row["to"], row["protocol"], row["summary"], row["doc"])
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def connections(service: str, path: str | None = None, visible: set[str] | None = None) -> str:
    """`visible`: see visible_connections (None = every connection)."""
    rows = all_rows = load_index(path or CONNECTIONS)
    if visible is not None:
        rows = all_rows = visible_connections(all_rows, visible)
    s = service.strip().lower()
    if s:
        rows = [r for r in rows if s in (str(r["from"]).lower(), str(r["to"]).lower())]
        if not rows:
            known = sorted(({r["from"] for r in all_rows} | {r["to"] for r in all_rows}) - {HIDDEN})
            return f"(no connections of '{service}'; services: {', '.join(known) or 'none you can see'})"
    if not rows:
        return "(no connections: no contract docs and no frontend calls found)"
    rows = sorted(rows, key=lambda r: (r["from"], r["to"], r["protocol"]))
    lines = []
    for r in rows:
        line = f"{r['from']} -> {r['to']} | {r['protocol'] or '?'} | {r['summary']}"
        lines.append(line + (f"\n    doc: {r['doc']}" if r["doc"] else ""))
    return "\n".join(lines)[:MAX_OUT]


# --------------------------------------------------------------------------- problems
def build_problems(docs: dict[str, str], calls: dict[str, tuple[str, list[str]]],
                   routes: dict[str, list[dict]], conn_rows: list[dict]) -> list[dict]:
    """Gaps the build finds. Each problem names the project that must fix it.

    docs:  project -> docs dir;  calls: frontend project -> (route project, source roots);
    routes: route project -> route:list rows;  conn_rows: the connection map.
    """
    out: list[dict] = []

    def add(project: str, kind: str, where: str, message: str, others: list[str] | None = None) -> None:
        # `others`: the other projects the row names, hidden from a caller who may not see them
        row = {"project": project, "kind": kind, "where": where, "message": message}
        if others:
            row["others"] = others
        out.append(row)

    for project, root in sorted(docs.items()):
        if not os.path.isdir(os.path.join(root, CONTRACT_DOCS_DIR)):
            add(project, "no-contracts-dir", f"{root}/{CONTRACT_DOCS_DIR}/",
                "No contract docs. Describe every connection of this project to another service "
                f"in {CONTRACT_DOCS_DIR}/<from>-<to>.md (front matter: from, to, protocol, summary).")
            continue
        for fn, fp in _contract_files(root):
            head = graph.read_source(fp, root, 8192)   # a regular file inside the docs root only
            if head is None:
                continue
            fm = frontmatter(head)
            missing = [k for k in CONTRACT_KEYS if not fm.get(k)]
            if missing:
                add(project, "bad-contract", f"{CONTRACT_DOCS_DIR}/{fn}",
                    f"Front matter misses: {', '.join(missing)}.")

    documented = {(r["from"], r["to"]) for r in conn_rows if r.get("doc")}
    for r in conn_rows:
        if not r.get("doc") and (r["from"], r["to"]) not in documented:
            add(r["from"], "missing-contract", f"{r['from']} -> {r['to']}",
                f"The code calls {r['to']} ({r['protocol']}, {r['summary'].split(' (')[0]}), "
                f"but no {CONTRACT_DOCS_DIR}/*.md has from: {r['from']}, to: {r['to']}.", [r["to"]])

    # Frontend call sites without a route: the same scanner as link_http.py.
    import lang  # noqa: PLC0415
    from lang import js_ts  # noqa: PLC0415
    for fe, (serves, roots) in sorted(calls.items()):
        table: dict[str, set[str]] = {}
        apis = [a for a in serves.split(",") if a]          # one frontend may call several APIs
        for r in (row for a in apis for row in routes.get(a, [])):
            for v in str(r.get("method") or "").split("|"):
                for uri in route_variants(str(r.get("uri") or "")):
                    table.setdefault(v.strip().lower(), set()).add(uri)
        if not table:
            continue

        def known(verb: str, uri: str, table=table) -> bool:
            uris = table.get(verb, set())
            return uri in uris or any(uri_matches(uri, u, prefix=True) for u in uris)

        for root in roots:
            base = os.path.dirname(root.rstrip("/"))
            for dp, dirs, files in os.walk(root):
                dirs[:] = js_ts.prune(dirs)
                for fn in sorted(files):
                    if not fn.endswith(lang.FRONTEND):
                        continue
                    fp = os.path.join(dp, fn)
                    # The same read as link_http.py: not a device or a pipe, not a link out of the
                    # calls folder, at most graph.MAX_SOURCE_BYTES. The build holds update.lock here.
                    text = graph.read_source(fp, root)
                    if text is None:
                        continue
                    for i, verb, raw in js_ts.iter_calls(text.splitlines()):
                        uri = norm_uri(raw)
                        # A call that starts with a variable (`${base}/...`) names no route; a
                        # literal ending in `/` is glued to a variable (`'v1/items/' + id`).
                        if not uri or uri.startswith("{}") or raw.split("?")[0].endswith("/") \
                                or known(verb, uri):
                            continue
                        add(fe, "unknown-route", f"{os.path.relpath(fp, base)}:L{i}",
                            f"Calls {verb.upper()} {raw}, but {' / '.join(apis)} has no such route.",
                            [a for a in apis if a != fe])
    return out


def _other_ends(r: dict) -> list[str]:
    """The other projects a problem row names. An index built before the `others` field: read them
    from the text of the row."""
    if "others" in r:
        return list(r["others"])
    if r.get("kind") == "missing-contract":
        return [str(r.get("where", "")).partition(" -> ")[2]]
    m = re.search(r", but (.+) has no such route\.$", str(r.get("message", "")))
    return m.group(1).split(" / ") if r.get("kind") == "unknown-route" and m else []


def _hide_ends(r: dict, visible: set[str]) -> dict:
    """The row with every other project that `visible` does not hold written as "(hidden)".

    Only the slots where the build writes a project name change (`problems` above): the `-> X` and
    `calls X (` / `to: X.` of a missing contract, the `but X / Y has no such route.` tail of an
    unknown route. A name that is also a path segment or a URI part (`src/api/x.ts`, `api/v1`)
    stays as it is."""
    hidden = {o for o in _other_ends(r) if o and o not in visible}
    if not hidden:
        return r
    where, message = str(r.get("where", "")), str(r.get("message", ""))
    if r.get("kind") == "missing-contract":
        src, arrow, to = where.partition(" -> ")
        if arrow and to in hidden:
            where = f"{src} -> {HIDDEN}"
            message = re.sub(r"^(The code calls )(\S+)( \()",
                             lambda m: m.group(1) + (HIDDEN if m.group(2) in hidden else m.group(2)) + m.group(3),
                             message, count=1)
            message = re.sub(r"(, to: )(\S+)(\.)$",
                             lambda m: m.group(1) + (HIDDEN if m.group(2) in hidden else m.group(2)) + m.group(3),
                             message, count=1)
        return {**r, "where": where, "message": message}
    if r.get("kind") == "unknown-route":
        m = re.search(r", but (.+) has no such route\.$", message)
        if m:
            names = " / ".join(HIDDEN if n in hidden else n for n in m.group(1).split(" / "))
            message = message[:m.start(1)] + names + message[m.end(1):]
        return {**r, "message": message}
    # a row of another kind that names other projects: hide every whole-word mention
    pat = re.compile(r"(?<![\w/-])(?:" + "|".join(re.escape(o) for o in sorted(hidden)) + r")(?![\w/-])")
    return {**r, "where": pat.sub(HIDDEN, where), "message": pat.sub(HIDDEN, message)}


def problems(project: str, allowed: set[str] | None, path: str | None = None,
             extra: list[dict] | None = None, visible: set[str] | None = None) -> str:
    """`extra`: rows an add-on finds at call time (the data dictionary), merged with the built index.
    `visible`: the projects whose contracts the caller sees (`contracts: own`); another project that
    a row names is written as "(hidden)", as `connections` does. None = every name shows."""
    try:
        rows = load_index(path or PROBLEMS)
    except NotBuilt:
        if not extra:
            raise
        rows = []
    rows = list(rows) + list(extra or [])
    if allowed is not None:
        rows = [r for r in rows if r["project"] in allowed]
    p = project.strip().lower()
    if "/" in p:      # "api/missing-contract", the form the summary line prints
        proj, _, kind = p.partition("/")
        rows = [r for r in rows if str(r["project"]).lower() == proj and str(r["kind"]).lower() == kind]
    elif p:
        rows = [r for r in rows if str(r["project"]).lower() == p or str(r["kind"]).lower() == p]
    if not rows:
        return "(no problems" + (" in your projects)" if allowed is not None else ")")
    if visible is not None:
        rows = [_hide_ends(r, visible) for r in rows]
    rows = sorted(rows, key=lambda r: (r["project"], r["kind"], r["where"]))
    counts: dict[str, int] = {}
    for r in rows:
        k = f"{r['project']}/{r['kind']}"
        counts[k] = counts.get(k, 0) + 1
    head = "Problems: " + ", ".join(f"{k} {n}" for k, n in sorted(counts.items()))
    body = "\n".join(f"[{r['project']}] {r['kind']} {r['where']}\n    {r['message']}" for r in rows)
    return (head + "\n\n" + body)[:MAX_OUT]


# --------------------------------------------------------------------------- CLI (bin/kb)
def _main_contracts(argv: list[str]) -> int:
    src, dst, *specs = argv
    with open(src, encoding="utf-8") as f:
        g = json.load(f)
    routes = {}
    for spec in specs:
        project, _, rpath = spec.partition("=")
        try:
            with open(rpath, encoding="utf-8") as f:
                routes[project] = json.load(f)
        except (OSError, ValueError) as e:
            print(f"  [{project}] no route table ({e.__class__.__name__}), skipped", file=sys.stderr)
    out = build_contracts(g, routes)
    _write_atomic(out, dst)
    print(f"{len(out)} contracts, {sum(1 for c in out if c['callers'])} with frontend callers")
    return 0


def _main_connections(argv: list[str]) -> int:
    contracts_path, dst, *specs = argv
    try:
        with open(contracts_path, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        rows = []
    docs = dict(spec.partition("=")[::2] for spec in specs)
    out = build_connections(docs, rows)
    _write_atomic(out, dst)
    print(f"{len(out)} connections, {sum(1 for c in out if c['doc'])} from contract docs")
    return 0


def _main_problems(argv: list[str]) -> int:
    conn_path, dst, *specs = argv
    try:
        with open(conn_path, encoding="utf-8") as f:
            conn_rows = json.load(f)
    except (OSError, ValueError):
        conn_rows = []
    docs: dict[str, str] = {}
    routes: dict[str, list[dict]] = {}
    calls: dict[str, tuple[str, list[str]]] = {}
    for spec in specs:  # docs:<p>=<dir>  routes:<p>=<routes.json>  calls:<p>=<serves>=<dir,...>
        kind, _, rest = spec.partition(":")
        project, _, value = rest.partition("=")
        if kind == "docs":
            docs[project] = value
        elif kind == "routes":
            try:
                with open(value, encoding="utf-8") as f:
                    routes[project] = json.load(f)
            except (OSError, ValueError):
                pass
        elif kind == "calls":
            serves, _, dirs = value.partition("=")
            calls[project] = (serves, [d for d in dirs.split(",") if d])
    out = build_problems(docs, calls, routes, conn_rows)
    _write_atomic(out, dst)
    print(f"{len(out)} problems")
    return 0


def _main_stage_contracts(argv: list[str]) -> int:
    stage, *specs = argv
    n = stage_contracts(stage, dict(spec.partition("=")[::2] for spec in specs))
    print(f"{n} contract docs staged")
    return 0


if __name__ == "__main__":
    cmds = {"contracts": _main_contracts, "connections": _main_connections,
            "problems": _main_problems, "stage-contracts": _main_stage_contracts}
    if len(sys.argv) >= 3 and sys.argv[1] in cmds:
        sys.exit(cmds[sys.argv[1]](sys.argv[2:]))
    print(__doc__.split("CLI (used by bin/kb):")[1], file=sys.stderr)
    sys.exit(2)
