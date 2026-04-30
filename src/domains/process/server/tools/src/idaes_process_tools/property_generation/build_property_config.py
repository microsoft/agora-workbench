# Import typing for better type hints
from typing import Any, Dict, List, Optional, Tuple
import logging

import numpy as np
from chemicals.reaction import Hfg, Hfl, S0g

# Import IDAES cores
from idaes.core import Component, LiquidPhase, VaporPhase
from idaes.core.base.phases import PhaseType as PT
from idaes.models.properties.modular_properties.eos.ceos import Cubic, CubicType
from idaes.models.properties.modular_properties.eos.ideal import Ideal
from idaes.models.properties.modular_properties.phase_equil import SmoothVLE
from idaes.models.properties.modular_properties.phase_equil.bubble_dew import (
    IdealBubbleDew,
)
from idaes.models.properties.modular_properties.phase_equil.forms import fugacity, log_fugacity
from idaes.models.properties.modular_properties.pure.ConstantProperties import Constant
from idaes.models.properties.modular_properties.pure.NIST import NIST

# Import IDAES property models
from idaes.models.properties.modular_properties.pure.Perrys import Perrys
from idaes.models.properties.modular_properties.pure.RPP4 import RPP4
from idaes.models.properties.modular_properties.pure.RPP5 import RPP5
from idaes.models.properties.modular_properties.state_definitions import FpcTP, FTPx

# Import Pyomo units
from pyomo.environ import units as pyunits
from thermo import Chemical
from thermo.chemical_package import ChemicalConstantsPackage
from thermo.chemical_utils import S0_basis_converter
from thermo.interaction_parameters import IPDB

from .heos_fit import HEOSFitGAS, HEOSFitLIQ
from .polyfit_thermo import (
    SampleGrids,
    fit_antoine,
    fit_perrys_cp_liq,
    fit_perrys_density_eq1,
    fit_rpp4_cp,
    sample_from_thermo,
)

LOGGER = logging.getLogger(__name__)


# Method rankings for different property types
class PropertyMethodRankings:
    # Default rankings for different property types
    VAPOR: List[str] = []  # ["POLING_POLY", "HEOS_FIT"]
    LIQUID: List[str] = []  # ["HEOS_FIT"] TODO: Perry "DIPPR_PERRY_8E"]
    DENSITY: List[str] = []  # ["HEOS_FIT"] TODO: Perry "DIPPR_PERRY_8E"]
    PSAT: List[str] = []  # ["ANTOINE_WEBBOOK"] #["ANTOINE_WEBBOOK", "HEOS_FIT"]

    @classmethod
    def get_rankings(cls):
        return {"vapor": cls.VAPOR, "liquid": cls.LIQUID, "density": cls.DENSITY, "psat": cls.PSAT}


class PropertyMethodSelector:
    """Class for selecting appropriate property methods."""

    def __init__(self, method_rankings=None):
        if method_rankings is None:
            method_rankings = PropertyMethodRankings.get_rankings()
        self.vapor_methods = method_rankings.get("vapor", [])
        self.liquid_methods = method_rankings.get("liquid", [])
        self.density_methods = method_rankings.get("density", [])
        self.psat_methods = method_rankings.get("psat", [])

    def select_vapor_method(self, hc_gas, comp_name, temperature_range=None):
        """
        Select the appropriate method for vapor phase properties.

        Args:
            hc_gas: Heat capacity correlation object for gas phase
            comp_name: Component name for logging
            temperature_range: Optional tuple of (min, max) temperature for checking method applicability

        Returns:
            selected_method: The selected method name
        """
        selected_method = self._select_method(hc_gas, comp_name, self.vapor_methods, "vapor phase", temperature_range)
        return selected_method

    def select_liquid_method(self, hc_liq, comp_name, temperature_range=None):
        """
        Select the appropriate method for liquid phase properties.

        Args:
            hc_liq: Heat capacity correlation object for liquid phase
            comp_name: Component name for logging
            temperature_range: Optional tuple of (min, max) temperature for checking method applicability

        Returns:
            selected_method: The selected method name
        """
        selected_method = self._select_method(hc_liq, comp_name, self.liquid_methods, "liquid phase", temperature_range)
        return selected_method

    def select_density_method(self, vl, comp_name, temperature_range=None):
        """
        Select the appropriate method for density properties.

        Args:
            vl: Volume liquid correlation object
            comp_name: Component name for logging
            temperature_range: Optional tuple of (min, max) temperature for checking method applicability

        Returns:
            selected_method: The selected method name
        """
        selected_method = self._select_method(vl, comp_name, self.density_methods, "liquid density", temperature_range)
        return selected_method

    def select_psat_method(self, psat, comp_name, temperature_range=None):
        """
        Select the appropriate method for vapor pressure properties.

        Args:
            psat: Vapor pressure correlation object
            comp_name: Component name for logging
            temperature_range: Optional tuple of (min, max) temperature for checking method applicability

        Returns:
            selected_method: The selected method name
        """
        selected_method = self._select_method(psat, comp_name, self.psat_methods, "vapor pressure", temperature_range)
        return selected_method

    def _select_method(self, obj, comp_name, method_ranking, property_type, temperature_range=None):
        """
        Generic method selection logic with temperature range consideration.

        Args:
            obj: Correlation object
            comp_name: Component name for logging
            method_ranking: List of methods in order of preference
            property_type: Type of property for logging
            temperature_range: Optional tuple of (min, max) temperature for checking method applicability

        Returns:
            selected_method: The selected method name
        """
        selected_method = None
        valid_methods = []

        # First pass: check which methods are available and have valid temperature ranges
        for method in method_ranking:
            if method in obj.all_methods:
                # If temperature range is specified, check method's T_limits
                if temperature_range and hasattr(obj, "T_limits") and method in obj.T_limits:
                    # Handle different possible formats of T_limits
                    t_limits = obj.T_limits[method]

                    # T_limits always shows the full T range no matter if there are piecewise correlations or not
                    # Single range
                    is_contained, overlap = temperature_range_check(temperature_range, t_limits)
                    valid_methods.append((method, is_contained, overlap))
                else:
                    # No temperature check or no T_limits attribute
                    valid_methods.append((method, True, 1.0))  # Assume fully valid

        # Second pass: select first method with fully contained temperature range
        for method, is_contained, overlap in valid_methods:
            if is_contained:
                selected_method = method
                print(f"Using {selected_method} for {comp_name} {property_type} properties (within temperature range)")
                break

        # Third pass: if no method with fully contained range, use method with best overlap
        if selected_method is None and valid_methods:
            best_method = max(valid_methods, key=lambda x: x[2])  # Sort by overlap
            if best_method[2] > 0:  # If there's any overlap
                selected_method = best_method[0]
                print(
                    f"WARNING: Using {selected_method} for {comp_name} {property_type} properties with {best_method[2]:.1%} overlap"
                )
            else:
                # No overlap, just use first available method
                # TODO: Here we need to have some other fallback, like the LLM or using a constant value.
                selected_method = valid_methods[0][0]
                print(
                    f"WARNING: Using {selected_method} for {comp_name} {property_type} properties (outside temperature range)"
                )

        # Fourth pass: fallback to POLYFIT
        if selected_method is None:
            # selected_method = "HEOS_FIT" if "HEOS_FIT" in obj.correlations else None
            # If obj methods are not empty
            if obj.all_methods:
                selected_method = "POLYFIT"
                print(
                    f"WARNING: Using a custom polyfit for {comp_name} {property_type} to any available data. Possibly low accuracy. "
                )

        if selected_method is None:
            print(f"WARNING: No suitable {property_type} correlation found for {comp_name}")

        return selected_method


# Default sub-models for each phase
def temperature_range_check(
    user_range: Optional[Tuple[float, float]], correlation_range: Optional[Tuple[float, float]]
) -> Tuple[bool, float]:
    """
    Check if the user-defined temperature range is within the correlation's range.

    Args:
        user_range: Tuple of (min, max) temperature range specified by the user
        correlation_range: Tuple of (min, max) temperature range valid for the correlation

    Returns:
        bool: True if user_range is fully contained within correlation_range, False otherwise
        float: Overlap ratio between user range and correlation range (0.0-1.0)
    """
    # Check if ranges are valid
    if user_range is None or correlation_range is None:
        return False, 0.0

    # Calculate overlap
    overlap_start = max(user_range[0], correlation_range[0])
    overlap_end = min(user_range[1], correlation_range[1])

    if overlap_end <= overlap_start:
        # No overlap
        return False, 0.0

    # Calculate overlap as a ratio of user range
    user_range_span = user_range[1] - user_range[0]
    if user_range_span <= 0:
        return False, 0.0

    overlap_span = overlap_end - overlap_start
    overlap_ratio = overlap_span / user_range_span

    # Fully contained check
    is_fully_contained = user_range[0] >= correlation_range[0] and user_range[1] <= correlation_range[1]

    return is_fully_contained, overlap_ratio


class PropertyMethodApplicator:
    """Class for applying property methods to components."""

    def apply_vapor_method(
        self,
        comp,
        hc_gas,
        hc_liq,
        method,
        href,
        sref,
        temperature_range=None,
        comp_name=None,
        casnr=None,
    ):
        """
        Apply the selected vapor method to the component.

        Args:
            comp: Component dictionary to update
            hc_gas: Heat capacity correlation object for gas phase
            hc_liq: Heat capacity correlation object for liquid phase (for fallback)
            method: Method name to apply
            href: Formation enthalpy at standard conditions
            sref: Formation entropy at standard conditions
            temperature_range: Optional temperature range for some methods
            comp_name: Component name for logging
            casnr: CAS registry number for more precise lookup
        """
        if method == "HEOS_FIT":
            self._apply_heos_fit(comp, hc_gas, method, href, sref, phase="vapor")
        elif method == "POLING_POLY":
            self._apply_poling_poly(comp, hc_gas, href, sref, phase="vapor")
        elif method == "POLYFIT":
            self._apply_polyfit(
                comp,
                hc_liq,
                href,
                sref,
                property_type="cp_vap",
                casnr=casnr,
                T_range=temperature_range,
            )

    def apply_liquid_method(self, comp, hc_liq, method, href, sref, temperature_range=None, casnr=None):
        """
        Apply the selected liquid method to the component.

        Args:
            comp: Component dictionary to update
            hc_liq: Heat capacity correlation object for liquid phase
            method: Method name to apply
            href: Formation enthalpy at standard conditions for liquid
            sref: Formation entropy at standard conditions for liquid
            temperature_range: Optional temperature range for checking method validity
            casnr: CAS registry number for more precise lookup
        """
        if method == "HEOS_FIT":
            self._apply_heos_fit(comp, hc_liq, method, href, sref, phase="liquid")
        elif method == "POLING_POLY":
            self._apply_poling_poly(comp, hc_liq, href, sref, phase="liquid")
        elif method == "POLYFIT":
            self._apply_polyfit(
                comp,
                hc_liq,
                href,
                sref,
                property_type="cp_liq",
                casnr=casnr,
                T_range=temperature_range,
            )

    def apply_density_method(self, comp, vl, method, casnr=None, temperature_range=None):
        """
        Apply the selected density method to the component.

        Args:
            comp: Component dictionary to update
            vl: Volume liquid correlation object
            method: Method name to apply
            casnr: CAS registry number for more precise lookup
        """
        if method == "HEOS_FIT":
            self._apply_heos_fit(comp, vl, method, None, None, phase="density")
        elif method == "POLYFIT":
            self._apply_polyfit(
                comp,
                vl,
                None,
                None,
                property_type="density",
                casnr=casnr,
                T_range=temperature_range,
            )
        elif method == "Constant_fallback":
            self._apply_constant_fallback(
                comp,
                vl,
                None,
                None,
                property_type="density",
                casnr=casnr,
                T_range=temperature_range,
            )

    def apply_psat_method(self, comp, temperature_range, psat, method, comp_name=None, cas_nr=None):
        """
        Apply the selected vapor pressure method to the component.

        Args:
            comp: Component dictionary to update
            psat: Vapor pressure correlation object
            method: Method name to apply
            comp_name: Component name for lookup in databases
            cas_nr: CAS registry number for more precise lookup
        """
        if method == "HEOS_FIT":
            self._apply_heos_fit(comp, psat, method, None, None, phase="psat")
        elif method == "ANTOINE_WEBBOOK":
            self._apply_antoine_webbook(comp, temperature_range, psat)
        elif method == "POLYFIT":
            self._apply_polyfit(
                comp,
                psat,
                None,
                None,
                property_type="psat",
                casnr=cas_nr,
                T_range=temperature_range,
            )

    def _apply_polyfit(self, comp, obj, href, sref, property_type, casnr, T_range):
        """
        This method takes whatever data is available (and preferred) in the thermo package,
        samples data, and applies a polynomial fit to a form that's supported in IDAES.
        The coefficients are then used for IDAES native property correlations.
        For ideal gas properties: RPP4
        For ideal liquid properties: Perrys
        For vapor pressure: RPP4
        """
        Tref = 298.15
        # create the grid to sample based on temperature specifications
        # Depending on which property type, fit and extract the relevant coefficients
        if property_type == "cp_vap":
            grids = SampleGrids(Tref=Tref, T_ranges={"T_ig": np.linspace(T_range[0], T_range[1], 100)})
            data, used_methods = sample_from_thermo(identifier=casnr, grids=grids, properties=["cp_vap"])
            rpp4 = fit_rpp4_cp(grids.T_ranges["T_ig"], data["Cp_ig"])
            comp["parameter_data"]["cp_mol_ig_comp_coeff"] = {
                "A": (rpp4.A, pyunits.J / pyunits.mol / pyunits.K),
                "B": (rpp4.B, pyunits.J / pyunits.mol / pyunits.K**2),
                "C": (rpp4.C, pyunits.J / pyunits.mol / pyunits.K**3),
                "D": (rpp4.D, pyunits.J / pyunits.mol / pyunits.K**4),
            }
            # Set the property package:
            comp["cp_mol_ig_comp"] = RPP4
            comp["enth_mol_ig_comp"] = RPP4
            comp["entr_mol_ig_comp"] = RPP4

            # Formation enthalpy and entropy
            comp["parameter_data"]["enth_mol_form_vap_comp_ref"] = (href, pyunits.J / pyunits.mol)
            comp["parameter_data"]["entr_mol_form_vap_comp_ref"] = (
                sref,
                pyunits.J / pyunits.mol / pyunits.K,
            )
        elif property_type == "cp_liq":
            grids = SampleGrids(Tref=Tref, T_ranges={"T_liq": np.linspace(T_range[0], T_range[1], 100)})
            data, used_methods = sample_from_thermo(identifier=casnr, grids=grids, properties=["cp_liq"])
            perrys_liq = fit_perrys_cp_liq(grids.T_ranges["T_liq"], data["Cp_liq"])
            comp["parameter_data"]["cp_mol_liq_comp_coeff"] = {
                "1": (perrys_liq.C1, pyunits.J / pyunits.kmol / pyunits.K),
                "2": (perrys_liq.C2, pyunits.J / pyunits.kmol / pyunits.K**2),
                "3": (perrys_liq.C3, pyunits.J / pyunits.kmol / pyunits.K**3),
                "4": (perrys_liq.C4, pyunits.J / pyunits.kmol / pyunits.K**4),
                "5": (perrys_liq.C5, pyunits.J / pyunits.kmol / pyunits.K**5),
            }
            # Set the property package:
            comp["cp_mol_liq_comp"] = Perrys
            comp["enth_mol_liq_comp"] = Perrys
            comp["entr_mol_liq_comp"] = Perrys

            # will throw an error if href or sref is None
            if href is None or sref is None:
                raise ValueError(f"Missing reference values for {comp}")
            comp["parameter_data"]["enth_mol_form_liq_comp_ref"] = (href, pyunits.J / pyunits.mol)
            comp["parameter_data"]["entr_mol_form_liq_comp_ref"] = (
                sref,
                pyunits.J / pyunits.mol / pyunits.K,
            )
        elif property_type == "psat":
            grids = SampleGrids(Tref=Tref, T_ranges={"T_psat": np.linspace(T_range[0], T_range[1], 100)})
            data, used_methods = sample_from_thermo(identifier=casnr, grids=grids, properties=["psat"])
            antoine = fit_antoine(grids.T_ranges["T_psat"], data["Psat_bar"])
            comp["parameter_data"]["pressure_sat_comp_coeff"] = {
                "A": (antoine.A, pyunits.dimensionless),
                "B": (antoine.B, pyunits.K),
                "C": (antoine.C, pyunits.K),
            }

            # Set property package
            comp["pressure_sat_comp"] = NIST  # NIST has the Antoine equation for psat implemented

        elif property_type == "density":
            # Currently only density eq1 but for some components eq2 might be appropriate
            # However, for those probably polyfit is not needed
            # When implementing equation 2 the units would be different:
            # https://idaes-pse.readthedocs.io/en/stable/explanations/components/property_package/general/pure/Perrys.html
            grids = SampleGrids(Tref=Tref, T_ranges={"T_liq": np.linspace(T_range[0], T_range[1], 100)})
            data, used_methods = sample_from_thermo(identifier=casnr, grids=grids, properties=["density"])
            Tc = Chemical(casnr).Tc
            perrys_density = fit_perrys_density_eq1(grids.T_ranges["T_liq"], data["Density"], T_crit=Tc)
            comp["parameter_data"]["dens_mol_liq_comp_coeff"] = {
                "eqn_type": 1,
                "1": (perrys_density.C1, pyunits.kmol / pyunits.m**3),
                "2": (perrys_density.C2, None),
                "3": (perrys_density.C3, pyunits.K),
                "4": (perrys_density.C4, None),
            }
            # Set the property package:
            comp["dens_mol_liq_comp"] = Perrys

    def _apply_constant_fallback(self, comp, obj, href, sref, property_type, casnr, T_range):
        """
        Apply constant fallback method for the specified property.

        Args:
            comp: Component dictionary to update
            obj: Correlation object (heat capacity, volume, pressure)
            href: Formation enthalpy at standard conditions (if applicable)
            sref: Formation entropy at standard conditions (if applicable)
            property_type: Type of property being estimated (e.g., "density")
            casnr: CAS number of the component
            T_range: Temperature range for the component
        """
        if property_type == "density":
            comp["dens_mol_liq_comp"] = Constant
        else:
            raise ValueError(f"Unknown property type: {property_type}")

    def _apply_heos_fit(self, comp, obj, method, href, sref, phase):
        """
        Apply HEOS_FIT method for the specified phase and property.

        Args:
            comp: Component dictionary to update
            obj: Correlation object (heat capacity, volume, pressure)
            method: Method name
            href: Formation enthalpy at standard conditions (if applicable)
            sref: Formation entropy at standard conditions (if applicable)
            phase: Phase type ("vapor", "liquid", "density", "psat")
        """
        call, kwargs, model, extra = obj.correlations[method]
        # Common retrieval of coefficients and scaling parameters
        coeffs = kwargs.get("coeffs", None)
        int_coeffs = extra.get("int_coeffs", None) if phase in ["vapor", "liquid"] else None
        T0 = extra.get("offset", 0.0)
        delta = extra.get("scale", 1.0)

        # Phase-specific processing
        if phase == "vapor":
            # For entropy
            int_T_coeffs = extra.get("int_T_coeffs", None)
            int_T_log_coeff = extra.get("int_T_log_coeff", None)

            comp["cp_mol_ig_comp"] = HEOSFitGAS
            comp["enth_mol_ig_comp"] = HEOSFitGAS
            comp["entr_mol_ig_comp"] = HEOSFitGAS
            comp["parameter_data"]["cp_mol_ig_comp_coeff"] = {str(i): (ai, None) for i, ai in enumerate(coeffs or [])}
            comp["parameter_data"]["vap_offset"] = (T0, None)
            comp["parameter_data"]["vap_scale"] = (delta, 1 / pyunits.K)
            comp["parameter_data"]["cp_mol_ig_comp_int_coeff"] = {
                str(i): (bi, None) for i, bi in enumerate(int_coeffs or [])
            }
            comp["parameter_data"]["enth_mol_form_vap_comp_ref"] = (href, pyunits.J / pyunits.mol)
            comp["parameter_data"]["entr_mol_form_vap_comp_ref"] = (
                sref,
                pyunits.J / pyunits.mol / pyunits.K,
            )
            comp["parameter_data"]["cp_mol_ig_comp_int_T_coeff"] = {
                str(i): (ci, None) for i, ci in enumerate(int_T_coeffs)
            }
            comp["parameter_data"]["cp_mol_ig_comp_int_T_log_coeff"] = (int_T_log_coeff, None)

        elif phase == "liquid":
            # For entropy
            int_T_coeffs = extra.get("int_T_coeffs", None)
            int_T_log_coeff = extra.get("int_T_log_coeff", None)

            comp["cp_mol_liq_comp"] = HEOSFitLIQ
            comp["enth_mol_liq_comp"] = HEOSFitLIQ
            comp["entr_mol_liq_comp"] = HEOSFitLIQ
            comp["parameter_data"]["cp_mol_liq_comp_coeff"] = {str(i): (ai, None) for i, ai in enumerate(coeffs or [])}
            comp["parameter_data"]["liq_offset"] = (T0, None)
            comp["parameter_data"]["liq_scale"] = (delta, 1 / pyunits.K)
            comp["parameter_data"]["cp_mol_liq_comp_int_coeff"] = {
                str(i): (bi, None) for i, bi in enumerate(int_coeffs or [])
            }
            comp["parameter_data"]["enth_mol_form_liq_comp_ref"] = (href, pyunits.J / pyunits.mol)
            comp["parameter_data"]["entr_mol_form_liq_comp_ref"] = (
                sref,
                pyunits.J / pyunits.mol / pyunits.K,
            )
            comp["parameter_data"]["cp_mol_liq_comp_int_T_coeff"] = {
                str(i): (ci, None) for i, ci in enumerate(int_T_coeffs)
            }
            comp["parameter_data"]["cp_mol_liq_comp_int_T_log_coeff"] = (int_T_log_coeff, None)

        elif phase == "density":
            comp["dens_mol_liq_comp"] = HEOSFitLIQ
            comp["parameter_data"]["dens_mol_liq_comp_coeff"] = {str(i): (ai, None) for i, ai in enumerate(coeffs)}
            comp["parameter_data"]["density_offset"] = (T0, 1 / pyunits.K)
            comp["parameter_data"]["density_scale"] = (delta, None)

        elif phase == "psat":
            comp["pressure_sat_comp"] = HEOSFitGAS
            comp["parameter_data"]["pressure_sat_comp_coeff"] = {str(i): (ai, None) for i, ai in enumerate(coeffs)}
            comp["parameter_data"]["psat_offset"] = (T0, None)
            comp["parameter_data"]["psat_scale"] = (delta, 1 / pyunits.K)

    def _apply_poling_poly(self, comp, hc_gas, href, sref, phase):
        """
        Apply POLING_POLY method for the specified phase.

        Args:
            comp: Component dictionary to update
            hc_gas: Heat capacity correlation object (if for vapor phase)
            href: Formation enthalpy at standard conditions (if applicable)
            sref: Formation entropy at standard conditions (if applicable)
            phase: Phase type ("vapor" or "liquid")
        """
        if phase == "vapor":
            # TODO: Check temperature limits
            coeffs = hc_gas.POLING_coefs
            # T_limits = hc_gas.T_limits["POLING_POLY"]

            # Set parameters
            comp["cp_mol_ig_comp"] = RPP5
            comp["enth_mol_ig_comp"] = RPP5
            comp["entr_mol_ig_comp"] = RPP5
            comp["parameter_data"]["cp_mol_ig_comp_coeff"] = {
                "a" + str(i): (ai, 1 / pyunits.K**i) for i, ai in enumerate(coeffs)
            }
            comp["parameter_data"]["enth_mol_form_vap_comp_ref"] = (href, pyunits.J / pyunits.mol)
            comp["parameter_data"]["entr_mol_form_vap_comp_ref"] = (
                sref,
                pyunits.J / pyunits.mol / pyunits.K,
            )
        elif phase == "liquid":
            # TODO: implement the coefficient retrieval for Poling_poly for liquid
            pass

    def _apply_antoine_webbook(self, comp, psat, comp_name=None):
        """
        Apply ANTOINE_WEBBOOK method for vapor pressure.

        Args:
            comp: Component dictionary to update
            psat: Vapor pressure correlation object
            comp_name: Component name (required for LLM approach)
            cas_nr: Optional CAS registry number for more precise lookup
        """
        map_coeff_position_unit = {
            0: ("A", None),
            1: ("B", pyunits.K),
            2: ("C", pyunits.K),
        }

        # Use thermo package for Antoine coefficients
        if hasattr(psat, "ANTOINE_WEBBOOK_coefs"):
            coeffs = psat.ANTOINE_WEBBOOK_coefs
            comp["pressure_sat_comp"] = NIST
            comp["parameter_data"]["pressure_sat_comp_coeff"] = {
                map_coeff_position_unit[i][0]: (ai, map_coeff_position_unit[i][1]) for i, ai in enumerate(coeffs)
            }
            print(f"Using thermo package Antoine coefficients for {comp_name}")
        else:
            print(f"WARNING: No Antoine coefficients found for {comp_name} using thermo package")


def build_component_skeleton(
    name: str, phases: List[str], eos_config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Build a skeleton component entry for 'name' with valid phases.
    Empty fields filled with None or placeholder dicts.

    Args:
        name: Component name
        phases: List of phases this specific component appears in
        eos_config: Dictionary specifying equation of state configuration
    """
    comp = {
        "type": Component,
        "valid_phase_types": [
            PT.vaporPhase if "Vap" in phases else None,
            PT.liquidPhase if "Liq" in phases else None,
        ],
        # Remove None entries
    }
    comp["valid_phase_types"] = [p for p in comp["valid_phase_types"] if p]

    # Only set phase_equilibrium_form if this component exists in both vapor and liquid phases
    if set(phases) >= {"Vap", "Liq"}:
        # Determine phase equilibrium form based on EOS
        if eos_config:
            # Check if we have separate configs for each phase
            vap_config = eos_config.get("Vap", eos_config)
            # For cubic EOS, use log_fugacity; for ideal EOS, use fugacity
            if vap_config.get("type") == "cubic":
                comp["phase_equilibrium_form"] = {("Vap", "Liq"): log_fugacity}
                print(f"Using log_fugacity for {name} due to cubic EOS")
            else:
                comp["phase_equilibrium_form"] = {("Vap", "Liq"): fugacity}
                print(f"Using fugacity for {name} due to ideal EOS")
        else:
            # Default to fugacity if no EOS config provided
            comp["phase_equilibrium_form"] = {("Vap", "Liq"): fugacity}
            print(f"Using default fugacity for {name} (no EOS config provided)")

    # Initialize empty parameter dict
    comp["parameter_data"] = {}
    return comp


class PropertyDataProcessor:
    """Main class for processing property data."""

    def __init__(self, method_rankings=None):
        self.selector = PropertyMethodSelector(method_rankings)
        self.applicator = PropertyMethodApplicator()

    def process_component(self, comp, comp_name, c, constants, correlations, idx, temperature_range):
        """
        Process a single component's property data.

        Args:
            comp: Component dictionary to update
            comp_name: Component name
            c: Chemical object
            constants: Chemical constants package
            correlations: Chemical correlations package
            idx: Index of component in constants/correlations
            temperature_range: Temperature range for correlations
        """
        # Elemental compostion
        comp["elemental_composition"] = c.atoms

        # Basic properties
        comp["parameter_data"]["mw"] = (constants.MWs[idx], pyunits.g / pyunits.mol)
        comp["parameter_data"]["pressure_crit"] = (constants.Pcs[idx], pyunits.Pa)
        comp["parameter_data"]["temperature_crit"] = (constants.Tcs[idx], pyunits.K)

        # Add acentric factor (omega) for cubic EoS
        if hasattr(c, "omega"):
            comp["parameter_data"]["omega"] = (c.omega, None)  # Dimensionless
        else:
            print(f"WARNING: No acentric factor (omega) found for {comp_name}, using default 0.0")
            comp["parameter_data"]["omega"] = (0.0, None)

        # Formation properties
        href_g, sref_g, href_l, sref_l = self._get_formation_properties(c)

        # Process phase properties
        if PT.vaporPhase in comp["valid_phase_types"]:
            self._process_vapor_phase(comp, correlations, idx, comp_name, c, href_g, sref_g, temperature_range)

        if PT.liquidPhase in comp["valid_phase_types"]:
            self._process_liquid_phase(comp, correlations, idx, comp_name, c, href_l, sref_l, temperature_range)

        if PT.vaporPhase in comp["valid_phase_types"] and PT.liquidPhase in comp["valid_phase_types"]:
            self._process_vle(comp, correlations, idx, comp_name, c, temperature_range)

    def _get_formation_properties(self, c):
        """
        Get formation enthalpies and entropies.

        Args:
            c: Chemical object

        Returns:
            href_g: Formation enthalpy for gas phase
            sref_g: Formation entropy for gas phase
            href_l: Formation enthalpy for liquid phase
            sref_l: Formation entropy for liquid phase
            TODO: If not all of these can be found for a compound -> set using formation enthalpies and entropies in IDAES to False
        """
        href_g = href_l = sref_g = sref_l = None

        # Gas phase properties
        if hasattr(c, "CAS"):
            href_g = Hfg(c.CAS)  # formation enthalpy gas at STP
            if c.phase_STP == "g":
                # S0m and S0gm should be equal
                sref_g = S0g(c.CAS)
            if c.phase_STP == "l":
                sref_g = S0g(c.CAS)

            # Liquid phase properties
            href_l = Hfl(c.CAS)
            if c.phase_STP == "g":
                # S0m and S0gm should be equal, but we need the liquid one
                S0gm = S0g(c.CAS)
                sref_l = S0_basis_converter(c, S0_gas=S0gm)
            if c.phase_STP == "l":
                # S0m should be liquid value
                sref_l = c.S0m
            # Sometimes sref_l is not available
            if sref_l is None and sref_g is not None:
                sref_l = sref_g
                print(f"WARNING: No reference entropy (liquid) found for {c}, using sref_l = sref_g")
            # Sometimes href_l is not available e.g. for hydrogen -> Estimation
            # TODO: This is potentially not a very reliable fix
            if href_l is None and href_g is not None:
                # Latent heat at boiling point
                Hvap = c.Hvap_Tb  # J/mol
                if Hvap:
                    href_l = href_g - Hvap
                else:
                    # If Hvap is not available, set to gas phase value
                    href_l = href_g
                    # Print warning
                    print(
                        f"WARNING: No reference enthalpy (liquid) latent and no heat (Hvap) found for {c}, using href_l = href_g"
                    )

                # Optional: Cp correction (rough, 298K vs Tb)
                if c.Tb and c.Cplm and c.Cpgm:
                    dCp = c.Cplm - c.Cpgm  # J/mol/K
                    dT = 298.15 - c.Tb
                    href_l += dCp * dT

            # If one of the formation properties is still None, print a warning
            if href_g is None or sref_g is None or href_l is None or sref_l is None:
                print(f"WARNING: Incomplete formation properties found for {c}")

        return href_g, sref_g, href_l, sref_l

    def _process_vapor_phase(self, comp, correlations, idx, comp_name, c, href, sref, temperature_range):
        """
        Process vapor phase properties.

        Args:
            comp: Component dictionary to update
            correlations: Chemical correlations package
            idx: Index of component in correlations
            comp_name: Component name for logging
            c: Chemical object
            href: Formation enthalpy at standard conditions
            sref: Formation entropy at standard conditions
            temperature_range: Temperature range for correlations
        """
        hc_gas = correlations.HeatCapacityGases[idx]
        hc_liq = correlations.HeatCapacityLiquids[idx]

        method = self.selector.select_vapor_method(hc_gas, comp_name, temperature_range)
        if method:
            self.applicator.apply_vapor_method(
                comp, hc_gas, hc_liq, method, href, sref, temperature_range, comp_name, c.CAS
            )

    def _process_liquid_phase(self, comp, correlations, idx, comp_name, c, href, sref, temperature_range=None):
        """
        Process liquid phase properties.

        Args:
            comp: Component dictionary to update
            correlations: Chemical correlations package
            idx: Index of component in correlations
            comp_name: Component name for logging
            c: Chemical object
            href: Formation enthalpy at standard conditions for liquid
            sref: Formation entropy at standard conditions for liquid
            temperature_range: Optional temperature range for correlations
        """
        hc_liq = correlations.HeatCapacityLiquids[idx]

        # Select and apply liquid heat capacity method
        method = self.selector.select_liquid_method(hc_liq, comp_name, temperature_range)
        if method:
            self.applicator.apply_liquid_method(comp, hc_liq, method, href, sref, temperature_range, c.CAS)
        else:
            try:
                # TODO: Query constant value
                method = "Constant_fallback"
                self.applicator.apply_liquid_method(comp, hc_liq, method, href, sref, temperature_range, c.CAS)
                print(f"WARNING: Using constant value for liquid heat capacity of {comp_name}")
            except Exception as e:
                raise ValueError(f"No liquid heat capacity option found for {comp_name}: ERROR: {str(e)}")

        # Process density separately
        vl = correlations.VolumeLiquids[idx]
        density_method = self.selector.select_density_method(vl, comp_name, temperature_range)
        if density_method:
            self.applicator.apply_density_method(comp, vl, density_method, c.CAS, temperature_range=temperature_range)
        else:
            try:
                density_method = "Constant_fallback"
                self.applicator.apply_density_method(
                    comp, vl, density_method, c.CAS, temperature_range=temperature_range
                )
                print(f"WARNING: Using constant value for liquid density of {comp_name}")
            except Exception as e:
                raise ValueError(f"No liquid density option found for {comp_name}: ERROR: {str(e)}")

    def _process_vle(self, comp, correlations, idx, comp_name, c, temperature_range=None):
        """
        Process vapor-liquid equilibrium properties.

        Args:
            comp: Component dictionary to update
            correlations: Chemical correlations package
            idx: Index of component in correlations
            comp_name: Component name for logging
            c: Chemical object
            temperature_range: Optional temperature range for correlations
        """
        psat = correlations.VaporPressures[idx]
        method = self.selector.select_psat_method(psat, comp_name, temperature_range)
        if method:
            # Pass component name and CAS number for LLM-based extraction
            self.applicator.apply_psat_method(comp, temperature_range, psat, method, comp_name, c.CAS)
            # No need to set pressure_sat_comp here as it's set in apply_psat_method
            # Just make sure it's a valid model and not just a method name
            if method == "ANTOINE_WEBBOOK" and "pressure_sat_comp" not in comp:
                comp["pressure_sat_comp"] = NIST
            elif method == "HEOS_FIT" and "pressure_sat_comp" not in comp:
                comp["pressure_sat_comp"] = HEOSFitGAS


def build_property_config(
    comp_phases: Dict[str, List[str]],
    temperature_range: Tuple[float, float],
    eos_config: Optional[Dict[str, Any]] = None,
    component_specific_methods: Optional[Dict[str, Dict[str, List[str]]]] = None,
    state_definition: Optional[str] = "FTPx",
    state_bounds: Optional[Dict[str, Tuple[float, float, float, Any]]] = None,
) -> Dict[str, Any]:
    """
    Build a complete property configuration for IDAES, including skeleton configuration
    and filled property data in a single function.

    Args:
        comp_phases: Dictionary mapping component names to their specific phase lists
                    Example: {'H2O': ['Vap', 'Liq'], 'N2': ['Vap']}
        temperature_range: Tuple of (min, max) temperature in K for property correlations
        eos_config: Dictionary specifying equation of state configuration
                    Example: {
                        'Vap': {'type': 'cubic', 'cubic_type': 'PR'},
                        'Liq': {'type': 'ideal'}
                    }
                    or simply {'type': 'ideal'} to apply to all phases
        component_specific_methods: Optional dictionary specifying methods for specific components:
            {
                'H2O': {
                    'vapor': ['HEOS_FIT'],
                },
                'CO2': {
                    'vapor': ['HEOS_FIT'],
                    'psat': ['ANTOINE_WEBBOOK']
                }
            }
            These override the global property_methods for the specified components.
        state_definition: The state variables used in IDAES simulations. Options are:
            - FTPx (Flow rate, temperature, pressure, component mole fractions)
            - FpcTP (component and phase specific flow rates, temperature, pressure)
        state_bounds: The state variable bounds matching the state_definition.
            For each state variable, provide lower bound, initial guess, upper bound, and units.
            (Temperature bounds should match approximately the specified range)
            - Example for FTPx:
            {
            "flow_mol": (0.0, 100, 2000, pyunits.mol / pyunits.s),
            "temperature": (5.15, 300, 2500, pyunits.K),
            "pressure": (1e3, 1e5, 1e8, pyunits.Pa),
            }
            - Example for FpcTP:
            {
            "flow_mol_phase_comp": (0.0, 100, 2000, pyunits.mol / pyunits.s),
            "temperature": (5.15, 300, 2500, pyunits.K),
            "pressure": (1e3, 1e5, 1e8, pyunits.Pa),
            }

    Returns:
        config_dict: Complete configuration dictionary for IDAES-based simulations.

    Notes:
        - Phase equilibrium terms (phases_in_equilibrium, phase_equilibrium_state,
          bubble_dew_method) are only added if at least one component exists in both
          vapor and liquid phases.
        - Each component's phase_equilibrium_form is set based on its specific phases
          and the EOS configuration.
        - Available methods for properties:
          - Vapor phase: HEOS_FIT, POLING_POLY, POLYFIT
          - Liquid phase: HEOS_FIT
          - Density: HEOS_FIT
          - Vapor pressure: ANTOINE_WEBBOOK, HEOS_FIT
        - Fallback is a polynomial fit to IDAES forms from sampled data from thermo python package.
    """
    # PART 1: Build skeleton configuration

    # Collect all unique phases used across all components
    all_phases = set()
    for name, phase_list in comp_phases.items():
        all_phases.update(phase_list)

    # Set default EOS if not provided
    if eos_config is None:
        eos_config = {"type": "ideal"}

    # Load the respective state definitions and state bounds
    # TODO: Define more
    if state_definition == "FTPx":
        state_definition_idaes = FTPx
        # If the user does not input state_bounds, default values
        if not state_bounds:
            state_bounds = {
                "flow_mol": (0.0, 100, 2000, pyunits.mol / pyunits.s),
                "temperature": (5.15, 300, 2500, pyunits.K),
                "pressure": (1e3, 1e5, 1e8, pyunits.Pa),
            }
            print(
                "WARNING: No state bounds provided causes default values to be used, which might lead to convergence issues when solving units."
            )
        else:
            assert "flow_mol" in state_bounds
            assert "temperature" in state_bounds
            assert "pressure" in state_bounds
    elif state_definition == "FpcTP":
        # flow mol phase comp requires specification of the phase specific component molar flows
        state_definition_idaes = FpcTP
        if not state_bounds:
            state_bounds = {
                "flow_mol_phase_comp": (0.0, 100, 2000, pyunits.mol / pyunits.s),
                "temperature": (5.15, 300, 2500, pyunits.K),
                "pressure": (1e3, 1e5, 1e8, pyunits.Pa),
            }
            print(
                "WARNING: No state bounds provided causes default values to be used, which might lead to convergence issues when solving units."
            )
        else:
            assert "flow_mol_phase_comp" in state_bounds
            assert "temperature" in state_bounds
            assert "pressure" in state_bounds
    # Initialize configuration dictionary
    config_dict = {
        "components": {
            # Pass component-specific phases to build_component_skeleton
            name: build_component_skeleton(name, phase_list, eos_config)
            for name, phase_list in comp_phases.items()
        },
        "base_units": {
            "time": pyunits.s,
            "length": pyunits.m,
            "mass": pyunits.kg,
            "amount": pyunits.mol,
            "temperature": pyunits.K,
        },
        "state_definition": state_definition_idaes,
        "state_bounds": state_bounds,
        "pressure_ref": (1e5, pyunits.Pa),
        "temperature_ref": (298.15, pyunits.K),
        "phases": {},
        "phases_in_equilibrium": [],
        "phase_equilibrium_state": {},
        "bubble_dew_method": None,
    }

    # Define phase mapping
    phase_mapping = {"Vap": VaporPhase, "Liq": LiquidPhase}

    # Configure phases with appropriate equation of state
    # TODO: Add activity coefficient model for NRTL
    for phase_name in all_phases:
        if phase_name in phase_mapping:
            # Get phase type from mapping
            phase_type = phase_mapping[phase_name]

            # Add phase to config
            config_dict["phases"][phase_name] = {"type": phase_type}

            # Get EOS config for this phase, fall back to global config if not specific
            phase_eos = eos_config.get(phase_name, eos_config)

            # Configure equation of state based on type
            if isinstance(phase_eos, dict):
                eos_type = phase_eos.get("type", "ideal").lower()
            else:
                eos_type = "ideal"  # Default if no valid config found

            if eos_type == "ideal":
                config_dict["phases"][phase_name]["equation_of_state"] = Ideal
            # Cubic is only possible for vapor
            elif eos_type == "cubic":
                if phase_name == "Vap":
                    config_dict["phases"][phase_name]["equation_of_state"] = Cubic
                    # Get cubic type (PR or SRK) - directly using CubicType
                    cubic_type = phase_eos.get("cubic_type", "")
                    if cubic_type == "PR":
                        cubic_type = CubicType.PR
                    elif cubic_type == "SRK":
                        cubic_type = CubicType.SRK
                    else:
                        print(f"Unknown cubic type for {phase_name}: {cubic_type}. Using Peng Robinson")
                        cubic_type = CubicType.PR
                    config_dict["phases"][phase_name]["equation_of_state_options"] = {"type": cubic_type}
                elif phase_name == "Liq":
                    print(
                        f"WARNING: Cubic equation of state is not implemented for liquid phase {phase_name}. Using Ideal."
                    )
                    config_dict["phases"][phase_name]["equation_of_state"] = Ideal

    # Check if any component exists in both vapor and liquid phases
    has_vle_component = False
    for component_phases in comp_phases.values():
        if "Vap" in component_phases and "Liq" in component_phases:
            has_vle_component = True
            break

    # Only set up phase equilibrium if at least one component exists in both phases
    # TODO: non-ideal VLE -> CubicComplementarityVLE, LogBubbleDew
    if has_vle_component:
        config_dict["phases_in_equilibrium"] = [("Vap", "Liq")]
        config_dict["phase_equilibrium_state"] = {("Vap", "Liq"): SmoothVLE}
        config_dict["bubble_dew_method"] = IdealBubbleDew

    # PART 2: Fill property data

    method_rankings = PropertyMethodRankings.get_rankings()

    # Create main processor with global method rankings
    processor = PropertyDataProcessor(method_rankings)

    # Get configuration components dictionary
    components_cfg = config_dict["components"]
    components_ids = list(components_cfg.keys())
    constants, correlations = ChemicalConstantsPackage.from_IDs(components_ids)

    # Process each component
    for comp_name, comp in components_cfg.items():
        # Check if component has specific method preferences
        comp_specific_rankings = None
        if component_specific_methods and comp_name in component_specific_methods:
            comp_methods = component_specific_methods[comp_name]
            print(f"Using component-specific methods for {comp_name}: {comp_methods}")

            # Create component-specific method rankings
            comp_specific_rankings = (
                method_rankings.copy() if method_rankings else PropertyMethodRankings.get_rankings()
            )

            # Update with component-specific methods if they are specified
            for category, methods in comp_methods.items():
                if category in comp_specific_rankings and methods:
                    comp_specific_rankings[category] = methods

            # Create a component-specific processor
            comp_processor = PropertyDataProcessor(comp_specific_rankings)

            # Use component-specific processor for this component
            c = Chemical(comp_name)
            idx = components_ids.index(comp_name)
            comp_processor.process_component(comp, comp_name, c, constants, correlations, idx, temperature_range)
        else:
            # Use the global processor for components without specific methods
            c = Chemical(comp_name)
            idx = components_ids.index(comp_name)
            processor.process_component(comp, comp_name, c, constants, correlations, idx, temperature_range)

    # Add parameters needed for cubic equations of state if used (on general level, not per component)
    # Check if any phase uses cubic EOS
    cubic_type = None
    uses_cubic_eos = False

    # Iterate over all phases in the config
    for phase_name, phase_config in config_dict["phases"].items():
        # Check if this phase uses cubic EOS
        if phase_config.get("equation_of_state") == Cubic:
            uses_cubic_eos = True
            # Get the cubic type from the first cubic phase we find
            if cubic_type is None:
                cubic_type = phase_config["equation_of_state_options"]["type"]
            break

    # If any phase uses cubic EOS, add required parameters
    if uses_cubic_eos and len(components_ids) > 1:
        # Dictionary to store binary interaction parameters
        binary_params = {}

        # Determine parameter name based on cubic type
        if cubic_type == CubicType.PR:
            param_name = "PR_kappa"
        elif cubic_type == CubicType.SRK:
            param_name = "SRK_kappa"
        else:
            param_name = "PR_kappa"  # Default to PR if not specified

        # Get binary interaction parameters from IPDB for all component pairs
        for i, comp1 in enumerate(components_ids):
            for j, comp2 in enumerate(components_ids):
                # Get CAS numbers for the components
                cas1 = Chemical(comp1).CAS if hasattr(Chemical(comp1), "CAS") else None
                cas2 = Chemical(comp2).CAS if hasattr(Chemical(comp2), "CAS") else None

                # Default to 0.0 for same component
                if i == j:
                    kij = 0.0
                    binary_params[(comp1, comp2)] = kij
                    continue

                # Try to get kij from IPDB
                kij = 0.0
                if cas1 and cas2:
                    try:
                        kij = IPDB.get_ip(cas1, cas2, "kij")
                    except Exception as e:
                        LOGGER.debug(
                            "IPDB lookup failed for %s-%s (CAS: %s, %s): %s; using default kij=0.0",
                            comp1, comp2, cas1, cas2, e,
                            exc_info=True,
                        )
                        kij = 0.0

                # If no kij found, use 0.0 with a warning
                if kij is None:
                    print(f"WARNING: No binary interaction parameter found for {comp1}-{comp2}, using default kij=0.0")
                    kij = 0.0

                binary_params[(comp1, comp2)] = kij

        # Add the binary interaction parameters to the config
        config_dict["parameter_data"] = config_dict.get("parameter_data", {})
        config_dict["parameter_data"][param_name] = binary_params

    return config_dict
