# Getting started

1. **Two tools** the KB drives: `graphify` (`uv tool install graphifyy`) and `qmd`
   (`npm i -g @tobilu/qmd`), plus Python 3.11+.
2. **Install:**
   ```bash
   uv tool install knowledge-index     # or: pipx install knowledge-index
   kb-install                          # checks the tools, writes a config to fill in
   ```
3. **Name your projects** in `kb.projects.toml` (`kb --version` shows where it is): the code folders, the
   docs folder, and for a Laravel API the app folder (its routes become graph edges).
   ```toml
   [[project]]
   name   = "api"
   code   = ["/srv/shop/api/app"]
   docs   = "/srv/shop/api/docs"
   routes = "/srv/shop/api"

   [[project]]
   name   = "web"
   code   = ["/srv/shop/web/src"]
   calls  = ["/srv/shop/web/src"]      # its API calls are linked to ...
   serves = "api"                      # ... the controllers of this project
   ```
4. **Build:** `kb build` (a few minutes the first time; `kb update` after that).
5. **Connect your agent:** `claude mcp add kb -- kb-mcp` (or the `.mcp.json` entry in
   [Install](install.md#_1-mcp-server-config)), then restart the session.

That is the whole core. Everything else is optional and off until you turn it on.


Try it first without your own code: `make demo` opens the dashboard of a small made-up shop that ships with the repository. Red edges are routes: a Vue view's `api.get('/orders')` linked to the controller method that serves it.

Next: [all settings](configuration.md), [the shared server](server.md).
