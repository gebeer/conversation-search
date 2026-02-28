# PRD: Conversation Search MCP Server

## Overview

A single-file MCP server that indexes Claude Code conversation transcripts using BM25 and makes them searchable mid-session. Based on [Eric Tramel's approach](https://eric-tramel.github.io/blog/2026-02-07-searchable-agent-memory/).

## Problem

Claude Code sessions are isolated. When a session ends, the agent loses access to all context — decisions made, errors encountered, solutions found. Conversation transcripts exist as JSONL files but are opaque and unsearchable. Cross-session recall currently relies on manually curated MEMORY.md files, which are limited and lossy.

## Solution

A lightweight MCP server that:
1. Parses Claude Code JSONL transcripts into searchable turns
2. Indexes them with BM25 for fast keyword search
3. Exposes 4 tools for the agent to query its own history
4. Watches for filesystem changes and reindexes automatically
5. Dynamically discovers new project directories matching the configured pattern

## Scope

### Data Source

Index JSONL conversation files from all project directories matching a configurable glob pattern under `~/.claude/projects/`. Default pattern:
```
-home-gbr-work-001-sites*
```

The server accepts the pattern as a CLI argument, defaulting to the above if not provided.

Currently 9+ project directories with ~100+ transcript files.

### Architecture

- **Single Python file** with PEP 723 inline metadata
- **Execution**: `uv run` — no venv, no pyproject.toml
- **Dependencies**: `bm25s`, `mcp` (FastMCP), `watchdog`
- **Transport**: stdio (standard MCP server protocol)

## JSONL Format (Observed)

Each JSONL file is one session. Each line is a JSON record with a `type` field:

| Record Type | Description | Relevant Fields | Index? |
|---|---|---|---|
| `user` | User message | `message.content` (str or list), `timestamp`, `isMeta`, `slug` | Yes |
| `assistant` | Assistant response | `message.content` (list of blocks: `text`, `thinking`, `tool_use`), `timestamp`, `slug` | Yes |
| `summary` | Session summary | `summary` (string) | Metadata only |
| `progress` | Hook/system progress | — | Skip |
| `file-history-snapshot` | File state snapshot | — | Skip |
| `custom-title` | User-assigned session title | — | Skip |
| `queue-operation` | Internal queue state | — | Skip |
| `system` | System messages | — | Skip |

Common metadata on user/assistant records: `sessionId`, `cwd`, `gitBranch`, `version`, `timestamp`, `slug`.

### Session Summary Resolution

Session summary is resolved using this priority chain:
1. **`summary` record**: AI-generated session summary (e.g. "DDEV Project Inspection and Directory Rename Cleanup") — best quality
2. **`slug` field**: 3-word mnemonic from user/assistant records (e.g. "velvet-puzzling-eclipse") — available on ~91% of records
3. **First user message**: First 200 chars of first non-meta user message — fallback

The `slug` field is also exposed as a separate metadata field when available.

### Filtering Rules

- Skip records where `isMeta` is `true` (system-injected messages)
- Skip all non-`user`/`assistant` record types during turn construction
- Skip user messages containing only `<command-name>`, `<local-command-stdout>`, or `<local-command-caveat>` tags (internal Claude Code machinery)
- Skip user messages where `content` is a list (tool results) — these are part of the preceding turn
- Skip `thinking` blocks in assistant content (large, not useful for search)

## Tools

### 1. `search_conversations`

BM25 keyword search across all indexed turns.

**Parameters:**
| Name | Type | Default | Description |
|---|---|---|---|
| `query` | `str` | required | Search keywords (e.g. "watchdog reindex debounce") |
| `limit` | `int` | `10` | Max results to return |
| `session_id` | `str\|None` | `None` | Filter to specific session |
| `project` | `str\|None` | `None` | Filter by project directory name substring |

**Returns:** JSON with `results` array. Each result:
```json
{
  "session_id": "a378da2b-...",
  "project": "diff-website",
  "turn_number": 3,
  "score": 12.4532,
  "snippet": "first 300 chars of turn text...",
  "timestamp": "2026-02-10T08:19:22.658Z"
}
```

### 2. `list_conversations`

Browse all indexed sessions with metadata.

**Parameters:**
| Name | Type | Default | Description |
|---|---|---|---|
| `project` | `str\|None` | `None` | Filter by project directory name substring |
| `limit` | `int` | `50` | Max sessions to return |

**Returns:** JSON with `conversations` array sorted by `last_timestamp` descending (most recent activity first). Each entry:
```json
{
  "session_id": "a378da2b-...",
  "project": "diff-website",
  "summary": "DDEV Project Inspection and Directory Rename Cleanup",
  "slug": "velvet-puzzling-eclipse",
  "first_timestamp": "2026-02-10T08:17:07Z",
  "last_timestamp": "2026-02-10T09:45:12Z",
  "turn_count": 24,
  "cwd": "/home/gbr/work/001-sites/diff-website",
  "git_branch": "main"
}
```

### 3. `read_turn`

Fetch a single turn with full fidelity. Re-parses JSONL on demand (not from index). Resolves `session_id` to file path via the session-to-filepath mapping built during indexing.

**Parameters:**
| Name | Type | Default | Description |
|---|---|---|---|
| `session_id` | `str` | required | Session UUID |
| `turn_number` | `int` | required | 0-indexed turn number |

**Returns:** JSON with full turn content:
```json
{
  "session_id": "a378da2b-...",
  "turn_number": 3,
  "timestamp": "2026-02-10T08:19:22.658Z",
  "user_text": "full user message text",
  "assistant_text": "full assistant response text",
  "tools_used": [
    {"tool": "Read", "file": "/path/to/file"},
    {"tool": "Bash", "command": "git status"},
    {"tool": "Write", "file": "/path/to/file", "chars": 1234}
  ]
}
```

**Error responses:**
- Unknown session: `{"error": "Unknown session_id: <id>"}`
- Invalid turn number: `{"error": "Turn <n> out of range (session has <total> turns)"}`

Tool calls are rendered as compact summaries, not raw JSON. Rendering rules:
- `Read`: `{"tool": "Read", "file": <file_path>}`
- `Write`: `{"tool": "Write", "file": <file_path>, "chars": <len(content)>}`
- `Edit`: `{"tool": "Edit", "file": <file_path>}`
- `Bash`: `{"tool": "Bash", "command": <command, truncated to 200 chars>}`
- `Grep`/`Glob`: `{"tool": <name>, "pattern": <pattern>}`
- `Task`: `{"tool": "Task", "type": <subagent_type>, "description": <description>}`
- All others: `{"tool": <name>}`

### 4. `read_conversation`

Paginate through a session sequentially.

**Parameters:**
| Name | Type | Default | Description |
|---|---|---|---|
| `session_id` | `str` | required | Session UUID |
| `offset` | `int` | `0` | Turn offset to start from |
| `limit` | `int` | `10` | Number of turns to return |

**Returns:** JSON with `turns` array (same format as `read_turn` output per entry), plus pagination and session metadata:
```json
{
  "session_id": "a378da2b-...",
  "project": "diff-website",
  "cwd": "/home/gbr/work/001-sites/diff-website",
  "git_branch": "main",
  "total_turns": 24,
  "offset": 0,
  "limit": 10,
  "turns": [...]
}
```

**Error responses:**
- Unknown session: `{"error": "Unknown session_id: <id>"}`

## Indexing

### Turn Construction

A "turn" is one user-assistant exchange:
1. Starts when a non-meta `user` record with string `content` is encountered
2. Collects subsequent `assistant` record content (text blocks only, skip thinking blocks)
3. Collects tool names from `tool_use` blocks
4. Ends when the next non-meta `user` record begins

The search corpus text per turn is: `user_text + "\n" + assistant_text + "\n" + "tools: " + sorted_tool_names`

### Session-to-Filepath Mapping

During indexing, a `dict[str, Path]` mapping `session_id -> jsonl_file_path` is maintained. This mapping is used by `read_turn` and `read_conversation` to resolve session IDs to their source files for on-demand re-parsing. Each session UUID is unique across all project directories (UUID collision is not a concern). The mapping is rebuilt on every reindex alongside the BM25 index.

### BM25 Configuration

- Tokenizer: `bm25s.tokenize()` with `stopwords="en"`
- Index: `bm25s.BM25()` default parameters (k1=1.5, b=0.75)
- Query: same tokenization, `retriever.retrieve()` with configurable k

### Reindexing

- Watchdog `Observer` on each project directory (recursive=False since JSONL files are at directory root)
- 2-second debounce via `threading.Timer` — each filesystem event resets the timer
- Full reindex on trigger (incremental not worth the complexity for this corpus size)
- Thread-safe index swap via `threading.Lock`

## Multi-Directory Handling

The reference implementation assumes a single flat conversations directory. Our case has multiple project directories under `~/.claude/projects/`. Approach:

1. At startup, glob for all directories matching the configured pattern under `~/.claude/projects/`
2. Parse all JSONL files across all matching directories
3. Derive `project` name from directory name (strip common prefix): e.g. `-home-gbr-work-001-sites-diff-website` -> `diff-website`
4. Attach `project` to each turn and conversation metadata
5. Set up one watchdog observer per directory

### Dynamic Directory Discovery

New project directories may be created at any time (e.g. when Claude Code is first run in a new working directory). To handle this:

1. A watchdog observer watches the parent directory `~/.claude/projects/` for new subdirectories
2. On directory creation, check if the new directory name matches the configured pattern
3. If it matches, trigger a full reindex (which re-globs and picks up the new directory)
4. Register a new watchdog observer for the new directory
5. Uses the same 2-second debounce as JSONL file changes

## Configuration

The server accepts a single optional CLI argument for the project directory glob pattern:

```
uv run conversation_search.py [PATTERN]
```

- `PATTERN`: Glob pattern matched against directory names under `~/.claude/projects/`. Default: `-home-gbr-work-001-sites*`
- The base path `~/.claude/projects/` is fixed (this is where Claude Code stores transcripts)

Examples:
```bash
# Default: index work-001-sites projects
uv run conversation_search.py

# Index all projects
uv run conversation_search.py "*"

# Index only opencode projects
uv run conversation_search.py "-home-gbr-work-ai-opencode*"
```

## File Location

```
~/work/ai/cross-session-memory/conversation_search.py
```

## MCP Configuration (for later wiring)

In `~/.claude/settings.json` or project `.mcp.json`:
```json
{
  "mcpServers": {
    "conversation-search": {
      "command": "uv",
      "args": ["run", "/home/gbr/work/ai/cross-session-memory/conversation_search.py"]
    }
  }
}
```

With custom pattern:
```json
{
  "mcpServers": {
    "conversation-search": {
      "command": "uv",
      "args": ["run", "/home/gbr/work/ai/cross-session-memory/conversation_search.py", "*"]
    }
  }
}
```

## Testing Strategy

1. **Smoke test**: Run the server standalone, verify it starts and indexes without errors
2. **Manual search**: Use `mcp` CLI or a test script to call `search_conversations` and validate results
3. **Verify turn parsing**: Spot-check `read_turn` output against raw JSONL for a known session
4. **Reindex test**: Append to a JSONL file, verify the index updates within ~3 seconds
5. **New directory test**: Create a new matching project directory, verify it gets picked up

## Out of Scope (for now)

- Vector/embedding search
- Persistent index caching (index is rebuilt on startup, fast enough for this corpus size)
- Web UI or any interface beyond MCP tools
- Notes/code library search (mentioned in article as separate server)
- Date-range filtering on search (can be added later)

## Differences from Reference Implementation

The article's working example (~270 lines) implements `search` and `list_conversations`. Our implementation diverges in these ways:

| Aspect | Article | Ours |
|---|---|---|
| Tools | 2 (`search`, `list_conversations`) | 4 (add `read_turn`, `read_conversation`) |
| Directory scope | Single flat dir via CLI arg | Multi-directory with configurable glob pattern |
| Filtering | `project` filter on search/list | — |
| Directory input | CLI positional arg | Optional CLI arg with default pattern |
| Tool naming | `search` | `search_conversations` (better MCP discoverability) |
| New directories | Not handled | Dynamic discovery via parent dir watcher |
| Session summary | `slug` field | `summary` record > `slug` > first-message fallback |

## Reference

- Source article: `sources/Searchable_Agent _Memory_in_a_Single_File.md`
- Reference implementation: Working example in article (~270 lines, implements `search` + `list_conversations` only)
