"""Tests for BM25-based tool search."""

import pytest

from tools.search.bm25_tool_search import (
    _tokenize,
    BM25Index,
)
from tools.search.build_tool_list import ToolInfo


class TestTokenize:
    """Test cases for _tokenize function."""

    @pytest.mark.unit
    def test_tokenize_simple_text(self):
        """Test tokenization of simple text."""
        tokens = _tokenize("Hello World")
        assert tokens == ["hello", "world"]

    @pytest.mark.unit
    def test_tokenize_with_punctuation(self):
        """Test tokenization removes punctuation."""
        tokens = _tokenize("hello, world! test-case")
        assert tokens == ["hello", "world", "test", "case"]

    @pytest.mark.unit
    def test_tokenize_with_numbers(self):
        """Test tokenization preserves numbers."""
        tokens = _tokenize("tool123 test456")
        assert tokens == ["tool123", "test456"]

    @pytest.mark.unit
    def test_tokenize_empty_string(self):
        """Test tokenization of empty string."""
        tokens = _tokenize("")
        assert tokens == []

    @pytest.mark.unit
    def test_tokenize_underscores(self):
        """Test tokenization preserves underscores."""
        tokens = _tokenize("run_opf solve_network")
        assert tokens == ["run_opf", "solve_network"]


class TestBM25Index:
    """Test cases for BM25Index class."""

    @pytest.fixture
    def sample_tools(self):
        """Create sample tool info objects for testing."""
        tools = [
            ToolInfo(
                name="run_opf",
                description="Run optimal power flow analysis on a network",
                server_name="powergrid",
            ),
            ToolInfo(
                name="build_network",
                description="Build a power network topology from data",
                server_name="powergrid",
            ),
            ToolInfo(
                name="analyze_results",
                description="Analyze simulation results and generate reports",
                server_name="powergrid",
            ),
        ]
        return tools

    @pytest.fixture
    def empty_tool(self):
        """Create a tool with empty name and description."""
        return ToolInfo(
            name="",
            description="",
            server_name="test",
        )

    @pytest.mark.unit
    def test_bm25_initialization(self):
        """Test BM25Index initialization."""
        index = BM25Index()
        assert index.k1 == 1.5
        assert index.b == 0.75
        assert index._docs == []
        assert index._df == {}
        assert index._avgdl == 0.0

    @pytest.mark.unit
    def test_bm25_custom_parameters(self):
        """Test BM25Index with custom k1 and b parameters."""
        index = BM25Index(k1=2.0, b=0.5)
        assert index.k1 == 2.0
        assert index.b == 0.5

    @pytest.mark.unit
    def test_bm25_add_single_tool(self, sample_tools):
        """Test adding a single tool to the index."""
        index = BM25Index()
        index.add(sample_tools[0])

        assert len(index._docs) == 1
        assert index._avgdl > 0
        # Check that document frequency is updated
        assert len(index._df) > 0

    @pytest.mark.unit
    def test_bm25_add_multiple_tools(self, sample_tools):
        """Test adding multiple tools to the index."""
        index = BM25Index()
        for tool in sample_tools:
            index.add(tool)

        assert len(index._docs) == 3
        assert index._avgdl > 0

    @pytest.mark.unit
    def test_bm25_add_empty_tool(self, empty_tool):
        """Test adding a tool with empty name and description."""
        index = BM25Index()
        index.add(empty_tool)

        assert len(index._docs) == 1
        # avgdl should be 0 for empty document
        assert index._avgdl == 0.0

    @pytest.mark.unit
    def test_bm25_search_empty_index(self):
        """Test searching an empty index returns empty results."""
        index = BM25Index()
        results = index.search("test query")
        assert results == []

    @pytest.mark.unit
    def test_bm25_search_empty_query(self, sample_tools):
        """Test searching with empty query returns empty results."""
        index = BM25Index()
        for tool in sample_tools:
            index.add(tool)

        results = index.search("")
        assert results == []

    @pytest.mark.unit
    def test_bm25_search_by_exact_name(self, sample_tools):
        """Test BM25 search finds tool by exact name."""
        index = BM25Index()
        for tool in sample_tools:
            index.add(tool)

        results = index.search("run_opf", top_k=1)
        assert len(results) == 1
        assert results[0][0].name == "run_opf"
        assert results[0][1] > 0  # Should have positive score

    @pytest.mark.unit
    def test_bm25_search_by_description_keywords(self, sample_tools):
        """Test BM25 search finds tool by description keywords."""
        index = BM25Index()
        for tool in sample_tools:
            index.add(tool)

        results = index.search("optimal power flow", top_k=1)
        assert len(results) == 1
        assert results[0][0].name == "run_opf"
        assert results[0][1] > 0

    @pytest.mark.unit
    def test_bm25_search_top_k(self, sample_tools):
        """Test BM25 search respects top_k parameter."""
        index = BM25Index()
        for tool in sample_tools:
            index.add(tool)

        # Search for "network" which appears in multiple tools
        results = index.search("network", top_k=2)
        assert len(results) == 2
        # Results should be sorted by score descending
        assert results[0][1] >= results[1][1]

    @pytest.mark.unit
    def test_bm25_search_no_matches(self, sample_tools):
        """Test BM25 search with query that has no token matches."""
        index = BM25Index()
        for tool in sample_tools:
            index.add(tool)

        results = index.search("quantum blockchain cryptocurrency", top_k=1)
        # Should return results but with score of 0
        assert len(results) == 1
        assert results[0][1] == 0.0

    @pytest.mark.unit
    def test_bm25_search_score_ordering(self, sample_tools):
        """Test BM25 search returns results in descending score order."""
        index = BM25Index()
        for tool in sample_tools:
            index.add(tool)

        results = index.search("network power", top_k=3)
        assert len(results) == 3
        # Verify descending order
        for i in range(len(results) - 1):
            assert results[i][1] >= results[i + 1][1]

    @pytest.mark.unit
    def test_bm25_zero_avgdl_edge_case(self, empty_tool):
        """Test BM25 handles zero avgdl edge case without division by zero."""
        index = BM25Index()
        index.add(empty_tool)

        # Should not raise ZeroDivisionError
        results = index.search("test", top_k=1)
        # Query won't match empty document
        assert len(results) == 1
        assert results[0][1] == 0.0

    @pytest.mark.unit
    def test_bm25_idf_calculation(self, sample_tools):
        """Test BM25 IDF calculation for common vs rare terms."""
        index = BM25Index()
        for tool in sample_tools:
            index.add(tool)

        # "network" appears in 2 tools, "optimal" appears in 1 tool
        # "optimal" should have higher IDF and thus contribute more to score
        results_optimal = index.search("optimal", top_k=1)
        results_network = index.search("network", top_k=1)

        # Both should return results with positive scores
        assert results_optimal[0][1] > 0
        assert results_network[0][1] > 0
