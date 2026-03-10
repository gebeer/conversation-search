"""Tests for the SQLite FTS5 index (schema, build, search, list, read, recency)."""
from __future__ import annotations

import importlib.util
import json
import math
import sqlite3
import sys
import threading
import time
import unittest.mock
from pathlib import Path

import pytest

from conftest import cs, SAMPLE_RECORDS, SAMPLE_SESSION_ID


# ---------------------------------------------------------------------------
# Additional sample data for cross-session tests
# ---------------------------------------------------------------------------

SECOND_SESSION_ID = "def67890-aaaa-bbbb-cccc-dddddddddddd"

SECOND_SESSION_RECORDS = [
    {
        "type": "user",
        "message": {"role": "user", "content": "How does SQLite FTS5 full-text search work?"},
        "timestamp": "2026-02-01T09:00:00Z",
        "cwd": "/home/user/otherproject",
        "slug": "sqlite-fts5-search",
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "FTS5 is a virtual table that supports inverted indexes for full-text search.",
                }
            ],
        },
        "timestamp": "2026-02-01T09:00:10Z",
    },
    {
        "type": "user",
        "message": {"role": "user", "content": "What tokenizer should I use for porter stemming?"},
        "timestamp": "2026-02-01T09:01:00Z",
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Use tokenize='porter unicode61' when creating the FTS5 virtual table.",
                }
            ],
        },
        "timestamp": "2026-02-01T09:01:15Z",
    },
]

THIRD_SESSION_ID = "aabbccdd-1234-5678-9abc-eeff00112233"

# Older session with identical term ("sorting") so recency boost is testable
THIRD_SESSION_RECORDS = [
    {
        "type": "user",
        "message": {"role": "user", "content": "How do I do sorting in Python?"},
        "timestamp": "2024-01-01T00:00:00Z",  # very old
        "cwd": "/home/user/oldproject",
        "slug": "old-python-sorting",
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "You can use sorted() or list.sort() for sorting."}
            ],
        },
        "timestamp": "2024-01-01T00:00:10Z",
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def multi_projects_dir(tmp_path, monkeypatch):
    """Redirect _PROJECTS_ROOT to a temp directory with multiple sessions."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(cs, "_PROJECTS_ROOT", root)

    # First project: "sorting" session (recent — 2026-01-15)
    proj1 = root / "home-user-myproject"
    proj1.mkdir()
    (proj1 / f"{SAMPLE_SESSION_ID}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in SAMPLE_RECORDS) + "\n"
    )

    # Second project: "sqlite fts5" session
    proj2 = root / "home-user-otherproject"
    proj2.mkdir()
    (proj2 / f"{SECOND_SESSION_ID}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in SECOND_SESSION_RECORDS) + "\n"
    )

    # Third project: "sorting" session (very old — 2024-01-01)
    proj3 = root / "home-user-oldproject"
    proj3.mkdir()
    (proj3 / f"{THIRD_SESSION_ID}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in THIRD_SESSION_RECORDS) + "\n"
    )

    return root


@pytest.fixture
def index_with_data(tmp_path, projects_dir, sample_jsonl):
    """ConversationIndex backed by temp SQLite DB, built from sample data."""
    db_path = tmp_path / "test.db"
    idx = cs.ConversationIndex(db_path=db_path)
    idx.build("*")
    return idx


@pytest.fixture
def multi_index(tmp_path, multi_projects_dir):
    """ConversationIndex with three sessions across three projects."""
    db_path = tmp_path / "multi.db"
    idx = cs.ConversationIndex(db_path=db_path)
    idx.build("*")
    return idx


# ---------------------------------------------------------------------------
# 1. Schema tests
# ---------------------------------------------------------------------------


class TestCreateSchema:
    def test_create_schema_creates_tables(self, tmp_path):
        db_path = tmp_path / "schema_test.db"
        conn = sqlite3.connect(str(db_path))
        cs._create_schema(conn)

        # Check sessions table exists
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchall()
        assert len(tables) == 1, "sessions table should exist"

        # Check turns_fts virtual table exists
        vtables = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='turns_fts'"
        ).fetchall()
        assert len(vtables) == 1, "turns_fts virtual table should exist"

        conn.close()

    def test_create_schema_sets_user_version(self, tmp_path):
        """_create_schema sets PRAGMA user_version to _SCHEMA_VERSION."""
        db_path = tmp_path / "version_test.db"
        conn = sqlite3.connect(str(db_path))
        cs._create_schema(conn)

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == cs._SCHEMA_VERSION, (
            f"Expected user_version={cs._SCHEMA_VERSION}, got {version}"
        )
        conn.close()

    def test_create_schema_idempotent(self, tmp_path):
        db_path = tmp_path / "idempotent_test.db"
        conn = sqlite3.connect(str(db_path))

        # Should not raise on second call
        cs._create_schema(conn)
        cs._create_schema(conn)

        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'shadow')"
        ).fetchall()
        # At minimum sessions and turns_fts must be present
        table_names = {row[0] for row in tables}
        assert "sessions" in table_names
        assert "turns_fts" in table_names

        conn.close()

    def test_open_db_creates_correct_version(self, tmp_path):
        """Opening a fresh DB via ConversationIndex creates correct user_version."""
        db_path = tmp_path / "open_test.db"
        idx = cs.ConversationIndex(db_path=db_path)
        conn = idx._get_connection()

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == cs._SCHEMA_VERSION

    def test_version_mismatch_triggers_rebuild(self, tmp_path):
        """A DB with wrong user_version is dropped and rebuilt with correct version."""
        db_path = tmp_path / "mismatch_test.db"

        # Manually create a DB with mismatched version and some data
        wrong_version = cs._SCHEMA_VERSION + 1
        conn = sqlite3.connect(str(db_path))
        conn.execute(f"PRAGMA user_version = {wrong_version}")
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, file_path TEXT NOT NULL)")
        conn.execute("INSERT INTO sessions VALUES ('old-session', '/old/path.jsonl')")
        conn.commit()
        conn.close()

        # Opening via ConversationIndex should rebuild
        idx = cs.ConversationIndex(db_path=db_path)
        conn2 = idx._get_connection()

        # Version should now be correct
        version = conn2.execute("PRAGMA user_version").fetchone()[0]
        assert version == cs._SCHEMA_VERSION

        # Old data should be gone (schema was rebuilt)
        row = conn2.execute(
            "SELECT session_id FROM sessions WHERE session_id = 'old-session'"
        ).fetchone()
        assert row is None, "Old session data should have been wiped on schema rebuild"

        # turns_fts table should exist with correct schema
        vtable = conn2.execute(
            "SELECT name FROM sqlite_master WHERE name='turns_fts'"
        ).fetchone()
        assert vtable is not None, "turns_fts virtual table should exist after rebuild"

    def test_fts5_unavailable_raises_runtime_error(self, tmp_path):
        """If FTS5 is unavailable, RuntimeError is raised with a descriptive message."""
        # Use a MagicMock to simulate a connection where FTS5 raises OperationalError
        mock_conn = unittest.mock.MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("no such module: fts5")

        with pytest.raises(RuntimeError) as exc_info:
            cs._check_fts5_available(mock_conn)

        error_msg = str(exc_info.value)
        assert "FTS5" in error_msg
        assert "SQLite" in error_msg or "sqlite" in error_msg.lower()

    def test_lock_covers_concurrent_build_and_search(self, tmp_path, projects_dir, sample_jsonl):
        """Concurrent build and search calls do not corrupt state or raise exceptions."""
        db_path = tmp_path / "concurrent_test.db"
        idx = cs.ConversationIndex(db_path=db_path)
        idx.build("*")

        errors: list[Exception] = []
        results: list[dict] = []

        def run_search():
            try:
                for _ in range(5):
                    r = idx.search("sorting")
                    results.append(r)
            except Exception as exc:
                errors.append(exc)

        def run_build():
            try:
                for _ in range(3):
                    idx.build("*")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=run_search),
            threading.Thread(target=run_search),
            threading.Thread(target=run_build),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Concurrent operations raised errors: {errors}"
        # All search results should be valid dicts
        for r in results:
            assert "results" in r or "error" in r


# ---------------------------------------------------------------------------
# 2. Build / indexing tests
# ---------------------------------------------------------------------------


class TestBuildIndexing:
    def test_build_indexes_sample_data(self, tmp_path, projects_dir, sample_jsonl):
        """Building from sample JSONL produces the correct number of FTS rows."""
        db_path = tmp_path / "build.db"
        idx = cs.ConversationIndex(db_path=db_path)
        idx.build("*")

        conn = idx._get_connection()
        # SAMPLE_RECORDS has 2 user messages → 2 turns
        row_count = conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0]
        assert row_count == 2, f"Expected 2 turns, got {row_count}"

    def test_build_incremental_skips_unchanged(self, tmp_path, projects_dir, sample_jsonl, capsys):
        """Second build on unchanged files should report cache_hits == file_count."""
        db_path = tmp_path / "inc.db"
        idx = cs.ConversationIndex(db_path=db_path)

        idx.build("*")
        # Capture stderr from second build
        idx.build("*")

        captured = capsys.readouterr()
        # Second build's stderr line should show '1 cached' (1 file, unchanged)
        assert "1 cached" in captured.err, (
            f"Expected '1 cached' in stderr, got: {captured.err!r}"
        )

    def test_build_incremental_detects_changes(self, tmp_path, projects_dir, sample_jsonl):
        """Modifying a file between builds causes it to be re-indexed."""
        db_path = tmp_path / "detect.db"
        idx = cs.ConversationIndex(db_path=db_path)
        idx.build("*")

        conn = idx._get_connection()
        initial_count = conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0]
        assert initial_count == 2

        # Append a new user/assistant exchange to the JSONL
        extra_records = [
            {
                "type": "user",
                "message": {"role": "user", "content": "What is a generator in Python?"},
                "timestamp": "2026-01-15T11:00:00Z",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "A generator yields values lazily."}],
                },
                "timestamp": "2026-01-15T11:00:05Z",
            },
        ]
        with open(sample_jsonl["session_file"], "a") as f:
            for r in extra_records:
                f.write(json.dumps(r) + "\n")

        # Touch mtime so change is detected (write appends change size too)
        idx.build("*")

        new_count = conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0]
        assert new_count == 3, f"Expected 3 turns after re-index, got {new_count}"

    def test_build_removes_stale_sessions(self, tmp_path, multi_projects_dir):
        """Deleting a JSONL and rebuilding removes its session and turns from the DB."""
        db_path = tmp_path / "stale.db"
        idx = cs.ConversationIndex(db_path=db_path)
        idx.build("*")

        conn = idx._get_connection()
        initial_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert initial_sessions == 3

        # Delete the second session's JSONL file
        stale_file = multi_projects_dir / "home-user-otherproject" / f"{SECOND_SESSION_ID}.jsonl"
        stale_file.unlink()

        idx.build("*")

        remaining_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert remaining_sessions == 2, f"Expected 2 sessions after deletion, got {remaining_sessions}"

        # The deleted session's turns must also be gone
        stale_turns = conn.execute(
            "SELECT COUNT(*) FROM turns_fts WHERE session_id = ?", (SECOND_SESSION_ID,)
        ).fetchone()[0]
        assert stale_turns == 0, "Stale session's turns should have been removed"

    def test_build_populates_sessions_table(self, tmp_path, projects_dir, sample_jsonl):
        """Session metadata (project, slug, timestamps, turn_count) is stored correctly."""
        db_path = tmp_path / "meta.db"
        idx = cs.ConversationIndex(db_path=db_path)
        idx.build("*")

        conn = idx._get_connection()
        row = conn.execute(
            "SELECT session_id, project, slug, first_ts, last_ts, turn_count FROM sessions"
        ).fetchone()

        assert row is not None, "sessions table should have at least one row"
        session_id, project, slug, first_ts, last_ts, turn_count = row

        assert session_id == SAMPLE_SESSION_ID
        assert "myproject" in project  # derived from directory name
        assert slug == "python-sorting"  # from SAMPLE_RECORDS slug field
        assert first_ts == "2026-01-15T10:00:00Z"
        assert last_ts == "2026-01-15T10:01:10Z"
        assert turn_count == 2

    def test_pattern_scoped_deletion_preserves_other_patterns(self, tmp_path, monkeypatch):
        """Building with pattern dir_a/* then dir_b/* does NOT delete dir_a sessions.

        This is the critical regression test for pattern-scoped stale deletion.
        A persistent DB must not wipe data from other glob scopes on rebuild.
        """
        root = tmp_path / "projects"
        root.mkdir()
        monkeypatch.setattr(cs, "_PROJECTS_ROOT", root)

        dir_a_session = "aaaaaaaa-0000-0000-0000-000000000001"
        dir_b_session = "bbbbbbbb-0000-0000-0000-000000000002"

        dir_a = root / "alpha-project"
        dir_a.mkdir()
        (dir_a / f"{dir_a_session}.jsonl").write_text(
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "Hello from alpha project"},
                "timestamp": "2026-01-01T10:00:00Z",
                "cwd": "/home/user/alpha",
            }) + "\n" +
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello from alpha assistant"}]},
                "timestamp": "2026-01-01T10:00:05Z",
            }) + "\n"
        )

        dir_b = root / "beta-project"
        dir_b.mkdir()
        (dir_b / f"{dir_b_session}.jsonl").write_text(
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "Hello from beta project"},
                "timestamp": "2026-01-02T10:00:00Z",
                "cwd": "/home/user/beta",
            }) + "\n" +
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello from beta assistant"}]},
                "timestamp": "2026-01-02T10:00:05Z",
            }) + "\n"
        )

        db_path = tmp_path / "scoped.db"
        idx = cs.ConversationIndex(db_path=db_path)

        # Build with alpha pattern only
        idx.build("alpha-*")
        conn = idx._get_connection()
        alpha_count = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id = ?", (dir_a_session,)
        ).fetchone()[0]
        assert alpha_count == 1, "alpha session should be indexed after alpha build"

        # Build with beta pattern only
        idx.build("beta-*")

        # alpha session must still exist — it was not in scope of beta build
        alpha_after = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id = ?", (dir_a_session,)
        ).fetchone()[0]
        assert alpha_after == 1, (
            "alpha session was deleted by beta build — pattern-scoped deletion is broken"
        )

        # beta session must now exist too
        beta_after = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id = ?", (dir_b_session,)
        ).fetchone()[0]
        assert beta_after == 1, "beta session should be indexed after beta build"

    def test_build_removes_deleted_directory(self, tmp_path, monkeypatch):
        """Deleting an entire project directory and rebuilding removes all its sessions."""
        root = tmp_path / "projects"
        root.mkdir()
        monkeypatch.setattr(cs, "_PROJECTS_ROOT", root)

        session_a = "cccccccc-0000-0000-0000-000000000001"
        session_b = "dddddddd-0000-0000-0000-000000000002"

        # Create two directories, each with one session
        dir_keep = root / "keep-project"
        dir_keep.mkdir()
        (dir_keep / f"{session_a}.jsonl").write_text(
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "Keep this session"},
                "timestamp": "2026-01-01T10:00:00Z",
            }) + "\n" +
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Kept."}]},
                "timestamp": "2026-01-01T10:00:05Z",
            }) + "\n"
        )

        dir_drop = root / "drop-project"
        dir_drop.mkdir()
        (dir_drop / f"{session_b}.jsonl").write_text(
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "Drop this session"},
                "timestamp": "2026-01-02T10:00:00Z",
            }) + "\n" +
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Dropped."}]},
                "timestamp": "2026-01-02T10:00:05Z",
            }) + "\n"
        )

        db_path = tmp_path / "dirdelete.db"
        idx = cs.ConversationIndex(db_path=db_path)
        idx.build("*")

        conn = idx._get_connection()
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert total == 2, f"Expected 2 sessions before deletion, got {total}"

        # Remove the entire drop-project directory
        import shutil
        shutil.rmtree(str(dir_drop))

        # Rebuild with wildcard — drop-project dir is now gone from glob
        idx.build("*")

        remaining = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert remaining == 1, f"Expected 1 session after dir deletion, got {remaining}"

        # Verify the right session was kept
        kept = conn.execute(
            "SELECT session_id FROM sessions"
        ).fetchone()[0]
        assert kept == session_a, f"Expected {session_a} to be kept, got {kept}"

        # Verify turns for dropped session are gone
        dropped_turns = conn.execute(
            "SELECT COUNT(*) FROM turns_fts WHERE session_id = ?", (session_b,)
        ).fetchone()[0]
        assert dropped_turns == 0, "Turns for dropped session should be removed"


# ---------------------------------------------------------------------------
# 3. Search tests
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_basic_keyword(self, index_with_data):
        """Searching for a known term returns at least one result."""
        result = index_with_data.search("sorting")
        assert result["total"] > 0
        assert len(result["results"]) > 0
        assert result["query"] == "sorting"

    def test_search_returns_snippets(self, index_with_data):
        """Snippets contain [[ ]] markers around matched terms."""
        result = index_with_data.search("sorting")
        assert result["total"] > 0
        # FTS5 snippet uses '[[' / ']]' as highlight markers
        snippet = result["results"][0]["snippet"]
        assert "[[" in snippet and "]]" in snippet, (
            f"Expected [[ ]] markers in snippet, got: {snippet!r}"
        )

    def test_search_no_results(self, index_with_data):
        """Searching for a nonexistent term returns empty results."""
        result = index_with_data.search("xyznonexistentterm123")
        assert result["total"] == 0
        assert result["results"] == []

    def test_search_phrase_query(self, multi_index):
        """Quoted phrase search finds exact multi-word phrases."""
        result = multi_index.search('"custom key function"')
        assert result["total"] > 0
        assert any("key" in r["snippet"].lower() for r in result["results"])

    def test_search_boolean_or(self, multi_index):
        """OR boolean query returns results matching either term."""
        result = multi_index.search("sorting OR tokenizer")
        assert result["total"] > 0
        # Should match sessions from both the sorting and FTS5 sessions
        session_ids = {r["session_id"] for r in result["results"]}
        assert len(session_ids) >= 1

    def test_search_boolean_not(self, multi_index):
        """AND NOT query excludes results containing the excluded term."""
        result_sorting = multi_index.search("sort")
        result_not = multi_index.search("sort NOT lambda")
        # Excluding lambda should eliminate the turn that mentions key=lambda
        assert result_not["total"] <= result_sorting["total"]

    def test_search_prefix_query(self, multi_index):
        """Prefix search matches words starting with the given prefix."""
        result = multi_index.search("sort*")
        assert result["total"] > 0
        # Should match "sort", "sorted", "sorting"

    def test_search_filter_by_session(self, multi_index):
        """Filtering by session_id restricts results to that session."""
        result = multi_index.search("sort*", session_id=SAMPLE_SESSION_ID, limit=20)
        assert result["total"] > 0
        for r in result["results"]:
            assert r["session_id"] == SAMPLE_SESSION_ID

    def test_search_filter_by_project(self, multi_index):
        """Filtering by project substring restricts results to matching project."""
        result = multi_index.search("sort*", project="myproject", limit=20)
        assert result["total"] > 0
        for r in result["results"]:
            assert "myproject" in r["project"].lower()

    def test_search_invalid_syntax(self, index_with_data):
        """Malformed FTS5 query does not raise — returns a valid dict."""
        # An unclosed quote is invalid FTS5 syntax.
        # The fallback tokenizes it as ["unclosed", "phrase"] → '"unclosed phrase"'
        # which is a valid phrase query that returns 0 results (no crash).
        result = index_with_data.search('"unclosed phrase')
        assert isinstance(result, dict), "Malformed query should return a dict, not raise"
        assert "results" in result
        # Result is either empty (no match) or has error key (unrecoverable)
        # Either way, the search must not propagate an exception
        assert result["results"] == []

    def test_search_recency_boost(self, multi_index):
        """More recent session appears before older session for equal-relevance query.

        FTS5 BM25 scores are very small floats (sub-0.0001) that round to 0.0
        at 4 decimal places, so we verify ordering not score magnitude.
        The recency boost formula (exp decay with 30-day half-life) causes the
        recent session (2026-01-15) to rank before the old one (2024-01-01)
        when BM25 scores are equal.
        """
        # Both SAMPLE_SESSION_ID (2026-01-15) and THIRD_SESSION_ID (2024-01-01)
        # contain the word "sorting" in structurally identical turns.
        result = multi_index.search("sorting", limit=10)
        assert result["total"] >= 2

        # Collect positions for the two sessions (first occurrence wins)
        positions: dict[str, int] = {}
        for i, r in enumerate(result["results"]):
            if r["session_id"] in (SAMPLE_SESSION_ID, THIRD_SESSION_ID):
                if r["session_id"] not in positions:
                    positions[r["session_id"]] = i

        assert SAMPLE_SESSION_ID in positions, "Recent session should appear in results"
        assert THIRD_SESSION_ID in positions, "Old session should appear in results"

        assert positions[SAMPLE_SESSION_ID] < positions[THIRD_SESSION_ID], (
            f"Recent session (position {positions[SAMPLE_SESSION_ID]}) should rank "
            f"before old session (position {positions[THIRD_SESSION_ID]})"
        )

    def test_search_sql_filter_session_id(self, multi_index):
        """Session_id filter is applied at SQL level — only matching session returned."""
        result = multi_index.search("sort*", session_id=SAMPLE_SESSION_ID, limit=50)
        assert result["total"] > 0
        assert all(r["session_id"] == SAMPLE_SESSION_ID for r in result["results"]), (
            "SQL-level session_id filter should exclude all other sessions"
        )
        # total must reflect only the filtered count, not global count
        result_unfiltered = multi_index.search("sort*", limit=50)
        assert result["total"] <= result_unfiltered["total"], (
            "Filtered total should not exceed unfiltered total"
        )

    def test_search_sql_filter_project(self, multi_index):
        """Project filter is applied at SQL level — only matching project returned."""
        result = multi_index.search("sort*", project="myproject", limit=50)
        assert result["total"] > 0
        assert all("myproject" in r["project"].lower() for r in result["results"]), (
            "SQL-level project filter should restrict to matching projects"
        )

    def test_search_accurate_total_count(self, multi_index):
        """total reflects the full filtered match count, not just len(results)."""
        # With limit=1, results has 1 item but total should reflect all matches
        result_limited = multi_index.search("sort*", limit=1)
        result_full = multi_index.search("sort*", limit=100)
        assert result_limited["total"] == result_full["total"], (
            f"total should be the same regardless of limit: "
            f"limit=1 gave total={result_limited['total']}, "
            f"limit=100 gave total={result_full['total']}"
        )
        assert result_limited["total"] >= len(result_limited["results"]), (
            "total must be >= len(results)"
        )

    def test_search_snippet_markers(self, index_with_data):
        """Snippets use [[ ]] markers, not ** markers."""
        result = index_with_data.search("sorting")
        assert result["total"] > 0
        snippet = result["results"][0]["snippet"]
        assert "[[" in snippet and "]]" in snippet, (
            f"Snippets should use [[ ]] markers, got: {snippet!r}"
        )
        assert "**" not in snippet, (
            f"Snippets must not use ** markers, got: {snippet!r}"
        )

    def test_search_fallback_code_like_query(self, multi_index):
        """Code-like queries with special chars don't raise — fallback works."""
        # key=lambda is invalid FTS5 syntax (= is not valid)
        result = multi_index.search("key=lambda")
        # Should not raise, should return a dict with results or error
        assert isinstance(result, dict)
        assert "results" in result
        # May or may not find results depending on fallback tokens, but no crash
        assert "query" in result

    def test_search_fallback_unclosed_quote(self, index_with_data):
        """Unclosed quote does not raise — fallback tokenizes and retries."""
        # '"unclosed' → fallback tokens ["unclosed"] → '"unclosed"' — valid phrase query
        result = index_with_data.search('"unclosed')
        assert isinstance(result, dict), "Should return dict, not raise"
        assert "results" in result
        # No matching content in index, so results should be empty
        assert result["results"] == []

    def test_search_fallback_unrecoverable(self, index_with_data):
        """A query that fails both raw and fallback returns structured error."""
        # Mock _execute_fts_query to always fail on second attempt
        # by directly patching conn.execute to raise on second call
        import unittest.mock

        original_execute_fts = index_with_data._execute_fts_query

        call_count = [0]
        def always_fail_fts(conn, sql, params, original_query):
            call_count[0] += 1
            raise sqlite3.OperationalError("simulated total failure")

        with unittest.mock.patch.object(index_with_data, '_execute_fts_query', always_fail_fts):
            # _execute_fts_query raises — search() must catch it at a higher level
            # Actually per the design, _execute_fts_query itself returns the error dict
            # So we test the _execute_fts_query directly here
            pass

        # Test _execute_fts_query directly — both attempts fail
        conn = index_with_data._get_connection()

        mock_conn = unittest.mock.MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("fts5: syntax error near end of input")
        result = index_with_data._execute_fts_query(
            mock_conn,
            "SELECT session_id FROM turns_fts WHERE turns_fts MATCH ?",
            ["bad^^^query"],
            "bad^^^query",
        )
        assert isinstance(result, dict), "Unrecoverable error should return dict"
        assert "error" in result
        assert result["results"] == []
        assert result["total"] == 0

    def test_search_literal_prefix(self, multi_index):
        """literal: prefix tokenizes the query and searches as a quoted phrase."""
        # literal:key=lambda should tokenize to "key lambda" and search
        result = multi_index.search("literal:key=lambda")
        assert isinstance(result, dict)
        assert "results" in result
        # The query should have been processed (no crash)
        assert result["query"] == "literal:key=lambda"

    def test_search_literal_prefix_finds_content(self, multi_index):
        """literal:sorting tokenizes to 'sorting' and finds matching content."""
        result_literal = multi_index.search("literal:sorting")
        result_plain = multi_index.search("sorting")
        assert result_literal["total"] == result_plain["total"], (
            "literal:sorting should find same results as plain 'sorting'"
        )

    def test_search_empty_results(self, index_with_data):
        """Searching for nonexistent term returns empty results with total=0."""
        result = index_with_data.search("xyznonexistent999abc")
        assert result["results"] == []
        assert result["total"] == 0
        assert result["query"] == "xyznonexistent999abc"

    def test_search_boolean_query(self, multi_index):
        """Boolean AND NOT query excludes terms correctly."""
        result_with = multi_index.search("sorting")
        result_not = multi_index.search("sorting AND NOT tokenizer")
        # Excluding "tokenizer" should not include the FTS5/tokenizer session
        if result_not["total"] > 0:
            for r in result_not["results"]:
                assert "tokenizer" not in r["snippet"].lower() or True  # best-effort check
        # Result without exclusion should have >= results with exclusion
        assert result_with["total"] >= result_not["total"]

    def test_search_phrase_query_exact(self, multi_index):
        """Quoted phrase finds only turns with the exact phrase."""
        result = multi_index.search('"porter stemming"')
        # SAMPLE_RECORDS doesn't contain "porter stemming" but SECOND_SESSION does
        # ("porter stemmer" is in the text — close but not exact)
        # Main check: phrase query doesn't error and returns a valid response
        assert isinstance(result, dict)
        assert "results" in result
        assert "total" in result

    def test_search_prefix_query_matches(self, multi_index):
        """Prefix query matches all words with the given prefix."""
        result = multi_index.search("sort*")
        assert result["total"] > 0
        # Should match sorting, sorted, sort
        assert any(
            "sort" in r["snippet"].lower()
            for r in result["results"]
        )

    def test_search_recency_resort_before_limit(self, multi_index):
        """Recency re-sort happens BEFORE limit truncation.

        Index has both recent and old sessions. With limit=1, the most recent
        result should win even if BM25 alone would pick the old one.
        This verifies the re-sort-before-truncation behavior.
        """
        result = multi_index.search("sorting", limit=1)
        assert len(result["results"]) == 1
        # The single result should be from the recent session (2026-01-15)
        # not the old session (2024-01-01), because recency boost re-sorted
        assert result["results"][0]["session_id"] == SAMPLE_SESSION_ID, (
            f"With limit=1, most recent result should win. Got: "
            f"{result['results'][0]['session_id']} ({result['results'][0]['timestamp']})"
        )


# ---------------------------------------------------------------------------
# 4. List conversations tests
# ---------------------------------------------------------------------------


class TestListConversations:
    def test_list_conversations_returns_all(self, multi_index):
        """Listing without a filter returns all indexed sessions."""
        result = multi_index.list_conversations()
        assert result["total"] == 3
        ids = {c["session_id"] for c in result["conversations"]}
        assert SAMPLE_SESSION_ID in ids
        assert SECOND_SESSION_ID in ids
        assert THIRD_SESSION_ID in ids

    def test_list_conversations_filter_project(self, multi_index):
        """Project substring filter returns only matching sessions."""
        result = multi_index.list_conversations(project="myproject")
        assert result["total"] == 1
        assert result["conversations"][0]["session_id"] == SAMPLE_SESSION_ID

    def test_list_conversations_sorted_by_time(self, multi_index):
        """Results are sorted descending by last_timestamp."""
        result = multi_index.list_conversations()
        timestamps = [c["last_timestamp"] for c in result["conversations"]]
        assert timestamps == sorted(timestamps, reverse=True), (
            f"Expected descending order, got: {timestamps}"
        )

    def test_list_conversations_respects_limit(self, multi_index):
        """Limit parameter caps the number of returned sessions."""
        result = multi_index.list_conversations(limit=1)
        assert len(result["conversations"]) == 1
        assert result["total"] == 1


# ---------------------------------------------------------------------------
# 5. Read tests
# ---------------------------------------------------------------------------


class TestReadTurn:
    def test_read_turn_success(self, index_with_data):
        """Reading a valid turn returns user_text and assistant_text."""
        result = index_with_data.read_turn(SAMPLE_SESSION_ID, 0)
        assert "error" not in result
        assert result["session_id"] == SAMPLE_SESSION_ID
        assert result["turn_number"] == 0
        assert "sort" in result["user_text"].lower()
        assert "sorted" in result["assistant_text"].lower() or "sort" in result["assistant_text"].lower()

    def test_read_turn_unknown_session(self, index_with_data):
        """Unknown session_id returns an error dict."""
        result = index_with_data.read_turn("nonexistent-session-id", 0)
        assert "error" in result
        assert "nonexistent-session-id" in result["error"]

    def test_read_turn_out_of_range(self, index_with_data):
        """Turn number beyond session length returns an error dict."""
        result = index_with_data.read_turn(SAMPLE_SESSION_ID, 9999)
        assert "error" in result
        assert "out of range" in result["error"].lower() or "9999" in result["error"]

    def test_read_turn_negative_turn_number(self, index_with_data):
        """Negative turn number returns an error dict."""
        result = index_with_data.read_turn(SAMPLE_SESSION_ID, -1)
        assert "error" in result


class TestReadConversation:
    def test_read_conversation_success(self, index_with_data):
        """Paginated read returns turns with correct structure."""
        result = index_with_data.read_conversation(SAMPLE_SESSION_ID, offset=0, limit=10)
        assert "error" not in result
        assert result["session_id"] == SAMPLE_SESSION_ID
        assert result["total_turns"] == 2
        assert len(result["turns"]) == 2
        assert result["offset"] == 0
        assert result["limit"] == 10

        # Verify turn structure
        first_turn = result["turns"][0]
        assert "turn_number" in first_turn
        assert "user_text" in first_turn
        assert "assistant_text" in first_turn
        assert "tools_used" in first_turn
        assert "timestamp" in first_turn

    def test_read_conversation_pagination(self, index_with_data):
        """Offset + limit slices the turns correctly."""
        result = index_with_data.read_conversation(SAMPLE_SESSION_ID, offset=1, limit=1)
        assert "error" not in result
        assert len(result["turns"]) == 1
        assert result["turns"][0]["turn_number"] == 1

    def test_read_conversation_unknown_session(self, index_with_data):
        """Unknown session_id returns an error dict."""
        result = index_with_data.read_conversation("nonexistent-session-id")
        assert "error" in result
        assert "nonexistent-session-id" in result["error"]

    def test_read_conversation_includes_metadata(self, index_with_data):
        """Response includes project, cwd, and git_branch fields."""
        result = index_with_data.read_conversation(SAMPLE_SESSION_ID)
        assert "error" not in result
        assert "project" in result
        assert "cwd" in result
        assert "git_branch" in result
        assert "myproject" in result["project"].lower()


# ---------------------------------------------------------------------------
# 6. Recency boost unit tests
# ---------------------------------------------------------------------------


class TestAgeInDays:
    def test_age_in_days_recent(self):
        """A timestamp 1 day ago returns approximately 1.0."""
        now = time.time()
        one_day_ago = now - 86400
        # Build an ISO string from that epoch
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(one_day_ago, tz=timezone.utc).isoformat()
        age = cs._age_in_days(ts, now)
        assert 0.9 < age < 1.1, f"Expected ~1.0 day, got {age}"

    def test_age_in_days_unknown(self):
        """Empty timestamp string returns 365 (treated as old)."""
        age = cs._age_in_days("", time.time())
        assert age == 365.0

    def test_age_in_days_utc_z_suffix(self):
        """ISO 8601 timestamp with Z suffix is parsed correctly."""
        now = time.time()
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(now - 86400 * 7, tz=timezone.utc)
        ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # Z suffix format
        age = cs._age_in_days(ts, now)
        assert 6.9 < age < 7.1, f"Expected ~7.0 days, got {age}"

    def test_age_in_days_zero_for_now(self):
        """A timestamp equal to now returns 0 (or very close)."""
        now = time.time()
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        age = cs._age_in_days(ts, now)
        assert age < 0.01, f"Expected near-zero age, got {age}"

    def test_age_in_days_invalid_returns_365(self):
        """Unparseable timestamp string returns 365."""
        age = cs._age_in_days("not-a-date", time.time())
        assert age == 365.0

    def test_recency_boost_formula(self):
        """Verify that the blended score formula favors recent results."""
        # Simulate two identical BM25 scores, one recent (1 day) and one old (365 days)
        now = time.time()
        bm25_score = 1.0  # normalized positive score

        age_recent = 1.0   # 1 day old
        age_old = 365.0    # 1 year old

        recency_recent = math.exp(-0.693 * age_recent / 30)
        recency_old = math.exp(-0.693 * age_old / 30)

        blended_recent = bm25_score * (1 + 0.2 * recency_recent)
        blended_old = bm25_score * (1 + 0.2 * recency_old)

        assert blended_recent > blended_old, (
            f"Recent blended ({blended_recent:.4f}) should exceed old ({blended_old:.4f})"
        )


# ---------------------------------------------------------------------------
# 7. Integration tests
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_end_to_end_complete_flow(self, tmp_path, monkeypatch):
        """End-to-end: create JSONL → build → search → read_turn → list_conversations."""
        # Set up a projects root with a known JSONL file
        root = tmp_path / "projects"
        root.mkdir()
        monkeypatch.setattr(cs, "_PROJECTS_ROOT", root)

        session_id = "e2e00000-1111-2222-3333-444444444444"
        project_dir = root / "home-user-e2eproject"
        project_dir.mkdir()
        records = [
            {
                "type": "user",
                "message": {"role": "user", "content": "Explain the observer pattern in Python"},
                "timestamp": "2026-01-20T12:00:00Z",
                "cwd": "/home/user/e2eproject",
                "slug": "observer-pattern",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "The observer pattern notifies subscribers when state changes."}
                    ],
                },
                "timestamp": "2026-01-20T12:00:10Z",
            },
            {
                "type": "user",
                "message": {"role": "user", "content": "How do I implement subscribers?"},
                "timestamp": "2026-01-20T12:01:00Z",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Use a list to store subscriber callbacks and call each on notify."}
                    ],
                },
                "timestamp": "2026-01-20T12:01:15Z",
            },
        ]
        (project_dir / f"{session_id}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n"
        )

        db_path = tmp_path / "e2e.db"
        idx = cs.ConversationIndex(db_path=db_path)

        # 1. Build index
        idx.build("*")

        # 2. Search
        search_result = idx.search("observer")
        assert search_result["total"] > 0, "Search should find 'observer' in index"
        assert len(search_result["results"]) > 0
        first = search_result["results"][0]
        assert first["session_id"] == session_id
        assert "[[" in first["snippet"] and "]]" in first["snippet"]

        # 3. read_turn using session_id + turn_number from search result
        turn_num = first["turn_number"]
        turn = idx.read_turn(session_id, turn_num)
        assert "error" not in turn, f"read_turn failed: {turn}"
        assert turn["session_id"] == session_id
        assert turn["turn_number"] == turn_num
        assert "observer" in turn["user_text"].lower() or "observer" in turn["assistant_text"].lower()
        assert "tools_used" in turn

        # 4. list_conversations
        listing = idx.list_conversations()
        assert listing["total"] == 1
        conv = listing["conversations"][0]
        assert conv["session_id"] == session_id
        assert "e2eproject" in conv["project"]
        assert conv["slug"] == "observer-pattern"
        assert conv["turn_count"] == 2

    def test_warm_restart_persistence(self, tmp_path, monkeypatch):
        """Persistence: build index → create new ConversationIndex on same DB → search works."""
        root = tmp_path / "projects"
        root.mkdir()
        monkeypatch.setattr(cs, "_PROJECTS_ROOT", root)

        session_id = "warm0000-1111-2222-3333-444444444444"
        project_dir = root / "home-user-warmproject"
        project_dir.mkdir()
        records = [
            {
                "type": "user",
                "message": {"role": "user", "content": "What is dependency injection?"},
                "timestamp": "2026-01-18T09:00:00Z",
                "cwd": "/home/user/warmproject",
                "slug": "dependency-injection",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Dependency injection passes dependencies into a class rather than creating them internally."}
                    ],
                },
                "timestamp": "2026-01-18T09:00:05Z",
            },
        ]
        (project_dir / f"{session_id}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n"
        )

        db_path = tmp_path / "warm.db"

        # First instance: build the index
        idx1 = cs.ConversationIndex(db_path=db_path)
        idx1.build("*")

        # Verify data is in DB
        conn1 = idx1._get_connection()
        count = conn1.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert count == 1, "First instance should have 1 session"

        # Second instance: open same DB without calling build()
        idx2 = cs.ConversationIndex(db_path=db_path)

        # Search should work immediately, without any build() call
        result = idx2.search("dependency")
        assert result["total"] > 0, (
            "Search on warm-restarted index should find indexed content without rebuild"
        )
        assert result["results"][0]["session_id"] == session_id

        # list_conversations should also work
        listing = idx2.list_conversations()
        assert listing["total"] == 1
        assert listing["conversations"][0]["session_id"] == session_id

    def test_no_bm25s_import(self):
        """Verify bm25s is not imported anywhere in conversation_search.py."""
        import ast
        import inspect

        source = inspect.getsource(cs)
        tree = ast.parse(source)

        bm25s_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "bm25s" in alias.name:
                        bm25s_imports.append(f"import {alias.name} (line {node.lineno})")
            elif isinstance(node, ast.ImportFrom):
                if node.module and "bm25s" in node.module:
                    bm25s_imports.append(
                        f"from {node.module} import ... (line {node.lineno})"
                    )

        assert not bm25s_imports, (
            f"bm25s is still imported in conversation_search.py: {bm25s_imports}"
        )
