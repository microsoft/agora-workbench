"""
DWSIM Results Extraction Tools.

Read simulation results from a solved DWSIM flowsheet — stream conditions,
unit-operation duties, and high-level flowsheet summaries.

All data is read through the ISimulationObject interface using
``GetPropertyValue("PROP_XX_N")`` (numeric properties) and
``__implementation__`` (phase/composition data on material streams).

Key material-stream property codes (SI units):
    PROP_MS_0  – Temperature (K)
    PROP_MS_1  – Pressure (Pa)
    PROP_MS_2  – Mass flow (kg/s)
    PROP_MS_3  – Molar flow (mol/s)
    PROP_MS_4  – Volumetric flow (m³/s)

Key heater/cooler property codes:
    PROP_HT_3 / PROP_CL_3 – Duty (kW)
"""

from __future__ import annotations

import logging

LOGGER = logging.getLogger(__name__)


def get_stream_results(flowsheet: object, stream_name: str) -> dict:
    """
    Return thermodynamic properties and compositions for a material stream.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    stream_name : str
        Tag of the material stream to query.

    Returns
    -------
    dict
        ``{"success", "temperature", "pressure", "total_molar_flow",
        "total_mass_flow", "vapor_fraction", "phase_compositions", "error"}``
    """
    result = {
        "success": False,
        "temperature": None,
        "pressure": None,
        "total_molar_flow": None,
        "total_mass_flow": None,
        "vapor_fraction": None,
        "phase_compositions": None,
        "error": None,
    }

    try:
        stream = flowsheet.GetFlowsheetSimulationObject(stream_name)
        if stream is None:
            result["error"] = f"Stream '{stream_name}' not found in flowsheet."
            return result

        # Read scalar properties via PROP codes
        result["temperature"] = float(stream.GetPropertyValue("PROP_MS_0"))  # K
        result["pressure"] = float(stream.GetPropertyValue("PROP_MS_1"))  # Pa
        result["total_mass_flow"] = float(stream.GetPropertyValue("PROP_MS_2"))  # kg/s
        result["total_molar_flow"] = float(stream.GetPropertyValue("PROP_MS_3"))  # mol/s

        # Vapor fraction from Phase 2 (Vapor) molar fraction
        impl = stream.__implementation__
        phases = impl.Phases
        vap_frac = 0.0
        if phases and 2 in [int(k) for k in phases.Keys]:
            vap_phase = phases[2]
            props = vap_phase.Properties
            if props is not None and props.molarfraction is not None:
                vap_frac = float(props.molarfraction)
        result["vapor_fraction"] = vap_frac

        # Per-phase compound compositions
        _PHASE_NAMES = {
            0: "Mixture",
            1: "OverallLiquid",
            2: "Vapor",
            3: "Liquid1",
            4: "Liquid2",
            7: "Solid",
        }
        compositions: dict[str, dict] = {}
        for phase_key, phase_label in _PHASE_NAMES.items():
            try:
                phase = phases[phase_key]
            except (KeyError, IndexError):
                continue
            if phase is None:
                continue
            comp_dict: dict[str, dict] = {}
            for cname in phase.Compounds.Keys:
                c = phase.Compounds[cname]
                comp_dict[str(cname)] = {
                    "mole_fraction": float(c.MoleFraction or 0.0),
                    "mass_fraction": float(c.MassFraction or 0.0),
                }
            if comp_dict:
                compositions[phase_label] = comp_dict

        result["phase_compositions"] = compositions
        result["success"] = True

    except Exception as e:
        result["error"] = f"Failed to get stream results: {e}"

    return result


def get_unit_operation_results(flowsheet: object, unit_name: str) -> dict:
    """
    Return key results for a unit operation (duty, efficiency, etc.).

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    unit_name : str
        Tag of the unit operation to query.

    Returns
    -------
    dict
        ``{"success", "unit_type", "duty", "efficiency", "details", "error"}``
    """
    result = {
        "success": False,
        "unit_type": None,
        "duty": None,
        "efficiency": None,
        "details": None,
        "error": None,
    }

    try:
        from dwsim_tools.clr_helpers import get_automation  # noqa: F401 – ensure CLR
        from DWSIM.Interfaces.Enums import PropertyType

        unit = flowsheet.GetFlowsheetSimulationObject(unit_name)
        if unit is None:
            result["error"] = f"Unit operation '{unit_name}' not found in flowsheet."
            return result

        result["unit_type"] = str(unit.GetType().Name)

        # Read all available properties via GetPropertyValue
        details: dict[str, float | str] = {}
        all_props = list(unit.GetProperties(PropertyType.ALL))
        for prop in all_props:
            try:
                val = unit.GetPropertyValue(prop)
                if val is not None:
                    details[prop] = float(val)
            except (TypeError, ValueError, Exception):
                pass

        # Extract duty and efficiency for common unit types
        type_name = result["unit_type"]
        if type_name == "Heater":
            result["duty"] = details.get("PROP_HT_3")
            result["efficiency"] = details.get("PROP_HT_1")
        elif type_name == "Cooler":
            result["duty"] = details.get("PROP_CL_3")
            result["efficiency"] = details.get("PROP_CL_1")
        elif type_name == "Pump":
            result["duty"] = details.get("PROP_PU_3")
            result["efficiency"] = details.get("PROP_PU_2")
        elif type_name == "Compressor":
            result["duty"] = details.get("PROP_CO_2")
            result["efficiency"] = details.get("PROP_CO_3")
        else:
            # Generic fallback — try the concrete type's DeltaQ / Efficiency
            impl = unit.__implementation__
            for attr in ("DeltaQ", "HeatDuty", "DutyQ"):
                if hasattr(impl, attr):
                    val = getattr(impl, attr, None)
                    if val is not None:
                        result["duty"] = float(val)
                        break
            for attr in ("Efficiency",):
                if hasattr(impl, attr):
                    val = getattr(impl, attr, None)
                    if val is not None:
                        result["efficiency"] = float(val)
                        break

        result["details"] = details
        result["success"] = True

    except Exception as e:
        result["error"] = f"Failed to get unit operation results: {e}"

    return result


def get_flowsheet_summary(flowsheet: object) -> dict:
    """
    Return a high-level summary of the flowsheet.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.

    Returns
    -------
    dict
        ``{"success", "object_list", "convergence_status", "mass_balance",
        "energy_balance", "error"}``
    """
    result = {
        "success": False,
        "object_list": None,
        "convergence_status": None,
        "mass_balance": None,
        "energy_balance": None,
        "error": None,
    }

    try:
        objects = []
        errors: list[str] = []
        for key in flowsheet.SimulationObjects.Keys:
            obj = flowsheet.SimulationObjects[key]
            tag = obj.GraphicObject.Tag if obj.GraphicObject else str(key)
            obj_type = str(obj.GetType().Name)
            has_error = bool(obj.ErrorMessage)
            objects.append(
                {
                    "tag": tag,
                    "type": obj_type,
                    "calculated": bool(obj.Calculated),
                    "has_error": has_error,
                }
            )
            if has_error:
                errors.append(f"{tag}: {obj.ErrorMessage}")

        result["object_list"] = objects
        result["convergence_status"] = "converged" if not errors else "errors"

        # Mass / energy balances — classify streams by connectivity
        total_mass_in = 0.0
        total_mass_out = 0.0

        for key in flowsheet.SimulationObjects.Keys:
            obj = flowsheet.SimulationObjects[key]
            type_name = str(obj.GetType().Name)
            if type_name != "MaterialStream":
                continue
            try:
                mass_flow = float(obj.GetPropertyValue("PROP_MS_2") or 0.0)
            except Exception:
                LOGGER.debug("Could not read PROP_MS_2 for stream '%s'", key, exc_info=True)
                mass_flow = 0.0

            # A stream with nothing connected to its *input* is a feed.
            go = obj.GraphicObject
            if go and go.InputConnectors.Count > 0:
                if not go.InputConnectors[0].IsAttached:
                    total_mass_in += mass_flow
                else:
                    total_mass_out += mass_flow

        result["mass_balance"] = {
            "total_mass_in_kg_s": total_mass_in,
            "total_mass_out_kg_s": total_mass_out,
            "difference_kg_s": total_mass_in - total_mass_out,
        }

        result["success"] = True

    except Exception as e:
        result["error"] = f"Failed to get flowsheet summary: {e}"

    return result
