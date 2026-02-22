#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["bm25s", "mcp", "uvicorn", "watchdog"]
# ///
"""MCP server that indexes Claude Code JSONL conversation transcripts with BM25."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import socket
import sys
import threading
from pathlib import Path

import bm25s
from mcp.server.fastmcp import FastMCP
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

mcp_server = FastMCP("conversation-search", instructions="""\
BM25 keyword search over Claude Code conversation history (JSONL transcripts from ~/.claude/projects/).

Workflow — search wide, then read deep:
1. search_conversations: Find relevant turns by keyword. Uses BM25, so prefer specific terms over vague queries. Returns snippets (300 chars) with session_id and turn_number.
2. list_conversations: Browse sessions with metadata (project, summary, timestamps, cwd). Filter by project substring.
3. read_turn: Retrieve one turn in full fidelity (complete user text, assistant text, tools used). Use session_id + turn_number from search results.
4. read_conversation: Paginated reading of consecutive turns from a session. Use for context around a specific turn.

Key details:
- A "turn" is one user message paired with the full assistant response (text + tool calls).
- The "project" filter is a substring match against encoded directory names (e.g., "hugoerke" matches "home-gbr-work-001-sites-hugoerke-local").
- Search results are ranked by BM25 score. Retrieve more than you need (raise limit) when filtering by session or project, as filtering happens post-retrieval.
- Timestamps are ISO 8601 UTC.
""")

# ---------------------------------------------------------------------------
# Regex for detecting command-tag-only user messages
# ---------------------------------------------------------------------------
_COMMAND_TAG_RE = re.compile(
    r"\s*(<(command-name|command-message|command-args|local-command-stdout|local-command-stderr|local-command-caveat)[^>]*>.*?</\2>\s*)+",
    re.DOTALL,
)

# Skip session UUID directories and subagent artifacts
_SESSION_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# ---------------------------------------------------------------------------
# JSONL parsing and turn construction
# ---------------------------------------------------------------------------

def _render_tool(block: dict) -> dict:
    """Render a tool_use block as a compact summary dict."""
    name = block.get("name", "")
    inp = block.get("input", {})

    if name == "Read":
        return {"tool": "Read", "file": inp.get("file_path", "")}
    elif name == "Write":
        content = inp.get("content", "")
        return {"tool": "Write", "file": inp.get("file_path", ""), "chars": len(content)}
    elif name == "Edit":
        return {"tool": "Edit", "file": inp.get("file_path", "")}
    elif name == "Bash":
        cmd = inp.get("command", "")
        return {"tool": "Bash", "command": cmd[:200]}
    elif name in ("Grep", "Glob"):
        return {"tool": name, "pattern": inp.get("pattern", "")}
    elif name == "Task":
        return {
            "tool": "Task",
            "type": inp.get("subagent_type", ""),
            "description": inp.get("description", ""),
        }
    else:
        return {"tool": name}


def _parse_conversation(jsonl_path: Path) -> tuple[list[dict], dict]:
    """Parse a JSONL conversation file into search-ready turns and session metadata.

    Returns (turns, session_metadata) where each turn has a 'text' field
    suitable for BM25 indexing.
    """
    session_id = jsonl_path.stem
    turns: list[dict] = []
    slug = ""
    first_ts = ""
    last_ts = ""
    summary = ""
    summary_from_record = ""
    cwd = ""
    git_branch = ""

    current_user_text = ""
    current_assistant_text = ""
    current_tool_names: set[str] = set()
    current_ts = ""
    in_turn = False

    def _save_turn() -> None:
        nonlocal current_user_text, current_assistant_text, current_tool_names, current_ts
        if not current_user_text:
            return
        text_parts = [current_user_text, current_assistant_text]
        if current_tool_names:
            text_parts.append("tools: " + " ".join(sorted(current_tool_names)))
        turns.append({
            "text": "\n".join(text_parts),
            "turn_number": len(turns),
            "session_id": session_id,
            "timestamp": current_ts,
            "slug": slug,
        })

    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                # Summary record (separate type, not user/assistant)
                if record.get("type") == "summary":
                    s = record.get("summary", "")
                    if s and not summary_from_record:
                        summary_from_record = s
                    continue

                # Skip isMeta records
                if record.get("isMeta"):
                    continue

                # Extract slug from any record
                rec_slug = record.get("slug", "")
                if rec_slug and not slug:
                    slug = rec_slug

                # Extract cwd / gitBranch from user records
                if record.get("cwd") and not cwd:
                    cwd = record["cwd"]
                if record.get("gitBranch") and not git_branch:
                    git_branch = record["gitBranch"]

                ts = record.get("timestamp", "")
                if ts:
                    if not first_ts:
                        first_ts = ts
                    last_ts = ts

                msg_type = record.get("type")
                if msg_type not in ("user", "assistant"):
                    continue

                message = record.get("message", {})
                content = message.get("content")

                if msg_type == "user":
                    if isinstance(content, list):
                        # Tool results — skip, part of preceding turn
                        continue

                    if not isinstance(content, str):
                        continue

                    # Filter command-tag-only messages
                    if _COMMAND_TAG_RE.fullmatch(content):
                        continue

                    # Valid user message — start new turn
                    if in_turn:
                        _save_turn()

                    current_user_text = content
                    current_assistant_text = ""
                    current_tool_names = set()
                    current_ts = ts
                    in_turn = True

                elif msg_type == "assistant":
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "thinking":
                            continue
                        elif btype == "text":
                            current_assistant_text += block.get("text", "") + "\n"
                        elif btype == "tool_use":
                            name = block.get("name", "")
                            if name:
                                current_tool_names.add(name)

        if in_turn:
            _save_turn()
    except OSError:
        pass

    # Update slug on all turns (slug may have appeared after first turns)
    for turn in turns:
        turn["slug"] = slug

    # Summary resolution chain: summary record > slug > first user message
    first_user_msg = ""
    if turns:
        first_user_msg = turns[0]["text"][:200]
    summary = summary_from_record or slug or first_user_msg

    metadata = {
        "slug": slug,
        "summary": summary,
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "turn_count": len(turns),
        "cwd": cwd,
        "git_branch": git_branch,
    }

    return turns, metadata


def _reparse_turns(jsonl_path: Path) -> list[dict]:
    """Full-fidelity re-parse for read_turn / read_conversation.

    Returns turns with full user_text, assistant_text, and rendered tools_used.
    """
    session_id = jsonl_path.stem
    turns: list[dict] = []
    slug = ""
    cwd = ""
    git_branch = ""

    current_user_text = ""
    current_assistant_text = ""
    current_tools: list[dict] = []
    current_tool_names: set[str] = set()
    current_ts = ""
    in_turn = False

    def _save_turn() -> None:
        nonlocal current_user_text, current_assistant_text, current_tools, current_tool_names, current_ts
        if not current_user_text:
            return
        turns.append({
            "session_id": session_id,
            "turn_number": len(turns),
            "timestamp": current_ts,
            "user_text": current_user_text,
            "assistant_text": current_assistant_text.rstrip("\n"),
            "tools_used": current_tools,
        })

    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if record.get("isMeta"):
                    continue

                rec_slug = record.get("slug", "")
                if rec_slug and not slug:
                    slug = rec_slug
                if record.get("cwd") and not cwd:
                    cwd = record["cwd"]
                if record.get("gitBranch") and not git_branch:
                    git_branch = record["gitBranch"]

                msg_type = record.get("type")
                if msg_type not in ("user", "assistant"):
                    continue

                message = record.get("message", {})
                content = message.get("content")

                if msg_type == "user":
                    if isinstance(content, list):
                        continue
                    if not isinstance(content, str):
                        continue
                    if _COMMAND_TAG_RE.fullmatch(content):
                        continue

                    if in_turn:
                        _save_turn()

                    current_user_text = content
                    current_assistant_text = ""
                    current_tools = []
                    current_tool_names = set()
                    current_ts = record.get("timestamp", "")
                    in_turn = True

                elif msg_type == "assistant":
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "thinking":
                            continue
                        elif btype == "text":
                            current_assistant_text += block.get("text", "") + "\n"
                        elif btype == "tool_use":
                            current_tools.append(_render_tool(block))
                            name = block.get("name", "")
                            if name:
                                current_tool_names.add(name)

        if in_turn:
            _save_turn()
    except OSError:
        pass

    return turns


# ---------------------------------------------------------------------------
# Multi-directory discovery and BM25 indexing
# ---------------------------------------------------------------------------

def _discover_directories(pattern: str) -> list[Path]:
    """Glob ~/.claude/projects/ for subdirectories matching pattern.

    Filters out session UUID directories (subagent artifacts) that may
    appear directly under the projects root.
    """
    matches = sorted(
        p for p in _PROJECTS_ROOT.glob(pattern)
        if p.is_dir() and not _SESSION_UUID_RE.match(p.name)
    )
    return matches


def _derive_project_name(dir_name: str, all_dir_names: list[str]) -> str:
    """Derive a human-readable project name from an encoded directory name.

    Strips the common prefix shared by all directories, leaving the
    project-specific suffix.
    """
    if not all_dir_names:
        return dir_name

    if len(all_dir_names) == 1:
        # Single directory — use last meaningful segments
        parts = dir_name.split("-")
        # Find non-empty parts from the end
        meaningful = [p for p in parts if p]
        if len(meaningful) >= 2:
            return "-".join(meaningful[-2:])
        return dir_name

    # Find common prefix across all directory names
    segments_list = [name.split("-") for name in all_dir_names]
    min_len = min(len(s) for s in segments_list)
    common_len = 0
    for i in range(min_len):
        if all(s[i] == segments_list[0][i] for s in segments_list):
            common_len = i + 1
        else:
            break

    parts = dir_name.split("-")
    suffix_parts = parts[common_len:]
    result = "-".join(suffix_parts)
    return result if result else dir_name


# ---------------------------------------------------------------------------
# Core index class
# ---------------------------------------------------------------------------

# Type alias for the per-file cache entry: (mtime, size, turns, metadata)
_CacheEntry = tuple[float, int, list[dict], dict]


class ConversationIndex:
    """In-memory BM25 index over JSONL conversation transcripts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._retriever: bm25s.BM25 | None = None
        self._corpus: list[dict] = []
        self._conversations: dict[str, dict] = {}
        self._session_files: dict[str, Path] = {}
        # Incremental cache: path_str -> (mtime, size, turns, metadata)
        self._file_cache: dict[str, _CacheEntry] = {}

    def build(self, pattern: str) -> None:
        """Build or rebuild the index from matching directories.

        Uses incremental caching: only reparses JSONL files whose mtime or
        size changed since the last build. Unchanged files reuse cached
        parsed turns, avoiding redundant JSON parsing of the full corpus.
        """
        # Snapshot the previous cache under lock
        with self._lock:
            old_cache = dict(self._file_cache)

        directories = _discover_directories(pattern)
        all_dir_names = [d.name for d in directories]

        corpus: list[dict] = []
        conversations: dict[str, dict] = {}
        session_files: dict[str, Path] = {}
        new_cache: dict[str, _CacheEntry] = {}

        file_count = 0
        cache_hits = 0

        for directory in directories:
            project = _derive_project_name(directory.name, all_dir_names)
            for jsonl_path in sorted(directory.glob("*.jsonl")):
                # Skip subagent session files (defensive — glob is non-recursive
                # so these shouldn't appear, but guard against edge cases)
                if jsonl_path.name.startswith("agent-"):
                    continue

                file_count += 1
                session_id = jsonl_path.stem
                path_key = str(jsonl_path)

                try:
                    stat = jsonl_path.stat()
                    mtime = stat.st_mtime
                    size = stat.st_size
                except OSError:
                    continue

                # Check cache: reuse parsed data if file unchanged
                cached = old_cache.get(path_key)
                if cached is not None and cached[0] == mtime and cached[1] == size:
                    # Shallow-copy each turn dict to avoid mutating cached data
                    turns = [dict(t) for t in cached[2]]
                    metadata = dict(cached[3])
                    cache_hits += 1
                else:
                    turns, metadata = _parse_conversation(jsonl_path)

                # Attach project label to turns and metadata
                for turn in turns:
                    turn["project"] = project
                metadata["project"] = project

                # Store original (un-mutated) turns in cache for safe reuse
                cache_turns = [
                    {k: v for k, v in t.items() if k != "project"} for t in turns
                ]
                new_cache[path_key] = (mtime, size, cache_turns, metadata)
                conversations[session_id] = metadata
                session_files[session_id] = jsonl_path
                corpus.extend(turns)

        retriever = None
        if corpus:
            corpus_tokens = bm25s.tokenize(
                [entry["text"] for entry in corpus], stopwords="en"
            )
            retriever = bm25s.BM25()
            retriever.index(corpus_tokens)

        parsed = file_count - cache_hits
        print(
            f"Indexed {len(directories)} dirs, {file_count} files "
            f"({cache_hits} cached, {parsed} parsed), "
            f"{len(corpus)} turns",
            file=sys.stderr,
        )

        with self._lock:
            self._corpus = corpus
            self._retriever = retriever
            self._conversations = conversations
            self._session_files = session_files
            self._file_cache = new_cache

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
# Filesystem watching, MCP server, and CLI
# ---------------------------------------------------------------------------

_REINDEX_INTERVAL = 60.0  # seconds between reindexes


class _ConvChangeHandler(FileSystemEventHandler):
    """Watches a project directory for JSONL changes and triggers reindex."""

    def __init__(self, pattern: str, index: ConversationIndex) -> None:
        self._pattern = pattern
        self._index = index
        self._debounce_timer: threading.Timer | None = None
        self._debounce_lock = threading.Lock()
        self._reindex_pending = False
        self._reindex_running = threading.Lock()

    def _schedule_reindex(self) -> None:
        with self._debounce_lock:
            if self._reindex_pending:
                return  # Already queued — discard
            self._reindex_pending = True
            self._debounce_timer = threading.Timer(_REINDEX_INTERVAL, self._do_reindex)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _do_reindex(self) -> None:
        with self._debounce_lock:
            self._reindex_pending = False
        if not self._reindex_running.acquire(blocking=False):
            self._schedule_reindex()
            return
        try:
            self._index.build(self._pattern)
        except Exception:
            import traceback
            print(f"[conversation-search] reindex error: {traceback.format_exc()}", file=sys.stderr)
        finally:
            self._reindex_running.release()

    def _maybe_reindex(self, path: str) -> None:
        if not path.endswith(".jsonl"):
            return
        self._schedule_reindex()

    def on_created(self, event):  # type: ignore[override]
        if not event.is_directory:
            self._maybe_reindex(event.src_path)

    def on_modified(self, event):  # type: ignore[override]
        if not event.is_directory:
            self._maybe_reindex(event.src_path)

    def on_deleted(self, event):  # type: ignore[override]
        if not event.is_directory:
            self._maybe_reindex(event.src_path)

    def on_moved(self, event):  # type: ignore[override]
        if not event.is_directory:
            self._maybe_reindex(event.dest_path)


class _DirDiscoveryHandler(FileSystemEventHandler):
    """Watches ~/.claude/projects/ for new subdirectories matching the pattern."""

    def __init__(self, pattern: str, observer: Observer, conv_handler: _ConvChangeHandler) -> None:
        self._pattern = pattern
        self._observer = observer
        self._conv_handler = conv_handler
        self._debounce_timer: threading.Timer | None = None
        self._debounce_lock = threading.Lock()
        self._watched_dirs: set[str] = set()

    def _schedule_check(self, dir_path: str) -> None:
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(5.0, self._do_check, args=(dir_path,))
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _do_check(self, dir_path: str) -> None:
        p = Path(dir_path)
        if not p.is_dir():
            return
        # Check if directory name matches the configured pattern
        if not fnmatch.fnmatch(p.name, self._pattern):
            return
        dir_str = str(p)
        if dir_str in self._watched_dirs:
            return
        self._watched_dirs.add(dir_str)
        print(f"New directory discovered: {p.name}", file=sys.stderr)
        self._observer.schedule(self._conv_handler, dir_str, recursive=False)
        # Trigger full reindex
        self._conv_handler._schedule_reindex()

    def on_created(self, event):  # type: ignore[override]
        if event.is_directory:
            self._schedule_check(event.src_path)


# ---------------------------------------------------------------------------
# Daemon helpers
# ---------------------------------------------------------------------------

_DAEMON_CACHE_DIR = Path.home() / ".cache" / "conversation-search"
_DEFAULT_PORT = 9237
_DEFAULT_IDLE_TIMEOUT = 900  # 15 minutes


def _daemon_cache_dir() -> Path:
    """Return the cache dir, creating it if needed."""
    _DAEMON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _DAEMON_CACHE_DIR


def _read_daemon_state() -> tuple[int, int] | None:
    """Read (pid, port) from cache files. Returns None if files missing or malformed."""
    cache = _DAEMON_CACHE_DIR
    pid_file = cache / "daemon.pid"
    port_file = cache / "daemon.port"
    try:
        pid = int(pid_file.read_text().strip())
        port = int(port_file.read_text().strip())
        return pid, port
    except (FileNotFoundError, ValueError):
        return None


def _is_pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID exists.

    PermissionError means the process exists but is owned by another user —
    still considered alive. ProcessLookupError means no such process.
    """
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # process exists, owned by another user
    except (OSError, ProcessLookupError):
        return False


def _is_port_responding(port: int) -> bool:
    """Return True if something is listening on localhost:port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _is_daemon_healthy(pid: int, port: int) -> bool:
    """Return True if daemon PID is alive and port is responding."""
    return _is_pid_alive(pid) and _is_port_responding(port)


def _cleanup_daemon_files() -> None:
    """Remove PID and port files from cache dir."""
    for name in ("daemon.pid", "daemon.port"):
        try:
            (_DAEMON_CACHE_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass


def _write_daemon_files(pid: int, port: int) -> None:
    """Write PID and port to cache dir. Not atomic — callers tolerate partial writes."""
    cache = _daemon_cache_dir()
    (cache / "daemon.pid").write_text(str(pid))
    (cache / "daemon.port").write_text(str(port))


def _run_daemon(port: int = _DEFAULT_PORT, idle_timeout: float = _DEFAULT_IDLE_TIMEOUT) -> None:
    """Start the SSE daemon process.

    Checks for an existing healthy daemon first. If one exists, exits immediately.
    Otherwise builds the index, starts watchers, and runs the SSE server.
    Writes PID and port to ~/.cache/conversation-search/.
    Exits after idle_timeout seconds with no MCP tool calls.
    """
    import signal
    import time

    # Check for existing daemon
    state = _read_daemon_state()
    if state is not None:
        pid, existing_port = state
        if _is_daemon_healthy(pid, existing_port):
            print(
                f"[conversation-search] daemon already running (PID {pid}, port {existing_port})",
                file=sys.stderr,
            )
            return
        # Stale files — clean up
        print("[conversation-search] cleaning up stale daemon files", file=sys.stderr)
        _cleanup_daemon_files()

    # Build index (always full corpus)
    index = ConversationIndex()
    index.build("*")

    # Start filesystem watchers
    conv_handler = _ConvChangeHandler("*", index)
    observer = Observer()
    observer.daemon = True

    directories = _discover_directories("*")
    for d in directories:
        observer.schedule(conv_handler, str(d), recursive=False)

    dir_discovery = _DirDiscoveryHandler("*", observer, conv_handler)
    dir_discovery._watched_dirs = {str(d) for d in directories}
    observer.schedule(dir_discovery, str(_PROJECTS_ROOT), recursive=False)
    observer.start()

    # Idle timeout tracking
    last_activity = [time.monotonic()]  # list so closure can mutate it

    def touch_activity() -> None:
        last_activity[0] = time.monotonic()

    def idle_watcher() -> None:
        while True:
            time.sleep(60)
            if time.monotonic() - last_activity[0] > idle_timeout:
                print(
                    f"[conversation-search] idle timeout ({idle_timeout}s), shutting down",
                    file=sys.stderr,
                )
                _cleanup_daemon_files()
                # os._exit is required: sys.exit() from a non-main thread only
                # raises SystemExit in that thread, leaving the daemon alive.
                os._exit(0)

    idle_thread = threading.Thread(target=idle_watcher, daemon=True)
    idle_thread.start()

    # Build SSE FastMCP server
    daemon_server = FastMCP(
        "conversation-search",
        instructions=mcp_server.instructions,
        host="127.0.0.1",
        port=port,
    )

    # Register tools with activity tracking.
    # These are re-registered here (not via _register_tools) because each tool
    # must call touch_activity(). _register_tools does not take a callback
    # parameter — adding one would complicate the simpler stdio path for no gain.
    @daemon_server.tool()
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
        touch_activity()
        return json.dumps(index.search(query, limit, session_id, project))

    @daemon_server.tool()
    def list_conversations(project: str | None = None, limit: int = 50) -> str:
        """List all indexed conversations with metadata.

        Args:
            project: Optional substring filter for project name.
            limit: Maximum number of conversations to return.
        """
        touch_activity()
        return json.dumps(index.list_conversations(project, limit))

    @daemon_server.tool()
    def read_turn(session_id: str, turn_number: int) -> str:
        """Read a specific turn from a conversation with full fidelity.

        Args:
            session_id: The session UUID to read from.
            turn_number: Zero-based turn index.
        """
        touch_activity()
        return json.dumps(index.read_turn(session_id, turn_number))

    @daemon_server.tool()
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
        touch_activity()
        return json.dumps(index.read_conversation(session_id, offset, limit))

    # Write PID/port before starting uvicorn. There is a short window between
    # this write and the port actually being bound (~1s). A concurrent `connect`
    # checking _is_daemon_healthy() in this window will get False and may attempt
    # to start a second daemon, which will fail on port bind. This is acceptable —
    # the second attempt will retry and find the healthy daemon.
    _write_daemon_files(os.getpid(), port)

    def _shutdown(signum: int, frame: object) -> None:
        print(f"[conversation-search] daemon shutting down (signal {signum})", file=sys.stderr)
        _cleanup_daemon_files()
        # os._exit terminates all threads immediately (including the observer).
        # observer.stop() is omitted — it would be a no-op before os._exit.
        os._exit(0)

    # Register cleanup handlers. Note: uvicorn's serve() will override SIGTERM/SIGINT
    # with its own graceful-shutdown handler, which saves our handlers first and
    # re-raises after uvicorn teardown completes. _shutdown therefore runs after
    # uvicorn has already shut down HTTP — observer.stop() and os._exit(0) are safe.
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    import atexit
    atexit.register(_cleanup_daemon_files)

    print(f"[conversation-search] daemon starting on http://127.0.0.1:{port}", file=sys.stderr)
    daemon_server.run(transport="sse")


def _run_connect(port: int = _DEFAULT_PORT, idle_timeout: float = _DEFAULT_IDLE_TIMEOUT) -> None:
    """Launcher + stdio↔SSE bridge for MCP config.

    Ensures the daemon is running (starts it if not), then bridges
    Claude Code's stdio MCP protocol to the daemon's SSE endpoint.
    Runs until the SSE connection closes or stdin reaches EOF.
    """
    import subprocess
    import time
    import anyio

    sse_url = f"http://127.0.0.1:{port}/sse"

    def _ensure_daemon_running() -> None:
        """Start the daemon if not already healthy. Waits up to 30s for it to be ready."""
        state = _read_daemon_state()
        if state is not None:
            pid, existing_port = state
            if existing_port == port and _is_daemon_healthy(pid, existing_port):
                return  # Already up

        # Start daemon in background
        print(f"[conversation-search] starting daemon on port {port}...", file=sys.stderr)
        subprocess.Popen(
            [
                sys.executable,
                __file__,
                "daemon",
                "--port", str(port),
                "--idle-timeout", str(idle_timeout),
            ],
            stdout=subprocess.DEVNULL,
            stderr=sys.stderr,
            start_new_session=True,
        )

        # Wait for port to respond (up to 30s)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _is_port_responding(port):
                return
            time.sleep(0.5)

        raise RuntimeError(
            f"[conversation-search] daemon failed to start on port {port} within 30s"
        )

    _ensure_daemon_running()

    # Bridge stdio ↔ SSE
    from mcp.client.sse import sse_client
    from mcp.server.stdio import stdio_server

    async def _bridge() -> None:
        async with stdio_server() as (stdio_read, stdio_write):
            async with sse_client(sse_url, sse_read_timeout=idle_timeout + 60) as (sse_read, sse_write):
                async with anyio.create_task_group() as tg:
                    async def forward_to_daemon() -> None:
                        async for message in stdio_read:
                            await sse_write.send(message)

                    async def forward_to_client() -> None:
                        async for message in sse_read:
                            await stdio_write.send(message)

                    tg.start_soon(forward_to_daemon)
                    tg.start_soon(forward_to_client)

    anyio.run(_bridge)


def _register_tools(server: FastMCP, index: ConversationIndex) -> None:
    """Register the four MCP search tools on *server*, closing over *index*.

    Parameterised so the same tool set can be wired to distinct FastMCP
    instances (e.g. the stdio server and a daemon SSE server) each backed
    by its own ConversationIndex. Call once per server instance, before
    server.run().
    """

    @server.tool()
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

    @server.tool()
    def list_conversations(project: str | None = None, limit: int = 50) -> str:
        """List all indexed conversations with metadata.

        Args:
            project: Optional substring filter for project name.
            limit: Maximum number of conversations to return.
        """
        return json.dumps(index.list_conversations(project, limit))

    @server.tool()
    def read_turn(session_id: str, turn_number: int) -> str:
        """Read a specific turn from a conversation with full fidelity.

        Args:
            session_id: The session UUID to read from.
            turn_number: Zero-based turn index.
        """
        return json.dumps(index.read_turn(session_id, turn_number))

    @server.tool()
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
    _register_tools(mcp_server, index)
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

    # --- daemon (SSE server mode) ---
    daemon_parser = subparsers.add_parser("daemon", help="Run as persistent SSE daemon")
    daemon_parser.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help=f"Localhost port for SSE server (default: {_DEFAULT_PORT})",
    )
    daemon_parser.add_argument(
        "--idle-timeout",
        type=float,
        default=_DEFAULT_IDLE_TIMEOUT,
        metavar="SECONDS",
        help=f"Seconds of inactivity before daemon exits (default: {_DEFAULT_IDLE_TIMEOUT})",
    )

    # --- connect (launcher + stdio<->SSE bridge) ---
    connect_parser = subparsers.add_parser(
        "connect",
        help="Ensure daemon is running and bridge stdio to it (use this in MCP config)",
    )
    connect_parser.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help=f"Daemon port (default: {_DEFAULT_PORT})",
    )
    connect_parser.add_argument(
        "--idle-timeout",
        type=float,
        default=_DEFAULT_IDLE_TIMEOUT,
        metavar="SECONDS",
        help=f"Idle timeout passed to daemon on spawn (default: {_DEFAULT_IDLE_TIMEOUT})",
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
    elif args.command == "daemon":
        _run_daemon(port=args.port, idle_timeout=args.idle_timeout)
    elif args.command == "connect":
        _run_connect(port=args.port, idle_timeout=args.idle_timeout)
    else:
        # CLI subcommands build a local index directly. Forwarding queries to a
        # running daemon (to reuse its warm index) is deferred to v2 — it would
        # require a full JSON-RPC client against the SSE server, duplicating the
        # connect bridge for marginal benefit on one-shot CLI queries.
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
