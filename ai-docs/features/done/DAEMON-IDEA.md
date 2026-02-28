# Daemon Mode: Shared conversation-search Process

A future improvement idea — not currently implemented.

## Problem

Each Claude Code CLI session spawns its own `conversation-search` MCP server via stdio. Each instance independently:

- Loads and parses all JSONL conversation files from disk
- Builds a full BM25 index in memory
- Sets up inotify watchers on the sessions directory
- Triggers reindexing on any file change

With three concurrent sessions, this means three redundant indexes, three sets of filesystem watchers consuming inotify file descriptors, and three separate reindex cycles firing in response to the same file change events. Measured memory usage is roughly 225 MB per instance, so three sessions costs ~675 MB for identical data.

## Proposal

Run a single long-lived daemon process. All CLI sessions connect to it as clients rather than spawning their own server.

The daemon owns the index and the watchers. CLI sessions ask it questions; they do not do any indexing themselves.

## Architecture Sketch

```
[Claude Code session A] --\
[Claude Code session B] ---+--[Unix socket / TCP localhost]--> [conversation-search daemon]
[Claude Code session C] --/                                         |
                                                              - one BM25 index
                                                              - one inotify watch set
                                                              - one reindex loop
```

**Daemon responsibilities:**
- Watch the JSONL sessions directory via inotify
- Maintain a single in-memory BM25 index
- Listen on a Unix socket (e.g., `/tmp/conversation-search.sock`) or localhost TCP port
- Expose the same MCP tool surface (`search_conversations`, `list_conversations`, `read_turn`, `read_conversation`) over SSE or a simple JSON-RPC protocol

**Client (MCP shim) responsibilities:**
- On session start, check if the daemon is running (try connecting to the socket)
- If not running, start the daemon in the background and wait for it to be ready
- Forward all MCP tool calls to the daemon and relay responses back to Claude Code
- On session exit, do nothing — the daemon stays alive for other sessions

The Claude Code MCP config would point to a thin shim script instead of the server directly. The shim handles the stdio<->socket bridging.

## Benefits

- One index in memory instead of N
- One set of inotify watchers regardless of session count
- Reindexing happens once per file change, not N times
- Faster session startup (no index build on connect if daemon is already warm)
- Lower inotify FD consumption (relevant on systems with many open files)

## Challenges

**Session lifecycle.** The daemon must outlive individual sessions but also eventually shut down when nothing needs it. Options: a short idle timeout (e.g., exit after 5 minutes with no connected clients), or a systemd user service that the shim activates via `systemctl --user start`.

**MCP transport.** Claude Code currently expects MCP servers on stdio. A shim script bridging stdio to a Unix socket adds a layer, but is straightforward. Alternatively, Claude Code's SSE transport could be used if the daemon listens on a localhost port — but that requires a persistent known port and a config change.

**Error isolation.** If the daemon crashes, all sessions lose search simultaneously. With per-session processes, a crash only affects one session. The shim should handle reconnect/restart gracefully.

**Index consistency during writes.** The current per-session model means each session sees its own snapshot. A shared daemon needs to be careful about serving queries while a reindex is in progress (read-write lock or copy-on-write index swap).

## Simpler Alternative: Shared Index File + Lock

Instead of a daemon, the server could serialize its BM25 index to a file (e.g., `/tmp/conversation-search.index`). On startup, each instance checks if the index file is fresh enough (mtime vs newest JSONL mtime). If fresh, load it instead of rebuilding. A lock file prevents simultaneous rebuilds.

This is simpler to implement and requires no transport changes. The downside: each session still holds its own copy of the index in memory and still runs its own inotify watchers. It only saves the CPU cost of rebuilding — not the RAM cost of N copies.

The daemon approach is the fuller solution; the shared index file is a lower-effort partial improvement.
