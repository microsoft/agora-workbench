"""
Unit tests for the tool-learning memory module (middleware.tool_learning).

Tests cover:
  - Model validation (kind payload constraints, scoping)
  - Deterministic ID generation (dedupe)
  - Deterministic renderer snapshot tests
  - Scope filter logic (search_repo)
  - Vignette compiler
"""

from __future__ import annotations

import pytest

from middleware.tool_learning.models import (
    AntiPattern,
    MatchSpec,
    RepairStrategy,
    ToolSignature,
    Vignette,
    compute_vignette_id,
)
from middleware.tool_learning.render import (
    render_anti_pattern,
    render_repair_template,
    render_guardrails_block,
    render_repair_block,
)
from middleware.tool_learning.search_repo import _build_scope_filter
from middleware.tool_learning.compile import compile_vignettes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def anti_pattern_vignette() -> Vignette:
    return Vignette(
        vignette_id="ap-001",
        kind="anti_pattern",
        scope="user",
        tenant_id="tenant-1",
        user_id="user-1",
        tool=ToolSignature(tool_name="excel.add_column"),
        match=MatchSpec(error_class="FormulaError", arg_keys=["formula"]),
        title="Avoid column-label SUM ranges",
        summary="Using SUM(A:B) triggers FormulaError; prefer explicit ranges.",
        anti_pattern=AntiPattern(
            rule="Avoid SUM(A:B) (column labels). Prefer explicit ranges like A1:B10.",
            severity="soft",
        ),
    )


@pytest.fixture
def hard_anti_pattern_vignette() -> Vignette:
    return Vignette(
        vignette_id="ap-002",
        kind="anti_pattern",
        scope="user",
        tenant_id="tenant-1",
        user_id="user-1",
        tool=ToolSignature(tool_name="calendar.create_event"),
        match=MatchSpec(arg_keys=["timezone"]),
        title="Timezone required",
        summary="Omitting timezone causes silent failures.",
        anti_pattern=AntiPattern(
            rule="Do not omit 'timezone' for calendar.create_event.",
            severity="hard",
        ),
    )


@pytest.fixture
def repair_vignette() -> Vignette:
    return Vignette(
        vignette_id="rt-001",
        kind="repair_template",
        scope="user",
        tenant_id="tenant-1",
        user_id="user-1",
        tool=ToolSignature(tool_name="http.request"),
        match=MatchSpec(error_class="AuthenticationError"),
        title="Repair 401 Unauthorized",
        summary="Repair playbook for http.request after AuthenticationError.",
        repair=RepairStrategy(
            steps=[
                "Verify Authorization header is present.",
                "Refresh token via managed identity.",
                "Retry once.",
            ],
            max_retries=1,
        ),
    )


# ---------------------------------------------------------------------------
# Model validation tests
# ---------------------------------------------------------------------------


class TestVignetteModelValidation:
    """Test Vignette model validation rules."""

    @pytest.mark.unit
    def test_anti_pattern_vignette_requires_payload(self):
        """anti_pattern kind must have anti_pattern payload."""
        with pytest.raises(ValueError, match="anti_pattern vignette requires anti_pattern payload"):
            Vignette(
                vignette_id="test",
                kind="anti_pattern",
                scope="user",
                tenant_id="t1",
                user_id="u1",
                tool=ToolSignature(tool_name="my_tool"),
                match=MatchSpec(),
                title="Test",
                summary="Test summary",
                # anti_pattern is intentionally missing
            )

    @pytest.mark.unit
    def test_repair_template_vignette_requires_payload(self):
        """repair_template kind must have repair payload."""
        with pytest.raises(ValueError, match="repair_template vignette requires repair payload"):
            Vignette(
                vignette_id="test",
                kind="repair_template",
                scope="user",
                tenant_id="t1",
                user_id="u1",
                tool=ToolSignature(tool_name="my_tool"),
                match=MatchSpec(),
                title="Test",
                summary="Test summary",
                # repair is intentionally missing
            )

    @pytest.mark.unit
    def test_user_scope_requires_user_id(self):
        """user scope requires both user_id and tenant_id."""
        with pytest.raises(ValueError, match="user scope requires both user_id and tenant_id"):
            Vignette(
                vignette_id="test",
                kind="anti_pattern",
                scope="user",
                # user_id and tenant_id intentionally missing
                tool=ToolSignature(tool_name="my_tool"),
                match=MatchSpec(),
                title="Test",
                summary="Test",
                anti_pattern=AntiPattern(rule="Avoid X; prefer Y."),
            )

    @pytest.mark.unit
    def test_org_scope_requires_tenant_id(self):
        """org scope requires tenant_id."""
        with pytest.raises(ValueError, match="org scope requires tenant_id"):
            Vignette(
                vignette_id="test",
                kind="anti_pattern",
                scope="org",
                # tenant_id intentionally missing
                tool=ToolSignature(tool_name="my_tool"),
                match=MatchSpec(),
                title="Test",
                summary="Test",
                anti_pattern=AntiPattern(rule="Avoid X; prefer Y."),
            )

    @pytest.mark.unit
    def test_global_scope_no_constraints(self):
        """global scope requires neither tenant_id nor user_id."""
        v = Vignette(
            vignette_id="test",
            kind="anti_pattern",
            scope="global",
            tool=ToolSignature(tool_name="my_tool"),
            match=MatchSpec(),
            title="Test",
            summary="Test",
            anti_pattern=AntiPattern(rule="Avoid X; prefer Y."),
        )
        assert v.scope == "global"

    @pytest.mark.unit
    def test_org_scope_with_tenant_id(self):
        """org scope with tenant_id is valid."""
        v = Vignette(
            vignette_id="test",
            kind="anti_pattern",
            scope="org",
            tenant_id="tenant-1",
            tool=ToolSignature(tool_name="my_tool"),
            match=MatchSpec(),
            title="Test",
            summary="Test",
            anti_pattern=AntiPattern(rule="Avoid X; prefer Y."),
        )
        assert v.scope == "org"
        assert v.tenant_id == "tenant-1"

    @pytest.mark.unit
    def test_vignette_default_confidence(self):
        """Vignette has a sensible default confidence."""
        v = Vignette(
            vignette_id="test",
            kind="anti_pattern",
            scope="global",
            tool=ToolSignature(tool_name="my_tool"),
            match=MatchSpec(),
            title="Test",
            summary="Test",
            anti_pattern=AntiPattern(rule="Avoid X; prefer Y."),
        )
        assert v.confidence == 0.70

    @pytest.mark.unit
    def test_vignette_round_trip_serialization(self, anti_pattern_vignette):
        """Vignette should round-trip through JSON serialization."""
        json_str = anti_pattern_vignette.model_dump_json()
        restored = Vignette.model_validate_json(json_str)
        assert restored.vignette_id == anti_pattern_vignette.vignette_id
        assert restored.kind == anti_pattern_vignette.kind
        assert restored.anti_pattern is not None
        assert restored.anti_pattern.rule == anti_pattern_vignette.anti_pattern.rule


# ---------------------------------------------------------------------------
# Deterministic ID tests
# ---------------------------------------------------------------------------


class TestComputeVignetteId:
    """Test deterministic vignette ID generation."""

    @pytest.mark.unit
    def test_same_inputs_same_id(self):
        """Same inputs always produce the same ID."""
        id1 = compute_vignette_id(
            tool_name="my_tool",
            kind="anti_pattern",
            error_class="ValueError",
            rule_or_steps="Avoid X; prefer Y.",
            arg_keys=["param_a", "param_b"],
        )
        id2 = compute_vignette_id(
            tool_name="my_tool",
            kind="anti_pattern",
            error_class="ValueError",
            rule_or_steps="Avoid X; prefer Y.",
            arg_keys=["param_a", "param_b"],
        )
        assert id1 == id2

    @pytest.mark.unit
    def test_different_tool_name_different_id(self):
        """Different tool names produce different IDs."""
        id1 = compute_vignette_id("tool_a", "anti_pattern", "ValueError", "rule", ["k"])
        id2 = compute_vignette_id("tool_b", "anti_pattern", "ValueError", "rule", ["k"])
        assert id1 != id2

    @pytest.mark.unit
    def test_different_kind_different_id(self):
        """Different kind produces different ID."""
        id1 = compute_vignette_id("tool", "anti_pattern", "ValueError", "rule", [])
        id2 = compute_vignette_id("tool", "repair_template", "ValueError", "rule", [])
        assert id1 != id2

    @pytest.mark.unit
    def test_arg_keys_order_independent(self):
        """arg_keys are sorted, so order doesn't affect the ID."""
        id1 = compute_vignette_id("tool", "anti_pattern", None, "rule", ["b", "a"])
        id2 = compute_vignette_id("tool", "anti_pattern", None, "rule", ["a", "b"])
        assert id1 == id2

    @pytest.mark.unit
    def test_none_error_class_consistent(self):
        """None error_class produces consistent IDs."""
        id1 = compute_vignette_id("tool", "anti_pattern", None, "rule", [])
        id2 = compute_vignette_id("tool", "anti_pattern", None, "rule", [])
        assert id1 == id2

    @pytest.mark.unit
    def test_id_is_64_hex_chars(self):
        """ID is a valid 64-character hex string (SHA-256)."""
        vid = compute_vignette_id("tool", "anti_pattern", None, "rule", [])
        assert len(vid) == 64
        assert all(c in "0123456789abcdef" for c in vid)


# ---------------------------------------------------------------------------
# Renderer tests
# ---------------------------------------------------------------------------


class TestRenderer:
    """Test deterministic renderer functions."""

    @pytest.mark.unit
    def test_render_soft_anti_pattern(self, anti_pattern_vignette):
        """Soft anti-pattern renders without HARD prefix."""
        result = render_anti_pattern(anti_pattern_vignette)
        assert result.startswith("- ")
        assert "HARD" not in result
        assert "SUM(A:B)" in result

    @pytest.mark.unit
    def test_render_hard_anti_pattern(self, hard_anti_pattern_vignette):
        """Hard anti-pattern renders with HARD prefix."""
        result = render_anti_pattern(hard_anti_pattern_vignette)
        assert result.startswith("- HARD: ")
        assert "timezone" in result

    @pytest.mark.unit
    def test_render_repair_template(self, repair_vignette):
        """Repair template renders header and numbered steps."""
        result = render_repair_template(repair_vignette)
        assert "[Repair Playbook: http.request | AuthenticationError]" in result
        assert "Step 1:" in result
        assert "Step 2:" in result
        assert "Step 3:" in result
        assert "Verify Authorization header" in result

    @pytest.mark.unit
    def test_render_guardrails_block_empty(self):
        """Empty vignette list produces empty string."""
        assert render_guardrails_block([]) == ""

    @pytest.mark.unit
    def test_render_guardrails_block_single(self, anti_pattern_vignette):
        """Single anti-pattern vignette renders a guardrails block."""
        result = render_guardrails_block([anti_pattern_vignette])
        assert "[Tool Guardrails: excel.add_column]" in result
        assert "SUM(A:B)" in result

    @pytest.mark.unit
    def test_render_guardrails_block_mixed_kinds(self, anti_pattern_vignette, repair_vignette):
        """Only anti-pattern vignettes appear in guardrails block."""
        result = render_guardrails_block([anti_pattern_vignette, repair_vignette])
        assert "[Tool Guardrails: excel.add_column]" in result
        assert "Repair Playbook" not in result

    @pytest.mark.unit
    def test_render_repair_block_empty(self):
        """Empty vignette list produces empty string."""
        assert render_repair_block([]) == ""

    @pytest.mark.unit
    def test_render_repair_block_single(self, repair_vignette):
        """Single repair vignette renders a repair block."""
        result = render_repair_block([repair_vignette])
        assert "[Repair Playbook: http.request | AuthenticationError]" in result

    @pytest.mark.unit
    def test_render_repair_block_excludes_anti_patterns(self, anti_pattern_vignette, repair_vignette):
        """Only repair_template vignettes appear in repair block."""
        result = render_repair_block([anti_pattern_vignette, repair_vignette])
        assert "Repair Playbook" in result
        assert "Tool Guardrails" not in result

    @pytest.mark.unit
    def test_render_anti_pattern_raises_on_missing_payload(self, repair_vignette):
        """render_anti_pattern raises ValueError when anti_pattern payload is absent."""
        # repair_vignette has no anti_pattern payload
        with pytest.raises(ValueError, match="no anti_pattern payload"):
            render_anti_pattern(repair_vignette)

    @pytest.mark.unit
    def test_render_repair_template_raises_on_missing_payload(self, anti_pattern_vignette):
        """render_repair_template raises ValueError when repair payload is absent."""
        # anti_pattern_vignette has no repair payload
        with pytest.raises(ValueError, match="no repair payload"):
            render_repair_template(anti_pattern_vignette)


# ---------------------------------------------------------------------------
# Scope filter tests
# ---------------------------------------------------------------------------


class TestScopeFilter:
    """Test the OData scope filter builder."""

    @pytest.mark.unit
    def test_no_tenant_no_user_global_only(self):
        """Without tenant/user, only global vignettes are accessible."""
        f = _build_scope_filter(None, None)
        assert "scope eq 'global'" in f
        assert "org" not in f
        assert "user" not in f or f.count("scope eq 'user'") == 0

    @pytest.mark.unit
    def test_with_tenant_includes_org(self):
        """With tenant_id, org-scoped vignettes are accessible."""
        f = _build_scope_filter("tenant-1", None)
        assert "scope eq 'global'" in f
        assert "scope eq 'org'" in f
        assert "tenant-1" in f

    @pytest.mark.unit
    def test_with_tenant_and_user_includes_user_scope(self):
        """With tenant + user, user-scoped vignettes are accessible."""
        f = _build_scope_filter("tenant-1", "user-1")
        assert "scope eq 'global'" in f
        assert "scope eq 'org'" in f
        assert "scope eq 'user'" in f
        assert "user-1" in f

    @pytest.mark.unit
    def test_filter_is_non_empty_string(self):
        """Scope filter always returns a non-empty string."""
        f = _build_scope_filter(None, None)
        assert isinstance(f, str)
        assert len(f) > 0

    @pytest.mark.unit
    def test_single_quotes_in_tenant_id_are_escaped(self):
        """Single quotes in tenant_id are escaped to prevent OData injection."""
        f = _build_scope_filter("tenant'with'quotes", None)
        assert "tenant''with''quotes" in f
        # No unescaped single quote should appear inside the filter value
        # (except as OData string delimiters)
        assert "tenant'with" not in f

    @pytest.mark.unit
    def test_single_quotes_in_user_id_are_escaped(self):
        """Single quotes in user_id are escaped to prevent OData injection."""
        f = _build_scope_filter("tenant-1", "user'one")
        assert "user''one" in f


# ---------------------------------------------------------------------------
# Compiler tests
# ---------------------------------------------------------------------------


class TestCompileVignettes:
    """Test the vignette compiler."""

    @pytest.mark.unit
    def test_compile_creates_anti_pattern_when_args_changed(self):
        """Compiler creates an anti-pattern when args changed."""
        vignettes = compile_vignettes(
            tool_name="my_tool",
            original_args={"param": "bad_value"},
            patched_args={"param": "good_value"},
            error_class="ValueError",
            error_message="Invalid param value",
            repair_steps=["Check param value.", "Use good_value."],
            scope="user",
            tenant_id="t1",
            user_id="u1",
        )
        kinds = {v.kind for v in vignettes}
        assert "anti_pattern" in kinds

    @pytest.mark.unit
    def test_compile_creates_repair_template_with_steps(self):
        """Compiler creates a repair template when steps are provided."""
        vignettes = compile_vignettes(
            tool_name="my_tool",
            original_args={"param": "bad"},
            patched_args={"param": "good"},
            error_class="ValueError",
            error_message="Invalid param",
            repair_steps=["Step 1.", "Step 2."],
            scope="user",
            tenant_id="t1",
            user_id="u1",
        )
        kinds = {v.kind for v in vignettes}
        assert "repair_template" in kinds

    @pytest.mark.unit
    def test_compile_no_anti_pattern_when_args_unchanged(self):
        """No anti-pattern is created when args did not change."""
        vignettes = compile_vignettes(
            tool_name="my_tool",
            original_args={"param": "same"},
            patched_args={"param": "same"},
            error_class="NetworkError",
            error_message="Transient failure",
            repair_steps=["Retry."],
            scope="user",
            tenant_id="t1",
            user_id="u1",
        )
        kinds = {v.kind for v in vignettes}
        assert "anti_pattern" not in kinds
        assert "repair_template" in kinds

    @pytest.mark.unit
    def test_compile_no_repair_template_without_steps(self):
        """No repair template is created when steps are empty."""
        vignettes = compile_vignettes(
            tool_name="my_tool",
            original_args={"param": "bad"},
            patched_args={"param": "good"},
            error_class="ValueError",
            error_message="Invalid param",
            repair_steps=[],
            scope="user",
            tenant_id="t1",
            user_id="u1",
        )
        kinds = {v.kind for v in vignettes}
        assert "repair_template" not in kinds

    @pytest.mark.unit
    def test_compile_vignette_ids_are_deterministic(self):
        """Compiled vignette IDs are deterministic for the same inputs."""

        def _compile() -> list:
            return compile_vignettes(
                tool_name="my_tool",
                original_args={"param": "bad"},
                patched_args={"param": "good"},
                error_class="ValueError",
                error_message="Invalid param",
                repair_steps=["Fix it."],
                scope="user",
                tenant_id="t1",
                user_id="u1",
            )

        vs1 = _compile()
        vs2 = _compile()
        ids1 = {v.vignette_id for v in vs1}
        ids2 = {v.vignette_id for v in vs2}
        assert ids1 == ids2

    @pytest.mark.unit
    def test_compile_tags_include_auto_compiled(self):
        """Compiled vignettes include the 'auto_compiled' tag."""
        vignettes = compile_vignettes(
            tool_name="my_tool",
            original_args={"p": "a"},
            patched_args={"p": "b"},
            error_class="Err",
            error_message="err",
            repair_steps=["step"],
            scope="user",
            tenant_id="t1",
            user_id="u1",
        )
        for v in vignettes:
            assert "auto_compiled" in v.tags
