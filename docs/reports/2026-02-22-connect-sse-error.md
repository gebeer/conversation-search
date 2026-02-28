# Bug Report: `connect` fails with `RemoteProtocolError` immediately after SSE handshake

**Date:** 2026-02-22
**Branch:** feature/daemon-mode
**Symptom:** `uv run conversation_search.py connect` exits immediately with traceback
**Reproducible:** Yes (interactive terminal only — not yet tested under Claude Code)

---

## Observed Error

```
HTTP Request: GET http://127.0.0.1:9237/sse "HTTP/1.1 200 OK"
Error in sse_reader
httpx.RemoteProtocolError: peer closed connection without sending complete message body
  (incomplete chunked read)
```

The SSE GET succeeds (200 OK, `endpoint` event received). The error occurs on the *next* SSE read iteration — before any MCP messages are exchanged.

---

## What the Error Means

The daemon's uvicorn server sends the SSE stream using HTTP/1.1 chunked transfer encoding. `peer closed connection without sending complete message body (incomplete chunked read)` means the server closed the TCP connection mid-stream without sending the terminating `0\r\n\r\n` chunk. This is an *abrupt* server-side close, not a clean stream end.

---

## Call Chain: How the Daemon Closes the Connection

```
daemon: FastMCP.sse_app() → handle_sse()
          └─ SseServerTransport.connect_sse()
               ├─ task: response_wrapper → EventSourceResponse (SSE stream)
               └─ yield (read_stream, write_stream) to handle_sse
                    └─ mcp_server.run(read_stream, write_stream)
                         └─ ServerSession._receive_loop()
                              └─ async for message in read_stream  [BLOCKS - no messages]
```

The SSE connection stays open as long as `response_wrapper` (and therefore `EventSourceResponse`) is running. `EventSourceResponse` exits when:
1. `sse_stream_reader` is exhausted (all SSE events sent and sender closed), **or**
2. It detects an HTTP client disconnect via ASGI `receive` (`http.disconnect`)

When `EventSourceResponse` exits, `response_wrapper` closes `read_stream_writer` → `read_stream` exhausts → `_receive_loop` exits → `mcp_server.run()` returns → `connect_sse`'s task group exits (cancelling `response_wrapper` if still running) → **abrupt SSE stream close**.

---

## Call Chain: Client Side Bridge

```
connect: _run_connect()
  └─ _ensure_daemon_running()  [passes - daemon is up]
  └─ anyio.run(_bridge)
       └─ stdio_server() as (stdio_read, stdio_write)
            └─ sse_client(sse_url, sse_read_timeout=960) as (sse_read, sse_write)
                 [sse_reader receives `endpoint` event, task_status.started() called]
                 └─ task group:
                      ├─ forward_to_daemon: async for msg in stdio_read → sse_write.send(msg)
                      └─ forward_to_client: async for msg in sse_read  → stdio_write.send(msg)
```

Inside `sse_client`, `sse_reader` and `post_writer` run concurrently:
- `sse_reader` iterates `event_source.aiter_sse()` — blocks waiting for more SSE events
- `post_writer` iterates `write_stream_reader` — blocks waiting for messages to POST

---

## Root Cause: Unknown — Needs Active Investigation

Static analysis cannot determine *why* the daemon closes the SSE connection immediately. The daemon should stay in `_receive_loop` waiting for an MCP `initialize` message indefinitely, with `EventSourceResponse` staying alive.

**Primary hypothesis:** Something causes `EventSourceResponse` to see an `http.disconnect` from uvicorn. This would happen if the `connect` process closes its side of the HTTP connection. If `sse_client`'s internal httpx connection closes (e.g., due to a task cancellation propagating through the nested task groups), uvicorn would detect the disconnect and trigger the unwind.

**Secondary hypothesis:** `stdio_server()` behaves unexpectedly with a non-piped terminal stdin. `async for line in stdin` (where `stdin = anyio.wrap_file(TextIOWrapper(sys.stdin.buffer))`) may immediately return EOF or raise in an interactive terminal context under anyio's asyncio backend. If `stdin_reader` exits, `read_stream_writer` closes, `stdio_read` exhausts, `forward_to_daemon` exits. Normally this doesn't cancel the task group (normal completion), but the downstream exception from the failing `sse_read` (see below) might interact with anyio task group semantics in a way that triggers a cascade.

**Exception propagation note:** `sse_reader` catches the `RemoteProtocolError` and sends it as an exception to `read_stream_writer` (which feeds `sse_read`). When `forward_to_client` iterates `sse_read`, it receives the exception and re-raises it. This raises in `_bridge`'s task group, which cancels `forward_to_daemon`. But this is downstream — something must cause the daemon to close *first*.

---

## Key Question: Does It Work Under Claude Code?

The entire error chain requires the daemon to close the SSE connection. If this only happens when `connect` is run interactively (stdin = terminal), and not when Claude Code pipes stdin properly, the bridge may work correctly in production.

**Not yet tested.** This test is the single most valuable next step.

---

## Stack Layers Inspected

| Layer | Finding |
|-------|---------|
| `mcp` version | 1.26.0 |
| `sse_client` endpoint validation | Origin check passes (127.0.0.1:9237 matches) |
| `ServerSession._receive_loop` | No init timeout found |
| `sse_starlette` ping | 15s default — sends SSE comment lines, handled by client |
| `EventSourceResponse` disconnect | Responds to ASGI `http.disconnect` from uvicorn |

---

## Recommended Investigation Steps

1. **Test under Claude Code** — configure MCP with `connect`, open a session, run a search. If it works, the bug is terminal-stdin-only and low priority.

2. **Enable debug logging** — run daemon with `PYTHONLOGLEVEL=DEBUG` and `connect` separately, capture full logs from both sides to pinpoint which side initiates the close.

3. **Test with piped stdin** — `echo '' | uv run conversation_search.py connect` — if this also fails, rules out the interactive-terminal hypothesis.

4. **Check anyio task group cancellation semantics** — specifically: does a normally-completing task in an inner task group (inside a yielding context manager) propagate any cancellation to the outer task group in anyio's asyncio backend.

---

## Not a Problem For

- `serve` subcommand (stdio mode, unchanged — no bridge)
- Multiple concurrent sessions (daemon stays up after `connect` exits)
- The daemon's idle timeout, health check, or indexing — all confirmed working
