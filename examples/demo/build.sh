#!/usr/bin/env bash
# Rebuild the bundled demo graph (examples/demo/graphify-out/) from the demo shop in src/.
#
# The build runs in a throwaway copy of this checkout, not in the checkout itself: bin/kb stages repos,
# route tables and clustering relative to the checkout root, so building here would overwrite your
# own graph. Only the dashboard data, the graph and the report are copied back.
#
# Needs graphify and php on PATH (php only runs the route-table stub in src/shop-api/artisan).
# Usage: examples/demo/build.sh        (or: make demo-build)
set -euo pipefail

DEMO="$(cd "$(dirname "$0")" && pwd)"
KB="$(cd "$DEMO/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Tracked files only (working-tree versions): no private config, aliases or generated data come along.
git -C "$KB" ls-files -z -- . ':!examples' | rsync -a --from0 --files-from=- "$KB/" "$TMP/"

SRC="$DEMO/src"
cat > "$TMP/kb.projects.toml" <<TOML
[[project]]
name   = "shop-api"
code   = ["$SRC/shop-api/app", "$SRC/shop-api/database"]
docs   = "$SRC/shop-api/docs"
routes = "$SRC/shop-api"

[[project]]
name   = "shop-web"
code   = ["$SRC/shop-web/src"]
calls  = ["$SRC/shop-web/src"]
serves = "shop-api"
TOML

# A collection name that does not exist, so the Status panel cannot pick up your real qmd numbers.
KB_COLLECTION=kb-demo-none KB_ALIASES=/nonexistent "$TMP/bin/kb" graph

OUT="$DEMO/graphify-out"
rm -rf "$OUT"; mkdir -p "$OUT"
cp -r "$TMP/graphify-out/kb-graph-data" "$TMP/graphify-out/kb-manifest.js" "$TMP/graphify-out/kb-status.js" \
      "$TMP/graphify-out/graph.json" "$TMP/graphify-out/GRAPH_REPORT.md" "$OUT/"
# Absolute paths of this machine must not land in git.
{ grep -rl "$SRC" "$OUT" || true; } | xargs -r sed -i "s#$SRC/##g"
{ grep -rl "$TMP" "$OUT" || true; } | xargs -r sed -i "s#$TMP/##g; s#$TMP#examples/demo#g"
echo "demo graph -> $OUT  (open it: make demo)"
