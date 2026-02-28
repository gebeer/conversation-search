# Packaging: uvx Distribution

## Goal

Users install and run via:

```bash
uvx conversation-search connect
uvx conversation-search daemon
```

No cloning. No absolute paths in MCP config. MCP config becomes:

```json
{
  "mcpServers": {
    "conversation-search": {
      "command": "uvx",
      "args": ["conversation-search", "connect"]
    }
  }
}
```

## Prerequisite

Users must have `uv` installed. That's the only requirement — we can't help people who don't use uv.

## What Needs to Change

The project is currently a single-file PEP 723 uv script. To publish to PyPI via `uvx`, it needs to become a proper Python package:

- `pyproject.toml` with package metadata, entry points, and dependencies
- Package structure (`src/conversation_search/` or flat)
- Entry point wiring `conversation-search` CLI to `main()`
- The inline `# /// script` dependency block becomes redundant (deps declared in `pyproject.toml`)

## Open Questions

- Package name on PyPI: `conversation-search-mcp`? `conversation-search`? Check availability.
- Single-file vs. proper package structure — can stay single-file with the right `pyproject.toml` setup
- Version strategy
- Whether to keep the PEP 723 block for local dev convenience or drop it
