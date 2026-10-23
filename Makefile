.PHONY: install check build graph kb update label status open vendor demo demo-build qmd-backend serve test help
.DEFAULT_GOAL := help

install: ## First-run setup: check requirements, create config, print the MCP snippet
	@bin/install

check:  ## Report what is missing without writing anything
	@bin/install --check

build:  ## Full build: cross-repo graph + qmd KB
	@bin/kb build

graph:  ## Rebuild the merged code graph only
	@bin/kb graph

kb:     ## Reindex docs into the qmd collection only
	@bin/kb kb

update: ## Refresh graph + KB (AST + embeddings, no API cost)
	@bin/kb update

label:  ## (Re)name communities by namespace/domain (deterministic, no API cost)
	@bin/kb label

status: ## Graph diagnostics + qmd index health
	@bin/kb status

open:   ## Open the dashboard (graph + status) in a browser
	@bin/kb open

vendor: ## Download the renderer once for offline use (git-ignored; works installed too)
	@bin/kb vendor

demo:   ## Open the bundled demo dashboard (a fictional shop) — no build, no config needed
	@KB_HOME="$(CURDIR)/examples/demo" python3 -c "import sys; sys.path.insert(0,'bin'); import paths; paths.stage_web()"
	@if [ -f web/vendor/vis-network.min.js ]; then mkdir -p examples/demo/web/vendor && \
		cp web/vendor/vis-network.min.js examples/demo/web/vendor/; fi
	@xdg-open examples/demo/web/index.html 2>/dev/null || echo "open examples/demo/web/index.html"

demo-build: ## Rebuild the demo graph from examples/demo/src (needs graphify + php)
	@examples/demo/build.sh

qmd-backend: ## Re-pick the qmd embedding backend (none|llama|other)
	@bin/kb qmd-backend

serve:  ## HTTP API on 127.0.0.1:8899 (same core as MCP; PORT=… TOKEN=… to override)
	@bin/kb-api --port $${PORT:-8899} $${TOKEN:+--token $$TOKEN}

test:   ## Run the unit tests (stdlib unittest, no deps)
	@python3 -m unittest discover -s tests

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort
