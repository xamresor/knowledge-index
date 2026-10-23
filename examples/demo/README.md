# Demo: a fictional shop

A tiny code base to show what the knowledge index builds, with the result committed so you can open it
without installing anything but python3. None of it is real code; it is written to have a little of
everything the graph knows about.

| Path | What |
|---|---|
| `src/shop-api/` | Laravel-style API: 4 controllers, 5 models, 2 services, a migration, 2 docs |
| `src/shop-api/artisan` | **A stub, not Laravel.** It prints the route table `php artisan route:list --json` would |
| `src/shop-web/` | Vue frontend: 4 views and a cart store that call the API through `api.get/post` |
| `graphify-out/` | The built result: dashboard data (`kb-manifest.js`, `kb-status.js`, `kb-graph-data/`), `graph.json`, `GRAPH_REPORT.md` |
| `build.sh` | Rebuilds `graphify-out/` from `src/` |
| `web/` | Not in git: `make demo` copies the shell here, next to the data |

## Open it

```bash
make demo
```

What to look at:
- **Red edges** are routes. `OrdersView.vue` → `OrderController.index()` came from `api.get('/orders')`
  matched against the route table.
- **Double-click `Order`** to see its controller, model, table and methods; edges reroute to the real
  members.
- **Status tab**: node and edge counts, when the graph was built, tool versions.
- `StockService` and `InvoiceService` carry `NOTE:` / `WHY:` comments; they become rationale nodes.

## Rebuild it

```bash
make demo-build      # needs graphify and php on PATH
```

The build runs in a temporary copy of this repo and copies only the results back, so your own
`graphify-out/` is never touched. Machine paths are stripped from the output before it lands here.
Change something in `src/`, rebuild, and `make demo` shows the change.
