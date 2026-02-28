# Daemon Log File Option

**Author**: Claude (Opus 4.6)
**Date**: 2026-02-28
**Status**: Draft
**Context**: Observed during live debugging that daemon stderr is piped through the SSE socket and not persisted. Reindex events, errors, and lifecycle messages are invisible.

---

## Problem

When the daemon runs via `connect`, stderr is inherited from the spawning subprocess and routed through the SSE socket back to the MCP bridge. This means:

- Reindex logs (`Indexed N dirs, N files (N cached, N parsed), N turns`) are not visible
- Errors during reindex are silently swallowed
- Daemon lifecycle events (startup, shutdown, idle timeout) cannot be inspected after the fact
- Debugging daemon behavior requires attaching strace or restarting in foreground mode

## Goal

Add a `--log-file` option to the `daemon` and `connect` subcommands that redirects daemon stderr to a persistent log file.

## Requirements

1. **Optional** — when omitted, behavior is unchanged (stderr to inherited fd)
2. **Works on both `daemon` and `connect`** — `connect` passes the flag through when spawning the daemon subprocess
3. **Default path** — when `--log-file` is passed without a value, use `~/.cache/conversation-search/daemon.log`
4. **Rotation** — not required in v1. File is truncated on daemon restart (append mode would also be acceptable — document the choice)
5. **Timestamps** — all log lines should be prefixed with ISO 8601 timestamps (currently they are not)
6. **No new dependencies** — use stdlib `logging` or simple print wrapper

## Design

### CLI interface

```
uv run conversation_search.py daemon --log-file                    # default path
uv run conversation_search.py daemon --log-file /tmp/daemon.log    # custom path
uv run conversation_search.py connect --log-file                   # passed through to daemon
```

### MCP config example

```json
{
  "mcpServers": {
    "conversation-search": {
      "command": "uv",
      "args": ["run", "/path/to/conversation_search.py", "connect", "--log-file"]
    }
  }
}
```

### Implementation notes

- Add `--log-file` as `nargs="?"` argument with `const=DEFAULT_LOG_PATH` and `default=None`
- In `daemon` subcommand: if `--log-file` is set, open the file and redirect stderr (`sys.stderr = open(...)` or `logging.FileHandler`)
- In `connect` subcommand: if `--log-file` is set, pass it through to the `subprocess.Popen` args and set `stderr=subprocess.DEVNULL` (since the file handles it)
- Add a timestamp prefix to all existing `print(..., file=sys.stderr)` calls, or replace with a simple log function:

```python
def _log(msg: str) -> None:
    print(f"[{datetime.utcnow().isoformat()}] {msg}", file=sys.stderr)
```

### What gets logged

All existing stderr output, now with timestamps:

- `[conversation-search] daemon starting on http://127.0.0.1:9237`
- `Indexed 12 dirs, 45 files (40 cached, 5 parsed), 312 turns`
- `New directory discovered: home-gbr-work-new-project`
- `[conversation-search] reindex error: ...`
- `[conversation-search] daemon shutting down (signal 15)`
- `[conversation-search] idle timeout (900s), shutting down`

## Out of scope

- Log rotation / max file size (can be added later or handled by external logrotate)
- Log levels / verbosity flags
- Structured JSON logging
