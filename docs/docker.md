# Docker

One image runs the shared server (`kb-http`) with everything it needs: Python, graphify with rustworkx, and
qmd. The indexed code comes in read-only. A local single-user install does not need Docker: use
`uv tool install knowledge-index` ([install](install.md)).

## What is in the image

| Part | Version | Where |
|---|---|---|
| Python | 3.13.18 (`python:3.13.18-slim-bookworm`) | the base image |
| graphify | `graphifyy==0.9.25`, with `rustworkx==0.18.1` | the venv `/opt/kb` |
| knowledge-index | this checkout | the venv `/opt/kb` |
| Node | 24.22.2 (from `node:24.22.2-bookworm-slim`) | `/usr/local/bin/node` |
| qmd | `@tobilu/qmd@2.5.3` | `/usr/local/bin/qmd` |
| run_code sandbox | `sandbox/` with its packages | `/opt/kb/sandbox` |
| rsync, git, ripgrep | Debian bookworm | system packages |

The `Dockerfile` pins every version. `tests/test_docker.py` fails when a pin is missing.

The server runs as the user `kb` (uid 10001), not as root.

## Build

```bash
docker build -t knowledge-index:local .
```

Run it in the root of the checkout. `.dockerignore` is an allow-list: your `kb.projects.toml`, `aliases.toml`,
`users.json`, `smtp.env` and the built index never go into the image.

The image has no text extractors for PDF, DOCX and scans. Add them with a build argument:

```bash
docker build --build-arg EXTRA_PACKAGES="poppler-utils pandoc tesseract-ocr" -t knowledge-index:local .
```

The image leaves out the CUDA and Vulkan builds of llama.cpp (about 670 MB). For a GPU host, keep them with
`--build-arg KEEP_GPU=1` and run the container with `QMD_FORCE_CPU=0`.

## Run

Start from [`docker/compose.example.yml`](../docker/compose.example.yml). Copy it, then replace the `/srv`
paths and the webhook addresses.

```bash
docker compose -f compose.yml up -d
docker compose -f compose.yml exec kb kb build     # the first build
```

`kb-http` starts only with `KB_SERVER=on` and a permission webhook (`KB_PERMISSION_URL`), as outside Docker
([shared server](server.md)). Without them, `kb-http` exits with a message that says what is missing.

The image listens on `0.0.0.0:8790` inside the container. The example publishes the port on `127.0.0.1` of
the host only. Put a TLS proxy in front of it (`examples/server/nginx-kb.conf`).

## Volumes

| Path in the container | Mode | Holds |
|---|---|---|
| `/var/lib/knowledge-index` | read-write, a named volume | the graph, the staged code, the docs index, the qmd model, logs, usage, `addons.json`, `admins.txt`, `users.json` |
| `/etc/knowledge-index` | read-only | `kb.projects.toml`, `kb.hubs.txt` |
| `/code/<project>` | read-only | the indexed code, one mount per project |
| `/routes` | read-only | the route tables of the Laravel apps |

Write the paths in `kb.projects.toml` as the container sees them:

```toml
[[project]]
name   = "api"
code   = ["/code/api/app"]
docs   = "/code/api/docs"
routes = "/code/api"          # marks a Laravel app; the table comes from /routes/api.json

[[project]]
name   = "web"
code   = ["/code/web/src"]
calls  = ["/code/web/src"]
serves = "api"
```

## Environment

All `KB_*` variables work as described in the [configuration reference](configuration.md). The image sets
these:

| Variable | Value in the image | Why |
|---|---|---|
| `KB_HTTP_HOST` | `0.0.0.0` | the port must be reachable from outside the container |
| `KB_HTTP_PORT` | `8790` | |
| `XDG_DATA_HOME` | `/var/lib` | data goes to `/var/lib/knowledge-index` |
| `XDG_CONFIG_HOME` | `/etc` | config comes from `/etc/knowledge-index` |
| `XDG_CACHE_HOME` | `/var/lib/knowledge-index/cache` | the qmd index and model stay in the data volume |
| `QMD_CONFIG_DIR` | `/var/lib/knowledge-index/qmd` | the qmd collection list stays in the data volume |
| `QMD_FORCE_CPU` | `1` | embeddings run on the CPU; the image has no GPU builds of llama.cpp |

Set `KB_QMD_BACKEND` (`llama`, `none` or `other`) in the compose file. A build in a container cannot answer
the question that it asks otherwise. With `llama`, the first build downloads a small model into the data
volume.

## Add-ons

`KB_ADDONS` switches add-ons on, comma-separated. It overrides `addons.json`:

```yaml
KB_ADDONS: tool-multicall,fast-routes,metrics
```

The admin pages need no add-on: they are part of the core.

- `fast-routes` works at once: rustworkx is in the image.
- `code-mode` needs the sandbox container (below).
- A plugin needs its folder in the data volume. For the KB's own users, the image has the plugin.
  Without a permission webhook, `kb-http` does not start, so `exec` has no container to enter. Use a
  one-off container, before the first `up` or with the `kb` service stopped:

  ```bash
  docker compose -f compose.yml run --rm --no-deps kb kb addon install /opt/kb/contrib/users
  docker compose -f compose.yml run --rm --no-deps kb python3 /opt/kb/contrib/users/plugin.py add admin --admin
  ```

  The second command prints the key of the first admin. `kb addon install` writes `addons.json`;
  when `KB_ADDONS` is set, add `users` to it too.

Restart the container after a change: `docker compose restart kb`.

## Route tables from the app's container

There is no PHP in the image, so `bin/kb` cannot run `php artisan route:list`. The Laravel app writes its
route table in its own container, and the KB reads the file.

1. In the app's deploy step (or in a cron job on the host), write the table:

   ```bash
   docker compose -f /srv/shop/compose.yml exec -T api php artisan route:list --json > /srv/kb/routes/api.json.new \
     && mv /srv/kb/routes/api.json.new /srv/kb/routes/api.json
   ```

2. Mount that folder read-only at `/routes` and set `KB_ROUTES_DIR=/routes`.

For each project with `routes` in `kb.projects.toml`, `bin/kb` reads `$KB_ROUTES_DIR/<name>.json`. A
`routes` value that ends in `.json` is read as the table itself, also outside Docker. A broken or missing
file keeps the previous table, as a failed `artisan` run does.

## Update and rebuild

The index follows the code. Run an update after the indexed checkouts change:

```bash
docker compose -f compose.yml exec -T kb kb-refresh        # only when a checkout has a new HEAD
docker compose -f compose.yml exec -T kb kb update         # always
```

For a server, put the first line in the host crontab, once a minute, as in `examples/server/crontab.txt`.
The server sees the new graph without a restart. One build runs at a time: while another build runs,
`kb update` stops at once with exit code 75 and `busy`. Run it again when the build ends.

A new version of the KB, graphify or qmd is a new image:

```bash
git pull && docker build -t knowledge-index:local .
docker compose -f compose.yml up -d                        # recreates the containers
docker compose -f compose.yml exec -T kb kb update
```

The data volume stays. A new graphify version rebuilds the graphs of every project on the next update.

## The sandbox container (code-mode)

`run_code` runs an agent's program in QuickJS inside Node. On a server without Docker, the KB starts that
Node process itself. With Docker, the sandbox can live in its own container:

- no network (`network_mode: none`);
- a read-only root, no Linux capabilities, `no-new-privileges`;
- none of the KB volumes: no index, no config, no code;
- limits on memory, processes and CPU.

The two containers share one volume with a Unix socket. The sandbox container runs
`sandbox/socket-server.mjs`. It listens on the socket and starts one `runner.mjs` for each connection, with
the same limits as outside Docker: the Node permission model, CPU seconds and open files. The KB server
connects to the socket when `KB_SANDBOX_SOCKET` is set. Each `kb()` call of the program comes back over the
socket and runs in the KB server with the caller's own rights.

To switch it on:

1. Add `code-mode` to `KB_ADDONS` of the `kb` service.
2. Start with the profile: `docker compose -f compose.yml --profile code-mode up -d`.

Without `KB_SANDBOX_SOCKET`, the KB server starts the runner in its own container. The image has Node and the
sandbox packages, so that works too, with less isolation. When `KB_SANDBOX_SOCKET` is set and the sandbox
container is down, `run_code` answers that the sandbox service does not answer.

`KB_SANDBOX_MAX` (default `4`) limits the programs that run at once. Keep `KB_RUN_SECONDS` the same in
both containers.
