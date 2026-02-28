# CLI + MCP Refactor Design

**Date:** 2026-02-19
**Status:** Approved
**Repo:** `~/repos/conversation-search-mcp/`
**Approach:** Single file, dual mode (Approach A)

---

## Goal

Refactor `conversation_search.py` so the core functionality (indexing, search, list, read) lives in a `ConversationIndex` class callable from both CLI subcommands and a thin MCP wrapper. This enables direct CLI usage for scripting and piping, and creates a clean foundation for future work (disk caching, adapter abstraction).

## Decisions from Brainstorming

| Question | Decision |
|----------|----------|
| Primary motivation | Scripting & piping |
| Output format | Pretty-printed JSON (indent=2) always |
| `--pattern` flag | Defaults to `*` (optional, not required) |
| Index caching | Disk serialization, but as a separate follow-up |
| Scope | Refactor first, cache second |
| File structure | Single file — `uv run --script` model preserved |

---

## 1. ConversationIndex Class

Extract all mutable state (6 module globals) and all query logic (4 MCP tool functions) into a single class.

```
ConversationIndex
├── __init__()              → empty index, threading lock
├── build(pattern)          → calls _build_index(), swaps state under lock
├── search(...)             → dict
├── list_conversations(...) → dict
├── read_turn(...)          → dict
└── read_conversation(...)  → dict
```

- Methods return **dicts**, not JSON strings. Callers serialize.
- Lock stays in the class (harmless in CLI, required for MCP's watcher threads).
- Pure functions (`_parse_conversation`, `_reparse_turns`, `_build_index`, `_render_tool`, `_discover_directories`, `_derive_project_name`) stay **module-level**. They don't touch mutable state.
- `mcp_server` (FastMCP instance) stays module-level.

## 2. CLI Subcommands

`main()` becomes subcommand-based:

```
conversation_search.py <command> [options]

  serve       Run as MCP server (long-running, with filesystem watchers)
  search      BM25 search, print results, exit
  list        List indexed sessions, exit
  read-turn   Read one turn at full fidelity, exit
  read-conv   Read consecutive turns, exit
```

- `--pattern` defaults to `"*"` on all subcommands.
- CLI commands: build index → call method → `print(json.dumps(result, indent=2))` → exit.
- Index progress goes to stderr; stdout is clean JSON for piping.
- Exit code 0 on success, 1 on errors (Python default).

**Breaking change:** MCP configs must add `serve` to args. Two files to update.

## 3. MCP Server Wrapper

The MCP path moves into `_run_mcp_server(pattern)`:

```
_run_mcp_server(pattern)
├── Create ConversationIndex, build
├── Set up filesystem watchers (watchers call index.build())
├── Register 4 MCP tools as thin wrappers:
│   └── each: return json.dumps(index.method(...))
└── mcp_server.run()
```

- MCP tool docstrings preserved exactly (clients depend on them).
- `_ConvChangeHandler.__init__` gains `index: ConversationIndex`. `_do_reindex` calls `self._index.build()`.
- `_DirDiscoveryHandler` unchanged — delegates to `_conv_handler._schedule_reindex()`.

## 4. Out of Scope

- **Disk cache** — follow-up. Serialize BM25 index + corpus, check JSONL mtimes on startup.
- **Adapter abstraction** — separate plan (`CONVERSATION_SEARCH_ADAPTER_PLAN.md`). This refactor creates the insertion point.
- **`--source` flag** — adapter plan Phase 2.
- **Test infrastructure** — separate decision.
- **Package splitting** — not needed at ~880 lines.

## Files Modified

| File | Change |
|------|--------|
| `conversation_search.py` | Class extraction, CLI subcommands, MCP wrapper |
| `~/.mcporter/mcporter.json` | Add `"serve"` to args |
| `.mcp.json` (repo) | Add `"serve"` to args |
| `README.md` | Update config example, add CLI usage section |
