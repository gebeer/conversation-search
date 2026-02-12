# conversation-search

MCP server that provides BM25 keyword search over Claude Code conversation history. Indexes JSONL transcripts from `~/.claude/projects/` and exposes them as searchable memory across sessions.

Based on [Searchable Agent Memory in a Single File](https://eric-tramel.github.io/blog/2026-02-07-searchable-agent-memory/) by Eric Tramel.

## Development process

The implementation was produced by Claude Code following a structured pipeline:

1. **PRD** (`ai-docs/features/001-conversation-search-mcp.md`) — requirements and design decisions
2. **Build spec** (`specs/conversation-search-mcp.md`) — generated from the PRD, containing exact function signatures, filtering rules, acceptance criteria, and validation commands
3. **Implementation** (`conversation_search.py`) — written by Claude Code using the build spec as instructions

## How it works

Claude Code stores conversation transcripts as JSONL files under `~/.claude/projects/<encoded-dir>/`. This server:

1. Discovers matching project directories via glob pattern
2. Parses JSONL into turns (user message + assistant response + tool calls)
3. Builds a BM25 index over the corpus
4. Watches the filesystem for changes and reindexes with 2s debounce
5. Serves 4 MCP tools over stdio

## Requirements

- Python >= 3.10
- [`uv`](https://docs.astral.sh/uv/) (dependencies are managed via PEP 723 inline metadata)

No venv or manual install needed. `uv run` handles `bm25s`, `mcp`, and `watchdog` automatically.

## Configuration

Add to `.mcp.json` (project-level or `~/.claude/.mcp.json` for global):

```json
{
  "mcpServers": {
    "conversation-search": {
      "command": "uv",
      "args": ["run", "/absolute/path/to/conversation_search.py", "--pattern", "<pattern>"]
    }
  }
}
```

The `--pattern` flag is **required**. It's a glob matched against directory names under `~/.claude/projects/`. Examples:

| Pattern | Scope |
|---------|-------|
| `*` | All projects |
| `-home-gbr-work-001-sites*` | All sites projects |
| `-home-gbr-work-ai-*` | All AI projects |

Restart Claude Code after editing `.mcp.json`.

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

Search wide, then read deep:

```
search_conversations("ProcessWire login redirect")  ->  find relevant turns
read_turn(session_id, turn_number)                   ->  get full context
read_conversation(session_id, offset, limit)         ->  read surrounding turns
```

BM25 works best with specific keywords. Vague queries return noise. Post-retrieval filtering (by session/project) happens after BM25 scoring, so increase `limit` when filtering.
