# Conversation Search MCP — Multi-Format Adapter Plan

**Author**: Claude
**Date**: 2026-02-19
**Status**: Draft — fact-checked, revision 1
**Repo**: `~/repos/conversation-search-mcp/`

---

## Goal

Extend the conversation-search MCP server to index JSONL session logs from sources beyond Claude Code. First adapter: OpenClaw. Architecture should support future adapters for OpenCode, Gemini CLI, Kimi CLI, etc.

## Requirements

1. **Backward compatible**: Existing Claude Code indexing must not change behavior
2. **Pluggable adapters**: Each source format gets its own adapter that normalizes records to a common internal representation
3. **Configurable paths**: Support `--source` arguments pointing to arbitrary directories, not just `~/.claude/projects/`
4. **Clear separation**: Adapter logic isolated from indexing/search logic
5. **Future-proof**: Adding a new CLI format should require only writing one new adapter file — no changes to core

---

## Current Architecture

```
conversation_search.py (single file, 722 lines)
├── _parse_conversation(jsonl_path)      → turns[], metadata{}
├── _reparse_turns(jsonl_path)           → turns[] (full fidelity)
├── _discover_directories(pattern)        → Path[]
├── _build_index(pattern)                → corpus, retriever, conversations, session_files
├── MCP tools: search, list, read_turn, read_conversation
└── Filesystem watchers
```

The parser (`_parse_conversation`) is hardcoded for Claude Code's JSONL format:
- Filters on `type in ("user", "assistant")` (line 167)
- Skips `isMeta` records (line 147)
- Expects user `message.content` to be a string (or list for tool results — lists are skipped)
- Filters out command-tag-only user messages via `_COMMAND_TAG_RE.fullmatch()` (line 182)
- Expects assistant `message.content` to be a list of blocks with `type: text|tool_use|thinking`
- Extracts `cwd`, `gitBranch`, `slug` from record-level fields (present on most record types)
- Handles `type: "summary"` records for session summary extraction

---

## Schema Comparison: OpenClaw vs Claude Code

### Key Structural Differences

| Aspect | Claude Code | OpenClaw |
|--------|------------|----------|
| **Message type field** | `type: "user"` / `type: "assistant"` | `type: "message"` for all roles |
| **Role location** | Both `record.type` and `message.role` | Only `message.role` (record type is always `"message"`) |
| **User content format** | String for text, list for tool results | Always list of blocks: `[{"type": "text", "text": "..."}]` |
| **Assistant content blocks** | `text`, `tool_use`, `thinking` | `text`, `toolCall`, `thinking` (thinking may have `thoughtSignature` instead of readable text) |
| **Tool call block type** | `"tool_use"` with `name` + `input` | `"toolCall"` with `name` + `arguments` (both are dicts) |
| **Tool results** | `type: "user"` with `content: [{"type": "tool_result", ...}]` | `type: "message"` with `role: "toolResult"`, `toolCallId`, `toolName`, `content` (list of blocks), `details` (status, exitCode, cwd) |
| **Session metadata** | `cwd`, `gitBranch`, `slug` on most records (user, assistant, progress) | Top-level `cwd` only on `type: "session"` record; `cwd` also nested in `toolResult.details.cwd` but not at record level |
| **Session start** | No explicit start record (has `queue-operation`, `progress` types) | `type: "session"` record with `version`, `id`, `cwd` |
| **Auto-generated startup message** | N/A | First assistant message after session record: `"New session started · model: ..."` with `provider: "openclaw"` |
| **Summary/compaction** | `type: "summary"` record with `summary`, `leafUuid` | `type: "compaction"` with `summary`, `firstKeptEntryId`, `tokensBefore`, `details`, `fromHook` |
| **Error messages** | N/A (errors are tool_result with `is_error: true`) | Assistant messages with `content: []` and `errorMessage` field, `stopReason: "error"` |
| **Timestamps** | ISO 8601 on records, Unix ms inside messages | ISO 8601 on records, Unix ms inside messages |
| **Tool names (core)** | `Bash`, `Read`, `Write`, `Edit`, `Grep`, `Glob`, `Task` | `exec`, `read`, `write`, `edit`, `browser`, `process`, `web_fetch`, `web_search` |
| **Tool names (OpenClaw-specific)** | N/A | `sessions_send`, `message`, `sessions_history`, `sessions_list`, `sessions_spawn`, `session_status`, `cron`, `mcporter`, `agents_list`, `gateway`, `memory_search` |
| **Tool arg field names** | `input.file_path` (Read), `input.command` (Bash), `input.content` (Write) | `arguments.file_path` (read), `arguments.command` (exec), `arguments.content` (write) |
| **Non-message record types** | `queue-operation`, `progress`, `summary` | `session`, `model_change`, `thinking_level_change`, `custom`, `compaction` |
| **Meta records** | `isMeta: true` flag on some records | N/A (no isMeta concept) |
| **Directory structure** | Nested: `~/.claude/projects/{dir}/*.jsonl` | Flat: `~/.openclaw/agents/{agent}/sessions/*.jsonl` |
| **Non-JSONL files in dir** | None expected | `sessions.json`, `*.jsonl.bak`, `*.jsonl.reset.*`, `*.jsonl.deleted.*` (all correctly excluded by `glob("*.jsonl")`) |

### What an OpenClaw Adapter Must Do

1. **Map `type: "message"` → `"user"` or `"assistant"`** based on `message.role`
2. **Convert user content** from `[{"type": "text", "text": "..."}]` → plain string
3. **Map tool call block type** from `"toolCall"` → normalized form; `arguments` → normalized
4. **Skip `role: "toolResult"` messages** (tool results are not turn-starting)
5. **Extract session metadata** from the `type: "session"` record (cwd) since it's not on every record
6. **Map `type: "compaction"` → summary** for session summary extraction
7. **Skip non-message types**: `session`, `model_change`, `thinking_level_change`, `custom`
8. **Filter OpenClaw startup messages**: Skip the auto-generated "New session started" assistant message (identifiable by `provider: "openclaw"` or `model: "delivery-mirror"`)
9. **Handle error messages**: Skip or include assistant messages with `content: []` and `errorMessage`

---

## Proposed Architecture

### Directory Structure

```
conversation_search/
├── __init__.py
├── server.py                    # MCP server, tools, watchers (from current main)
├── indexer.py                   # BM25 indexing, _build_index, search logic
├── types.py                     # Common data types (Turn, SessionMetadata)
├── adapters/
│   ├── __init__.py
│   ├── base.py                  # Abstract adapter interface
│   ├── claude_code.py           # Claude Code JSONL adapter (current parser logic)
│   └── openclaw.py              # OpenClaw JSONL adapter (new)
└── conversation_search.py       # Entry point (thin, calls server.py)
```

**Wait — this repo is a single-file uv script.**

The current file uses the `uv run --script` shebang with inline `pyproject.toml`-style dependencies. Splitting into a package changes the execution model. Two options:

#### Option A: Stay Single-File, Adapters as Functions

Keep `conversation_search.py` as a single file. Add adapter functions within it. Adapters are just functions that take a JSONL line and return a normalized record (or None to skip).

**Pros**: No package restructuring. Same deployment model. Simple.
**Cons**: File grows. Less clean separation. But at 722 lines currently, adding ~200 for adapter logic keeps it under 1000 — manageable.

#### Option B: Split Into Package

Convert to a proper Python package with `pyproject.toml`, separate adapter files.

**Pros**: Clean separation. Easy to add adapters.
**Cons**: Changes execution model. Needs packaging. Breaks the simple `uv run --script` pattern that ZERO's repo uses.

### Recommendation: Option A (Single File with Adapter Functions)

The repo is ZERO's — it's designed as a lightweight single-file MCP server. Respecting that design choice matters more than architectural purity. The adapter logic is small enough to fit inline.

---

## Implementation Plan

### Phase 1: Introduce Adapter Abstraction (Refactor Only)

**Goal**: Extract the Claude Code parsing logic into an adapter interface without changing behavior.

#### Step 1.1: Define Common Types

Add at the top of `conversation_search.py`:

```python
@dataclass
class NormalizedRecord:
    """Common representation for records from any JSONL source."""
    type: str                    # "user", "assistant", "summary", "meta", "skip"
    content_text: str            # For user: the message text. For assistant: accumulated text.
    tool_names: list[str]        # Tool names used (assistant only)
    tools_rendered: list[dict]   # Full tool rendering (for read_turn)
    timestamp: str               # ISO 8601
    cwd: str                     # Working directory (if available)
    git_branch: str              # Git branch (if available)
    slug: str                    # Project slug (if available)
    summary: str                 # Summary text (for summary records only)
    is_tool_result: bool         # True if user message is a tool result (skip as turn start)
    is_command_tag: bool         # True if user message is command-tag-only (Claude Code specific)
    raw: dict                    # Original record for adapter-specific needs
```

#### Step 1.2: Define Adapter Protocol

```python
class Adapter(Protocol):
    """Interface for JSONL format adapters."""
    name: str

    def normalize(self, record: dict) -> NormalizedRecord:
        """Convert a raw JSONL record to normalized form."""
        ...

    def render_tool(self, block: dict) -> dict:
        """Render a tool use block for full-fidelity output."""
        ...
```

**Design note**: Command-tag filtering (`_COMMAND_TAG_RE`) is Claude Code-specific. The `ClaudeCodeAdapter.normalize()` method should set `is_command_tag=True` for these records. The shared parser then skips them. This keeps the filtering logic in the adapter where it belongs, while keeping the skip behavior in the parser.

#### Step 1.3: Extract Claude Code Adapter

Move the current parsing logic (record type filtering, content extraction, tool rendering, command-tag detection, `isMeta` filtering) into a `ClaudeCodeAdapter` class. The `_parse_conversation` and `_reparse_turns` functions become adapter-agnostic — they call `adapter.normalize()` on each record instead of inline parsing.

**Test**: Run existing MCP tools against Claude Code sessions. Output must be identical.

### Phase 2: Add OpenClaw Adapter

#### Step 2.1: Implement `OpenClawAdapter`

```python
class OpenClawAdapter:
    name = "openclaw"

    def normalize(self, record: dict) -> NormalizedRecord:
        rtype = record.get("type")

        # Session start → extract cwd
        if rtype == "session":
            return NormalizedRecord(
                type="meta",
                cwd=record.get("cwd", ""),
                ...  # other fields empty/default
            )

        # Compaction → summary
        if rtype == "compaction":
            return NormalizedRecord(
                type="summary",
                summary=record.get("summary", ""),
                ...
            )

        # Skip non-message types
        if rtype != "message":
            return NormalizedRecord(type="skip", ...)

        msg = record.get("message", {})
        role = msg.get("role", "")
        content = msg.get("content", [])

        if role == "user":
            # Extract text from content blocks
            text = " ".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            return NormalizedRecord(type="user", content_text=text, ...)

        elif role == "assistant":
            # Skip auto-generated startup messages
            if msg.get("model") == "delivery-mirror":
                return NormalizedRecord(type="skip", ...)

            # Skip error-only messages (empty content + errorMessage)
            if not content and msg.get("errorMessage"):
                return NormalizedRecord(type="skip", ...)

            text_parts = []
            tool_names = []
            tools_rendered = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "toolCall":
                    tool_names.append(block.get("name", ""))
                    tools_rendered.append(self.render_tool(block))
                elif btype == "thinking":
                    pass  # Skip thinking blocks (may have thoughtSignature, not readable text)
            return NormalizedRecord(
                type="assistant",
                content_text="\n".join(text_parts),
                tool_names=tool_names,
                tools_rendered=tools_rendered,
                ...
            )

        elif role == "toolResult":
            return NormalizedRecord(type="skip", is_tool_result=True, ...)

        return NormalizedRecord(type="skip", ...)

    def render_tool(self, block: dict) -> dict:
        name = block.get("name", "")
        args = block.get("arguments", {})

        # Map OpenClaw tool names to display format
        if name == "exec":
            return {"tool": "exec", "command": args.get("command", "")[:200]}
        elif name == "read":
            return {"tool": "read", "file": args.get("file_path", "")}
        elif name in ("edit", "write"):
            return {"tool": name, "file": args.get("file_path", "")}
        elif name == "browser":
            return {"tool": "browser", "action": args.get("action", "")}
        elif name in ("web_search", "web_fetch"):
            return {"tool": name, "query": args.get("query", args.get("url", ""))[:100]}
        elif name in ("sessions_send", "message"):
            return {"tool": name, "target": args.get("accountId", args.get("to", ""))}
        else:
            return {"tool": name}
```

#### Step 2.2: Wire Up Source Configuration

Add new CLI arguments:

```python
parser.add_argument(
    "--source",
    action="append",
    default=[],
    help="Additional source: 'format:path' (e.g., 'openclaw:~/.openclaw/agents/clawd/sessions'). Can be repeated."
)
```

Each `--source` arg specifies a format and path. The indexer discovers JSONL files in each path and uses the appropriate adapter.

**Default behavior preserved**: Without `--source`, the server still indexes `~/.claude/projects/{pattern}` using the Claude Code adapter.

#### Step 2.3: Update Discovery and Indexing

Modify `_build_index` to handle multiple sources:

```python
def _build_index(pattern, sources):
    # 1. Index Claude Code sessions (existing behavior)
    directories = _discover_directories(pattern)
    all_dir_names = [d.name for d in directories]
    for directory in directories:
        adapter = ClaudeCodeAdapter()
        project = _derive_project_name(directory.name, all_dir_names)
        for jsonl_path in sorted(directory.glob("*.jsonl")):
            turns, metadata = _parse_conversation(jsonl_path, adapter)
            # ... index as before, with project from directory name

    # 2. Index additional sources
    for source_spec in sources:
        format_name, source_path = source_spec.split(":", 1)
        adapter = get_adapter(format_name)  # Returns ClaudeCodeAdapter or OpenClawAdapter
        source_dir = Path(source_path).expanduser()

        # Derive project name from source path (e.g., "openclaw-clawd" from path ending in "clawd/sessions")
        project = _derive_source_project(format_name, source_dir)

        for jsonl_path in sorted(source_dir.glob("*.jsonl")):
            session_id = f"{format_name}:{jsonl_path.stem}"  # Composite key to avoid collisions
            turns, metadata = _parse_conversation(jsonl_path, adapter)
            # ... index same as above, with composite session_id

    # 3. Build BM25 index over combined corpus
    ...
```

**Note**: The composite session_id (`format_name:uuid`) must be used consistently in `conversations`, `session_files`, and turn records. The MCP tools (`read_turn`, `read_conversation`) must pass the composite key back to `_reparse_turns` after stripping the prefix to get the file path.

#### Step 2.4: Update Filesystem Watchers

Add watchers for each additional source directory, using the same debounced reindex pattern.

**Note**: OpenClaw source directories are flat (no subdirectories to discover), so only `_ConvChangeHandler` is needed — no `_DirDiscoveryHandler` for additional sources.

### Phase 3: MCP Config Update

Update `~/.mcporter/mcporter.json` to pass the OpenClaw source:

```json
{
  "command": "uv",
  "args": [
    "run",
    "/home/claude/repos/conversation-search-mcp/conversation_search.py",
    "--pattern", "*",
    "--source", "openclaw:~/.openclaw/agents/clawd/sessions",
    "--source", "openclaw:~/.openclaw/agents/claude/sessions"
  ]
}
```

---

## Adapter Interface Contract

Every adapter must handle these responsibilities:

1. **Identify record type**: Map source-specific type → `"user"`, `"assistant"`, `"summary"`, `"meta"`, or `"skip"`
2. **Extract user text**: Convert whatever format into a plain string
3. **Extract assistant text**: Accumulate text blocks, skip thinking blocks
4. **Identify tool names**: Extract tool names for BM25 indexing
5. **Render tools**: Provide compact tool summaries for `read_turn` output
6. **Extract metadata**: cwd, git_branch, slug, summary — whatever the format provides
7. **Identify tool results**: Mark user messages that are tool results (not turn-starting)
8. **Format-specific filtering**: Claude Code filters `isMeta` and command-tag-only messages; OpenClaw filters startup messages and error-only messages

The adapter does NOT need to handle:
- Turn construction (that's the parser's job)
- BM25 indexing
- Session metadata aggregation

---

## Edge Cases

1. **Empty sessions**: Session record only, no messages (OpenClaw) or no user/assistant records (Claude Code). Result: 0 turns, metadata only.
2. **Error-only sessions**: OpenClaw assistant messages with `content: []` and `errorMessage`. Adapter skips these.
3. **Compaction records**: OpenClaw `type: "compaction"` with rich `summary` field. May contain markdown and structured context. Adapter extracts `summary` text.
4. **Large sessions**: Clawd has files up to 2.7MB (65 sessions total). Must not cause memory issues.
5. **Mixed sources**: Claude Code + OpenClaw indexed together. Session ID collisions prevented by composite key.
6. **Auto-generated messages**: OpenClaw inserts a "New session started" assistant message at session start. Must be filtered to avoid polluting search results.
7. **Provider variations in tool args**: Some OpenClaw sessions use different arg names depending on the backend (e.g., `old_string`/`new_string` vs `oldText`/`newText` for edit). The `render_tool` should try multiple field names.
8. **Non-JSONL files in OpenClaw dirs**: `sessions.json`, `*.jsonl.bak`, `*.jsonl.reset.*`, `*.jsonl.deleted.*` are correctly excluded by `glob("*.jsonl")`.

---

## Future Adapters

When adding support for a new CLI format:

1. Obtain sample JSONL files from the CLI
2. Document the schema (record types, fields, content structure)
3. Implement an adapter class with `normalize()` and `render_tool()` methods
4. Register it in `get_adapter()` function
5. Add `--source format:path` to MCP config

Expected future adapters:
- **OpenCode**: Unknown JSONL format — needs research when available
- **Gemini CLI**: Unknown JSONL format — needs research
- **Kimi CLI**: Unknown JSONL format — needs research

---

## Testing Strategy

1. **Regression test**: Run existing search/list/read against Claude Code sessions after refactor. Diff output — must be identical.
2. **OpenClaw smoke test**: Index Clawd's sessions, search for known terms (e.g., "heartbeat", "blog", tool names), verify results make sense.
3. **Edge cases**:
   - Empty sessions (session record only, no messages)
   - Error-only sessions (assistant messages with empty content + errorMessage)
   - Compaction records (verify summary extraction)
   - Large sessions (Clawd has files up to 2.7MB)
   - Mixed sources (Claude Code + OpenClaw indexed together)
   - Auto-generated startup messages (should not appear as turns)
   - Sessions with provider variations in tool arg names
4. **Performance**: Measure index build time with Clawd's 65 sessions + Claude Code's existing corpus.

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Refactor breaks existing Claude Code parsing | Phase 1 is refactor-only with regression testing before adding new adapter |
| OpenClaw format evolves | Adapter is isolated; changes don't affect core or other adapters |
| Single file gets too large | ~200 lines of adapter code + ~50 lines of config. Under 1000 total. If it grows past 1200, reconsider package split. |
| Session filename collisions across sources | Use `{format}:{session_id}` as composite key in conversations dict |
| Different tool name sets confuse search | Tool names are just strings in the BM25 index — different names are fine, they just match different queries |
| Composite session_id breaks MCP tool API | Must document that `read_turn` and `read_conversation` accept composite IDs; internally resolve to file path |
| Provider variations in tool args | `render_tool` tries multiple field names with fallbacks |

---

## Implementation Order

1. **Phase 1**: Refactor current parser → adapter abstraction. Test regression. (~1 hour)
2. **Phase 2**: Implement OpenClaw adapter + `--source` CLI arg. Test with Clawd sessions. (~1.5 hours)
3. **Phase 3**: Update MCP config. Restart conversation-search MCP. Verify live. (~10 min)

Total estimated effort: ~3 hours.

---

## Verification Notes

*Added 2026-02-19 after fact-check review against actual JSONL files.*

### Schema Claims Verified

| Claim | Verdict | Evidence |
|-------|---------|----------|
| OpenClaw uses `type: "message"` for all roles | **CONFIRMED** | All user, assistant, and toolResult records have `type: "message"` in Clawd sessions |
| User content in OpenClaw is always a list | **CONFIRMED** | `[{"type": "text", "text": "..."}]` format in all user messages examined |
| Tool block type is `"toolCall"` (not `"tool_use"`) | **CONFIRMED** | 36 occurrences in sample Clawd session |
| Tool result role is `"toolResult"` | **CONFIRMED** | 38 occurrences in sample Clawd session, with `toolCallId`, `toolName`, `content`, `details` |
| `cwd` only on `type: "session"` record (top-level) | **CONFIRMED** | Only line 1 (session record) has top-level `cwd` in Clawd session; `cwd` also appears nested inside `toolResult.details.cwd` but not at record level for messages |
| `type: "compaction"` exists with `summary` field | **CONFIRMED** | Found in 3 Clawd sessions; fields include `summary`, `firstKeptEntryId`, `tokensBefore`, `details`, `fromHook` |
| Claude Code summary uses `type: "summary"` | **CONFIRMED** | Found `{"type":"summary","summary":"...","leafUuid":"..."}` in Claude Code session |
| OpenClaw `arguments` is a dict (like CC's `input`) | **CONFIRMED** | Verified: `arguments` is `dict`, not string |

### Parser Claims Verified

| Claim | Verdict | Evidence |
|-------|---------|----------|
| `_parse_conversation` filters on `type in ("user", "assistant")` | **CONFIRMED** | Line 167 of `conversation_search.py` |
| User content-as-list treated as tool results and skipped | **CONFIRMED** | Lines 174-176: `if isinstance(content, list): continue` |
| Command-tag regex exists and is used via `fullmatch` | **CONFIRMED** | Lines 54-57 define `_COMMAND_TAG_RE`, line 182 calls `.fullmatch(content)` |
| Turn construction: user message starts turn, assistant accumulates | **CONFIRMED** | Lines 186-209: user message triggers `_save_turn()` for previous turn, then resets accumulators |

### Corrections Made in This Revision

1. **Tool arg field name**: Original plan had `args.get("path", "")` for read/write/edit in `render_tool`. Actual OpenClaw uses `file_path` not `path`. Fixed to `args.get("file_path", "")`.
2. **Tool names list**: Original plan listed only 7 OpenClaw tools. Actual count is 18+. Added comprehensive list split into "core" and "OpenClaw-specific" categories.
3. **Auto-generated startup message**: Original plan did not mention OpenClaw's auto-generated "New session started" message (`model: "delivery-mirror"`). Added as filtering requirement.
4. **Error messages**: Original plan mentioned edge case but didn't show adapter handling. Added `errorMessage` detection to adapter code.
5. **Provider variations**: Some sessions show different edit arg names (`old_string`/`new_string` vs `oldText`/`newText`). Added as edge case.
6. **Command-tag filtering ownership**: Original plan said "Command-tag filtering (Claude Code-specific; other formats don't have this)" is not an adapter responsibility. Revised: it IS an adapter responsibility (Claude Code adapter sets `is_command_tag`, parser skips).
7. **Session count/size**: Updated from "60 sessions, 2.6MB" to "65 sessions, 2.7MB" based on current data.
8. **Missing Claude Code record types**: Added `queue-operation` and `progress` to the schema table.
9. **Missing `isMeta` distinction**: Added row showing Claude Code has `isMeta` flag, OpenClaw does not.
10. **Directory structure comparison**: Added row documenting nested (Claude Code) vs flat (OpenClaw) directory layout.
11. **Non-JSONL files**: Documented `sessions.json`, `.bak`, `.reset.*`, `.deleted.*` files in OpenClaw dirs.
12. **Composite session_id propagation**: Added note about ensuring composite IDs work through the MCP tool API.
13. **NormalizedRecord**: Added `is_command_tag` field for Claude Code-specific filtering.
14. **Adapter contract item 8**: Added format-specific filtering as an adapter responsibility.
15. **Edge cases section**: Extracted from testing strategy into its own section for visibility.
16. **Phase 2 estimate**: Increased from 1 hour to 1.5 hours given additional complexity discovered.
