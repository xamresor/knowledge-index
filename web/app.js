/* Code Knowledge Base — the shell's behaviour.
 *
 * This file is versioned and readable on its own; it used to be a `TEMPLATE` string inside
 * bin/render_viz.py, rendered into a git-ignored directory, which meant the interface could not be
 * diffed, linted or tested. Now the generator emits **data only** and this shell consumes it:
 *
 *   ../graphify-out/kb-manifest.js  -> kbManifest({domains, superNodes, fileMap, dataPath, ...})
 *   ../graphify-out/kb-status.js    -> kbStatus({graph, docs, aliases, quality, versions, takenAt})
 *   <dataPath>/<domain>.js          -> kbRecv(domain, {nodes, edges})   loaded on demand
 *   <dataPath>/_cross.js            -> kbCross([[from, fromDomain, to, toDomain, relation], …])
 *   <dataPath>/_index.js            -> kbIndex([[label, id, domain], …])  loaded on first search
 *
 * Data arrives as `.js` calls rather than JSON + fetch on purpose: a page opened from `file://`
 * cannot fetch a sibling file, but it can load a sibling <script>. That keeps the whole thing working
 * with nothing running — which is exactly when you want to look at it.
 *
 * RULE: node labels come from other people's repositories, so every value reaches the DOM through
 * textContent. No innerHTML anywhere in this file.
 */
"use strict";

const RED = "#e15759", BLUE = "#4e79a7", GREEN = "#59a14f";

const LEGEND = [
  ["dot ●", "class / file"],
  ["triangle ▲", "method / function"],
  ["hexagon ⬡", "controller"],
  ["database", "model"],
  ["square ▭", "db_table"],
  ["box", "resource / request"],
  ["diamond ◆", "service / repository / interface / trait"],
  ["star ★", "enum / template (.vue)"],
  ["text", "rationale (NOTE / WHY / HACK / SECURITY)"],
  ["ellipse", "doc (markdown file in the graph)"],
];

let MANIFEST = null;   // set by kbManifest()
const hidden = new Set();   // domains unticked in the Domains list
let STATUS = null;     // set by kbStatus()
let net = null, nodes = null, edges = null;
const loaded = {};     // domain -> {nodeIds, edgeIds}
let CROSS = null, crossIds = [], pendingFocus = null;
const pendingPos = {};
let IDX = null, idxLoading = false;

/* --- small DOM helpers (textContent only) ----------------------------------------------------- */
const el = (id) => document.getElementById(id);

function text(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
}

function banner(message) {
  const b = el("banner");
  b.hidden = false;
  b.textContent = b.textContent ? `${b.textContent} · ${message}` : message;
}

function defList(target, pairs) {
  const dl = el(target);
  dl.replaceChildren();
  for (const [key, value] of pairs) {
    if (value === undefined || value === null || value === "") continue;
    dl.appendChild(text("dt", key));
    dl.appendChild(text("dd", String(value)));
  }
}

/* --- data callbacks ---------------------------------------------------------------------------- */
window.kbManifest = (m) => { MANIFEST = m; };
window.kbStatus = (s) => { STATUS = s; };
window.kbCross = (arr) => { CROSS = arr; rebuildCross(); };
window.kbIndex = (arr) => { IDX = arr; };

window.kbRecv = (domain, data) => {
  const spot = pendingPos[domain];
  delete pendingPos[domain];
  if (spot) {                                   // seed members around the domain's own position
    const radius = Math.min(450, 60 + data.nodes.length * 3);
    for (const n of data.nodes) {
      const angle = Math.random() * 6.283, dist = Math.sqrt(Math.random()) * radius;
      n.x = spot.x + Math.cos(angle) * dist;
      n.y = spot.y + Math.sin(angle) * dist;
    }
  }
  const nodeIds = nodes.add(data.nodes), edgeIds = edges.add(data.edges);
  loaded[domain] = { nodeIds, edgeIds };
  if (nodes.get("domain:" + domain)) nodes.remove("domain:" + domain);
  rebuildCross();
  if (pendingFocus && nodes.get(pendingFocus)) {
    net.selectNodes([pendingFocus]);
    net.focus(pendingFocus, { scale: 1.1, animation: true });
    pendingFocus = null;
  }
};

/* --- graph ------------------------------------------------------------------------------------- */
function representative(id, domain) { return loaded[domain] ? id : "domain:" + domain; }

// A super-node carries its domain as `domkey`, a member as `group`.
const domainOfNode = (n) => (n ? n.domkey || n.group : undefined);
const domainOfId = (id) => domainOfNode(nodes.get(id));

/* Hiding a domain filters the views the network draws; the data sets themselves stay whole, so
 * expanding, collapsing and the cross-domain rerouting work the same whether a domain is shown or not. */
function setShown(domain, shown) {
  if (shown) hidden.delete(domain); else hidden.add(domain);
  net.nodesView.refresh();
  net.edgesView.refresh();
}

function rebuildCross() {
  if (!CROSS || !edges) return;
  if (crossIds.length) edges.remove(crossIds);
  const aggregated = new Map(), individual = [];
  for (const [from, fromDomain, to, toDomain, relation] of CROSS) {
    const a = representative(from, fromDomain), b = representative(to, toDomain);
    if (a === b) continue;
    const bothCollapsed = a.startsWith("domain:") && b.startsWith("domain:");
    if (bothCollapsed) {
      const key = [a, b].sort().join("|");
      const entry = aggregated.get(key) || { from: a, to: b, count: 0, http: false };
      entry.count++;
      if (relation === "http_request") entry.http = true;
      aggregated.set(key, entry);
    } else {
      individual.push({
        from: a, to: b, title: relation, arrows: "to",
        color: { color: relation === "http_request" ? RED : BLUE, opacity: 0.7 },
        width: relation === "http_request" ? 3 : 2,
      });
    }
  }
  const batch = [];
  aggregated.forEach((v) => batch.push({
    from: v.from, to: v.to,
    title: v.count + " links" + (v.http ? " · incl. http_request" : ""),
    color: { color: v.http ? RED : BLUE, opacity: v.http ? 0.7 : 0.45 },
    width: Math.min(8, 1 + v.count / 4),
  }));
  crossIds = edges.add(batch.concat(individual));
}

function loadData(file) {
  const s = document.createElement("script");
  s.src = MANIFEST.dataPath + file;
  document.body.appendChild(s);
  return s;
}

function expand(domain) {
  if (loaded[domain]) return;
  const pos = net.getPositions(["domain:" + domain])["domain:" + domain];
  if (pos) pendingPos[domain] = pos;
  loadData(MANIFEST.fileMap[domain] + ".js");
}

function collapse(domain) {
  const entry = loaded[domain];
  if (!entry) return;
  edges.remove(entry.edgeIds);
  nodes.remove(entry.nodeIds);
  delete loaded[domain];
  const superNode = MANIFEST.superNodes.find((n) => n.domkey === domain);
  if (superNode) nodes.add(superNode);
  rebuildCross();
}

function focusNode(id, domain) {
  if (hidden.has(domain)) { setShown(domain, true); renderDomains(); }
  if (loaded[domain]) {
    net.selectNodes([id]);
    net.focus(id, { scale: 1.1, animation: true });
  } else {
    pendingFocus = id;
    expand(domain);
  }
}

/* --- search ------------------------------------------------------------------------------------ */
function ensureIndex(done) {
  if (IDX) return done();
  if (!idxLoading) {
    idxLoading = true;
    loadData("_index.js").onload = () => done();
  } else {
    setTimeout(() => ensureIndex(done), 80);
  }
}

function renderHits(hits) {
  const box = el("qres");
  box.replaceChildren();
  if (!hits.length) {
    box.appendChild(text("i", "no match", "muted"));
    return;
  }
  for (const [label, id, domain] of hits) {
    const row = document.createElement("div");
    row.appendChild(text("span", label));              // textContent: a label may contain markup
    row.appendChild(text("span", " · " + domain, "domain"));
    row.onclick = () => focusNode(id, domain);
    box.appendChild(row);
  }
}

/* --- panels ------------------------------------------------------------------------------------ */
function renderLegend() {
  const box = el("legend");
  box.replaceChildren();
  for (const [shape, meaning] of LEGEND) {
    const row = document.createElement("div");
    row.appendChild(text("b", shape));
    row.appendChild(text("span", " — " + meaning));
    box.appendChild(row);
  }
}

function renderDomains() {
  const box = el("domains");
  box.replaceChildren();
  for (const node of MANIFEST.superNodes) {
    const row = document.createElement("div");
    const tick = document.createElement("input");
    tick.type = "checkbox";
    tick.checked = !hidden.has(node.domkey);
    tick.onchange = () => setShown(node.domkey, tick.checked);
    const swatch = document.createElement("span");
    swatch.className = "dot";
    swatch.style.background = node.color.background;
    const name = text("span", node.label, "name");
    name.onclick = () => (loaded[node.domkey] ? collapse(node.domkey) : expand(node.domkey));
    row.append(tick, swatch, name);
    box.appendChild(row);
  }
}

function showAll(shown) {
  for (const d of MANIFEST.domains) {
    if (shown) hidden.delete(d); else hidden.add(d);
  }
  net.nodesView.refresh();
  net.edgesView.refresh();
  renderDomains();
}

/* --- ask the index (needs `make serve`) ---------------------------------------------------------
 * graphify query and qmd docs search run as processes, so they cannot work from file://. Opened from
 * kb-api the page shares its origin and calls /search directly; opened as a file it says how to get
 * there. The answer is plain text from the same core the MCP tools use. */
function wireAsk() {
  const answer = el("answer");
  const served = location.protocol === "http:" || location.protocol === "https:";
  if (!served) {
    const hint = el("server-hint");
    hint.hidden = false;
    hint.textContent = "Needs the server: run `make serve`, then open http://127.0.0.1:8899/";
  }
  for (const input of document.querySelectorAll("input.ask")) {
    input.disabled = !served;
    input.onkeydown = async (event) => {
      if (event.key !== "Enter" || !input.value.trim()) return;
      answer.hidden = false;
      answer.textContent = "searching… (the first docs search loads a model and can take a while)";
      try {
        const res = await fetch("../search", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ q: input.value.trim(), domain: input.dataset.domain }),
        });
        const body = await res.json();
        answer.textContent = res.ok ? body.text : `error ${res.status}: ${body.error}`;
      } catch (err) {
        answer.textContent = "server unreachable — is `make serve` still running?";
      }
    };
  }
}

function renderStatus() {
  if (!STATUS) {
    el("index-health").appendChild(text("dt", "no snapshot — run `make build`"));
    return;
  }
  defList("index-health", [
    ["nodes", STATUS.graph && STATUS.graph.nodes],
    ["edges", STATUS.graph && STATUS.graph.edges],
    ["domains", STATUS.graph && STATUS.graph.domains],
    ["projects", STATUS.graph && STATUS.graph.projects],
    ["graph built", STATUS.graph && STATUS.graph.built_at],
  ]);
  defList("docs-health", [
    ["collection", STATUS.docs && STATUS.docs.collection],
    ["files", STATUS.docs && STATUS.docs.files],
    ["indexed", STATUS.docs && STATUS.docs.updated],
  ]);
  defList("quality", Object.entries((STATUS.quality && STATUS.quality.metrics) || {})
    .concat(STATUS.quality && STATUS.quality.measured_at
      ? [["measured", STATUS.quality.measured_at]] : []));
  defList("versions", Object.entries(STATUS.versions || {})
    .concat([["snapshot taken", STATUS.takenAt]]));

  const body = document.querySelector("#aliases tbody");
  body.replaceChildren();
  for (const [from, to] of STATUS.aliases || []) {
    const tr = document.createElement("tr");
    tr.appendChild(text("td", from, "from"));
    tr.appendChild(text("td", "→ " + to));
    body.appendChild(tr);
  }
  if (!(STATUS.aliases || []).length) {
    const tr = document.createElement("tr");
    tr.appendChild(text("td", "none configured", "muted"));
    body.appendChild(tr);
  }
}

function wireTabs() {
  for (const button of document.querySelectorAll("#tabs button")) {
    button.onclick = () => {
      for (const other of document.querySelectorAll("#tabs button")) {
        other.classList.toggle("active", other === button);
      }
      el("panel-graph").hidden = button.dataset.panel !== "graph";
      el("panel-status").hidden = button.dataset.panel !== "status";
    };
  }
}

/* --- boot -------------------------------------------------------------------------------------- */
function loadVendorFallback() {
  const s = document.createElement("script");
  s.src = "https://unpkg.com/vis-network/standalone/umd/vis-network.min.js";
  s.onload = () => start();
  s.onerror = () => banner("renderer unavailable: run `make vendor` (needs network once)");
  document.head.appendChild(s);
  banner("using the CDN renderer — run `make vendor` for an offline copy");
}

function start() {
  wireTabs();
  renderLegend();

  if (window.__kbManifestMissing || !MANIFEST) {
    banner("no generated data — run `make build`");
    el("summary").textContent = "no graph data";
    renderStatus();
    return;
  }

  el("summary").textContent =
    `${MANIFEST.domains.length} domains · ${MANIFEST.nodeCount} nodes · members load on demand`;

  nodes = new vis.DataSet(MANIFEST.superNodes);
  edges = new vis.DataSet([]);
  const nodesView = new vis.DataView(nodes, { filter: (n) => !hidden.has(domainOfNode(n)) });
  const edgesView = new vis.DataView(edges, {
    filter: (e) => !hidden.has(domainOfId(e.from)) && !hidden.has(domainOfId(e.to)),
  });
  net = new vis.Network(el("net"), { nodes: nodesView, edges: edgesView }, {
    physics: {
      solver: "forceAtlas2Based",
      stabilization: { iterations: 150 },
      forceAtlas2Based: { gravitationalConstant: -80, springLength: 140, avoidOverlap: 0.5 },
    },
    interaction: { hover: true, tooltipDelay: 120 },
  });

  net.nodesView = nodesView;
  net.edgesView = edgesView;

  net.on("doubleClick", (params) => {
    if (!params.nodes.length) return;
    const id = params.nodes[0];
    if (typeof id === "string" && id.startsWith("domain:")) expand(id.slice(7));
    else {
      const node = nodes.get(id);
      if (node && node.group) collapse(node.group);
    }
  });

  el("expand-all").onclick = () =>
    Object.keys(MANIFEST.fileMap).forEach((d) => { if (!loaded[d] && !hidden.has(d)) expand(d); });
  el("show-all").onclick = () => showAll(true);
  el("hide-all").onclick = () => showAll(false);
  el("collapse-all").onclick = () => Object.keys(loaded).slice().forEach(collapse);

  const input = el("q");
  input.oninput = () => {
    const value = input.value.trim().toLowerCase();
    if (value.length < 2) { el("qres").replaceChildren(); return; }
    ensureIndex(() => renderHits(
      IDX.filter((row) => row[0].toLowerCase().includes(value)).slice(0, 40)));
  };

  renderDomains();
  renderStatus();
  wireAsk();
  loadData("_cross.js");                      // cross-domain structure, once
}

window.addEventListener("load", () => {
  if (window.__kbVendorMissing || typeof vis === "undefined") loadVendorFallback();
  else start();
});
