# Conversation Search — CLI + MCP Refactor Plan

**Author**: Claude
**Date**: 2026-02-19
**Status**: Draft — fact-checked, revision 1
**Repo**: `~/repos/conversation-search-mcp/`
**Supersedes**: Part of the adapter plan (`CONVERSATION_SEARCH_ADAPTER_PLAN.md`) — this refactor should happen first, then the adapter work layers on top.

---

## Goal

Refactor `conversation_search.py` so the core functionality (indexing, search, list, read) lives in a CLI tool, and the MCP server becomes a thin wrapper that calls the same core. This enables:

1. Direct CLI usage for scripting, debugging, and non-MCP consumers
2. Cleaner separation of concerns (core logic vs transport)
3. A natural place for the adapter abstraction to land (in the core, not the MCP layer)

---

## Current Architecture (721 lines, single file)

```
conversation_search.py
├── Module globals: _index_lock, _bm25_retriever, _corpus, _conversations, _session_files, _pattern
├── _render_tool(block)                    → dict
├── _parse_conversation(jsonl_path)        → turns[], metadata{}
├── _reparse_turns(jsonl_path)             → turns[] (full fidelity)
├── _derive_project_name(dir_name, all)    → str
├── _discover_directories(pattern)         → Path[]
├── _build_index(pattern)                  → corpus, retriever, conversations, session_files
├── MCP tools (4):
│   ├── search_conversations(query, limit, session_id, project)
│   ├── list_conversations(project, limit)
│   ├── read_turn(session_id, turn_number)
│   └── read_conversation(session_id, offset, limit)
├── Filesystem watchers:
│   ├── _ConvChangeHandler        → debounced reindex on JSONL change
│   └── _DirDiscoveryHandler      → watches for new project dirs
└── main()                        → argparse + build index + start watchers + mcp_server.run()
```

Key observations:
- All state is module-level globals behind `_index_lock`
- The 4 MCP tool functions contain the actual search/read logic inline
- The parser functions (`_parse_conversation`, `_reparse_turns`) are pure — no MCP dependency
- `_build_index` is also pure — returns data, doesn't touch globals
- The watchers and MCP server are the only parts that need long-running process semantics
- The file uses `uv run --script` with inline PEP 723 dependencies — no package structure

---

## Proposed Architecture

### Option A: Single File, Dual Mode (Recommended)

Keep the single-file `uv run --script` model. Add a `--mode` flag (or infer from subcommand presence):

```
# MCP mode (existing behavior, long-running)
uv run conversation_search.py serve --pattern "*"

# CLI mode (one-shot, JSON to stdout)
uv run conversation_search.py search --pattern "*" --query "heartbeat"
uv run conversation_search.py list --pattern "*" --project "claude"
uv run conversation_search.py read-turn --pattern "*" --session-id "abc123" --turn 5
uv run conversation_search.py read-conv --pattern "*" --session-id "abc123" --offset 0 --limit 10
```

**Why single file**: ZERO designed this as a lightweight `uv run --script` tool. The `.mcp.json` and `mcporter.json` configs both reference the single file directly. Splitting into a package changes the execution model and deployment story. Not worth it at 722 lines (will be ~900 after this refactor).

### Option B: Package Split

```
conversation_search/
├── __init__.py
├── core.py           # ConversationIndex class
├── cli.py            # argparse subcommands
└── server.py         # MCP wrapper + watchers
```

**Rejected** for reasons above. Revisit if the file passes ~1200 lines.

---

## Design: ConversationIndex Class

Extract all state and logic into a class. This is the core change.

### Current (module globals + free functions)

```python
_index_lock = threading.Lock()
_bm25_retriever: bm25s.BM25 | None = None
_corpus: list[dict] = []
_conversations: dict[str, dict] = {}
_session_files: dict[str, Path] = {}

def search_conversations(query, limit, session_id, project):
    with _index_lock:
        retriever = _bm25_retriever
        corpus = _corpus
    # ... inline search logic
```

### Proposed (class)

```python
class ConversationIndex:
    """In-memory BM25 index over JSONL conversation transcripts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._retriever: bm25s.BM25 | None = None
        self._corpus: list[dict] = []
        self._conversations: dict[str, dict] = {}
        self._session_files: dict[str, Path] = {}

    def build(self, pattern: str) -> None:
        """Build or rebuild the index from matching directories."""
        corpus, retriever, conversations, session_files = _build_index(pattern)
        with self._lock:
            self._corpus = corpus
            self._retriever = retriever
            self._conversations = conversations
            self._session_files = session_files

    def search(self, query: str, limit: int = 10,
               session_id: str | None = None,
               project: str | None = None) -> dict:
        """BM25 search. Returns dict with 'results', 'query', 'total'."""
        with self._lock:
            retriever = self._retriever
            corpus = list(self._corpus)  # snapshot
        # ... search logic (moved from MCP tool function)
        # Returns dict instead of JSON string

    def list_conversations(self, project: str | None = None,
                           limit: int = 50) -> dict:
        """List indexed sessions. Returns dict with 'conversations', 'total'."""
        with self._lock:
            conversations = dict(self._conversations)
        # ... list logic
        # Returns dict instead of JSON string

    def read_turn(self, session_id: str, turn_number: int) -> dict:
        """Full-fidelity read of a single turn."""
        with self._lock:
            session_files = dict(self._session_files)
        # ... read logic
        # Returns dict instead of JSON string

    def read_conversation(self, session_id: str, offset: int = 0,
                          limit: int = 10) -> dict:
        """Paginated reading of turns."""
        with self._lock:
            session_files = dict(self._session_files)
            conversations = dict(self._conversations)
        # ... read logic
        # Returns dict instead of JSON string
```

### Key Design Decisions

1. **Methods return dicts, not JSON strings.** The MCP layer calls `json.dumps()`. The CLI layer calls `json.dumps()` with `indent=2`. This avoids double-serialization and lets both consumers format as they want.

2. **`_build_index`, `_parse_conversation`, `_reparse_turns`, `_render_tool`, `_discover_directories`, `_derive_project_name` remain as module-level functions.** They don't touch mutable module globals — their only side effects are filesystem I/O (reading JSONL files) and `_build_index` prints a progress line to stderr. The class calls them. Moving them into the class gains nothing.

3. **The lock stays in the class.** CLI mode doesn't need it (single-threaded, one-shot), but it doesn't hurt, and the class should be correct in both contexts.

4. **Thread-safety on read**: The current code copies references under the lock (`retriever = _bm25_retriever`). The class does the same. For `search`, the corpus reference is stable after build — BM25 retriever indexes by position into the corpus list, so both must be consistent. The snapshot pattern (copy reference under lock) is sufficient since rebuilds replace the entire list, not mutate in place.

---

## CLI Entry Point

### Subcommand Structure

```python
def main() -> None:
    parser = argparse.ArgumentParser(
        description="BM25 search over conversation transcripts"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- serve (MCP mode) ---
    serve_parser = subparsers.add_parser("serve", help="Run as MCP server")
    serve_parser.add_argument("--pattern", required=True, help="Glob pattern for project dirs")

    # --- search ---
    search_parser = subparsers.add_parser("search", help="Search conversations")
    search_parser.add_argument("--pattern", required=True)
    search_parser.add_argument("--query", "-q", required=True, help="Search query")
    search_parser.add_argument("--limit", "-n", type=int, default=10)
    search_parser.add_argument("--session-id", default=None)
    search_parser.add_argument("--project", "-p", default=None)

    # --- list ---
    list_parser = subparsers.add_parser("list", help="List conversations")
    list_parser.add_argument("--pattern", required=True)
    list_parser.add_argument("--project", "-p", default=None)
    list_parser.add_argument("--limit", "-n", type=int, default=50)

    # --- read-turn ---
    rt_parser = subparsers.add_parser("read-turn", help="Read a specific turn")
    rt_parser.add_argument("--pattern", required=True)
    rt_parser.add_argument("--session-id", required=True)
    rt_parser.add_argument("--turn", type=int, required=True, help="Zero-based turn number")

    # --- read-conv ---
    rc_parser = subparsers.add_parser("read-conv", help="Read consecutive turns")
    rc_parser.add_argument("--pattern", required=True)
    rc_parser.add_argument("--session-id", required=True)
    rc_parser.add_argument("--offset", type=int, default=0)
    rc_parser.add_argument("--limit", "-n", type=int, default=10)

    args = parser.parse_args()

    if args.command == "serve":
        _run_mcp_server(args.pattern)
    else:
        # CLI mode: build index once, run command, print JSON, exit
        index = ConversationIndex()
        index.build(args.pattern)

        if args.command == "search":
            result = index.search(args.query, args.limit, args.session_id, args.project)
        elif args.command == "list":
            result = index.list_conversations(args.project, args.limit)
        elif args.command == "read-turn":
            result = index.read_turn(args.session_id, args.turn)
        elif args.command == "read-conv":
            result = index.read_conversation(args.session_id, args.offset, args.limit)

        print(json.dumps(result, indent=2))
```

### `_run_mcp_server` function

Encapsulates the current `main()` logic: create `ConversationIndex`, build, set up watchers, register MCP tools (as thin wrappers around index methods), run.

```python
def _run_mcp_server(pattern: str) -> None:
    index = ConversationIndex()
    index.build(pattern)

    # Filesystem watchers — same as current, but call index.build() on changes
    conv_handler = _ConvChangeHandler(pattern, index)
    observer = Observer()
    observer.daemon = True
    directories = _discover_directories(pattern)
    for d in directories:
        observer.schedule(conv_handler, str(d), recursive=False)
    dir_discovery = _DirDiscoveryHandler(pattern, observer, conv_handler)
    dir_discovery._watched_dirs = {str(d) for d in directories}
    observer.schedule(dir_discovery, str(_PROJECTS_ROOT), recursive=False)
    observer.start()

    # MCP tool registration
    @mcp_server.tool()
    def search_conversations(query: str, limit: int = 10,
                             session_id: str | None = None,
                             project: str | None = None) -> str:
        """BM25 keyword search across all conversation turns. ..."""
        return json.dumps(index.search(query, limit, session_id, project))

    @mcp_server.tool()
    def list_conversations(project: str | None = None, limit: int = 50) -> str:
        """List all indexed conversations with metadata. ..."""
        return json.dumps(index.list_conversations(project, limit))

    @mcp_server.tool()
    def read_turn(session_id: str, turn_number: int) -> str:
        """Read a specific turn with full fidelity. ..."""
        return json.dumps(index.read_turn(session_id, turn_number))

    @mcp_server.tool()
    def read_conversation(session_id: str, offset: int = 0, limit: int = 10) -> str:
        """Read multiple turns from a conversation. ..."""
        return json.dumps(index.read_conversation(session_id, offset, limit))

    mcp_server.run()
```

### Watcher Changes

`_ConvChangeHandler._do_reindex()` currently calls `_build_index()` and updates module globals directly. `_DirDiscoveryHandler` triggers reindex indirectly via `self._conv_handler._schedule_reindex()` — it does NOT call `_build_index()` itself.

After refactor, `_ConvChangeHandler` receives the `ConversationIndex` instance and calls `index.build(pattern)`. `_DirDiscoveryHandler` continues to trigger reindex indirectly through its `_conv_handler` reference — no change in that delegation pattern.

```python
class _ConvChangeHandler(FileSystemEventHandler):
    def __init__(self, pattern: str, index: ConversationIndex) -> None:
        self._pattern = pattern
        self._index = index
        # ... debounce logic unchanged

    def _do_reindex(self) -> None:
        self._index.build(self._pattern)
```

---

## Breaking Changes

### MCP Config

The `serve` subcommand is new. Existing config:

```json
"args": ["run", "conversation_search.py", "--pattern", "*"]
```

Must become:

```json
"args": ["run", "conversation_search.py", "serve", "--pattern", "*"]
```

This affects:
- `~/.mcporter/mcporter.json` (Archibald)
- `.mcp.json` in the repo (ZERO's personal config)
- Any other installations

**Decision**: Break it. Update both configs. The tool has one user (ZERO) and two config files — a 10-second edit each.

---

## Scope Boundary: What This Plan Does NOT Cover

1. **Adapter abstraction** — that's the adapter plan (`CONVERSATION_SEARCH_ADAPTER_PLAN.md`). This refactor creates the clean foundation for adapters to slot into.
2. **`--source` flag for additional JSONL directories** — adapter plan, Phase 2.
3. **OpenClaw adapter implementation** — adapter plan, Phase 2.
4. **Package splitting** — not needed at current size.

After this refactor, the adapter plan's Phase 1 (introduce adapter abstraction) modifies `_parse_conversation` and `_reparse_turns` to accept an adapter parameter. That change happens inside the `ConversationIndex` class's `build` and `read_turn`/`read_conversation` methods — the class provides a natural home for it.

---

## Implementation Steps

### Step 1: Create `ConversationIndex` class (~30 min)

1. Define the class with `__init__`, `build`, `search`, `list_conversations`, `read_turn`, `read_conversation`.
2. Move search/read logic from the 4 MCP tool functions into the class methods.
3. Change return types from `str` (JSON) to `dict`.
4. Module-level pure functions (`_parse_conversation`, `_reparse_turns`, `_render_tool`, `_build_index`, `_discover_directories`, `_derive_project_name`, `_COMMAND_TAG_RE`) remain untouched.

### Step 2: Update watchers to use index instance (~10 min)

1. `_ConvChangeHandler.__init__` takes `index: ConversationIndex`.
2. `_do_reindex` calls `self._index.build(self._pattern)` instead of touching globals.
3. `_DirDiscoveryHandler` receives `index` transitively through its `_conv_handler`.

### Step 3: Add CLI subcommands (~20 min)

1. Replace current `main()` with subcommand-based argparse.
2. `serve` subcommand: instantiate index, build, start watchers, register MCP tools, run server.
3. `search`, `list`, `read-turn`, `read-conv` subcommands: instantiate index, build, call method, `print(json.dumps(result, indent=2))`.

### Step 4: Update MCP tool registrations (~10 min)

1. Move MCP tool definitions into `_run_mcp_server()`.
2. Each tool becomes a thin wrapper: call `index.method()`, return `json.dumps(result)`.
3. Preserve existing docstrings and parameter types exactly (MCP clients depend on these).

### Step 5: Remove module-level globals (~5 min)

1. Delete `_index_lock`, `_bm25_retriever`, `_corpus`, `_conversations`, `_session_files`, `_pattern` from module scope.
2. The `mcp_server` (FastMCP instance) stays module-level — it's the MCP server object, not index state.

### Step 6: Update configs and test (~15 min)

1. Update `~/.mcporter/mcporter.json`: add `"serve"` to args.
2. Restart conversation-search MCP.
3. Test MCP mode: run search, list, read-turn, read-conversation from Claude Code.
4. Test CLI mode: `uv run conversation_search.py search --pattern "*" --query "heartbeat" | jq`.
5. Verify output is identical (MCP returns same JSON, CLI returns pretty-printed).

---

## Edge Cases

1. **`--pattern` required in both modes**: Every subcommand needs `--pattern`. Could make it a top-level arg, but argparse with subcommands handles this cleanly by adding it to each subparser. Minor repetition, explicit behavior.

2. **CLI index build time**: Full index build takes ~2-3 seconds (819 sessions). Acceptable for CLI one-shot usage. Could add `--quiet` to suppress the stderr progress line, but not critical.

3. **Thread lock in CLI mode**: The lock in `ConversationIndex` is unnecessary for single-threaded CLI usage but harmless. No special-casing needed.

4. **MCP tool docstrings**: The `@mcp_server.tool()` decorator uses the function docstring for tool descriptions exposed to MCP clients. When moving tools into `_run_mcp_server`, the wrapper functions must preserve the full docstrings from the current implementations — including the `Args:` blocks.

5. **FastMCP instance**: `mcp_server = FastMCP("conversation-search", instructions=...)` stays module-level. It's referenced by `_run_mcp_server` but never used in CLI mode. The import/instantiation cost is negligible.

6. **`_PROJECTS_ROOT` constant**: Stays module-level. Used by `_discover_directories` (called from `_build_index`) and `_DirDiscoveryHandler`. No change.

---

## Estimated Effort

| Step | Time |
|------|------|
| 1. ConversationIndex class | 30 min |
| 2. Watcher updates | 10 min |
| 3. CLI subcommands | 20 min |
| 4. MCP tool wrappers | 10 min |
| 5. Remove globals | 5 min |
| 6. Config + test | 15 min |
| **Total** | **~1.5 hours** |

---

## Relationship to Adapter Plan

This refactor is **prerequisite** to the adapter plan. After this:

- **Adapter plan Phase 1** (introduce adapter abstraction) modifies `_parse_conversation` and `_reparse_turns` to accept an adapter parameter. These are called by `ConversationIndex.build()` and `ConversationIndex.read_turn()`/`read_conversation()`. Clean insertion point.
- **Adapter plan Phase 2** (`--source` flag) adds arguments to both `serve` and CLI subcommands. The index's `build()` method gains a `sources` parameter.
- **Adapter plan Phase 3** (config update) is identical regardless of this refactor.

The two plans stack cleanly. Do this refactor first, then the adapter work.

---

## Files Modified

| File | Change |
|------|--------|
| `conversation_search.py` | Core refactor — class extraction, CLI subcommands, MCP wrapper |
| `~/.mcporter/mcporter.json` | Add `"serve"` subcommand to args |
| `.mcp.json` (repo) | Add `"serve"` subcommand to args |
| `README.md` | Update usage section with CLI examples |

---

## Verification Notes

*Added 2026-02-19 after Opus fact-check review against actual source code.*

### Corrections Applied

1. **Tree diagram globals list**: Was missing `_index_lock` and `_pattern`. Fixed — now lists all 6 module globals.
2. **`_DirDiscoveryHandler` behavior**: Originally claimed both watchers "call `_build_index()` and update module globals directly." Wrong — `_DirDiscoveryHandler` triggers reindex indirectly via `_conv_handler._schedule_reindex()`. Fixed with accurate description.
3. **Purity claim for module functions**: Originally called them "pure — no state, no side effects." Revised to accurately note they perform filesystem I/O and `_build_index` prints to stderr. The key property (don't touch mutable globals) is preserved.
4. **Line count**: 721, not 722. Fixed.
5. **Watcher description clarity**: Clarified that the current `_ConvChangeHandler.__init__` takes only `pattern` (not index) — the index parameter is part of the proposed change.

None of these corrections affect the plan's design decisions or implementation approach.
