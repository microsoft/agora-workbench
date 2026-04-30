"""Tests for DWSIM tool input validation logic.

These tests exercise the pure-Python validation helpers that do NOT
require the CLR / DWSIM runtime.  They verify that bad inputs are
caught early with clear error messages rather than propagating to the
.NET layer where they cause opaque timeouts or silent mis-configuration.
"""

import json
import pytest

# ---------------------------------------------------------------------------
# We only import the pure-Python helpers — CLR is not needed for these tests.
# ---------------------------------------------------------------------------
from dwsim_tools.tools.unit_operations import (
    _normalize_rxn_def,
    _validate_reaction_common,
    _warn_unknown_keys,
    _KNOWN_CONVERSION_KEYS,
    _KNOWN_EQUILIBRIUM_KEYS,
)


# =========================================================================
# _normalize_rxn_def
# =========================================================================


class TestNormalizeRxnDef:
    """Test JSON parsing and key aliasing in _normalize_rxn_def."""

    def test_basic_object(self):
        raw = json.dumps({"base_compound": "A", "stoichiometry": {"A": -1}})
        result = _normalize_rxn_def(raw)
        assert result["base_compound"] == "A"

    def test_single_element_array(self):
        raw = json.dumps([{"base_compound": "A", "stoichiometry": {"A": -1}}])
        result = _normalize_rxn_def(raw)
        assert result["base_compound"] == "A"

    def test_empty_array_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _normalize_rxn_def("[]")

    def test_base_alias(self):
        raw = json.dumps({"base": "A", "stoichiometry": {"A": -1}})
        result = _normalize_rxn_def(raw)
        assert "base_compound" in result
        assert "base" not in result
        assert result["base_compound"] == "A"

    def test_keq_alias(self):
        """The 'Keq' alias must be normalised to 'Keq_expression'.

        This is the root cause of the MAF agent failure: passing "Keq"
        instead of "Keq_expression" caused the Keq to silently default
        to 1, producing wrong simulation results.
        """
        raw = json.dumps(
            {
                "base_compound": "AcOH",
                "stoichiometry": {"AcOH": -1, "EtOH": -1, "EtAc": 1, "H2O": 1},
                "Keq": "exp(-0.227 + 481.2/T)",
            }
        )
        result = _normalize_rxn_def(raw)
        assert "Keq_expression" in result
        assert "Keq" not in result
        assert result["Keq_expression"] == "exp(-0.227 + 481.2/T)"

    def test_keq_expression_takes_precedence_over_alias(self):
        """If both 'Keq' and 'Keq_expression' are present, keep 'Keq_expression'."""
        raw = json.dumps(
            {
                "base_compound": "A",
                "stoichiometry": {"A": -1},
                "Keq": "100",
                "Keq_expression": "exp(5)",
            }
        )
        result = _normalize_rxn_def(raw)
        assert result["Keq_expression"] == "exp(5)"

    def test_invalid_json(self):
        with pytest.raises(json.JSONDecodeError):
            _normalize_rxn_def("not json")


# =========================================================================
# _validate_reaction_common
# =========================================================================


class TestValidateReactionCommon:
    """Test shared reactor validation logic."""

    def _valid_rxn(self, **overrides):
        rxn = {
            "base_compound": "Ethanol",
            "stoichiometry": {"Ethanol": -1, "Water": 1},
        }
        rxn.update(overrides)
        return rxn

    def test_valid_passes(self):
        assert _validate_reaction_common(self._valid_rxn(), "RX") is None

    def test_missing_base_compound(self):
        rxn = {"stoichiometry": {"A": -1}}
        err = _validate_reaction_common(rxn, "RX")
        assert err is not None
        assert "base_compound" in err

    def test_empty_base_compound(self):
        rxn = {"base_compound": "", "stoichiometry": {"A": -1}}
        err = _validate_reaction_common(rxn, "RX")
        assert err is not None
        assert "base_compound" in err

    def test_missing_stoichiometry(self):
        rxn = {"base_compound": "A"}
        err = _validate_reaction_common(rxn, "RX")
        assert err is not None
        assert "stoichiometry" in err

    def test_empty_stoichiometry(self):
        rxn = {"base_compound": "A", "stoichiometry": {}}
        err = _validate_reaction_common(rxn, "RX")
        assert err is not None
        assert "stoichiometry" in err

    def test_base_compound_not_in_stoichiometry(self):
        rxn = {"base_compound": "X", "stoichiometry": {"A": -1, "B": 1}}
        err = _validate_reaction_common(rxn, "RX")
        assert err is not None
        assert "X" in err
        assert "not present" in err

    def test_non_numeric_coefficient(self):
        rxn = {"base_compound": "A", "stoichiometry": {"A": "oops"}}
        err = _validate_reaction_common(rxn, "RX")
        assert err is not None
        assert "not numeric" in err

    def test_valid_reaction_phase_liquid(self):
        err = _validate_reaction_common(self._valid_rxn(reaction_phase="Liquid"), "RX")
        assert err is None

    def test_valid_reaction_phase_vapor(self):
        err = _validate_reaction_common(self._valid_rxn(reaction_phase="Vapor"), "RX")
        assert err is None

    def test_invalid_reaction_phase(self):
        err = _validate_reaction_common(self._valid_rxn(reaction_phase="Gas"), "RX")
        assert err is not None
        assert "reaction_phase" in err
        assert "Gas" in err

    def test_no_reaction_phase_is_ok(self):
        """reaction_phase is optional; omitting it is fine."""
        err = _validate_reaction_common(self._valid_rxn(), "RX")
        assert err is None


# =========================================================================
# _warn_unknown_keys
# =========================================================================


class TestWarnUnknownKeys:
    """Test detection of unrecognised keys in reaction_set."""

    def test_no_warning_for_valid_keys(self):
        rxn = {
            "base_compound": "A",
            "stoichiometry": {"A": -1},
            "conversion": 0.9,
        }
        assert _warn_unknown_keys(rxn, _KNOWN_CONVERSION_KEYS, "RX") is None

    def test_warns_on_unknown_key(self):
        rxn = {
            "base_compound": "A",
            "stoichiometry": {"A": -1},
            "Keq_expression": "exp(1)",  # wrong key for conversion reactor
        }
        warning = _warn_unknown_keys(rxn, _KNOWN_CONVERSION_KEYS, "RX")
        assert warning is not None
        assert "Keq_expression" in warning
        assert "ignored" in warning

    def test_equilibrium_keys_accepted(self):
        rxn = {
            "base_compound": "A",
            "stoichiometry": {"A": -1},
            "Keq_expression": "exp(1)",
            "reaction_phase": "Vapor",
        }
        assert _warn_unknown_keys(rxn, _KNOWN_EQUILIBRIUM_KEYS, "RX") is None

    def test_equilibrium_warns_on_conversion_key(self):
        rxn = {
            "base_compound": "A",
            "stoichiometry": {"A": -1},
            "conversion": 0.8,  # wrong key for equilibrium reactor
        }
        warning = _warn_unknown_keys(rxn, _KNOWN_EQUILIBRIUM_KEYS, "RX")
        assert warning is not None
        assert "conversion" in warning

    def test_warns_on_typo(self):
        """Catches common misspellings that would otherwise be silent."""
        rxn = {
            "base_compound": "A",
            "stoichiometry": {"A": -1},
            "Keq_expresion": "exp(1)",  # typo: missing 's'
        }
        warning = _warn_unknown_keys(rxn, _KNOWN_EQUILIBRIUM_KEYS, "RX")
        assert warning is not None
        assert "Keq_expresion" in warning
