.PHONY: build graph kb update label status open qmd-backend help
.DEFAULT_GOAL := help

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

open:   ## Open the full graph visualization in a browser
	@xdg-open graphify-out/kb-graph.html 2>/dev/null || echo "open graphify-out/kb-graph.html"

qmd-backend: ## Re-pick the qmd embedding backend (none|llama|other)
	@bin/kb qmd-backend

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort
