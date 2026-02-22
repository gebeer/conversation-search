# Daemon Mode Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `daemon` and `connect` subcommands so a single long-lived process owns the BM25 index while Claude Code sessions bridge to it via SSE, eliminating redundant in-memory indexes.

**Architecture:** The `daemon` subcommand runs FastMCP in SSE mode (localhost:9237) and writes PID/port files to `~/.cache/conversation-search/`. The `connect` subcommand is what Claude Code's MCP config points to — it checks if the daemon is alive, spawns it if not, then bridges stdio↔SSE using the mcp SDK's built-in stream types.

**Tech Stack:** Python 3.10+, FastMCP (SSE transport via uvicorn), `mcp.client.sse.sse_client`, `mcp.server.stdio.stdio_server`, anyio, stdlib (`os`, `signal`, `socket`, `threading`, `time`)

---

## Background: How the mcp SDK streams work

FastMCP's SSE transport (`mcp_server.run(transport="sse")`) starts a uvicorn server.
The `sse_client(url)` async context manager connects to it and yields `(read_stream, write_stream)` carrying `SessionMessage` objects.
The `stdio_server()` async context manager handles Claude Code's stdio and also yields `(read_stream, write_stream)` with the same `SessionMessage` type.
The bridge just cross-forwards these two stream pairs.

```
Claude Code stdin  → stdio_server read_stream  → sse_write  → daemon
Claude Code stdout ← stdio_server write_stream ← sse_read   ← daemon
```

---

## Task 1: Add `uvicorn` dependency and extract `_register_tools()`

**Files:**
- Modify: `conversation_search.py` (top of file + `_run_mcp_server` function)

The `mcp` package imports `uvicorn` at runtime for SSE transport — it's not automatically installed unless `mcp[cli]` is used. Add it explicitly.

The four MCP tool functions are currently defined inline inside `_run_mcp_server`. Extract them into a helper so both `serve` and `daemon` modes can register them without duplication.

**Step 1: Add `uvicorn` to the inline dependency block**

Find this block at the top of `conversation_search.py`:
```python
# /// script
# requires-python = ">=3.10"
# dependencies = ["bm25s", "mcp", "watchdog"]
# ///
```

Change to:
```python
# /// script
# requires-python = ">=3.10"
# dependencies = ["bm25s", "mcp", "uvicorn", "watchdog"]
# ///
```

**Step 2: Extract `_register_tools()` from `_run_mcp_server`**

Add this function immediately before `_run_mcp_server`:

```python
def _register_tools(server: FastMCP, index: ConversationIndex) -> None:
    """Register the four MCP tools on the given FastMCP server instance."""

    @server.tool()
    def search_conversations(
        query: str,
        limit: int = 10,
        session_id: str | None = None,
        project: str | None = None,
    ) -> str:
        """BM25 keyword search across all conversation turns.

        Args:
            query: Search query string.
            limit: Maximum number of results to return.
            session_id: Optional filter to restrict results to a specific session.
            project: Optional filter to restrict results to a specific project (substring match).
        """
        return json.dumps(index.search(query, limit, session_id, project))

    @server.tool()
    def list_conversations(project: str | None = None, limit: int = 50) -> str:
        """List all indexed conversations with metadata.

        Args:
            project: Optional substring filter for project name.
            limit: Maximum number of conversations to return.
        """
        return json.dumps(index.list_conversations(project, limit))

    @server.tool()
    def read_turn(session_id: str, turn_number: int) -> str:
        """Read a specific turn from a conversation with full fidelity.

        Args:
            session_id: The session UUID to read from.
            turn_number: Zero-based turn index.
        """
        return json.dumps(index.read_turn(session_id, turn_number))

    @server.tool()
    def read_conversation(
        session_id: str,
        offset: int = 0,
        limit: int = 10,
    ) -> str:
        """Read multiple turns from a conversation.

        Args:
            session_id: The session UUID to read from.
            offset: Zero-based starting turn index.
            limit: Number of turns to return.
        """
        return json.dumps(index.read_conversation(session_id, offset, limit))
```

**Step 3: Simplify `_run_mcp_server` to use the helper**

Replace the four `@mcp_server.tool()` decorated functions inside `_run_mcp_server` with:
```python
    _register_tools(mcp_server, index)
    mcp_server.run()
```

**Step 4: Verify `serve` still works**

```bash
uv run conversation_search.py serve --help
```
Expected: shows help without error.

**Step 5: Commit**

```bash
git add conversation_search.py
git commit -m "refactor: extract _register_tools() helper, add uvicorn dep"
```

---

## Task 2: Daemon helper utilities

**Files:**
- Modify: `conversation_search.py` (add helpers after the `_DirDiscoveryHandler` class, before `_run_mcp_server`)

These are small pure functions used by both `daemon` and `connect`.

**Step 1: Add the helpers**

```python
# ---------------------------------------------------------------------------
# Daemon helpers
# ---------------------------------------------------------------------------

_DAEMON_CACHE_DIR = Path.home() / ".cache" / "conversation-search"
_DEFAULT_PORT = 9237
_DEFAULT_IDLE_TIMEOUT = 900  # 15 minutes


def _daemon_cache_dir() -> Path:
    """Return the cache dir, creating it if needed."""
    _DAEMON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _DAEMON_CACHE_DIR


def _read_daemon_state() -> tuple[int, int] | None:
    """Read (pid, port) from cache files. Returns None if files missing or malformed."""
    cache = _DAEMON_CACHE_DIR
    pid_file = cache / "daemon.pid"
    port_file = cache / "daemon.port"
    try:
        pid = int(pid_file.read_text().strip())
        port = int(port_file.read_text().strip())
        return pid, port
    except (FileNotFoundError, ValueError):
        return None


def _is_pid_alive(pid: int) -> bool:
    """Return True if process with given PID exists."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _is_port_responding(port: int) -> bool:
    """Return True if something is listening on localhost:port."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _is_daemon_healthy(pid: int, port: int) -> bool:
    """Return True if daemon PID is alive and port is responding."""
    return _is_pid_alive(pid) and _is_port_responding(port)


def _cleanup_daemon_files() -> None:
    """Remove PID and port files from cache dir."""
    for name in ("daemon.pid", "daemon.port"):
        try:
            (_DAEMON_CACHE_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass


def _write_daemon_files(pid: int, port: int) -> None:
    """Write PID and port to cache dir."""
    cache = _daemon_cache_dir()
    (cache / "daemon.pid").write_text(str(pid))
    (cache / "daemon.port").write_text(str(port))
```

**Step 2: Quick smoke test**

```bash
uv run conversation_search.py serve --help
```
Expected: no import errors, help text displayed.

**Step 3: Commit**

```bash
git add conversation_search.py
git commit -m "feat: add daemon helper utilities (cache dir, PID/port, health check)"
```

---

## Task 3: Implement `_run_daemon()`

**Files:**
- Modify: `conversation_search.py` (add `_run_daemon` function after the helper utilities)

This is the SSE server. It creates its own FastMCP instance (separate from the module-level `mcp_server` used by `serve`), registers the tools, and runs with SSE transport.

**Step 1: Add the function**

```python
def _run_daemon(port: int = _DEFAULT_PORT, idle_timeout: float = _DEFAULT_IDLE_TIMEOUT) -> None:
    """Start the SSE daemon process.

    Checks for an existing healthy daemon first. If one exists, exits immediately.
    Otherwise builds the index, starts watchers, and runs the SSE server.
    Writes PID and port to ~/.cache/conversation-search/.
    Exits after idle_timeout seconds with no MCP tool calls.
    """
    import signal
    import time

    # Check for existing daemon
    state = _read_daemon_state()
    if state is not None:
        pid, existing_port = state
        if _is_daemon_healthy(pid, existing_port):
            print(
                f"[conversation-search] daemon already running (PID {pid}, port {existing_port})",
                file=sys.stderr,
            )
            return
        # Stale files — clean up
        print("[conversation-search] cleaning up stale daemon files", file=sys.stderr)
        _cleanup_daemon_files()

    # Build index (always full corpus)
    index = ConversationIndex()
    index.build("*")

    # Start filesystem watchers
    conv_handler = _ConvChangeHandler("*", index)
    observer = Observer()
    observer.daemon = True

    directories = _discover_directories("*")
    for d in directories:
        observer.schedule(conv_handler, str(d), recursive=False)

    dir_discovery = _DirDiscoveryHandler("*", observer, conv_handler)
    dir_discovery._watched_dirs = {str(d) for d in directories}
    observer.schedule(dir_discovery, str(_PROJECTS_ROOT), recursive=False)
    observer.start()

    # Idle timeout tracking
    last_activity = [time.monotonic()]  # list so closure can mutate it

    def touch_activity() -> None:
        last_activity[0] = time.monotonic()

    def idle_watcher() -> None:
        while True:
            time.sleep(60)
            if time.monotonic() - last_activity[0] > idle_timeout:
                print(
                    f"[conversation-search] idle timeout ({idle_timeout}s), shutting down",
                    file=sys.stderr,
                )
                _cleanup_daemon_files()
                os._exit(0)

    idle_thread = threading.Thread(target=idle_watcher, daemon=True)
    idle_thread.start()

    # Build SSE FastMCP server
    daemon_server = FastMCP(
        "conversation-search",
        instructions=mcp_server.instructions,
        host="127.0.0.1",
        port=port,
    )

    # Register tools with activity tracking
    @daemon_server.tool()
    def search_conversations(
        query: str,
        limit: int = 10,
        session_id: str | None = None,
        project: str | None = None,
    ) -> str:
        """BM25 keyword search across all conversation turns.

        Args:
            query: Search query string.
            limit: Maximum number of results to return.
            session_id: Optional filter to restrict results to a specific session.
            project: Optional filter to restrict results to a specific project (substring match).
        """
        touch_activity()
        return json.dumps(index.search(query, limit, session_id, project))

    @daemon_server.tool()
    def list_conversations(project: str | None = None, limit: int = 50) -> str:
        """List all indexed conversations with metadata.

        Args:
            project: Optional substring filter for project name.
            limit: Maximum number of conversations to return.
        """
        touch_activity()
        return json.dumps(index.list_conversations(project, limit))

    @daemon_server.tool()
    def read_turn(session_id: str, turn_number: int) -> str:
        """Read a specific turn from a conversation with full fidelity.

        Args:
            session_id: The session UUID to read from.
            turn_number: Zero-based turn index.
        """
        touch_activity()
        return json.dumps(index.read_turn(session_id, turn_number))

    @daemon_server.tool()
    def read_conversation(
        session_id: str,
        offset: int = 0,
        limit: int = 10,
    ) -> str:
        """Read multiple turns from a conversation.

        Args:
            session_id: The session UUID to read from.
            offset: Zero-based starting turn index.
            limit: Number of turns to return.
        """
        touch_activity()
        return json.dumps(index.read_conversation(session_id, offset, limit))

    # Write PID/port files and register cleanup
    _write_daemon_files(os.getpid(), port)

    def _shutdown(signum: int, frame: object) -> None:
        print(f"[conversation-search] daemon shutting down (signal {signum})", file=sys.stderr)
        _cleanup_daemon_files()
        observer.stop()
        os._exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    import atexit
    atexit.register(_cleanup_daemon_files)

    print(f"[conversation-search] daemon starting on http://127.0.0.1:{port}", file=sys.stderr)
    daemon_server.run(transport="sse")
```

**Note:** We duplicate the four tool registrations here (rather than calling `_register_tools`) because we need to inject `touch_activity()` into each one. This is the only place where the duplication is intentional.

**Step 2: Verify the import works (no runtime test yet — needs the argparse wiring from Task 4)**

```bash
uv run python -c "
import sys; sys.path.insert(0, '.')
exec(open('conversation_search.py').read().split('if __name__')[0])
print('imports OK')
" 2>/dev/null || uv run conversation_search.py serve --help
```

Expected: `serve --help` still works.

**Step 3: Commit**

```bash
git add conversation_search.py
git commit -m "feat: implement _run_daemon() with SSE transport, PID files, idle timeout"
```

---

## Task 4: Wire `daemon` subcommand into argparse

**Files:**
- Modify: `conversation_search.py` (`main()` function)

**Step 1: Add the subparser**

In `main()`, after the `serve_parser` block and before the `search_parser` block, add:

```python
    # --- daemon (SSE server mode) ---
    daemon_parser = subparsers.add_parser("daemon", help="Run as persistent SSE daemon")
    daemon_parser.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help=f"Localhost port for SSE server (default: {_DEFAULT_PORT})",
    )
    daemon_parser.add_argument(
        "--idle-timeout",
        type=float,
        default=_DEFAULT_IDLE_TIMEOUT,
        metavar="SECONDS",
        help=f"Seconds of inactivity before daemon exits (default: {_DEFAULT_IDLE_TIMEOUT})",
    )
```

**Step 2: Add the dispatch in the command handler**

In `main()`, find:
```python
    if args.command == "serve":
        _run_mcp_server(args.pattern)
    else:
```

Change to:
```python
    if args.command == "serve":
        _run_mcp_server(args.pattern)
    elif args.command == "daemon":
        _run_daemon(port=args.port, idle_timeout=args.idle_timeout)
    else:
```

**Step 3: Smoke test — daemon starts**

```bash
uv run conversation_search.py daemon --help
```
Expected: shows `--port` and `--idle-timeout` options.

**Step 4: Start daemon and verify it's listening**

In one terminal:
```bash
uv run conversation_search.py daemon &
DAEMON_PID=$!
sleep 5
```

Check it's up:
```bash
nc -z 127.0.0.1 9237 && echo "port open" || echo "port closed"
cat ~/.cache/conversation-search/daemon.pid
cat ~/.cache/conversation-search/daemon.port
```

Expected: "port open", PID and port files exist.

Kill daemon:
```bash
kill $DAEMON_PID
sleep 1
ls ~/.cache/conversation-search/  # pid/port files should be gone
```

**Step 5: Commit**

```bash
git add conversation_search.py
git commit -m "feat: add daemon subcommand to CLI"
```

---

## Task 5: Implement `_run_connect()`

**Files:**
- Modify: `conversation_search.py` (add `_run_connect` function after `_run_daemon`)

This is the stdio↔SSE bridge. It's an async function run via `anyio.run()`.

**Step 1: Add the function**

```python
def _run_connect(port: int = _DEFAULT_PORT, idle_timeout: float = _DEFAULT_IDLE_TIMEOUT) -> None:
    """Launcher + stdio↔SSE bridge for MCP config.

    Ensures the daemon is running (starts it if not), then bridges
    Claude Code's stdio MCP protocol to the daemon's SSE endpoint.
    Runs until the SSE connection closes or stdin reaches EOF.
    """
    import subprocess
    import time
    import anyio

    sse_url = f"http://127.0.0.1:{port}/sse"

    def _ensure_daemon_running() -> None:
        """Start the daemon if not already healthy. Waits up to 30s for it to be ready."""
        state = _read_daemon_state()
        if state is not None:
            pid, existing_port = state
            if existing_port == port and _is_daemon_healthy(pid, existing_port):
                return  # Already up

        # Start daemon in background
        print(f"[conversation-search] starting daemon on port {port}...", file=sys.stderr)
        subprocess.Popen(
            [
                sys.executable,
                __file__,
                "daemon",
                "--port", str(port),
                "--idle-timeout", str(idle_timeout),
            ],
            stdout=subprocess.DEVNULL,
            stderr=sys.stderr,
            start_new_session=True,
        )

        # Wait for port to respond (up to 30s)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _is_port_responding(port):
                return
            time.sleep(0.5)

        raise RuntimeError(
            f"[conversation-search] daemon failed to start on port {port} within 30s"
        )

    _ensure_daemon_running()

    # Bridge stdio ↔ SSE
    from mcp.client.sse import sse_client
    from mcp.server.stdio import stdio_server

    async def _bridge() -> None:
        async with stdio_server() as (stdio_read, stdio_write):
            async with sse_client(sse_url) as (sse_read, sse_write):
                async with anyio.create_task_group() as tg:
                    async def forward_to_daemon() -> None:
                        async for message in stdio_read:
                            await sse_write.send(message)

                    async def forward_to_client() -> None:
                        async for message in sse_read:
                            await stdio_write.send(message)

                    tg.start_soon(forward_to_daemon)
                    tg.start_soon(forward_to_client)

    anyio.run(_bridge)
```

**Step 2: Verify imports are available**

```bash
uv run conversation_search.py serve --help
```
Expected: no import errors.

**Step 3: Commit**

```bash
git add conversation_search.py
git commit -m "feat: implement _run_connect() — daemon launcher + stdio<->SSE bridge"
```

---

## Task 6: Wire `connect` subcommand into argparse

**Files:**
- Modify: `conversation_search.py` (`main()` function)

**Step 1: Add the subparser**

In `main()`, after the `daemon_parser` block and before the `search_parser` block, add:

```python
    # --- connect (launcher + stdio<->SSE bridge) ---
    connect_parser = subparsers.add_parser(
        "connect",
        help="Ensure daemon is running and bridge stdio to it (use this in MCP config)",
    )
    connect_parser.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help=f"Daemon port (default: {_DEFAULT_PORT})",
    )
    connect_parser.add_argument(
        "--idle-timeout",
        type=float,
        default=_DEFAULT_IDLE_TIMEOUT,
        metavar="SECONDS",
        help=f"Idle timeout passed to daemon on spawn (default: {_DEFAULT_IDLE_TIMEOUT})",
    )
```

**Step 2: Add the dispatch**

In `main()`, extend the command handler:

```python
    elif args.command == "connect":
        _run_connect(port=args.port, idle_timeout=args.idle_timeout)
```

**Step 3: Smoke test help**

```bash
uv run conversation_search.py connect --help
```
Expected: shows `--port` and `--idle-timeout`.

**Step 4: Integration test — connect starts daemon and bridges**

In one terminal, start connect (it will spawn the daemon):
```bash
uv run conversation_search.py connect &
CONNECT_PID=$!
sleep 8
```

Verify daemon is running:
```bash
cat ~/.cache/conversation-search/daemon.pid
nc -z 127.0.0.1 9237 && echo "daemon up" || echo "daemon down"
```

Expected: PID file exists, "daemon up".

Kill connect (daemon should stay alive):
```bash
kill $CONNECT_PID
sleep 1
nc -z 127.0.0.1 9237 && echo "daemon still up" || echo "daemon gone"
```

Expected: "daemon still up" (daemon outlives the connect process).

Kill daemon:
```bash
kill $(cat ~/.cache/conversation-search/daemon.pid)
```

**Step 5: Commit**

```bash
git add conversation_search.py
git commit -m "feat: add connect subcommand to CLI"
```

---

## Task 7: CLI daemon shortcut

**Files:**
- Modify: `conversation_search.py` (the `else` branch of `main()` where CLI commands run)

When the daemon is warm, CLI subcommands should forward queries via HTTP instead of building a local index. This is a progressive enhancement — if the daemon is down, fall back to the existing local index behavior.

**Step 1: Add the helper**

Add this function near the other daemon helpers (after `_write_daemon_files`):

```python
def _try_daemon_query(command: str, params: dict) -> dict | None:
    """Try to run a query against the running daemon via HTTP.

    Returns the parsed JSON response on success, None if daemon is unavailable.
    The daemon exposes MCP tools over SSE — we invoke them via a direct HTTP call
    to the tool endpoint using the MCP message protocol.

    Actually: the daemon doesn't expose a simple REST API — it uses SSE/JSON-RPC.
    For CLI shortcut, we just fall back to local index always.
    Returns None to signal "use local index".
    """
    # NOTE: Direct HTTP CLI shortcuts would require implementing a JSON-RPC client
    # against the SSE server, which duplicates the connect bridge complexity.
    # For v1, CLI subcommands always use local index. The daemon benefit is
    # for MCP sessions via `connect`, not CLI usage.
    return None
```

**Note:** On reflection, the CLI-via-daemon shortcut would require a full JSON-RPC SSE client for marginal benefit — CLI queries are one-shots where index build time is acceptable. The real win is for MCP sessions that stay connected. Marking this as a no-op for v1 and documenting the decision.

**Step 2: No argparse changes needed** — the `else` branch already handles all CLI commands with local index. The `_try_daemon_query` stub documents the decision.

**Step 3: Commit**

```bash
git add conversation_search.py
git commit -m "docs: note CLI daemon shortcut deferred to v2 (JSON-RPC client complexity)"
```

---

## Task 8: Full integration test

Manually verify the end-to-end flow works.

**Step 1: Clean state**

```bash
rm -f ~/.cache/conversation-search/daemon.pid ~/.cache/conversation-search/daemon.port
```

**Step 2: Test double-start guard**

```bash
# Start daemon in background
uv run conversation_search.py daemon &
DAEMON_PID=$!
sleep 8

# Try to start a second daemon — should print "already running" and exit
uv run conversation_search.py daemon
echo "Exit code: $?"
```

Expected: prints "daemon already running (PID ..., port 9237)" and exits 0.

**Step 3: Test connect auto-starts daemon**

```bash
# Kill existing daemon
kill $DAEMON_PID
sleep 2
rm -f ~/.cache/conversation-search/daemon.pid ~/.cache/conversation-search/daemon.port

# Run connect — should auto-start daemon then bridge
# We can't fully test the bridge interactively, but we can verify daemon starts
timeout 15 uv run conversation_search.py connect &
CONNECT_PID=$!
sleep 10

nc -z 127.0.0.1 9237 && echo "PASS: daemon started by connect" || echo "FAIL: daemon not started"
kill $CONNECT_PID 2>/dev/null
sleep 1
nc -z 127.0.0.1 9237 && echo "PASS: daemon outlived connect" || echo "FAIL: daemon died with connect"
```

**Step 4: Verify serve mode still works**

```bash
# Clean up daemon
kill $(cat ~/.cache/conversation-search/daemon.pid 2>/dev/null) 2>/dev/null || true

# Stdio serve should still work unchanged
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}' | timeout 5 uv run conversation_search.py serve 2>/dev/null | head -1
```

Expected: returns an `initialize` response JSON.

**Step 5: Test stale PID cleanup**

```bash
# Write fake stale PID
mkdir -p ~/.cache/conversation-search
echo "99999" > ~/.cache/conversation-search/daemon.pid
echo "9237" > ~/.cache/conversation-search/daemon.port

uv run conversation_search.py daemon &
DAEMON_PID=$!
sleep 8

nc -z 127.0.0.1 9237 && echo "PASS: daemon started despite stale PID" || echo "FAIL"
kill $DAEMON_PID
```

**Step 6: Commit (no code changes — just ran tests)**

```bash
# If any fixes were needed during testing, commit them here
git status
```

---

## Task 9: Update README

**Files:**
- Modify: `README.md`

**Step 1: Add daemon mode section**

Add a new section after the existing "Usage" or "MCP Config" section:

```markdown
## Daemon Mode (Recommended for Multiple Sessions)

When running multiple Claude Code sessions simultaneously, use daemon mode to share a single
BM25 index instead of building one per session.

### Setup

Update your MCP config to use the `connect` subcommand:

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

That's it. On first session start, `connect` automatically launches the daemon in the background.
Subsequent sessions reuse it. The daemon exits after 15 minutes of inactivity.

### Manual daemon control

```bash
# Start daemon in foreground (useful for debugging)
uv run conversation_search.py daemon

# Custom port and idle timeout
uv run conversation_search.py daemon --port 9300 --idle-timeout 1800

# Stop daemon
kill $(cat ~/.cache/conversation-search/daemon.pid)
```

### Configuration

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 9237 | Localhost port for the SSE server |
| `--idle-timeout` | 900 | Seconds of inactivity before daemon exits |

Both flags work on `daemon` and `connect` subcommands.

### How it works

```
Claude Code session A ──┐
Claude Code session B ──┼── connect (stdio↔SSE bridge) ──► daemon (SSE on localhost:9237)
Claude Code session C ──┘                                       │
                                                          • one BM25 index (~225 MB)
                                                          • one inotify watcher set
                                                          • one reindex loop
```

**Without daemon:** N sessions × ~225 MB = N × 225 MB RAM, N reindex cycles per file change.
**With daemon:** 1 × ~225 MB regardless of session count.
```

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add daemon mode section to README"
```

---

## Done

The implementation adds three capabilities to the single `conversation_search.py` file:

1. **`daemon` subcommand** — SSE server with shared BM25 index, PID/port files, idle timeout
2. **`connect` subcommand** — zero-config launcher + stdio↔SSE bridge for MCP config
3. **Updated README** — daemon mode setup and configuration docs

The existing `serve` subcommand and all CLI subcommands are unchanged.

**MCP config migration** (existing users):

Before:
```json
{"command": "uv", "args": ["run", "/path/to/conversation_search.py", "serve", "--pattern", "*"]}
```

After (daemon mode):
```json
{"command": "uv", "args": ["run", "/path/to/conversation_search.py", "connect"]}
```
