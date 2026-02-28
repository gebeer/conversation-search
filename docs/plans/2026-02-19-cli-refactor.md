# CLI + MCP Refactor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extract core search/index logic into a `ConversationIndex` class, add CLI subcommands, make MCP server a thin wrapper.

**Architecture:** Single-file refactor of `conversation_search.py`. Module globals + inline MCP tool logic become a `ConversationIndex` class with dict-returning methods. `main()` gains subcommands (`serve`, `search`, `list`, `read-turn`, `read-conv`). MCP tools become one-line wrappers. Pure functions stay module-level. No test infrastructure exists — verification is done by running the tool.

**Tech Stack:** Python 3.10+, bm25s, mcp (FastMCP), watchdog, argparse. Single `uv run --script` file.

**Design doc:** `docs/plans/2026-02-19-cli-refactor-design.md`

---

## Task 1: Add ConversationIndex Class

Additive change — the class is inserted alongside existing code. Nothing uses it yet. Existing behavior is unchanged.

**Files:**
- Modify: `conversation_search.py` — insert class between `_build_index` and the TASK 3 MCP tools section

**Step 1: Insert the ConversationIndex class**

Find this exact string in the file:

```
    return corpus, retriever, conversations, session_files


# ---------------------------------------------------------------------------
# TASK 3: MCP Tools
# ---------------------------------------------------------------------------
```

Replace with:

```
    return corpus, retriever, conversations, session_files


# ---------------------------------------------------------------------------
# Core index class
# ---------------------------------------------------------------------------


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

    def search(
        self,
        query: str,
        limit: int = 10,
        session_id: str | None = None,
        project: str | None = None,
    ) -> dict:
        """BM25 keyword search across all conversation turns.

        Returns dict with 'results', 'query', 'total'.
        """
        with self._lock:
            retriever = self._retriever
            corpus = self._corpus

        if retriever is None or not corpus:
            return {"results": [], "query": query, "total": 0}

        query_tokens = bm25s.tokenize([query], stopwords="en")
        k = min(limit * 3, len(corpus))
        results, scores = retriever.retrieve(query_tokens, k=k)

        search_results: list[dict] = []
        for i in range(results.shape[1]):
            if len(search_results) >= limit:
                break
            doc_idx = results[0, i]
            score = float(scores[0, i])
            if score <= 0:
                continue
            entry = corpus[doc_idx]
            if session_id and entry.get("session_id") != session_id:
                continue
            if project and project.lower() not in entry.get("project", "").lower():
                continue
            search_results.append({
                "session_id": entry["session_id"],
                "project": entry.get("project", ""),
                "turn_number": entry["turn_number"],
                "score": round(score, 4),
                "snippet": entry["text"][:300],
                "timestamp": entry.get("timestamp", ""),
            })

        return {"results": search_results, "query": query, "total": len(search_results)}

    def list_conversations(
        self,
        project: str | None = None,
        limit: int = 50,
    ) -> dict:
        """List indexed sessions. Returns dict with 'conversations', 'total'."""
        with self._lock:
            conversations = dict(self._conversations)

        conv_list = []
        for sid, meta in conversations.items():
            if project and project.lower() not in meta.get("project", "").lower():
                continue
            conv_list.append({"session_id": sid, **meta})

        conv_list.sort(key=lambda c: c.get("last_timestamp", ""), reverse=True)
        conv_list = conv_list[:limit]

        return {"conversations": conv_list, "total": len(conv_list)}

    def read_turn(self, session_id: str, turn_number: int) -> dict:
        """Full-fidelity read of a single turn."""
        with self._lock:
            session_files = dict(self._session_files)

        jsonl_path = session_files.get(session_id)
        if jsonl_path is None:
            return {"error": f"Unknown session_id: {session_id}"}

        turns = _reparse_turns(jsonl_path)

        if turn_number < 0 or turn_number >= len(turns):
            return {"error": f"Turn {turn_number} out of range (session has {len(turns)} turns)"}

        turn = turns[turn_number]
        return {
            "session_id": turn["session_id"],
            "turn_number": turn["turn_number"],
            "timestamp": turn["timestamp"],
            "user_text": turn["user_text"],
            "assistant_text": turn["assistant_text"],
            "tools_used": turn["tools_used"],
        }

    def read_conversation(
        self,
        session_id: str,
        offset: int = 0,
        limit: int = 10,
    ) -> dict:
        """Paginated reading of turns from a session."""
        with self._lock:
            session_files = dict(self._session_files)
            conversations = dict(self._conversations)

        jsonl_path = session_files.get(session_id)
        if jsonl_path is None:
            return {"error": f"Unknown session_id: {session_id}"}

        meta = conversations.get(session_id, {})
        turns = _reparse_turns(jsonl_path)
        sliced = turns[offset : offset + limit]

        return {
            "session_id": session_id,
            "project": meta.get("project", ""),
            "cwd": meta.get("cwd", ""),
            "git_branch": meta.get("git_branch", ""),
            "total_turns": len(turns),
            "offset": offset,
            "limit": limit,
            "turns": [
                {
                    "turn_number": t["turn_number"],
                    "timestamp": t["timestamp"],
                    "user_text": t["user_text"],
                    "assistant_text": t["assistant_text"],
                    "tools_used": t["tools_used"],
                }
                for t in sliced
            ],
        }


# ---------------------------------------------------------------------------
# TASK 3: MCP Tools — registered inside _run_mcp_server()
# ---------------------------------------------------------------------------
```

**Step 2: Verify the file still runs**

Run: `timeout 5 uv run /home/claude/repos/conversation-search-mcp/conversation_search.py --pattern "*" 2>&1 | head -1`
Expected: The index line prints (e.g. `Indexed 10 directories, 819 files, ...`). The server starts and gets killed by timeout. Exit code 124 is fine (that's timeout's code).

**Step 3: Commit**

```bash
cd /home/claude/repos/conversation-search-mcp
git add conversation_search.py
git commit -m "refactor: add ConversationIndex class (additive, not yet wired)"
```

---

## Task 2: Wire ConversationIndex + Add CLI Subcommands

The big switch. Five sequential edits to `conversation_search.py`, all committed together. The file will be broken between edits — that's fine, only the final state matters.

**Files:**
- Modify: `conversation_search.py` — 5 edits

### Step 1: Remove module-level globals

Find:

```python
# ---------------------------------------------------------------------------
# Module-level globals
# ---------------------------------------------------------------------------
_index_lock = threading.Lock()
_bm25_retriever: bm25s.BM25 | None = None
_corpus: list[dict] = []
_conversations: dict[str, dict] = {}
_session_files: dict[str, Path] = {}
_pattern: str = ""

_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
```

Replace with:

```python
# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
```

### Step 2: Remove the 4 old MCP tool functions

Find and replace the entire TASK 3 section. The old section starts with the section comment added in Task 1:

```python
# ---------------------------------------------------------------------------
# TASK 3: MCP Tools — registered inside _run_mcp_server()
# ---------------------------------------------------------------------------
```

Wait — after Task 1, this comment is already correct. The OLD MCP tool functions (the `@mcp_server.tool()` decorated ones) were between the original TASK 3 comment and the TASK 4 comment. But Task 1 replaced that original TASK 3 comment with the class + new comment.

Let me be precise. After Task 1, the file has:

1. The ConversationIndex class and the new `# TASK 3: MCP Tools — registered inside _run_mcp_server()` comment (from Task 1's insertion)
2. Then the OLD `@mcp_server.tool()` functions still exist right after
3. Then the TASK 4 section

So after Task 1, the file looks like:

```
... ConversationIndex class ...

# TASK 3: MCP Tools — registered inside _run_mcp_server()
# (new comment from Task 1)

@mcp_server.tool()
def search_conversations(...):     <-- OLD, still here
...
@mcp_server.tool()
def read_conversation(...):        <-- OLD, still here
...

# TASK 4: Filesystem Watching and CLI
```

Find the old tools block (everything from the first `@mcp_server.tool()` to just before TASK 4):

```python
@mcp_server.tool()
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
    with _index_lock:
        retriever = _bm25_retriever
        corpus = _corpus

    if retriever is None or not corpus:
        return json.dumps({"results": [], "query": query, "total": 0})

    query_tokens = bm25s.tokenize([query], stopwords="en")
    # Retrieve more than limit to account for post-retrieval filtering
    k = min(limit * 3, len(corpus))
    results, scores = retriever.retrieve(query_tokens, k=k)

    search_results: list[dict] = []
    for i in range(results.shape[1]):
        if len(search_results) >= limit:
            break
        doc_idx = results[0, i]
        score = float(scores[0, i])
        if score <= 0:
            continue
        entry = corpus[doc_idx]
        # Apply filters
        if session_id and entry.get("session_id") != session_id:
            continue
        if project and project.lower() not in entry.get("project", "").lower():
            continue
        search_results.append({
            "session_id": entry["session_id"],
            "project": entry.get("project", ""),
            "turn_number": entry["turn_number"],
            "score": round(score, 4),
            "snippet": entry["text"][:300],
            "timestamp": entry.get("timestamp", ""),
        })

    return json.dumps({"results": search_results, "query": query, "total": len(search_results)})


@mcp_server.tool()
def list_conversations(project: str | None = None, limit: int = 50) -> str:
    """List all indexed conversations with metadata.

    Args:
        project: Optional substring filter for project name.
        limit: Maximum number of conversations to return.
    """
    with _index_lock:
        conversations = _conversations

    conv_list = []
    for sid, meta in conversations.items():
        if project and project.lower() not in meta.get("project", "").lower():
            continue
        conv_list.append({"session_id": sid, **meta})

    conv_list.sort(key=lambda c: c.get("last_timestamp", ""), reverse=True)
    conv_list = conv_list[:limit]

    return json.dumps({"conversations": conv_list, "total": len(conv_list)})


@mcp_server.tool()
def read_turn(session_id: str, turn_number: int) -> str:
    """Read a specific turn from a conversation with full fidelity.

    Args:
        session_id: The session UUID to read from.
        turn_number: Zero-based turn index.
    """
    with _index_lock:
        session_files = _session_files

    jsonl_path = session_files.get(session_id)
    if jsonl_path is None:
        return json.dumps({"error": f"Unknown session_id: {session_id}"})

    turns = _reparse_turns(jsonl_path)

    if turn_number < 0 or turn_number >= len(turns):
        return json.dumps({
            "error": f"Turn {turn_number} out of range (session has {len(turns)} turns)"
        })

    turn = turns[turn_number]
    return json.dumps({
        "session_id": turn["session_id"],
        "turn_number": turn["turn_number"],
        "timestamp": turn["timestamp"],
        "user_text": turn["user_text"],
        "assistant_text": turn["assistant_text"],
        "tools_used": turn["tools_used"],
    })


@mcp_server.tool()
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
    with _index_lock:
        session_files = _session_files
        conversations = _conversations

    jsonl_path = session_files.get(session_id)
    if jsonl_path is None:
        return json.dumps({"error": f"Unknown session_id: {session_id}"})

    meta = conversations.get(session_id, {})
    turns = _reparse_turns(jsonl_path)
    sliced = turns[offset : offset + limit]

    return json.dumps({
        "session_id": session_id,
        "project": meta.get("project", ""),
        "cwd": meta.get("cwd", ""),
        "git_branch": meta.get("git_branch", ""),
        "total_turns": len(turns),
        "offset": offset,
        "limit": limit,
        "turns": [
            {
                "turn_number": t["turn_number"],
                "timestamp": t["timestamp"],
                "user_text": t["user_text"],
                "assistant_text": t["assistant_text"],
                "tools_used": t["tools_used"],
            }
            for t in sliced
        ],
    })


# ---------------------------------------------------------------------------
# TASK 4: Filesystem Watching and CLI
# ---------------------------------------------------------------------------
```

Replace with just:

```python
# ---------------------------------------------------------------------------
# TASK 4: Filesystem Watching and CLI
# ---------------------------------------------------------------------------
```

### Step 3: Update _ConvChangeHandler to accept index

Find:

```python
class _ConvChangeHandler(FileSystemEventHandler):
    """Watches a project directory for JSONL changes and triggers reindex."""

    def __init__(self, pattern: str) -> None:
        self._pattern = pattern
        self._debounce_timer: threading.Timer | None = None
        self._debounce_lock = threading.Lock()

    def _schedule_reindex(self) -> None:
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(2.0, self._do_reindex)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _do_reindex(self) -> None:
        global _bm25_retriever, _corpus, _conversations, _session_files
        corpus, retriever, conversations, session_files = _build_index(self._pattern)
        with _index_lock:
            _corpus = corpus
            _bm25_retriever = retriever
            _conversations = conversations
            _session_files = session_files
```

Replace with:

```python
class _ConvChangeHandler(FileSystemEventHandler):
    """Watches a project directory for JSONL changes and triggers reindex."""

    def __init__(self, pattern: str, index: ConversationIndex) -> None:
        self._pattern = pattern
        self._index = index
        self._debounce_timer: threading.Timer | None = None
        self._debounce_lock = threading.Lock()

    def _schedule_reindex(self) -> None:
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(2.0, self._do_reindex)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _do_reindex(self) -> None:
        self._index.build(self._pattern)
```

### Step 4: Add _run_mcp_server and replace main()

Find:

```python
def main() -> None:
    global _bm25_retriever, _corpus, _conversations, _session_files, _pattern

    parser = argparse.ArgumentParser(
        description="MCP server for searching Claude Code conversation transcripts"
    )
    parser.add_argument(
        "--pattern",
        required=True,
        help="Glob pattern for project directories under ~/.claude/projects/ (e.g. '*' for all, '-home-gbr-work-*' for a subtree)",
    )
    args = parser.parse_args()
    _pattern = args.pattern

    # Initial index build
    corpus, retriever, conversations, session_files = _build_index(_pattern)
    with _index_lock:
        _corpus = corpus
        _bm25_retriever = retriever
        _conversations = conversations
        _session_files = session_files

    # Set up filesystem watchers
    conv_handler = _ConvChangeHandler(_pattern)
    observer = Observer()
    observer.daemon = True

    # Watch each matching project directory for JSONL changes
    directories = _discover_directories(_pattern)
    for d in directories:
        observer.schedule(conv_handler, str(d), recursive=False)

    # Watch parent directory for new project directories
    dir_discovery = _DirDiscoveryHandler(_pattern, observer, conv_handler)
    dir_discovery._watched_dirs = {str(d) for d in directories}
    observer.schedule(dir_discovery, str(_PROJECTS_ROOT), recursive=False)

    observer.start()
    mcp_server.run()


if __name__ == "__main__":
    main()
```

Replace with:

```python
def _run_mcp_server(pattern: str) -> None:
    """Start the MCP server with filesystem watchers."""
    index = ConversationIndex()
    index.build(pattern)

    # Filesystem watchers
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

    # MCP tool registration — thin wrappers around index methods
    @mcp_server.tool()
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

    @mcp_server.tool()
    def list_conversations(project: str | None = None, limit: int = 50) -> str:
        """List all indexed conversations with metadata.

        Args:
            project: Optional substring filter for project name.
            limit: Maximum number of conversations to return.
        """
        return json.dumps(index.list_conversations(project, limit))

    @mcp_server.tool()
    def read_turn(session_id: str, turn_number: int) -> str:
        """Read a specific turn from a conversation with full fidelity.

        Args:
            session_id: The session UUID to read from.
            turn_number: Zero-based turn index.
        """
        return json.dumps(index.read_turn(session_id, turn_number))

    @mcp_server.tool()
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

    mcp_server.run()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BM25 search over Claude Code conversation transcripts"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- serve (MCP mode) ---
    serve_parser = subparsers.add_parser("serve", help="Run as MCP server")
    serve_parser.add_argument(
        "--pattern",
        default="*",
        help="Glob pattern for project directories under ~/.claude/projects/ (default: '*')",
    )

    # --- search ---
    search_parser = subparsers.add_parser("search", help="Search conversations")
    search_parser.add_argument("--pattern", default="*")
    search_parser.add_argument("--query", "-q", required=True, help="Search query")
    search_parser.add_argument("--limit", "-n", type=int, default=10)
    search_parser.add_argument("--session-id", default=None)
    search_parser.add_argument("--project", "-p", default=None)

    # --- list ---
    list_parser = subparsers.add_parser("list", help="List conversations")
    list_parser.add_argument("--pattern", default="*")
    list_parser.add_argument("--project", "-p", default=None)
    list_parser.add_argument("--limit", "-n", type=int, default=50)

    # --- read-turn ---
    rt_parser = subparsers.add_parser("read-turn", help="Read a specific turn")
    rt_parser.add_argument("--pattern", default="*")
    rt_parser.add_argument("--session-id", required=True)
    rt_parser.add_argument("--turn", type=int, required=True, help="Zero-based turn number")

    # --- read-conv ---
    rc_parser = subparsers.add_parser("read-conv", help="Read consecutive turns")
    rc_parser.add_argument("--pattern", default="*")
    rc_parser.add_argument("--session-id", required=True)
    rc_parser.add_argument("--offset", type=int, default=0)
    rc_parser.add_argument("--limit", "-n", type=int, default=10)

    args = parser.parse_args()

    if args.command == "serve":
        _run_mcp_server(args.pattern)
    else:
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


if __name__ == "__main__":
    main()
```

### Step 5: Verify --help shows subcommands

Run: `uv run /home/claude/repos/conversation-search-mcp/conversation_search.py --help`
Expected: Output includes `{serve,search,list,read-turn,read-conv}` in the usage line.

### Step 6: Verify CLI search works

Run: `uv run /home/claude/repos/conversation-search-mcp/conversation_search.py search -q "heartbeat" -n 3 2>/dev/null | python3 -m json.tool | head -20`
Expected: Pretty-printed JSON with `"results"` array, `"query"`, `"total"` keys.

### Step 7: Verify CLI list works

Run: `uv run /home/claude/repos/conversation-search-mcp/conversation_search.py list -n 3 2>/dev/null | python3 -m json.tool | head -5`
Expected: Pretty-printed JSON with `"conversations"` array.

### Step 8: Verify serve mode starts

Run: `timeout 5 uv run /home/claude/repos/conversation-search-mcp/conversation_search.py serve 2>&1 | head -1`
Expected: Prints the `Indexed N directories...` line to stderr. Exit code 124 (timeout).

### Step 9: Commit

```bash
cd /home/claude/repos/conversation-search-mcp
git add conversation_search.py
git commit -m "refactor: wire ConversationIndex, add CLI subcommands

BREAKING: 'serve' subcommand now required for MCP mode.
Before: conversation_search.py --pattern '*'
After:  conversation_search.py serve --pattern '*'

--pattern now defaults to '*' on all subcommands.

CLI subcommands: search, list, read-turn, read-conv.
All output JSON to stdout, index progress to stderr."
```

---

## Task 3: Update Configs and README

**Files:**
- Modify: `/home/claude/.mcporter/mcporter.json`
- Modify: `/home/claude/repos/conversation-search-mcp/.mcp.json`
- Modify: `/home/claude/repos/conversation-search-mcp/README.md`

### Step 1: Update mcporter.json

In `/home/claude/.mcporter/mcporter.json`, find:

```json
      "args": [
        "run",
        "/home/claude/repos/conversation-search-mcp/conversation_search.py",
        "--pattern",
        "*"
      ]
```

Replace with:

```json
      "args": [
        "run",
        "/home/claude/repos/conversation-search-mcp/conversation_search.py",
        "serve",
        "--pattern",
        "*"
      ]
```

### Step 2: Update .mcp.json

In `/home/claude/repos/conversation-search-mcp/.mcp.json`, find:

```json
      "args": ["run", "/home/gbr/work/ai/cross-session-memory/conversation_search.py", "--pattern", "*"]
```

Replace with:

```json
      "args": ["run", "/home/gbr/work/ai/cross-session-memory/conversation_search.py", "serve", "--pattern", "*"]
```

### Step 3: Update README.md — Configuration section

In `/home/claude/repos/conversation-search-mcp/README.md`, find:

```json
      "args": ["run", "/absolute/path/to/conversation_search.py", "--pattern", "<pattern>"]
```

Replace with:

```json
      "args": ["run", "/absolute/path/to/conversation_search.py", "serve", "--pattern", "<pattern>"]
```

### Step 4: Update README.md — add CLI Usage section

In `/home/claude/repos/conversation-search-mcp/README.md`, find:

```markdown
## Tools
```

Insert BEFORE it:

```markdown
## CLI Usage

The tool can also be used directly from the command line for scripting and debugging:

```bash
# Search conversations (--pattern defaults to '*')
uv run conversation_search.py search --query "heartbeat" --limit 5

# List conversations
uv run conversation_search.py list --project "claude" --limit 10

# Read a specific turn (full fidelity)
uv run conversation_search.py read-turn --session-id "<uuid>" --turn 5

# Read consecutive turns
uv run conversation_search.py read-conv --session-id "<uuid>" --offset 0 --limit 10
```

All CLI commands output pretty-printed JSON to stdout. Index progress is printed to stderr. Use `2>/dev/null` to suppress progress output when piping.

## Tools
```

Wait — that would duplicate `## Tools`. The insert should replace `## Tools` and re-add it. Let me be precise:

Find in README.md:

```markdown
Restart Claude Code after editing `.mcp.json`.

## Tools
```

Replace with:

```markdown
Restart Claude Code after editing `.mcp.json`.

## CLI Usage

The tool can also be used directly from the command line for scripting and debugging:

```bash
# Search conversations (--pattern defaults to '*')
uv run conversation_search.py search --query "heartbeat" --limit 5

# List conversations
uv run conversation_search.py list --project "claude" --limit 10

# Read a specific turn (full fidelity)
uv run conversation_search.py read-turn --session-id "<uuid>" --turn 5

# Read consecutive turns
uv run conversation_search.py read-conv --session-id "<uuid>" --offset 0 --limit 10
```

All CLI commands output pretty-printed JSON to stdout. Index progress is printed to stderr. Use `2>/dev/null` to suppress progress output when piping.

## Tools
```

### Step 5: Update README.md — update description

In `/home/claude/repos/conversation-search-mcp/README.md`, find the first line:

```markdown
# conversation-search

MCP server that provides BM25 keyword search over Claude Code conversation history. Indexes JSONL transcripts from `~/.claude/projects/` and exposes them as searchable memory across sessions.
```

Replace with:

```markdown
# conversation-search

BM25 keyword search over Claude Code conversation history. Indexes JSONL transcripts from `~/.claude/projects/` and exposes them as searchable memory. Available as both an MCP server and a CLI tool.
```

### Step 6: Commit

```bash
cd /home/claude/repos/conversation-search-mcp
git add .mcp.json README.md
git commit -m "docs: update configs and README for serve subcommand and CLI usage"
```

Note: `mcporter.json` is outside the repo — updated but not committed here.

---

## Verification Checklist (Post-Implementation)

After all 3 commits, run these to confirm everything works:

1. `uv run /home/claude/repos/conversation-search-mcp/conversation_search.py --help` — shows subcommands
2. `uv run /home/claude/repos/conversation-search-mcp/conversation_search.py search -q "heartbeat" 2>/dev/null | python3 -m json.tool` — CLI search works
3. `uv run /home/claude/repos/conversation-search-mcp/conversation_search.py list -n 5 2>/dev/null | python3 -m json.tool` — CLI list works
4. Pick a session_id from list output, run read-turn and read-conv against it
5. `timeout 5 uv run /home/claude/repos/conversation-search-mcp/conversation_search.py serve 2>&1 | head -1` — serve mode starts
6. Restart the conversation-search MCP server and verify tools work via MCP protocol
