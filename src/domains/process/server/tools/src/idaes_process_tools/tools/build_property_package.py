"""
Tool to build IDAES property packages with complete method implementations.

This tool uses the property_generation module to create fully configured
property packages that include all necessary thermodynamic correlations
and methods. The resulting PropertyPackageConfig is stored in the session's
object registry and can be referenced by flowsheet building tools.
"""

from typing import Dict, List, Optional, Any, Tuple

from ..property_generation.build_property_config import build_property_config


def build_idaes_property_package(
    comp_phases: Dict[str, List[str]],
    temperature_range: Tuple[float, float],
    eos_config: Optional[Dict[str, Any]] = None,
    component_specific_methods: Optional[Dict[str, Dict[str, List[str]]]] = None,
    state_definition: str = "FTPx",
    state_bounds: Optional[Dict[str, Tuple[float, float, float, Any]]] = None,
) -> dict:
    """
    Build a complete IDAES property package configuration with method implementations.

    This function creates a PropertyPackageConfig that includes all necessary
    thermodynamic correlations and methods for the specified components and phases.
    Unlike simple dictionary-based configs, this includes actual method implementations
    for properties like Cp, density, vapor pressure, etc.

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
            Example for FTPx:
            {
            "flow_mol": (0.0, 100, 2000, pyunits.mol / pyunits.s),
            "temperature": (5.15, 300, 2500, pyunits.K),
            "pressure": (1e3, 1e5, 1e8, pyunits.Pa),
            }

    Returns:
        dict: Contains the property package config handle
            {
                "property_config": "<handle_id>"  # PropertyPackageConfig object handle
            }

    Example:
        >>> config = build_idaes_property_package(
        ...     comp_phases={"H2O": ["Vap", "Liq"], "N2": ["Vap"]},
        ...     temperature_range=(273.15, 500.0),
        ...     eos_config={"type": "ideal"},
        ... )
        >>> # config["property_config"] is a handle to the PropertyPackageConfig

    Notes:
        - The property package is built using automated correlation fitting from
          thermodynamic databases (NIST, thermo package, etc.)
        - Available methods include HEOS_FIT, POLING_POLY, ANTOINE_WEBBOOK, etc.
        - The config_dict includes complete parameter_data and method implementations
        - Use this property_config handle with build_flowsheet tool
    """
    # Build the complete property configuration
    config_dict = build_property_config(
        comp_phases=comp_phases,
        temperature_range=temperature_range,
        eos_config=eos_config,
        component_specific_methods=component_specific_methods,
        state_definition=state_definition,
        state_bounds=state_bounds,
    )

    # Return the config_dict itself
    return {"property_config": config_dict}
