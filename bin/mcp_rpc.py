#!/usr/bin/env python3
"""MCP JSON-RPC over HTTP, shared by bin/kb-http (the team server) and bin/kb_api.py (the local API).

One place decides what a message gets: a notification (no `id`) runs nothing and gets no answer, a
message without `"jsonrpc": "2.0"` is an invalid request, a fault inside a tool is an error answer of
that message, and a fault outside a tool is an internal error of that message. A batch has at most
MAX_BATCH messages, checked before anything runs, and its answers stop at MAX_RESPONSE bytes: the
messages after that get an error answer and do not run. So one request cannot make the server build a
reply of hundreds of megabytes (16 000 `tools/list` fit into a 750 KB body).
"""
from __future__ import annotations

import json
import sys
import time

import kb_core

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_BATCH = 50                      # messages in one batch
MAX_RESPONSE = 16 * 1024 * 1024     # bytes of the answers of one batch


def rpc_result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def rpc_error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _log(msg: str) -> None:
    sys.stderr.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def handle_message(msg, *, call, instructions, extra_tools=(), extra_calls=None, on_call=None,
                   on_initialize=None, log=_log) -> dict | None:
    """One JSON-RPC message -> answer (None for a notification). Never raises.

    `call(name, args) -> text` runs a core tool (ToolError = an error answer). `instructions()` is the
    text of `initialize`. `extra_tools` are schemas besides the core's, answered by
    `extra_calls[name](args) -> text`. `on_call(name, args, ok, seconds)` counts a core tool call."""
    try:
        return _handle(msg, call, instructions, extra_tools, extra_calls or {}, on_call, on_initialize, log)
    except Exception as e:  # noqa: BLE001 — one bad message must not drop the connection
        log(f"rpc: internal error {type(e).__name__}: {str(e)[:200]}")
        if not isinstance(msg, dict) or "id" not in msg:
            return None
        return rpc_error(msg.get("id"), -32603, "internal error")


def _handle(msg, call, instructions, extra_tools, extra_calls, on_call, on_initialize, log) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
    method, mid = msg.get("method"), msg.get("id")
    if "id" not in msg:
        # A notification gets no answer, and a request without an id is a notification: it never
        # starts a tool (notifications/initialized and any other).
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        proto = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        if on_initialize:
            on_initialize()
        return rpc_result(mid, {
            "protocolVersion": proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "kb", "version": kb_core.VERSION, "apiContract": kb_core.API_CONTRACT},
            "instructions": instructions()})
    if method == "ping":
        return rpc_result(mid, {})
    if method == "tools/list":
        return rpc_result(mid, {"tools": [*kb_core.tools(), *extra_tools]})
    if method == "tools/call":
        if not isinstance(params, dict):
            return rpc_error(mid, -32602, "params must be an object")
        name, args = params.get("name", ""), params.get("arguments") or {}
        if not isinstance(name, str):
            return rpc_error(mid, -32602, "name must be a string")
        if name in extra_calls:
            return rpc_result(mid, {"content": [{"type": "text", "text": extra_calls[name](args)}], "isError": False})
        if name not in kb_core.TOOLS_BY_NAME:
            return rpc_error(mid, -32602, f"unknown tool: {name}")
        if not isinstance(args, dict):
            return rpc_error(mid, -32602, "arguments must be an object")
        t0 = time.monotonic()  # kb_core limits the processes at once (KB_MAX_*_JOBS, bounded wait)
        try:
            text, err = call(name, args), False
        except kb_core.ToolError as e:
            text, err = f"(error: {e})", True
        except Exception as e:  # noqa: BLE001 — a fault in a tool is an error answer, not a dropped socket
            log(f"call tool={name} failed: {type(e).__name__}: {str(e)[:200]}")
            text, err = f"(error: the tool failed inside the server: {type(e).__name__})", True
        if on_call:
            on_call(name, args, not err, time.monotonic() - t0)
        return rpc_result(mid, {"content": [{"type": "text", "text": text}], "isError": err})
    return rpc_error(mid, -32601, f"method not found: {method}")


def handle_payload(payload, one) -> tuple[int, dict | list | None]:
    """A parsed POST body -> (HTTP status, JSON body or None for 202). `one(msg)` answers one message."""
    if payload == []:
        return 400, rpc_error(None, -32600, "invalid request: an empty batch")
    if isinstance(payload, dict):
        answer = one(payload)
        return (200, answer) if answer is not None else (202, None)
    if not isinstance(payload, list):
        return 400, rpc_error(None, -32600, "invalid request")
    if len(payload) > MAX_BATCH:
        return 400, rpc_error(None, -32600, f"invalid request: a batch has {MAX_BATCH} messages at most")
    answers, size = [], 0
    for msg in payload:
        if size > MAX_RESPONSE:   # the rest does not run; a request among it gets an error answer
            if not isinstance(msg, dict) or "id" in msg:
                answers.append(rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32000,
                                         "the answer is too large: send fewer messages in one batch"))
            continue
        answer = one(msg)
        if answer is not None:
            answers.append(answer)
            size += len(json.dumps(answer))
    return (200, answers) if answers else (202, None)
