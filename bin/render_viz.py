#!/usr/bin/env python3
"""Render an enriched graphify graph as a HIERARCHICAL, lazily-loaded viz (vis-network).

A tiny shell with domain super-nodes; per-domain member files load on demand; cross-domain edges
reroute to real member nodes on expand. 2D canvas (vis-network) — no WebGL, works on file://.

  node SHAPE = type        node COLOR = domain
  edge color = scope:      red = route (http_request) · blue = cross-domain · green = internal

Outputs kb-graph.html + kb-graph-data/{<domain>.js,_cross.js,_index.js}.
Usage: render_viz.py <graph.json> <out.html>
"""
from __future__ import annotations

import colorsys
import json
import os
import re
import sys
from collections import Counter, defaultdict

# node type -> vis-network shape
SHAPE = {
    "class": "dot", "controller": "hexagon", "service": "diamond", "repository": "diamond",
    "resource": "box", "request": "box", "model": "database",
    "db_table": "square", "method": "triangle", "function": "triangle",
    "interface": "diamond", "trait": "diamond", "enum": "star", "template": "star",
    "file": "dot", "symbol": "dot", "rationale": "text",
}
BIG = {"controller", "model", "db_table"}     # types drawn a bit larger
LEGEND = [("class / file", "dot ●"), ("method / function", "triangle ▲"),
          ("controller", "hexagon ⬡"), ("model", "database"), ("db_table", "square ▭"),
          ("resource / request", "box"), ("service / repository / interface / trait", "diamond ◆"),
          ("enum / template (.vue)", "star ★"), ("rationale (NOTE/WHY/HACK/SECURITY)", "text")]
RED, BLUE, GREEN = "#e15759", "#4e79a7", "#59a14f"


def color_for(domain: str) -> str:
    h = (hash(domain) % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(h, 0.55, 0.65)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


TEMPLATE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>Code Knowledge Base</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
 body{margin:0;background:#1a1a1d;color:#ddd;font:13px system-ui}
 #net{position:absolute;top:0;left:300px;right:0;bottom:0}
 #side{position:absolute;top:0;left:0;width:300px;bottom:0;overflow:auto;padding:10px;
   box-sizing:border-box;background:#202024;border-right:1px solid #333}
 h3{margin:8px 0 4px} button,input{width:100%;margin:4px 0;background:#2a2a2e;color:#eee;
   border:1px solid #444;padding:6px;cursor:pointer} button:hover{background:#34343a}
 #legend div{margin:2px 0} #hint{color:#9a9;margin:6px 0} #qres div{padding:2px 4px;cursor:pointer}
</style></head><body>
<div id="side">
 <h3>Code Knowledge Base</h3>
 <div>__NDOM__ domains · members load on demand</div>
 <input id="q" placeholder="search a class / method / table…" autocomplete="off">
 <div id="qres" style="max-height:150px;overflow:auto;font-size:12px"></div>
 <button onclick="expandAll()">☢ Expand all domains</button>
 <button onclick="collapseAll()">⊟ Collapse all</button>
 <div id="hint">▶ double-click a domain to expand · double-click a member to collapse</div>
 <h3>Shapes = type</h3><div id="legend">__LEGEND__</div>
 <h3>Edge colors</h3>
 <div style="font-size:12px;line-height:1.8">
  <span style="color:#e15759">━</span> route (http_request)<br>
  <span style="color:#4e79a7">━</span> cross-domain link<br>
  <span style="color:#59a14f">━</span> domain-internal link</div>
 <h3>Colors = domain</h3>
</div>
<div id="net"></div>
<script>
 const SUPER_NODES=__SUPER__, FMAP=__FMAP__;
 const RED="#e15759", BLUE="#4e79a7", GREEN="#59a14f";
 const nodes=new vis.DataSet(SUPER_NODES), edges=new vis.DataSet([]);
 const net=new vis.Network(document.getElementById('net'),{nodes,edges},{
   physics:{solver:'forceAtlas2Based',stabilization:{iterations:150},
     forceAtlas2Based:{gravitationalConstant:-80,springLength:140,avoidOverlap:0.5}},
   interaction:{hover:true,tooltipDelay:120}});

 const loaded={};               // domain -> {nodeIds,edgeIds} (members + intra edges)
 let CROSS=null, crossIds=[], pendingFocus=null, pendingPos={};

 function rep(id,dom){ return loaded[dom] ? id : 'domain:'+dom; }
 function rebuildCross(){
   if(!CROSS) return;
   if(crossIds.length) edges.remove(crossIds);
   const agg=new Map(), indiv=[];
   for(const [a,ad,b,bd,rel] of CROSS){
     const ra=rep(a,ad), rb=rep(b,bd); if(ra===rb) continue;
     const bothSuper=ra[0]==='d'&&rb[0]==='d'&&ra.startsWith('domain:')&&rb.startsWith('domain:');
     if(bothSuper){
       const k=[ra,rb].sort().join('|'); const v=agg.get(k)||{from:ra,to:rb,c:0,http:false};
       v.c++; if(rel==='http_request')v.http=true; agg.set(k,v);
     } else indiv.push({from:ra,to:rb,title:rel,
       color:{color:rel==='http_request'?RED:BLUE,opacity:0.7},
       width:rel==='http_request'?3:2,arrows:'to'});
   }
   const add=[];
   agg.forEach(v=>add.push({from:v.from,to:v.to,title:v.c+' links'+(v.http?' · incl http_request':''),
     color:{color:v.http?RED:BLUE,opacity:v.http?0.7:0.45},width:Math.min(8,1+v.c/4)}));
   crossIds=edges.add(add.concat(indiv));
 }

 window.kbRecv=(d,data)=>{
   const p=pendingPos[d]; delete pendingPos[d];
   if(p){ const r=Math.min(450,60+data.nodes.length*3);   // seed members at the domain's spot
     data.nodes.forEach(n=>{ const a=Math.random()*6.283, q=Math.sqrt(Math.random())*r;
       n.x=p.x+Math.cos(a)*q; n.y=p.y+Math.sin(a)*q; }); }
   const nids=nodes.add(data.nodes), eids=edges.add(data.edges);
   loaded[d]={nodeIds:nids,edgeIds:eids};
   const s=nodes.get('domain:'+d); if(s) nodes.remove('domain:'+d);   // hide super-node
   rebuildCross();
   if(pendingFocus && nodes.get(pendingFocus)){
     net.selectNodes([pendingFocus]); net.focus(pendingFocus,{scale:1.1,animation:true}); pendingFocus=null;
   }
 };
 window.kbCross=arr=>{ CROSS=arr; rebuildCross(); };

 function expand(d){
   if(loaded[d]) return;
   const pos=net.getPositions(['domain:'+d])['domain:'+d];   // remember the domain's spot
   if(pos) pendingPos[d]=pos;
   const s=document.createElement('script'); s.src='kb-graph-data/'+FMAP[d]+'.js';
   document.body.appendChild(s);
 }
 function collapse(d){
   const L=loaded[d]; if(!L) return;
   edges.remove(L.edgeIds); nodes.remove(L.nodeIds); delete loaded[d];
   nodes.add(SUPER_NODES.find(n=>n.domkey===d));                      // restore super-node
   rebuildCross();
 }
 function expandAll(){ Object.keys(FMAP).forEach(d=>{ if(!loaded[d]) expand(d); }); }
 function collapseAll(){ Object.keys(loaded).slice().forEach(collapse); }
 function focusNode(id,d){
   if(loaded[d]){ net.selectNodes([id]); net.focus(id,{scale:1.1,animation:true}); }
   else { pendingFocus=id; expand(d); }
 }
 net.on('doubleClick',p=>{
   if(!p.nodes.length) return; const id=p.nodes[0];
   if(typeof id==='string' && id.startsWith('domain:')) expand(id.slice(7));
   else { const n=nodes.get(id); if(n && n.group) collapse(n.group); }
 });

 // load cross-edge structure at startup
 (function(){ const s=document.createElement('script'); s.src='kb-graph-data/_cross.js';
   document.body.appendChild(s); })();

 // lazy search
 let IDX=null, idxLoading=false;
 window.kbIndex=arr=>{ IDX=arr; };
 function ensureIndex(cb){ if(IDX) return cb();
   if(!idxLoading){ idxLoading=true; const s=document.createElement('script');
     s.src='kb-graph-data/_index.js'; s.onload=()=>cb(); document.body.appendChild(s);
   } else setTimeout(()=>ensureIndex(cb),80); }
 const qel=document.getElementById('q'), qres=document.getElementById('qres');
 qel.oninput=()=>{ const v=qel.value.trim().toLowerCase(); qres.innerHTML='';
   if(v.length<2) return;
   ensureIndex(()=>{ const hits=IDX.filter(r=>r[0].toLowerCase().includes(v)).slice(0,40);
     qres.innerHTML=hits.length?'':'<i style="color:#888">no match</i>';
     hits.forEach(([label,id,d])=>{ const el=document.createElement('div');
       el.innerHTML=label+' <span style="color:#888">· '+d+'</span>';
       el.onclick=()=>focusNode(id,d); qres.appendChild(el); }); }); };
</script></body></html>"""


def main() -> int:
    graph_path, out = sys.argv[1], sys.argv[2]
    data_dir = os.path.join(os.path.dirname(out), "kb-graph-data")
    os.makedirs(data_dir, exist_ok=True)
    g = json.load(open(graph_path))

    real = [n for n in g["nodes"] if n.get("type") != "domain"]
    by_id = {n["id"]: n for n in real}
    dom_of = {n["id"]: n.get("domain", "misc") for n in real}
    domains = sorted(set(dom_of.values()))
    palette = {d: color_for(d) for d in domains}
    counts = Counter(dom_of.values())
    fname = {d: safe(d) for d in domains}

    members = defaultdict(list)
    intra = defaultdict(list)
    cross = []
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
    for e in g["links"]:
        rel = e.get("relation", "")
        s, t = e["source"], e["target"]
        if rel == "in_domain" or s not in by_id or t not in by_id:
            continue
        ds, dt = dom_of[s], dom_of[t]
        if ds == dt:
            http = rel == "http_request"
            edge = {"from": s, "to": t,
                    "color": {"color": RED if http else GREEN, "opacity": 0.6 if http else 0.4},
                    "width": 3 if http else 1}
            if e.get("confidence") == "AMBIGUOUS":
                edge["dashes"] = True  # heuristic edge — visibly less trustworthy
            if http:
                edge["arrows"] = "to"
            intra[ds].append(edge)
        else:
            cross.append([s, ds, t, dt, rel])

    for d in domains:
        with open(os.path.join(data_dir, fname[d] + ".js"), "w") as f:
            f.write(f"kbRecv({json.dumps(d)},{json.dumps({'nodes': members[d], 'edges': intra[d]})});")
    with open(os.path.join(data_dir, "_cross.js"), "w") as f:
        f.write(f"kbCross({json.dumps(cross)});")
    with open(os.path.join(data_dir, "_index.js"), "w") as f:
        f.write(f"kbIndex({json.dumps([[n['label'], n['id'], dom_of[n['id']]] for n in real])});")

    super_nodes = [{"id": f"domain:{d}", "label": f"{d} ({counts[d]})", "shape": "dot", "domkey": d,
                    "color": {"background": palette[d], "border": "#111"},
                    "size": min(70, 14 + counts[d] / 4), "borderWidth": 3,
                    "font": {"size": 18, "color": "#fff", "strokeWidth": 4, "strokeColor": "#111"},
                    "title": f"{d} — {counts[d]} nodes (double-click to expand)"} for d in domains]

    legend = "".join(f"<div><b>{s}</b> — {lbl}</div>" for lbl, s in LEGEND)
    html = (TEMPLATE
            .replace("__SUPER__", json.dumps(super_nodes))
            .replace("__FMAP__", json.dumps(fname))
            .replace("__LEGEND__", legend)
            .replace("__NDOM__", str(len(domains))))
    open(out, "w").write(html)
    print(f"rendered {out} ({round(os.path.getsize(out)/1024)} KB shell, vis-network) + "
          f"{len(domains)} domain files + _cross ({len(cross)}) + _index")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
