# Bug: Daemon leaves zombie process after idle shutdown

**Author**: Claude (Opus 4.6)
**Date**: 2026-02-28
**Status**: Open
**Severity**: Low (cosmetic — no resource leak, but unclean process table)

---

## Problem

When the daemon exits (idle timeout or signal), it becomes a zombie process (`<defunct>`) because its parent (`connect` bridge) never calls `wait()`.

## Reproduction

1. Start a Claude Code session with `connect` mode
2. Wait for the daemon to start (auto-launched by `connect`)
3. Wait for idle timeout (default 900s) or stop the daemon manually
4. Observe: `ps aux | grep conversation_search` shows `<defunct>`

## Root cause

`conversation_search.py` line 997–1008:

```python
subprocess.Popen(
    [sys.executable, __file__, "daemon", ...],
    stdout=subprocess.DEVNULL,
    stderr=sys.stderr,
    start_new_session=True,
)
```

- `Popen()` makes the daemon a child of the `connect` process
- `start_new_session=True` gives it a new session but does not change the parent-child relationship
- The `connect` process (stdio MCP bridge) runs for the lifetime of the Claude Code session and never calls `wait()` on the daemon child
- When the daemon exits, the kernel holds its process table entry until the parent reaps it

## Suggested fix

Either:

**A) Ignore SIGCHLD in `connect`** (simplest):
```python
import signal
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
```
Tells the kernel to auto-reap child processes. Add this in the `connect` subcommand before spawning the daemon.

**B) Double fork** (most portable):
```python
pid = os.fork()
if pid == 0:
    os.setsid()
    os.execvp(sys.executable, [sys.executable, __file__, "daemon", ...])
else:
    os.waitpid(pid, 0)  # reap intermediate child immediately
```
The daemon gets reparented to PID 1, which always reaps.

**Recommendation**: Option A — one line, no fork complexity, and `connect` has no other child processes to worry about.
