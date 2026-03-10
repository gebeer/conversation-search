# Plan: Conversation Search MCP Server

> **Migration note (2026-03-10):** The search backend has been migrated from in-memory `bm25s`
> to SQLite FTS5. The index is now persisted at `~/.cache/conversation-search/index.db`.
> Query behavior changed from implicit OR to implicit AND. Snippets now use `[[match]]` markers
> instead of first-300-chars truncation. `bm25s` dependency removed. See `specs/fts5-migration.md`
> for the full migration spec.

## Task Description
Implement a single-file MCP server that indexes Claude Code JSONL conversation transcripts with BM25 and exposes them as searchable tools. The server parses multi-directory conversation files, builds an in-memory BM25 index, watches for filesystem changes, dynamically discovers new project directories, and serves 4 MCP tools for search and retrieval.

## Objective
A working `conversation_search.py` file at `~/work/ai/cross-session-memory/conversation_search.py` that:
- Indexes all JSONL transcripts across project directories matching a configurable glob pattern
- Exposes 4 MCP tools (`search_conversations`, `list_conversations`, `read_turn`, `read_conversation`)
- Runs via `uv run conversation_search.py [PATTERN]` with no venv or pyproject.toml
- Automatically reindexes on file changes with 2-second debounce
- Dynamically discovers new project directories matching the pattern

## Problem Statement
Claude Code sessions are isolated. When a session ends, the agent loses access to all context — decisions, errors, solutions. Conversation transcripts exist as JSONL files under `~/.claude/projects/` but are opaque and unsearchable. Cross-session recall relies on manually curated MEMORY.md files, which are limited and lossy. There is no way for the agent to search its own conversation history mid-session.

## Solution Approach
Build a single-file MCP server using the reference implementation from Eric Tramel's article as a starting point (~270 lines, 2 tools, single directory). Extend it to support:
- 4 tools (adding `read_turn` and `read_conversation`)
- Multi-directory indexing with configurable glob pattern
- Dynamic directory discovery via parent directory watcher
- Enhanced session summary resolution (summary record > slug > first-message fallback)
- Project name derivation from directory names
- Tool call rendering as compact summaries in `read_turn`/`read_conversation`

BM25 is the right choice because agents search with keywords (not natural language), and BM25 matches keywords with microsecond latency and zero external dependencies beyond `bm25s`.

## Relevant Files

### Architecture References
- `ai-docs/features/001-conversation-search-mcp.md` — Component specification (PRD)
- `ai-docs/sources/Searchable_Agent _Memory_in_a_Single_File.md` — Reference implementation article

### Existing Files
- None modified — this is a new component

### New Files
- `conversation_search.py` — The single-file MCP server (project root)

## Implementation Phases

### Phase 1: Foundation
- Create `conversation_search.py` with PEP 723 inline metadata
- Implement JSONL parsing with all filtering rules (skip `isMeta`, skip non-user/assistant types, skip command tags, skip list-content user messages, skip thinking blocks)
- Implement turn construction logic (user-assistant pairs)
- Implement session summary resolution chain (summary record > slug > first-message)
- Implement session-to-filepath mapping (`dict[str, Path]`)
- Implement tool call rendering rules for compact summaries

### Phase 2: Core Implementation
- Implement multi-directory JSONL discovery using configurable glob pattern against `~/.claude/projects/`
- Implement project name derivation from directory names (strip common prefix pattern)
- Implement BM25 indexing with `bm25s` (tokenize with English stopwords, default k1/b params)
- Implement all 4 MCP tools with FastMCP decorators:
  - `search_conversations` — BM25 search with optional session_id and project filters
  - `list_conversations` — Browse sessions sorted by last_timestamp desc, with optional project filter
  - `read_turn` — Full-fidelity single turn retrieval via on-demand JSONL re-parsing
  - `read_conversation` — Paginated session reading with offset/limit
- Implement thread-safe index access via `threading.Lock`

### Phase 3: Integration & Polish
- Implement debounced reindexing via watchdog (2-second debounce, per-directory observers)
- Implement dynamic directory discovery (parent dir watcher for new matching subdirectories)
- Implement CLI argument parsing (optional pattern, defaults to `-home-gbr-work-001-sites*`)
- Wire up `main()` with startup indexing, observer registration, and `mcp_server.run()`
- Smoke test: verify server starts, indexes, and responds to tool calls
- Verify reindexing works on file modification
- Verify dynamic directory discovery works

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.
  - This is critical. Your job is to act as a high level director of the team, not a builder.
  - Your role is to validate all work is going well and make sure the team is on track to complete the plan.
  - You'll orchestrate this by using the Task* Tools to manage coordination between the team members.
  - Communication is paramount. You'll use the Task* Tools to communicate with the team members and ensure they're on track to complete the plan.
- Take note of the session id of each team member. This is how you'll reference them.

### Team Members

- Builder
  - Name: builder-server
  - Role: Implements the complete conversation_search.py single-file MCP server
  - Agent Type: builder
  - Resume: true

- Validator
  - Name: validator-final
  - Role: Validates the implementation against acceptance criteria, runs smoke tests
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

- IMPORTANT: Execute every step in order, top to bottom. Each task maps directly to a `TaskCreate` call.
- Before you start, run `TaskCreate` to create the initial task list that all team members can see and execute.

### 1. Implement JSONL Parsing and Turn Construction
- **Task ID**: implement-jsonl-parsing
- **Depends On**: none
- **Assigned To**: builder-server
- **Agent Type**: builder
- **Parallel**: false
- Create `conversation_search.py` at `/home/gbr/work/ai/cross-session-memory/conversation_search.py`
- Add PEP 723 inline script metadata:
  ```python
  # /// script
  # requires-python = ">=3.10"
  # dependencies = ["bm25s", "mcp", "watchdog"]
  # ///
  ```
- Implement `_parse_conversation(jsonl_path: Path) -> tuple[list[dict], dict]`:
  - Read JSONL line by line with error handling for malformed lines
  - **Filtering rules** — skip these records:
    - Records where `isMeta` is `true`
    - All record types except `user` and `assistant` (for turn construction)
    - User messages where `content` is a `list` (tool results — part of preceding turn)
    - User messages containing only `<command-name>`, `<local-command-stdout>`, or `<local-command-caveat>` XML tags (Claude Code internal machinery)
    - `thinking` blocks in assistant content
  - **Turn construction**:
    - A turn starts when a non-meta `user` record with string `content` is encountered
    - Collects subsequent `assistant` record text blocks (skip thinking blocks)
    - Collects tool names from `tool_use` blocks in assistant records
    - Turn ends when the next valid user record begins
    - Search corpus text: `user_text + "\n" + assistant_text + "\n" + "tools: " + sorted_tool_names`
  - **Session summary resolution** (priority chain):
    1. `summary` record's `summary` field (look for `type: "summary"` records)
    2. `slug` field from user/assistant records
    3. First 200 chars of first non-meta user message with string content
  - **Metadata extraction per turn**: `session_id`, `turn_number`, `timestamp`, `slug`, `project`
  - **Session metadata**: `slug`, `summary`, `first_timestamp`, `last_timestamp`, `turn_count`, `cwd`, `git_branch`
  - Return `session_id -> Path` mapping entry for this file
- Implement **tool call rendering** helper for `read_turn`/`read_conversation` (used later but define now):
  - `Read`: `{"tool": "Read", "file": <file_path>}`
  - `Write`: `{"tool": "Write", "file": <file_path>, "chars": <len(content)>}`
  - `Edit`: `{"tool": "Edit", "file": <file_path>}`
  - `Bash`: `{"tool": "Bash", "command": <command, truncated to 200 chars>}`
  - `Grep`/`Glob`: `{"tool": <name>, "pattern": <pattern>}`
  - `Task`: `{"tool": "Task", "type": <subagent_type>, "description": <description>}`
  - All others: `{"tool": <name>}`
- Implement `_reparse_turns(jsonl_path: Path) -> list[dict]` for on-demand full-fidelity re-parsing:
  - Same parsing logic but returns full `user_text`, `assistant_text`, and rendered `tools_used` list per turn
  - This is used by `read_turn` and `read_conversation` (not from the BM25 index)

### 2. Implement Multi-Directory Indexing and BM25
- **Task ID**: implement-indexing
- **Depends On**: implement-jsonl-parsing
- **Assigned To**: builder-server
- **Agent Type**: builder
- **Parallel**: false
- Implement `_discover_directories(pattern: str) -> list[Path]`:
  - Glob `~/.claude/projects/` for subdirectories matching `pattern`
  - Return sorted list of matching directory paths
- Implement `_derive_project_name(dir_name: str) -> str`:
  - Strip common prefix from directory name: e.g. `-home-gbr-work-001-sites-diff-website` -> `diff-website`
  - Strategy: find the last path-like segment. The directory names use `-` as path separator. Take everything after the pattern's prefix portion. A reasonable heuristic: split on `-home-gbr-work-` prefix variations and take the remainder, or simply take the last meaningful segment(s).
  - Simpler approach per the PRD: the directories are like `-home-gbr-work-001-sites-diff-website`. The "project" is the meaningful tail. Since the prefix is the home/work path encoded with dashes, strip the common prefix shared by all matching directories and use the remainder.
- Implement `_build_index(pattern: str) -> tuple[list[dict], BM25 | None, dict[str, dict], dict[str, Path]]`:
  - Discover all matching directories
  - Parse all JSONL files across all directories
  - Attach `project` name to each turn and session metadata
  - Build session-to-filepath mapping: `dict[str, Path]` mapping `session_id -> jsonl_file_path`
  - Tokenize corpus with `bm25s.tokenize(texts, stopwords="en")`
  - Build BM25 index with `bm25s.BM25()` default params (k1=1.5, b=0.75)
  - Return corpus, retriever, conversations dict, session-to-filepath mapping
- Set up module-level globals with `threading.Lock` for thread-safe access:
  - `_bm25_retriever`, `_corpus`, `_conversations`, `_session_files`, `_index_lock`

### 3. Implement MCP Tools
- **Task ID**: implement-mcp-tools
- **Depends On**: implement-indexing
- **Assigned To**: builder-server
- **Agent Type**: builder
- **Parallel**: false
- Create `FastMCP("conversation-search")` server instance
- Implement `search_conversations(query, limit=10, session_id=None, project=None) -> str`:
  - Acquire index lock, get local references
  - Tokenize query with same params as indexing
  - Retrieve top-k results via `retriever.retrieve()`
  - Filter by `session_id` and/or `project` if provided (post-retrieval filtering; retrieve more than `limit` to account for filtering, e.g. `min(limit * 3, len(corpus))`)
  - Return JSON with results array: `session_id`, `project`, `turn_number`, `score`, `snippet` (first 300 chars), `timestamp`
- Implement `list_conversations(project=None, limit=50) -> str`:
  - Acquire index lock, get conversations dict
  - Optionally filter by project substring
  - Sort by `last_timestamp` descending
  - Apply limit
  - Return JSON with conversations array: `session_id`, `project`, `summary`, `slug`, `first_timestamp`, `last_timestamp`, `turn_count`, `cwd`, `git_branch`
- Implement `read_turn(session_id, turn_number) -> str`:
  - Look up `session_id` in `_session_files` mapping to get JSONL path
  - Error if unknown: `{"error": "Unknown session_id: <id>"}`
  - Re-parse JSONL on demand via `_reparse_turns()`
  - Error if turn out of range: `{"error": "Turn <n> out of range (session has <total> turns)"}`
  - Return JSON with full turn: `session_id`, `turn_number`, `timestamp`, `user_text`, `assistant_text`, `tools_used`
- Implement `read_conversation(session_id, offset=0, limit=10) -> str`:
  - Look up session, re-parse on demand
  - Error if unknown session
  - Slice turns by `[offset:offset+limit]`
  - Return JSON with: `session_id`, `project`, `cwd`, `git_branch`, `total_turns`, `offset`, `limit`, `turns` array

### 4. Implement Filesystem Watching and CLI
- **Task ID**: implement-watching-cli
- **Depends On**: implement-mcp-tools
- **Assigned To**: builder-server
- **Agent Type**: builder
- **Parallel**: false
- Implement `_ConvChangeHandler(FileSystemEventHandler)`:
  - 2-second debounce via `threading.Timer`
  - Thread-safe timer management with `threading.Lock`
  - Filter: only trigger on `.jsonl` file events
  - On trigger: call `_build_index()` and swap globals under `_index_lock`
  - Handle `on_created`, `on_modified`, `on_deleted`, `on_moved`
- Implement `_DirDiscoveryHandler(FileSystemEventHandler)`:
  - Watches `~/.claude/projects/` parent directory for new subdirectories
  - On directory creation: check if name matches the configured pattern
  - If match: trigger full reindex (re-globs, picks up new dir) with same 2-second debounce
  - Register a new watchdog observer for the new directory
- Implement `main()`:
  - `argparse` with optional positional `pattern` argument (default: `-home-gbr-work-001-sites*`)
  - Initial index build at startup
  - Log index stats to stderr: number of directories, files, turns indexed
  - Set up watchdog observers:
    - One per matching project directory (for JSONL changes, `recursive=False`)
    - One on `~/.claude/projects/` parent (for new directory discovery, `recursive=False`)
  - All observers set as daemon threads
  - Call `mcp_server.run()`
- Wire up `if __name__ == "__main__": main()`

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: implement-watching-cli
- **Assigned To**: validator-final
- **Agent Type**: validator
- **Parallel**: false
- Verify `conversation_search.py` exists at the correct path
- Verify PEP 723 inline metadata is present with correct dependencies
- Verify all 4 MCP tools are defined with correct signatures and docstrings
- Verify JSONL filtering rules are implemented (isMeta skip, command tag skip, thinking block skip, list-content skip)
- Verify session summary resolution chain (summary > slug > first-message)
- Verify tool call rendering rules for all specified tool types
- Verify multi-directory support with glob pattern
- Verify project name derivation logic
- Verify debounced reindexing implementation
- Verify dynamic directory discovery implementation
- Verify thread-safe index access
- Run `python3 -c "import ast; ast.parse(open('conversation_search.py').read())"` to verify syntax
- Run `uvx ruff check conversation_search.py` to verify linting passes
- Run `uv run conversation_search.py --help` to verify CLI works (should show usage)
- Smoke test: Run the server briefly and verify it starts and indexes without errors

## Acceptance Criteria
- [ ] Single file `conversation_search.py` at project root with PEP 723 metadata
- [ ] Dependencies: `bm25s`, `mcp` (FastMCP), `watchdog` — no others
- [ ] Runs via `uv run conversation_search.py [PATTERN]` with no venv
- [ ] Parses JSONL transcripts with all filtering rules from the PRD
- [ ] Session summary uses resolution chain: summary record > slug > first-message
- [ ] Indexes across multiple project directories matching configurable glob pattern
- [ ] Default pattern: `-home-gbr-work-001-sites*`
- [ ] Project name derived from directory name (stripped prefix)
- [ ] 4 MCP tools: `search_conversations`, `list_conversations`, `read_turn`, `read_conversation`
- [ ] `search_conversations` returns scored results with snippets, supports session_id and project filters
- [ ] `list_conversations` returns sessions sorted by last_timestamp desc, supports project filter
- [ ] `read_turn` re-parses JSONL on demand, returns full turn with rendered tool calls
- [ ] `read_conversation` supports pagination with offset/limit
- [ ] Watchdog-based reindexing with 2-second debounce
- [ ] Dynamic directory discovery for new project directories
- [ ] Thread-safe index access via `threading.Lock`
- [ ] Tool call rendering as compact summaries (Read, Write, Edit, Bash, Grep, Glob, Task, others)
- [ ] No syntax errors, passes ruff linting
- [ ] Server starts and indexes successfully against real JSONL data

## Validation Commands
```bash
# Syntax check
python3 -c "import ast; ast.parse(open('/home/gbr/work/ai/cross-session-memory/conversation_search.py').read())"

# Lint check
uvx ruff check /home/gbr/work/ai/cross-session-memory/conversation_search.py

# CLI help
uv run /home/gbr/work/ai/cross-session-memory/conversation_search.py --help

# Smoke test (start server, should index and print stats to stderr, then wait for MCP connections)
timeout 10 uv run /home/gbr/work/ai/cross-session-memory/conversation_search.py 2>&1 || true

# Count indexed data (quick sanity check via the tool)
echo '{"method":"tools/list"}' | timeout 10 uv run /home/gbr/work/ai/cross-session-memory/conversation_search.py 2>/dev/null || true
```

## Notes
- The reference implementation (~270 lines) provides working code for `search` and `list_conversations` with single-directory support. Use it as a structural template but implement multi-directory support, 2 additional tools, and all PRD-specified enhancements from scratch.
- The JSONL format has been verified against actual files. Key observations:
  - `slug` appears on ~91% of user/assistant records (311/342 in sampled file)
  - `summary` record exists with AI-generated descriptions (e.g., "ProcessWire RockMultisite Memory Exhaustion Investigation")
  - Record types confirmed: `file-history-snapshot`, `progress`, `user`, `assistant`, `system`, `summary`
  - `cwd` and `gitBranch` fields present on user records
  - There are currently 10 project directories matching `-home-gbr-work-001-sites*` with 17+ JSONL files per directory
- The `_reparse_turns()` function for `read_turn`/`read_conversation` should NOT use the BM25 index. It re-parses the original JSONL file on demand to return full-fidelity content. This keeps the index lightweight while allowing detailed retrieval.
- For project name derivation: the directory names encode the full filesystem path with `-` separators (e.g., `-home-gbr-work-001-sites-diff-website`). A good heuristic is to find the common prefix across all matching directories and strip it, leaving just the project-specific suffix.
- The user messages to skip (containing only XML command tags) look like: `<command-name>/commit</command-name>` or `<local-command-stdout>...</local-command-stdout>`. Use regex or string matching to detect these.
