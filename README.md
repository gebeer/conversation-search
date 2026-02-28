# conversation-search

BM25 keyword search over Claude Code conversation history. Indexes JSONL transcripts from `~/.claude/projects/` and exposes them as searchable memory. Available as both an MCP server and a CLI tool.

Based on [Searchable Agent Memory in a Single File](https://eric-tramel.github.io/blog/2026-02-07-searchable-agent-memory/) by Eric Tramel.


## How it works

Claude Code stores conversation transcripts as JSONL files under `~/.claude/projects/<encoded-dir>/`. This server:

1. Discovers matching project directories via glob pattern
2. Parses JSONL into turns (user message + assistant response + tool calls)
3. Builds a BM25 index over the corpus
4. Watches the filesystem for changes and reindexes (60s debounce)
5. Serves 4 MCP tools via stdio (`serve`) or SSE (`daemon`)

## Requirements

- [`uv`](https://docs.astral.sh/uv/) (Python >= 3.10 is resolved automatically)

No venv or manual install needed.

## Installation

Add to your MCP configuration — either project-level (`.mcp.json`) or global (`~/.claude.json` under the `mcpServers` key):

```json
{
  "mcpServers": {
    "conversation-search": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/gebeer/conversation-search.git",
        "conversation-search", "connect"
      ]
    }
  }
}
```

`connect` starts a shared daemon on first use and reuses it across sessions (see [Daemon Mode](#daemon-mode-recommended-for-multiple-sessions)). For standalone stdio mode (one index per session), replace `connect` with `serve`.

Restart Claude Code after changing MCP configuration.

## CLI Usage

The tool can also be used directly from the command line for scripting and debugging:

```bash
uvx --from git+https://github.com/gebeer/conversation-search.git \
  conversation-search search --query "heartbeat" --limit 5

conversation-search list --project "claude" --limit 10

conversation-search read-turn --session-id "<uuid>" --turn 5

conversation-search read-conv --session-id "<uuid>" --offset 0 --limit 10
```

After the first `uvx` invocation, the `conversation-search` command is cached and can be called directly. Alternatively, use `uv run conversation_search.py <command>` from a local clone.

All CLI commands output pretty-printed JSON to stdout. Index progress is printed to stderr. Use `2>/dev/null` to suppress progress output when piping.

## Daemon Mode (Recommended for Multiple Sessions)

When running multiple Claude Code sessions simultaneously, use daemon mode to share a single
BM25 index instead of building one per session.

### Setup

The default [installation](#installation) config already uses `connect`, which enables daemon mode automatically. On first session start, `connect` launches the daemon in the background. Subsequent sessions reuse it. The daemon exits after 15 minutes of inactivity.

### Manual daemon control

```bash
# Start daemon in foreground (useful for debugging)
conversation-search daemon

# Custom port and idle timeout
conversation-search daemon --port 9300 --idle-timeout 1800

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
                                                          • one BM25 index (~250 MB)
                                                          • one inotify watcher set
                                                          • one reindex loop
```

**Without daemon:** N sessions × ~250 MB RAM, N reindex cycles per file change.
**With daemon:** 1 × ~250 MB regardless of session count.

## Tools

### `search_conversations`

BM25 keyword search across all indexed turns.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | `str` | required | Search query (specific keywords work best) |
| `limit` | `int` | `10` | Max results |
| `session_id` | `str \| None` | `None` | Filter to one session |
| `project` | `str \| None` | `None` | Substring filter on project name |

Returns ranked results with `session_id`, `turn_number`, `score`, `snippet` (300 chars), `timestamp`.

### `list_conversations`

Browse indexed sessions with metadata.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `project` | `str \| None` | `None` | Substring filter on project name |
| `limit` | `int` | `50` | Max results |

Returns sessions sorted by `last_timestamp` desc, with `summary`, `turn_count`, `cwd`, `git_branch`.

### `read_turn`

Full-fidelity retrieval of a single turn. Re-parses the source JSONL (not the index).

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session UUID |
| `turn_number` | `int` | Zero-based turn index |

Returns complete `user_text`, `assistant_text`, and `tools_used` with rendered tool details.

### `read_conversation`

Paginated reading of consecutive turns from a session.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `session_id` | `str` | required | Session UUID |
| `offset` | `int` | `0` | Starting turn |
| `limit` | `int` | `10` | Number of turns |

## Usage pattern

The 4 tools are automatically exposed to the assistant via MCP — no extra instructions in `CLAUDE.md`, `AGENTS.md`, or similar files are needed. The server also provides `instructions` metadata through the MCP protocol to guide the assistant on effective usage.

Search wide, then read deep:

```
search_conversations("ProcessWire login redirect")  ->  find relevant turns
read_turn(session_id, turn_number)                   ->  get full context
read_conversation(session_id, offset, limit)         ->  read surrounding turns
```

BM25 works best with specific keywords. Vague queries return noise. Post-retrieval filtering (by session/project) happens after BM25 scoring, so increase `limit` when filtering.
