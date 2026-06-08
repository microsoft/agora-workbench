"""Unit tests for the agent_guidance steering helpers."""

import pytest

from agora_workbench.code_execution import agent_guidance

pytestmark = pytest.mark.unit


class TestRedirect:
    def test_problem_comes_first(self):
        msg = agent_guidance.redirect("X is blocked.", intents=[agent_guidance.SAVE_OUTPUT])
        assert msg.startswith("X is blocked.")

    def test_lists_each_intent_as_a_bullet(self):
        msg = agent_guidance.redirect(
            "Nope.",
            intents=[agent_guidance.SAVE_OUTPUT, agent_guidance.LOAD_ASSET],
        )
        assert "Depending on what you are doing:" in msg
        assert f"- {agent_guidance.SAVE_OUTPUT}" in msg
        assert f"- {agent_guidance.LOAD_ASSET}" in msg

    def test_load_intent_uses_inline_tag_not_assumed_tools(self):
        # LOAD must lean on the universal <local>/<blob> tag mechanism, NOT assume
        # search_data exists (it is domain opt-in, not a platform builtin).
        msg = agent_guidance.redirect("Nope.", intents=[agent_guidance.LOAD_ASSET])
        assert "<local>" in msg
        assert "<blob>" in msg
        assert "search_data" not in msg

    def test_asset_tag_format_only_advertises_resolvable_types(self):
        # The default manager resolves only local + blob; advertising adls/delta/sql
        # would contradict the "Unsupported artifact type" error.
        fmt = agent_guidance.ASSET_TAG_FORMAT
        assert "<local>" in fmt and "<blob>" in fmt
        for bogus in ("adls", "delta", "sql"):
            assert bogus not in fmt


class TestOperatorGate:
    def test_says_deployment_policy_and_do_not_retry(self):
        msg = agent_guidance.operator_gate(
            "Host blocked.",
            tell_user="ask the operator to allow it.",
            env_var="SOME_ENV",
        )
        assert msg.startswith("Host blocked.")
        assert "deployment policy" in msg
        assert "Do not retry" in msg
        assert "ask the operator to allow it." in msg

    def test_names_the_env_var_when_given(self):
        msg = agent_guidance.operator_gate("X.", tell_user="y.", env_var="MY_VAR")
        assert "MY_VAR" in msg
        assert "environment variable" in msg

    def test_omits_env_var_phrase_when_none(self):
        msg = agent_guidance.operator_gate("Too big.", tell_user="shrink it.")
        assert "environment variable" not in msg
        assert "deployment policy" in msg


class TestNoResultsHint:
    def test_data_hint_points_to_list_domains(self):
        msg = agent_guidance.no_results_hint("data", "power grid")
        assert "power grid" in msg
        assert "list_domains" in msg

    def test_tools_hint_points_to_full_catalog(self):
        msg = agent_guidance.no_results_hint("tools", "foo")
        assert "foo" in msg
        assert "top=999" in msg
