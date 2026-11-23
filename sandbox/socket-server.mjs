// run_code sandbox as a service: one runner.mjs per connection on a Unix socket.
//
// The KB server normally starts runner.mjs itself (kb_core.run_code). With KB_SANDBOX_SOCKET set there,
// it connects to this service instead, and the runner lives in another container: no network, a
// read-only root and no access to the index or the config (docs/docker.md). The protocol is the
// runner's own (one JSON object per line, see runner.mjs); this file only pipes the socket to it.
//
// Each runner gets the same walls as on the KB host: the Node permission model with read access to
// this folder only, a CPU-seconds limit and at most 64 open files (ulimit). At most KB_SANDBOX_MAX
// runners at once; one more connection gets a "busy" answer at once.
import { spawn } from "node:child_process";
import { chmodSync, unlinkSync } from "node:fs";
import net from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const DIR = dirname(fileURLToPath(import.meta.url));
const SOCKET = process.env.KB_SANDBOX_SOCKET || "/run/kb-sandbox/sandbox.sock";
const MAX = Math.max(1, Number(process.env.KB_SANDBOX_MAX) || 4);
const CPU = (Number(process.env.KB_RUN_SECONDS) || 20) + 10;
const log = (s) => process.stderr.write(`${new Date().toISOString()} kb-sandbox: ${s}\n`);

let busy = 0;
const server = net.createServer((sock) => {
  sock.on("error", () => {});
  if (busy >= MAX) {
    sock.end(JSON.stringify({ done: true, output: "", value: null, calls: 0,
      error: `the sandbox runs ${MAX} programs already; try again` }) + "\n");
    return;
  }
  busy++;
  const child = spawn("/bin/sh", ["-c", `ulimit -t ${CPU} -n 64; exec "$@"`, "sh",
    process.execPath, "--permission", `--allow-fs-read=${DIR}`, join(DIR, "runner.mjs")],
    { cwd: DIR, env: { PATH: process.env.PATH || "/usr/bin:/bin", NODE_OPTIONS: "" } });
  let err = "";
  child.stderr.on("data", (d) => { err = (err + d).slice(-400); });
  child.stdin.on("error", () => {});
  sock.pipe(child.stdin);
  child.stdout.pipe(sock);
  sock.on("close", () => { if (child.exitCode === null) child.kill("SIGKILL"); });
  child.on("close", (code, signal) => {
    busy--;
    if (code !== 0) log(`runner stopped (${signal || code}): ${err.trim() || "no message"}`);
    sock.end();
  });
});

try { unlinkSync(SOCKET); } catch { /* no old socket */ }
server.listen(SOCKET, () => {
  chmodSync(SOCKET, 0o660);  // the KB server runs as the same user (uid 10001 in the image)
  log(`listening on ${SOCKET}, at most ${MAX} programs at once`);
});
for (const sig of ["SIGTERM", "SIGINT"]) process.on(sig, () => server.close(() => process.exit(0)));
