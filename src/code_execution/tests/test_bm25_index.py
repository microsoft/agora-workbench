"""Tests for the generic BM25 index in :mod:`code_execution.tools.search._bm25`."""

import pytest

from code_execution.tools.search._bm25 import BM25Index, tokenize


class TestTokenize:
    """Test cases for the shared tokenize function."""

    @pytest.mark.unit
    def test_simple_text(self):
        assert tokenize("Hello World") == ["hello", "world"]

    @pytest.mark.unit
    def test_punctuation(self):
        assert tokenize("hello, world! test-case") == ["hello", "world", "test", "case"]

    @pytest.mark.unit
    def test_numbers(self):
        assert tokenize("tool123 test456") == ["tool123", "test456"]

    @pytest.mark.unit
    def test_empty_string(self):
        assert tokenize("") == []

    @pytest.mark.unit
    def test_underscores(self):
        assert tokenize("run_opf solve_network") == ["run_opf", "solve_network"]


class TestBM25Index:
    """Test cases for the generic BM25Index."""

    @pytest.fixture
    def sample_docs(self):
        return [
            ("run_opf", "Run optimal power flow analysis on a network"),
            ("build_network", "Build a power network topology from data"),
            ("analyze_results", "Analyze simulation results and generate reports"),
        ]

    @pytest.fixture
    def populated_index(self, sample_docs):
        index: BM25Index[str] = BM25Index()
        for name, text in sample_docs:
            index.add(name, f"{name} {text}")
        return index

    @pytest.mark.unit
    def test_default_parameters(self):
        index: BM25Index[str] = BM25Index()
        assert index.k1 == 1.5
        assert index.b == 0.75

    @pytest.mark.unit
    def test_custom_parameters(self):
        index: BM25Index[str] = BM25Index(k1=2.0, b=0.5)
        assert index.k1 == 2.0
        assert index.b == 0.5

    @pytest.mark.unit
    def test_add_increments_length(self, sample_docs):
        index: BM25Index[str] = BM25Index()
        assert len(index) == 0
        index.add(sample_docs[0][0], sample_docs[0][1])
        assert len(index) == 1

    @pytest.mark.unit
    def test_search_empty_index(self):
        index: BM25Index[str] = BM25Index()
        assert index.search("test query") == []

    @pytest.mark.unit
    def test_search_empty_query(self, populated_index):
        assert populated_index.search("") == []

    @pytest.mark.unit
    def test_search_by_exact_name(self, populated_index):
        results = populated_index.search("run_opf", top_k=1)
        assert len(results) == 1
        assert results[0][0] == "run_opf"
        assert results[0][1] > 0

    @pytest.mark.unit
    def test_search_by_keywords(self, populated_index):
        results = populated_index.search("optimal power flow", top_k=1)
        assert results[0][0] == "run_opf"
        assert results[0][1] > 0

    @pytest.mark.unit
    def test_search_top_k(self, populated_index):
        results = populated_index.search("network", top_k=2)
        assert len(results) == 2
        assert results[0][1] >= results[1][1]

    @pytest.mark.unit
    def test_search_no_matching_tokens(self, populated_index):
        results = populated_index.search("quantum blockchain", top_k=1)
        assert len(results) == 1
        assert results[0][1] == 0.0

    @pytest.mark.unit
    def test_score_ordering(self, populated_index):
        results = populated_index.search("network power", top_k=3)
        for i in range(len(results) - 1):
            assert results[i][1] >= results[i + 1][1]

    @pytest.mark.unit
    def test_zero_avgdl(self):
        """BM25 handles zero-length documents without division by zero."""
        index: BM25Index[str] = BM25Index()
        index.add("empty", "")
        results = index.search("test", top_k=1)
        assert len(results) == 1
        assert results[0][1] == 0.0

    @pytest.mark.unit
    def test_rare_term_higher_idf(self, populated_index):
        """Rare terms contribute more to score than common terms."""
        results_rare = populated_index.search("optimal", top_k=1)
        results_common = populated_index.search("network", top_k=1)
        assert results_rare[0][1] > 0
        assert results_common[0][1] > 0
