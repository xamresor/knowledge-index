// run_code sandbox: one agent script in QuickJS (WebAssembly) inside this Node process.
//
// Two walls. (1) The script runs in QuickJS compiled to WebAssembly: it has no `require`, no `process`,
// no files, no network, no timers — only what this file hands it: `kb(tool, args)`, `print(...)` and
// `JSON`. (2) This Node process is started by kb_core with the permission model on (`--permission`,
// read access to this folder only): no network, no child processes, no workers, no file writes, even
// if (1) were broken. A memory limit, a stack limit and a deadline stop runaway scripts.
//
// Protocol over stdio, one JSON object per line:
//   in   {"code": "...", "deadline_ms": 20000, "memory_mb": 64, "max_calls": 30, "max_output": 60000}
//        (max_output caps the printed output and, separately, the returned value)
//   out  {"call": "<tool>", "args": {...}}      ->  in  {"result": "<text>"} | {"error": "<text>"}
//   out  {"done": true, "output": "...", "value": <json>|null, "error": "<text>"|null, "calls": n}
import { readSync, writeSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { getQuickJS, shouldInterruptAfterDeadline } from "quickjs-emscripten";

let pending = "";
function readLine() {
  const buf = Buffer.alloc(65536);
  for (;;) {
    const nl = pending.indexOf("\n");
    if (nl >= 0) {
      const line = pending.slice(0, nl);
      pending = pending.slice(nl + 1);
      return line;
    }
    let n = 0;
    try {
      n = readSync(0, buf, 0, buf.length, null);
    } catch (e) {
      if (e.code === "EAGAIN") continue;
      throw e;
    }
    if (n === 0) throw new Error("host closed the channel");
    pending += buf.toString("utf8", 0, n);
  }
}
const send = (obj) => writeSync(1, JSON.stringify(obj) + "\n");

const req = JSON.parse(readLine());
const deadlineMs = Math.min(Number(req.deadline_ms) || 20000, 60000);
const maxCalls = Math.min(Number(req.max_calls) || 30, 100);
const maxOutput = Math.min(Number(req.max_output) || 60000, 200000);

const QuickJS = await getQuickJS();
const runtime = QuickJS.newRuntime();
runtime.setMemoryLimit(Math.min(Number(req.memory_mb) || 64, 256) * 1024 * 1024);
runtime.setMaxStackSize(1024 * 1024);
const started = Date.now();
runtime.setInterruptHandler(shouldInterruptAfterDeadline(started + deadlineMs));
const vm = runtime.newContext();

let output = "";
let calls = 0;
const out = (s) => {
  if (output.length < maxOutput) output += s.slice(0, maxOutput - output.length);
};

const print = vm.newFunction("print", (...args) => {
  out(args.map((h) => {
    const v = vm.dump(h);
    return typeof v === "string" ? v : JSON.stringify(v);
  }).join(" ") + "\n");
});
vm.setProp(vm.global, "print", print);
print.dispose();

const kb = vm.newFunction("kb", (toolHandle, argsHandle) => {
  const tool = vm.getString(toolHandle);
  const args = argsHandle ? vm.dump(argsHandle) : {};
  if (++calls > maxCalls) return { error: vm.newError(`more than ${maxCalls} kb() calls`) };
  if (Date.now() - started > deadlineMs) return { error: vm.newError("time is up") };
  send({ call: tool, args: args && typeof args === "object" ? args : {} });
  const reply = JSON.parse(readLine());
  if (reply.error !== undefined) return { error: vm.newError(String(reply.error)) };
  return vm.newString(String(reply.result ?? ""));
});
vm.setProp(vm.global, "kb", kb);
kb.dispose();

// The script is a function body: `return` gives the value; print() writes the output. It may be
// TypeScript: Node strips the types (they become blanks, so line and column numbers stay) before
// QuickJS sees the code. Plain JavaScript passes through unchanged.
let source;
try {
  source = stripTypeScriptTypes(`(function () {\n${String(req.code)}\n})()`, { mode: "strip" });
} catch (e) {
  send({ done: true, output: "", value: null, error: `TypeScript: ${e.message}`, calls: 0 });
  process.exit(0);
}
const result = vm.evalCode(source, "agent.ts");
let value = null;
let error = null;
if (result.error) {
  const e = vm.dump(result.error);
  error = typeof e === "object" && e !== null ? `${e.name || "Error"}: ${e.message || JSON.stringify(e)}` : String(e);
  result.error.dispose();
} else {
  value = vm.dump(result.value);
  result.value.dispose();
  // The value is capped like the output: the host reads it whole and may keep it behind a cursor.
  if (value !== undefined && value !== null) {
    let text;
    try {
      text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    } catch (e) {
      text = String(value);
    }
    if (text !== undefined && text.length > maxOutput) {
      value = `${text.slice(0, maxOutput)}\n… (the returned value is cut at ${maxOutput} characters: return less)`;
    }
  }
}
vm.dispose();
runtime.dispose();
send({ done: true, output, value: value === undefined ? null : value, error, calls });
