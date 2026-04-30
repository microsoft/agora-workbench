"""
DWSIM Stream Tools.

Add material and energy streams to a DWSIM flowsheet.

Property-code reference for material streams (SI units):
    PROP_MS_0  – Temperature (K)
    PROP_MS_1  – Pressure (Pa)
    PROP_MS_2  – Mass flow (kg/s)
    PROP_MS_3  – Molar flow (mol/s)
    PROP_MS_102/<compound> – Overall mole fraction for *compound*
"""

import json


def add_material_stream(
    flowsheet: object,
    name: str,
    temperature: float,
    pressure: float,
    compound_mole_fractions: str,
    total_molar_flow: float,
) -> dict:
    """
    Add a material stream to the flowsheet.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Display name / tag for the stream.
    temperature : float
        Stream temperature in **Kelvin**.
    pressure : float
        Stream pressure in **Pascal**.
    compound_mole_fractions : str
        JSON object mapping compound name → mole fraction, e.g.
        ``'{"Water": 0.5, "Ethanol": 0.5}'``. Fractions should sum to 1.
    total_molar_flow : float
        Total molar flow rate in **mol/s**.

    Returns
    -------
    dict
        ``{"success": bool, "stream_name": str | None, "error": str | None}``
    """
    result = {"success": False, "stream_name": None, "error": None}

    try:
        from dwsim_tools.clr_helpers import get_automation  # noqa: F401 – ensures CLR loaded

        from DWSIM.Interfaces.Enums.GraphicObjects import ObjectType

        # Parse mole-fraction mapping
        try:
            fractions = json.loads(compound_mole_fractions)
        except json.JSONDecodeError as je:
            result["error"] = f"Invalid compound_mole_fractions JSON: {je}"
            return result

        if not isinstance(fractions, dict) or not fractions:
            result["error"] = (
                "compound_mole_fractions must be a non-empty JSON object "
                "mapping compound names to numeric fractions, e.g. "
                '\'{"Water": 0.5, "Ethanol": 0.5}\'. '
                f"Got: {type(fractions).__name__}"
            )
            return result

        for compound, frac in fractions.items():
            try:
                frac_value = float(frac)
            except (TypeError, ValueError):
                result["error"] = f"Mole fraction for '{compound}' is not numeric: {frac!r}"
                return result
            if not (0.0 <= frac_value <= 1.0):
                result["error"] = (
                    f"Mole fraction for '{compound}' must be between 0 and 1 inclusive; got {frac_value!r}"
                )
                return result
            fractions[compound] = frac_value

        total = sum(fractions.values())
        if abs(total - 1.0) > 0.01:
            import logging

            logging.getLogger(__name__).warning(
                "compound_mole_fractions sum to %.4f (expected 1.0) for "
                "stream '%s'. DWSIM may not normalise these automatically.",
                total,
                name,
            )

        # Create the material stream via the flowsheet object
        stream = flowsheet.AddObject(ObjectType.MaterialStream, 0, 0, name)

        # Assign thermodynamic state via property codes
        stream.SetPropertyValue("PROP_MS_0", float(temperature))  # K
        stream.SetPropertyValue("PROP_MS_1", float(pressure))  # Pa
        stream.SetPropertyValue("PROP_MS_3", float(total_molar_flow))  # mol/s

        # Assign per-compound overall mole fractions
        for compound_name, frac in fractions.items():
            stream.SetPropertyValue(f"PROP_MS_102/{compound_name}", float(frac))

        result["stream_name"] = name
        result["success"] = True

    except Exception as e:
        result["error"] = f"Failed to add material stream: {e}"

    return result


def add_energy_stream(flowsheet: object, name: str) -> dict:
    """
    Add an energy stream to the flowsheet.

    Energy streams carry heat or work between unit operations (e.g. a
    heater duty, pump work).

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Display name / tag for the energy stream.

    Returns
    -------
    dict
        ``{"success": bool, "stream_name": str | None, "error": str | None}``
    """
    result = {"success": False, "stream_name": None, "error": None}

    try:
        from dwsim_tools.clr_helpers import get_automation  # noqa: F401 – ensures CLR loaded

        from DWSIM.Interfaces.Enums.GraphicObjects import ObjectType

        flowsheet.AddObject(ObjectType.EnergyStream, 0, 0, name)

        result["stream_name"] = name
        result["success"] = True

    except Exception as e:
        result["error"] = f"Failed to add energy stream: {e}"

    return result
