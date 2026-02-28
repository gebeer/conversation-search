# Daemon Mode Design

Approved: 2026-02-21

## Problem

Each Claude Code session spawns its own conversation-search MCP server via stdio. Each instance independently loads all JSONL files, builds a full BM25 index, and runs inotify watchers. With N concurrent sessions this means N redundant indexes (~225 MB each), N sets of filesystem watchers, and N reindex cycles firing on the same file change events.

## Solution

A single long-lived daemon process owns the index and the watchers. Claude Code sessions connect to it through a thin launcher that bridges stdio to SSE.

## Architecture

```
Claude Code session A ──┐
Claude Code session B ──┼── [connect: stdio↔SSE bridge] ──► [daemon: SSE on localhost:9237]
Claude Code session C ──┘                                          │
                                                             • one BM25 index
                                                             • one watchdog observer
                                                             • one reindex loop
```

## Three Modes

The server operates in three modes via subcommands:

### `serve` (unchanged)

Existing stdio MCP server. Single-session, self-contained. Kept for backwards compatibility and simple setups.

```
conversation_search.py serve --pattern "*"
```

### `daemon`

The real server. Runs in foreground, SSE transport on localhost.

```
conversation_search.py daemon
conversation_search.py daemon --port 9300 --idle-timeout 900
```

- Always indexes everything (pattern `*`), no `--pattern` flag
- Writes `daemon.pid` and `daemon.port` to `~/.cache/conversation-search/`
- Exposes `/health` endpoint for liveness checks
- Idle timeout shuts down after no MCP tool calls for N seconds (default 900 = 15 min, configurable via `--idle-timeout`)

### `connect`

What Claude Code's MCP config points to. Ensures the daemon is running and bridges stdio to SSE.

```
conversation_search.py connect
conversation_search.py connect --port 9300
```

MCP config:
```json
{
  "mcpServers": {
    "conversation-search": {
      "command": "uv",
      "args": ["run", "/path/to/conversation_search.py", "connect"]
    }
  }
}
```

## Connect Launcher Behavior

On each session start:

1. Read `~/.cache/conversation-search/daemon.pid` and `daemon.port`
2. Health-check the port (HTTP GET `/health`)
3. If healthy: bridge stdio↔SSE
4. If stale or missing: spawn `daemon` as a background subprocess, wait for port to respond, then bridge
5. On session exit: leave daemon running for other sessions

## stdio↔SSE Bridge

The `connect` subcommand translates between Claude Code's stdio MCP protocol and the daemon's SSE MCP transport:

- Reads JSON-RPC messages from stdin
- POSTs each to the daemon's message endpoint
- Streams SSE events from the daemon back to stdout

Uses the `mcp` package's client-side SSE support (`mcp.client.sse`).

## Daemon Internals

### Startup Sequence

1. Check for existing daemon (PID file alive + port responds to `/health`)
2. If healthy daemon exists: print message and exit
3. If stale PID: clean up files
4. Build the index (pattern `*`)
5. Start watchdog observer
6. Write `daemon.pid` and `daemon.port` to `~/.cache/conversation-search/`
7. Register `atexit` + signal handlers for cleanup
8. Start SSE server (blocks)

### Idle Timeout

- Every incoming MCP tool call updates a `last_activity` timestamp
- A background thread checks every 60s: if `now - last_activity > idle_timeout`, clean shutdown
- Shutdown: stop observer, remove PID/port files, exit

### Index and Watchers

Identical to current implementation. `ConversationIndex` class, `_ConvChangeHandler`, `_DirDiscoveryHandler` — all unchanged. The daemon is the same server with a different transport.

## CLI Enhancement

CLI subcommands (`search`, `list`, `read-turn`, `read-conv`) gain daemon awareness:

1. Try reading `daemon.port` from cache dir
2. If daemon is up: forward query via HTTP, return response
3. If daemon is down: fall back to current behavior (build ephemeral local index)

This makes CLI usage instant when the daemon is warm.

## Error Handling

### Daemon crash while clients are connected

The `connect` bridge detects the lost SSE connection and attempts one daemon restart. If restart succeeds, re-establishes the bridge. If it fails twice, exits with error.

### Port conflict

If port 9237 is in use by a non-daemon process, the daemon exits with a clear error suggesting `--port`. The `connect` launcher also accepts `--port` to match.

### Stale PID file

Both daemon startup and `connect` validate: PID exists, process is alive (`os.kill(pid, 0)`), port responds to `/health`. All three must pass. Otherwise, clean up stale files and proceed.

### Concurrent connect race

Two sessions launch simultaneously, both see no daemon, both try to spawn one. First grabs the port and writes PID file. Second fails on port bind. Second `connect` retries health check, finds the first daemon, bridges to it. Natural resolution, no lock file needed.

### Multiple users on same machine

Cache dir is per-user (`~/.cache/conversation-search/`), so each user gets their own daemon on their own default port. No conflict.

## What Changes

### Unchanged
- `ConversationIndex` class
- All four MCP tool signatures and behavior
- Watchdog file monitoring
- `serve` subcommand (stdio mode)
- CLI subcommands (still work standalone)

### New Code
- `daemon` subcommand: SSE server startup, PID/port file management, idle timeout, `/health` endpoint, signal handlers (~80-100 lines)
- `connect` subcommand: daemon health check, spawn logic, stdio↔SSE bridge (~60-80 lines)
- CLI daemon shortcut: try daemon HTTP before building local index (~20 lines)

### New Dependencies
- Possibly `httpx` for the SSE client in the bridge (or use what `mcp` already bundles)

### Estimated Size
~160-200 new lines on top of the existing 870.

## Configuration

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 9237 | Localhost port for SSE server |
| `--idle-timeout` | 900 | Seconds without activity before daemon exits |

Both flags available on `daemon` and `connect` subcommands. Port also readable from `~/.cache/conversation-search/daemon.port` by CLI subcommands.
