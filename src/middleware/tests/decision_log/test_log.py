"""Tests for DecisionLog."""

import threading

import pytest

from middleware.decision_log.entry import DecisionLogEntry
from middleware.decision_log.log import DecisionLog


def _make_entry(
    agent: str = "planner",
    summary: str = "Some reasoning",
    timestamp: str = "2026-03-19T18:12:04Z",
) -> DecisionLogEntry:
    return DecisionLogEntry(
        timestamp=timestamp,
        agent=agent,
        summary=summary,
    )


class TestDecisionLog:
    """Test cases for the append-only DecisionLog."""

    @pytest.mark.unit
    def test_empty_on_creation(self):
        """A new DecisionLog starts with no entries."""
        log = DecisionLog()
        assert len(log) == 0
        assert log.entries == ()

    @pytest.mark.unit
    def test_append_adds_entry(self):
        """_append adds an entry and it appears in entries."""
        log = DecisionLog()
        entry = _make_entry()
        log._append(entry)
        assert len(log) == 1
        assert log.entries[0] is entry

    @pytest.mark.unit
    def test_entries_are_ordered_oldest_first(self):
        """entries returns entries in insertion order (oldest first)."""
        log = DecisionLog()
        e1 = _make_entry(summary="first", timestamp="2026-01-01T00:00:00Z")
        e2 = _make_entry(summary="second", timestamp="2026-01-02T00:00:00Z")
        log._append(e1)
        log._append(e2)
        entries = log.entries
        assert entries[0].summary == "first"
        assert entries[1].summary == "second"

    @pytest.mark.unit
    def test_entries_returns_tuple(self):
        """entries property returns a tuple (immutable snapshot)."""
        log = DecisionLog()
        log._append(_make_entry())
        result = log.entries
        assert isinstance(result, tuple)

    @pytest.mark.unit
    def test_entries_snapshot_is_independent(self):
        """Modifying the snapshot tuple does not affect the log."""
        log = DecisionLog()
        log._append(_make_entry(summary="first"))
        snapshot = log.entries
        # Add another entry after taking snapshot
        log._append(_make_entry(summary="second"))
        # The original snapshot should still have only 1 entry
        assert len(snapshot) == 1
        assert len(log) == 2

    @pytest.mark.unit
    def test_iter_returns_entries(self):
        """Iterating the log yields all entries in order."""
        log = DecisionLog()
        e1 = _make_entry(summary="a")
        e2 = _make_entry(summary="b")
        log._append(e1)
        log._append(e2)
        result = list(log)
        assert result == [e1, e2]

    @pytest.mark.unit
    def test_to_context_string_empty(self):
        """to_context_string returns placeholder message when log is empty."""
        log = DecisionLog()
        text = log.to_context_string()
        assert "No decisions recorded yet" in text

    @pytest.mark.unit
    def test_to_context_string_with_entries(self):
        """to_context_string formats entries as human-readable text."""
        log = DecisionLog()
        log._append(
            DecisionLogEntry(
                timestamp="2026-03-19T18:12:04Z",
                agent="planner",
                summary="Removed step 3",
                evidence={"tool": "fetch_resource", "error_code": "401"},
            )
        )
        text = log.to_context_string()
        assert "planner" in text
        assert "Removed step 3" in text
        assert "tool=fetch_resource" in text
        assert "error_code=401" in text

    @pytest.mark.unit
    def test_to_context_string_max_entries(self):
        """to_context_string respects the max_entries limit."""
        log = DecisionLog()
        for i in range(5):
            log._append(_make_entry(summary=f"decision {i}"))
        text = log.to_context_string(max_entries=2)
        # Should show only the 2 most recent entries
        assert "decision 4" in text
        assert "decision 3" in text
        assert "decision 2" not in text
        assert "decision 1" not in text
        assert "decision 0" not in text

    @pytest.mark.unit
    def test_to_context_string_max_entries_zero_shows_all(self):
        """Passing max_entries=0 includes all entries."""
        log = DecisionLog()
        for i in range(5):
            log._append(_make_entry(summary=f"decision {i}"))
        text = log.to_context_string(max_entries=0)
        for i in range(5):
            assert f"decision {i}" in text

    @pytest.mark.unit
    def test_to_context_string_no_evidence_no_evidence_line(self):
        """to_context_string omits the Evidence line when evidence is empty."""
        log = DecisionLog()
        log._append(_make_entry(summary="simple decision"))
        text = log.to_context_string()
        assert "Evidence:" not in text

    @pytest.mark.unit
    def test_thread_safety(self):
        """Concurrent _append calls from multiple threads are all recorded."""
        log = DecisionLog()
        n_threads = 20
        n_per_thread = 50

        def append_many():
            for _ in range(n_per_thread):
                log._append(_make_entry())

        threads = [threading.Thread(target=append_many) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(log) == n_threads * n_per_thread
