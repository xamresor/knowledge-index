# Shared server templates

Templates for running `bin/kb-http` on a server for a team. Replace the placeholders
(`kb.example.com`, `api.example.com`, `/srv/kb`, user `kb`) with yours.

| File | What |
|---|---|
| `kb-http.service` | systemd unit: `KB_SERVER=on`, the identity endpoint, memory limits |
| `nginx-kb.conf` | TLS in front of `127.0.0.1:8790`, only `POST /mcp` passes |
| `crontab.txt` | `bin/kb-refresh` every minute + a nightly forced rebuild |

The server, its variables and the identity contract are described in the main README, section
"Shared server (off by default)".
