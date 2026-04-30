from typing import Any, Union

from idaes.core import Component, LiquidPhase, Phase, SolidPhase, VaporPhase
from idaes.models.properties.modular_properties.eos.ideal import Ideal
from idaes.models.properties.modular_properties.state_definitions import FTPx, FpcTP
from pyomo.environ import units as pyunits

from ..builder import IdaesFlowsheetBuilder
from ..schema import FlowsheetConfig
from ..units import Quantity, PyomoUnit


def _convert_unit_string(unit_str: str):
    """Convert a unit string to a pyomo unit object."""
    # Common unit conversions for IDAES parameter_data
    unit_map = {
        "g/mol": pyunits.g / pyunits.mol,
        "kg/mol": pyunits.kg / pyunits.mol,
        "Pa": pyunits.Pa,
        "K": pyunits.K,
        "J/mol": pyunits.J / pyunits.mol,
        "J/(mol*K)": pyunits.J / (pyunits.mol * pyunits.K),
        "m": pyunits.m,
        "m3/mol": pyunits.m**3 / pyunits.mol,
        "mol/s": pyunits.mol / pyunits.s,
        "kmol/s": pyunits.kmol / pyunits.s,
    }
    return unit_map.get(unit_str, pyunits.dimensionless)


def _convert_idaes_types(config: dict) -> dict:
    """
    Convert string type references to actual IDAES classes in property package configs.

    IDAES expects actual class references (Component, VaporPhase, Ideal, etc.) but
    JSON can only contain strings. This function maps string names to the actual classes.
    Also converts lists to tuples for parameter_data values since IDAES expects tuples.
    """
    # Only process property_packages
    if "property_packages" not in config:
        return config

    # Type mapping for common IDAES classes
    type_map = {
        "Component": Component,
        "Phase": Phase,
        "LiquidPhase": LiquidPhase,
        "VaporPhase": VaporPhase,
        "SolidPhase": SolidPhase,
        "Ideal": Ideal,
    }

    # State definition mapping
    state_def_map = {
        "FTPx": FTPx,
        "FpcTP": FpcTP,
    }

    for pp in config.get("property_packages", []):
        if "config_dict" not in pp:
            continue

        config_dict = pp["config_dict"]

        # Convert component types and parameter_data
        if "components" in config_dict:
            for comp_name, comp_config in config_dict["components"].items():
                if "type" in comp_config and isinstance(comp_config["type"], str):
                    comp_config["type"] = type_map.get(comp_config["type"], Component)

                # Convert parameter_data lists to tuples (IDAES expects tuples)
                # Also convert unit strings to pyomo unit objects
                if "parameter_data" in comp_config:
                    for param_name, param_value in comp_config["parameter_data"].items():
                        if isinstance(param_value, list):
                            # Convert list to tuple and unit string to unit object
                            if len(param_value) == 2:
                                value, unit_str = param_value
                                unit_obj = _convert_unit_string(unit_str)
                                comp_config["parameter_data"][param_name] = (value, unit_obj)
                            else:
                                comp_config["parameter_data"][param_name] = tuple(param_value)
                        elif isinstance(param_value, tuple) and len(param_value) == 2:
                            # Already a tuple, but may need to convert unit string
                            value, unit = param_value
                            if isinstance(unit, str):
                                unit_obj = _convert_unit_string(unit)
                                comp_config["parameter_data"][param_name] = (value, unit_obj)

        # Convert phase types and equation_of_state
        if "phases" in config_dict:
            for phase_name, phase_config in config_dict["phases"].items():
                if "type" in phase_config and isinstance(phase_config["type"], str):
                    phase_config["type"] = type_map.get(phase_config["type"], Phase)
                if "equation_of_state" in phase_config and isinstance(phase_config["equation_of_state"], str):
                    phase_config["equation_of_state"] = type_map.get(phase_config["equation_of_state"], Ideal)

        # Convert state_definition from string to class
        if "state_definition" in config_dict and isinstance(config_dict["state_definition"], str):
            config_dict["state_definition"] = state_def_map.get(config_dict["state_definition"], FTPx)

        # Convert top-level tuples (pressure_ref, temperature_ref, state_bounds)
        # Also convert unit strings to unit objects
        for key in ["pressure_ref", "temperature_ref"]:
            if key in config_dict:
                value = config_dict[key]
                if isinstance(value, list) and len(value) == 2:
                    val, unit_str = value
                    unit_obj = _convert_unit_string(unit_str)
                    config_dict[key] = (val, unit_obj)
                elif isinstance(value, tuple) and len(value) == 2:
                    val, unit = value
                    if isinstance(unit, str):
                        unit_obj = _convert_unit_string(unit)
                        config_dict[key] = (val, unit_obj)

        # Convert state_bounds nested lists to tuples and unit strings to unit objects
        if "state_bounds" in config_dict:
            for bound_name, bound_value in config_dict["state_bounds"].items():
                if isinstance(bound_value, list) and len(bound_value) == 4:
                    # Format: (lower, init, upper, units)
                    lower, init, upper, unit_str = bound_value
                    unit_obj = _convert_unit_string(unit_str)
                    config_dict["state_bounds"][bound_name] = (lower, init, upper, unit_obj)
                elif isinstance(bound_value, tuple) and len(bound_value) == 4:
                    lower, init, upper, unit = bound_value
                    if isinstance(unit, str):
                        unit_obj = _convert_unit_string(unit)
                        config_dict["state_bounds"][bound_name] = (lower, init, upper, unit_obj)

    return config


def _convert_quantity_dict(value: Any) -> Any:
    """
    Recursively convert quantity dictionaries to Quantity objects.

    Looks for dicts with 'value' and 'units' keys and converts them to Quantity objects.
    Also handles nested dicts and lists.
    """
    if isinstance(value, dict):
        # Check if this is a quantity dict
        if "value" in value and "units" in value:
            # Convert units string to PyomoUnit attribute
            unit_str = value["units"]
            # Handle common unit patterns
            unit_map = {
                "K": PyomoUnit.K,
                "Pa": PyomoUnit.Pa,
                "mol/s": PyomoUnit.mol_per_s,
                "mol_per_s": PyomoUnit.mol_per_s,
                "dimensionless": PyomoUnit.dimensionless,
                "g/mol": PyomoUnit.g_per_mol,
                "g_per_mol": PyomoUnit.g_per_mol,
            }

            unit = unit_map.get(unit_str)
            if unit is None:
                raise ValueError(f"Unknown unit: {unit_str}")

            return Quantity(value["value"], unit)
        else:
            # Recursively process nested dicts
            return {k: _convert_quantity_dict(v) for k, v in value.items()}
    elif isinstance(value, list):
        # Recursively process lists
        return [_convert_quantity_dict(item) for item in value]
    else:
        # Return primitive types as-is
        return value


def build_idaes_flowsheet(
    flowsheet_config: Union[FlowsheetConfig, dict], property_config=None
) -> dict[str, IdaesFlowsheetBuilder]:
    """
    Build an IDAES flowsheet from a configuration.

    This tool creates an IDAES ConcreteModel from a FlowsheetConfig by:
    1. Instantiating an IdaesFlowsheetBuilder with the config
    2. Calling build() to construct the model with all property packages, units, and connections

    Args:
        flowsheet_config: FlowsheetConfig object or dict defining the flowsheet structure,
                         property packages, unit operations, streams, and connections
        property_config: Optional property package config dict (or handle) to inject into the
                        flowsheet. If provided, this will replace the property_packages in the config.
                        This should be a complete config_dict from build_idaes_property_package.

    Returns:
        dict with "builder" key containing the IdaesFlowsheetBuilder.
        All property packages, unit operations, and material blocks are accessible via model.fs
    """
    # Convert dict to FlowsheetConfig if needed
    if isinstance(flowsheet_config, dict):
        # If property_config is provided, inject it FIRST before any processing
        # property_config already has correct IDAES types from build_property_config
        if property_config is not None:
            # Add the property package config with the pre-built property_config
            flowsheet_config["property_packages"] = [{"name": "props", "config_dict": property_config}]
        elif "property_packages" not in flowsheet_config:
            # Ensure property_packages exists even if empty
            flowsheet_config["property_packages"] = []

        # Now convert string type references to IDAES classes (skip property_packages since already done)
        flowsheet_config = _convert_idaes_types(flowsheet_config)

        # Then, convert all quantity dicts to Quantity objects
        processed_config = _convert_quantity_dict(flowsheet_config)
        # Finally, instantiate FlowsheetConfig
        flowsheet_config = FlowsheetConfig(**processed_config)

    # Create the builder with the provided configuration
    builder = IdaesFlowsheetBuilder(flowsheet_config)

    # Build the flowsheet model
    builder.build()

    return {"builder": builder}
