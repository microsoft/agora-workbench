"""Tests for DecisionLogEntry."""

import dataclasses

import pytest

from middleware.decision_log.entry import DecisionLogEntry


class TestDecisionLogEntry:
    """Test cases for the immutable DecisionLogEntry dataclass."""

    @pytest.mark.unit
    def test_basic_creation(self):
        """DecisionLogEntry can be created with required fields."""
        entry = DecisionLogEntry(
            timestamp="2026-03-19T18:12:04Z",
            agent="planner",
            summary="Removed step 3 due to missing credentials",
        )
        assert entry.timestamp == "2026-03-19T18:12:04Z"
        assert entry.agent == "planner"
        assert entry.summary == "Removed step 3 due to missing credentials"
        assert entry.evidence == {}

    @pytest.mark.unit
    def test_creation_with_evidence(self):
        """DecisionLogEntry stores evidence dict correctly."""
        evidence = {"tool": "fetch_resource", "error_code": "401"}
        entry = DecisionLogEntry(
            timestamp="2026-03-19T18:12:04Z",
            agent="planner",
            summary="Removed step 3 due to missing credentials",
            evidence=evidence,
        )
        assert entry.evidence == {"tool": "fetch_resource", "error_code": "401"}

    @pytest.mark.unit
    def test_entry_is_frozen(self):
        """DecisionLogEntry fields cannot be reassigned after creation."""
        entry = DecisionLogEntry(
            timestamp="2026-03-19T18:12:04Z",
            agent="planner",
            summary="Some reasoning",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.agent = "executor"  # type: ignore[misc]

    @pytest.mark.unit
    def test_to_dict_without_evidence(self):
        """to_dict returns correct keys with empty evidence."""
        entry = DecisionLogEntry(
            timestamp="2026-03-19T18:12:04Z",
            agent="planner",
            summary="Need clarification",
        )
        result = entry.to_dict()
        assert result == {
            "timestamp": "2026-03-19T18:12:04Z",
            "agent": "planner",
            "summary": "Need clarification",
            "evidence": {},
        }

    @pytest.mark.unit
    def test_to_dict_with_evidence(self):
        """to_dict includes evidence values."""
        entry = DecisionLogEntry(
            timestamp="2026-03-19T18:12:04Z",
            agent="planner",
            summary="Removed step",
            evidence={"tool": "fetch_resource", "error_code": "401"},
        )
        result = entry.to_dict()
        assert result["evidence"] == {"tool": "fetch_resource", "error_code": "401"}

    @pytest.mark.unit
    def test_to_dict_returns_copy_of_evidence(self):
        """to_dict returns a copy of the evidence dict (not the original)."""
        evidence = {"key": "value"}
        entry = DecisionLogEntry(
            timestamp="2026-03-19T18:12:04Z",
            agent="planner",
            summary="Some reasoning",
            evidence=evidence,
        )
        result = entry.to_dict()
        result["evidence"]["extra"] = "added"
        # Original evidence in entry should be unchanged
        assert "extra" not in entry.evidence

    @pytest.mark.unit
    def test_evidence_is_immutable(self):
        """evidence cannot be mutated after creation."""
        entry = DecisionLogEntry(
            timestamp="2026-03-19T18:12:04Z",
            agent="planner",
            summary="Some reasoning",
            evidence={"key": "value"},
        )
        with pytest.raises(TypeError):
            entry.evidence["key"] = "changed"  # type: ignore[index]
        with pytest.raises(TypeError):
            entry.evidence["new"] = "added"  # type: ignore[index]

    @pytest.mark.unit
    def test_default_evidence_is_independent_per_instance(self):
        """Default evidence dict is not shared between instances."""
        entry1 = DecisionLogEntry(timestamp="t1", agent="a", summary="s1")
        entry2 = DecisionLogEntry(timestamp="t2", agent="b", summary="s2")
        # These should be separate dict objects
        assert entry1.evidence is not entry2.evidence
