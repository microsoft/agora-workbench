"""
Unit tests for AgentResponse.strip_markdown_wrapper validator.

Covers:
  1. Plain JSON string (no fencing)
  2. Fenced JSON (```json ... ```)
  3. Concatenated / "extra data" JSON (valid object + trailing junk)
  4. Already-parsed dict passthrough
  5. Invalid JSON raises ValueError
"""

import json

import pytest

from agent_bot.plan_then_execute.response_models import AgentResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_payload(**overrides) -> dict:
    """Return a minimal valid AgentResponse dict."""
    base = {
        "explanation": "thinking...",
        "response": {"action": "solution", "solution": "42"},
    }
    base.update(overrides)
    return base


def _to_json_str(d: dict) -> str:
    return json.dumps(d)


# ---------------------------------------------------------------------------
# 1. Plain JSON string (no markdown fencing)
# ---------------------------------------------------------------------------


class TestPlainJson:
    def test_plain_json_parses(self):
        payload = _minimal_payload()
        resp = AgentResponse.model_validate(_to_json_str(payload))
        assert resp.explanation == "thinking..."
        assert resp.response.action == "solution"
        assert resp.response.solution == "42"

    def test_plain_json_with_whitespace(self):
        payload = _minimal_payload()
        raw = f"  \n {_to_json_str(payload)}  \n "
        resp = AgentResponse.model_validate(raw)
        assert resp.explanation == "thinking..."


# ---------------------------------------------------------------------------
# 2. Fenced JSON (```json ... ``` and ``` ... ```)
# ---------------------------------------------------------------------------


class TestFencedJson:
    def test_json_fenced_block(self):
        payload = _minimal_payload()
        raw = f"```json\n{_to_json_str(payload)}\n```"
        resp = AgentResponse.model_validate(raw)
        assert resp.response.action == "solution"

    def test_plain_fenced_block(self):
        """Fence without 'json' language tag."""
        payload = _minimal_payload()
        raw = f"```\n{_to_json_str(payload)}\n```"
        resp = AgentResponse.model_validate(raw)
        assert resp.response.action == "solution"

    def test_fenced_with_extra_whitespace(self):
        payload = _minimal_payload()
        raw = f"  ```json  \n  {_to_json_str(payload)}  \n  ```  "
        resp = AgentResponse.model_validate(raw)
        assert resp.explanation == "thinking..."


# ---------------------------------------------------------------------------
# 3. Concatenated / "extra data" JSON
# ---------------------------------------------------------------------------


class TestConcatenatedJson:
    def test_extra_json_object_after_valid(self):
        """LLM outputs valid response followed by a hallucinated tool call."""
        payload = _minimal_payload()
        extra = {"tool_calls": [{"name": "fake_tool", "args": {}}]}
        raw = _to_json_str(payload) + _to_json_str(extra)
        resp = AgentResponse.model_validate(raw)
        assert resp.explanation == "thinking..."
        assert resp.response.action == "solution"

    def test_extra_json_array_after_valid(self):
        """Valid response followed by a JSON array."""
        payload = _minimal_payload()
        raw = _to_json_str(payload) + "[1, 2, 3]"
        resp = AgentResponse.model_validate(raw)
        assert resp.response.solution == "42"

    def test_extra_data_in_fenced_block(self):
        """Fenced block containing concatenated JSON objects."""
        payload = _minimal_payload()
        extra = {"action": "help", "question": "hallucinated"}
        inner = _to_json_str(payload) + _to_json_str(extra)
        raw = f"```json\n{inner}\n```"
        resp = AgentResponse.model_validate(raw)
        assert resp.response.action == "solution"

    def test_extra_whitespace_between_objects(self):
        """Valid object + whitespace + second object."""
        payload = _minimal_payload()
        extra = {"something": "else"}
        raw = _to_json_str(payload) + "  " + _to_json_str(extra)
        resp = AgentResponse.model_validate(raw)
        assert resp.response.action == "solution"


# ---------------------------------------------------------------------------
# 4. Dict passthrough (already parsed)
# ---------------------------------------------------------------------------


class TestDictPassthrough:
    def test_dict_passes_through(self):
        payload = _minimal_payload()
        resp = AgentResponse.model_validate(payload)
        assert resp.explanation == "thinking..."

    def test_dict_with_all_fields(self):
        payload = _minimal_payload(
            plan="step 1: do something",
            status="Running analysis",
        )
        resp = AgentResponse.model_validate(payload)
        assert resp.plan == "step 1: do something"
        assert resp.status == "Running analysis"


# ---------------------------------------------------------------------------
# 5. Invalid JSON
# ---------------------------------------------------------------------------


class TestInvalidJson:
    def test_completely_invalid_json_raises(self):
        with pytest.raises(Exception):
            AgentResponse.model_validate("this is not json at all")

    def test_truncated_json_raises(self):
        with pytest.raises(Exception):
            AgentResponse.model_validate('{"explanation": "thinking..."')

    def test_empty_string_raises(self):
        with pytest.raises(Exception):
            AgentResponse.model_validate("")


# ---------------------------------------------------------------------------
# 6. All response action types
# ---------------------------------------------------------------------------


class TestResponseTypes:
    def test_help_response(self):
        payload = {
            "explanation": "I need more info",
            "response": {"action": "help", "question": "What bus?"},
        }
        resp = AgentResponse.model_validate(_to_json_str(payload))
        assert resp.response.action == "help"
        assert resp.response.question == "What bus?"

    def test_solution_dict_coerced_to_json_string(self):
        """LLMs often return solution as a dict — it should be serialized to a JSON string."""
        payload = {
            "explanation": "analysis complete",
            "response": {
                "action": "solution",
                "solution": {"buses": 50, "lines": 128},
                "provenance": "PyPSA",
            },
        }
        resp = AgentResponse.model_validate(payload)
        assert resp.response.action == "solution"
        assert isinstance(resp.response.solution, str)
        parsed = json.loads(resp.response.solution)
        assert parsed["buses"] == 50

    def test_solution_list_coerced_to_json_string(self):
        """Solution as a list should also be serialized."""
        payload = {
            "explanation": "found items",
            "response": {
                "action": "solution",
                "solution": ["item1", "item2"],
            },
        }
        resp = AgentResponse.model_validate(payload)
        assert isinstance(resp.response.solution, str)
        assert json.loads(resp.response.solution) == ["item1", "item2"]

    def test_unknown_action_raises(self):
        """Test that unknown action types (e.g. removed 'retrieval') raise validation error."""
        payload = {
            "explanation": "Need tools",
            "response": {
                "action": "retrieval",
                "query": "optimal power flow",
                "reasoning": "need OPF solver",
            },
        }
        with pytest.raises(Exception):
            AgentResponse.model_validate(_to_json_str(payload))
