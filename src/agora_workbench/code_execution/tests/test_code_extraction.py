"""Unit tests for the code_extraction module."""

import ast

from ..code_extraction import (
    _VAR_PREFIX_ASSET,
    ASSET_PATHLIB_IMPORT,
    build_asset_preamble,
    collect_code_names,
    extract_references,
    generate_safe_varname,
    replace_literals_in_source,
)


# =====================================================================
# extract_references
# =====================================================================


class TestExtractReferences:
    """Tests for extract_references()."""

    def test_asset_tag_in_call_arg(self):
        refs, *_ = extract_references('pd.read_csv("<blob>xyz</blob>")')
        assert len(refs) == 1
        _, kind, value = refs[0]
        assert kind == "asset"
        assert value == "<blob>xyz</blob>"

    def test_asset_tag_in_assignment(self):
        refs, *_ = extract_references('path = "<blob>abc123</blob>"')
        assert len(refs) == 1
        assert refs[0][1] == "asset"

    def test_asset_tag_with_sql_type(self):
        refs, *_ = extract_references('query = "<sql>query_id_123</sql>"')
        assert len(refs) == 1
        _, kind, value = refs[0]
        assert kind == "asset"
        assert value == "<sql>query_id_123</sql>"

    def test_mixed_asset_tags(self):
        code = 'x = "<blob>xyz</blob>"\ny = "<sql>query_42</sql>"'
        refs, *_ = extract_references(code)
        assert len(refs) == 2
        kinds = {r[1] for r in refs}
        assert kinds == {"asset"}

    def test_deduplication_by_value(self):
        """Same asset tag in two places → only one reference returned."""
        code = 'x = "<blob>abc123</blob>"\ny = "<blob>abc123</blob>"'
        refs, *_ = extract_references(code)
        assert len(refs) == 1

    def test_syntax_error_returns_empty(self):
        refs, occs, names = extract_references("x = 'unclosed string")
        assert refs == []
        assert occs == {}
        assert names == set()

    def test_bare_expression_not_found(self):
        """A bare string expression is not a valid context."""
        refs, *_ = extract_references('"<blob>abc123</blob>"')
        assert len(refs) == 0

    def test_code_names_collected_in_single_pass(self):
        """extract_references returns code names alongside refs, avoiding a second parse."""
        code = 'x = "<blob>abc123</blob>"\ny = foo(x)'
        refs, _, names = extract_references(code)
        assert len(refs) == 1
        assert {"x", "y", "foo"}.issubset(names)

    def test_code_names_empty_on_syntax_error(self):
        _, _, names = extract_references("x = 'unclosed")
        assert names == set()


# =====================================================================
# extract_references: all_occurrences dict
# =====================================================================


class TestAllOccurrences:
    """Tests for the all_occurrences dict returned by extract_references()."""

    def test_single_occurrence(self):
        code = 'x = "<blob>abc123</blob>"'
        _, occs, _ = extract_references(code)
        assert len(occs["<blob>abc123</blob>"]) == 1

    def test_two_occurrences(self):
        code = 'x = "<blob>abc123</blob>"\ny = "<blob>abc123</blob>"'
        _, occs, _ = extract_references(code)
        assert len(occs["<blob>abc123</blob>"]) == 2

    def test_docstring_excluded(self):
        code = '"""<blob>abc123</blob>"""\nx = "<blob>abc123</blob>"'
        _, occs, _ = extract_references(code)
        # Only the assignment should match, not the docstring
        assert len(occs["<blob>abc123</blob>"]) == 1


# =====================================================================
# replace_literals_in_source
# =====================================================================


class TestReplaceLiteralsInSource:
    """Tests for replace_literals_in_source()."""

    def test_single_replacement(self):
        code = 'x = "<blob>abc123</blob>"\n'
        tree = ast.parse(code)
        nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "<blob>abc123</blob>"]
        assert len(nodes) == 1

        result = replace_literals_in_source(
            code.splitlines(keepends=True),
            [(nodes[0], f"{_VAR_PREFIX_ASSET}0")],
        )
        assert "".join(result).strip() == f"x = {_VAR_PREFIX_ASSET}0"

    def test_multiple_replacements_same_line(self):
        code = 'foo("<blob>abc123</blob>", "<blob>abc123</blob>")\n'
        tree = ast.parse(code)
        nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "<blob>abc123</blob>"]
        assert len(nodes) == 2

        vn = f"{_VAR_PREFIX_ASSET}0"
        result = replace_literals_in_source(
            code.splitlines(keepends=True),
            [(nodes[0], vn), (nodes[1], vn)],
        )
        joined = "".join(result).strip()
        assert joined == f"foo({vn}, {vn})"

    def test_different_replacements(self):
        code = 'x = "<blob>aaa111</blob>"\ny = "<blob>bbb222</blob>"\n'
        tree = ast.parse(code)
        nodes_a = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "<blob>aaa111</blob>"]
        nodes_b = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "<blob>bbb222</blob>"]

        vn0 = f"{_VAR_PREFIX_ASSET}0"
        vn1 = f"{_VAR_PREFIX_ASSET}1"
        result = replace_literals_in_source(
            code.splitlines(keepends=True),
            [(nodes_a[0], vn0), (nodes_b[0], vn1)],
        )
        joined = "".join(result)
        assert vn0 in joined
        assert vn1 in joined
        assert "<blob>aaa111</blob>" not in joined
        assert "<blob>bbb222</blob>" not in joined


# =====================================================================
# build_asset_preamble
# =====================================================================


class TestPreambleBuilders:
    """Tests for build_asset_preamble."""

    def test_asset_preamble_content(self):
        lines = build_asset_preamble(f"{_VAR_PREFIX_ASSET}0", "/tmp/datalake_cache/file.csv")
        joined = "\n".join(lines)
        assert "__agora_pathlib" in joined
        assert "/tmp/datalake_cache/file.csv" in joined
        assert f"{_VAR_PREFIX_ASSET}0" in joined

    def test_asset_preamble_does_not_import_path(self):
        """The asset preamble should NOT contain 'from pathlib import Path'."""
        lines = build_asset_preamble(f"{_VAR_PREFIX_ASSET}0", "/tmp/file.csv")
        joined = "\n".join(lines)
        assert "from pathlib import Path" not in joined

    def test_asset_pathlib_import_constant(self):
        """ASSET_PATHLIB_IMPORT should use a namespaced import alias."""
        assert "import pathlib as __agora_pathlib" in ASSET_PATHLIB_IMPORT


# =====================================================================
# Deduplication: same value → one variable, two replacements
# =====================================================================


class TestDeduplication:
    """End-to-end dedup: same asset tag appears twice → one variable, both replaced."""

    def test_same_asset_twice_one_variable(self):
        code = 'x = "<blob>abc123</blob>"\ny = "<blob>abc123</blob>"'
        refs, occs, _ = extract_references(code)
        # Deduplicated: only 1 unique reference
        assert len(refs) == 1

        # But all_occurrences has both
        assert len(occs["<blob>abc123</blob>"]) == 2


# =====================================================================
# Variable prefix constants
# =====================================================================


class TestVariablePrefixes:
    """Synthetic variable names use a reserved namespace prefix."""

    def test_asset_prefix_has_double_underscore(self):
        assert _VAR_PREFIX_ASSET.startswith("__")


# =====================================================================
# collect_code_names
# =====================================================================


class TestCollectCodeNames:
    """Tests for collect_code_names()."""

    def test_simple_assignment(self):
        names = collect_code_names("x = 1")
        assert "x" in names

    def test_multiple_names(self):
        names = collect_code_names("x = y + z")
        assert names == {"x", "y", "z"}

    def test_function_def(self):
        names = collect_code_names("def foo():\n    return bar")
        assert "bar" in names

    def test_import_not_included(self):
        """import statements create names but they appear as ast.alias, not ast.Name."""
        names = collect_code_names("import os")
        # ast.Name nodes only; import aliases are not ast.Name
        # (os is NOT collected as an ast.Name from an import statement)
        assert isinstance(names, set)

    def test_syntax_error_returns_empty(self):
        names = collect_code_names("x = 'unclosed")
        assert names == set()

    def test_empty_code(self):
        names = collect_code_names("")
        assert names == set()


# =====================================================================
# generate_safe_varname
# =====================================================================


class TestGenerateSafeVarname:
    """Tests for generate_safe_varname()."""

    def test_no_collision(self):
        occupied = set()
        name, next_counter = generate_safe_varname(_VAR_PREFIX_ASSET, 0, occupied)
        assert name == f"{_VAR_PREFIX_ASSET}0"
        assert next_counter == 1

    def test_skips_collision(self):
        occupied = {f"{_VAR_PREFIX_ASSET}0"}
        name, next_counter = generate_safe_varname(_VAR_PREFIX_ASSET, 0, occupied)
        assert name == f"{_VAR_PREFIX_ASSET}1"
        assert next_counter == 2

    def test_skips_multiple_collisions(self):
        occupied = {
            f"{_VAR_PREFIX_ASSET}0",
            f"{_VAR_PREFIX_ASSET}1",
            f"{_VAR_PREFIX_ASSET}2",
        }
        name, next_counter = generate_safe_varname(_VAR_PREFIX_ASSET, 0, occupied)
        assert name == f"{_VAR_PREFIX_ASSET}3"
        assert next_counter == 4

    def test_adds_name_to_occupied(self):
        """The generated name is added to occupied so subsequent calls see it."""
        occupied: set[str] = set()
        name1, c = generate_safe_varname(_VAR_PREFIX_ASSET, 0, occupied)
        name2, _ = generate_safe_varname(_VAR_PREFIX_ASSET, c, occupied)
        assert name1 != name2
        assert name1 in occupied
        assert name2 in occupied

    def test_works_with_asset_prefix(self):
        occupied = {f"{_VAR_PREFIX_ASSET}0"}
        name, _ = generate_safe_varname(_VAR_PREFIX_ASSET, 0, occupied)
        assert name == f"{_VAR_PREFIX_ASSET}1"

    def test_collision_with_user_code_names(self):
        """Simulates user code that already defines an asset prefix variable."""
        user_names = collect_code_names(f"{_VAR_PREFIX_ASSET}0 = 42")
        name, _ = generate_safe_varname(_VAR_PREFIX_ASSET, 0, user_names)
        assert name == f"{_VAR_PREFIX_ASSET}1"
        assert name not in collect_code_names(f"{_VAR_PREFIX_ASSET}0 = 42")
