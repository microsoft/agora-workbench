"""
DWSIM Unit-Operation Tools.

Add and configure common process equipment in a DWSIM flowsheet.
Every function follows the same return convention:
``{"success": bool, "unit_name": str | None, "error": str | None}``

Key API patterns used throughout this module
--------------------------------------------
* ``flowsheet.AddObject(ObjectType.<type>, x, y, tag)`` creates objects.
* ``flowsheet.ConnectObjects(src.GraphicObject, dst.GraphicObject, -1, -1)``
  wires them together (``-1`` = first available port).
* ``obj.SetPropertyValue("PROP_XX_N", float_value)`` sets numeric properties.
* Enum-based settings (e.g. ``CalcMode``) **cannot** be set via
  ``SetPropertyValue``; they must be assigned on the **concrete** .NET type
  accessed through ``obj.__implementation__``.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_object_type():
    """Lazy-import the ObjectType enum (requires CLR to be loaded)."""
    from DWSIM.Interfaces.Enums.GraphicObjects import ObjectType

    return ObjectType


def _lookup(flowsheet, tag: str):
    """Return the ISimulationObject with the given *tag*."""
    return flowsheet.GetFlowsheetSimulationObject(tag)


def _connect(flowsheet, src_obj, dst_obj) -> None:
    """Wire *src_obj* → *dst_obj* using first available ports."""
    flowsheet.ConnectObjects(src_obj.GraphicObject, dst_obj.GraphicObject, -1, -1)


def _connect_inlet(flowsheet, unit, stream_tag: str) -> None:
    """Connect an inlet stream (looked up by *stream_tag*) to *unit*.

    If *stream_tag* is ``None`` or an empty string the call is silently
    skipped, which allows callers to treat energy-stream connections as
    optional.
    """
    if not stream_tag:
        return
    _connect(flowsheet, _lookup(flowsheet, stream_tag), unit)


def _connect_outlet(flowsheet, unit, stream_tag: str, *, create_if_missing: bool = False) -> None:
    """Connect *unit* to an outlet stream (looked up by *stream_tag*).

    If *stream_tag* is ``None`` or an empty string the call is silently
    skipped.  When *create_if_missing* is ``True`` a MaterialStream is
    auto-created when the tag does not yet exist in the flowsheet.
    """
    if not stream_tag:
        return
    if create_if_missing:
        obj = _ensure_material_stream(flowsheet, stream_tag)
    else:
        obj = _lookup(flowsheet, stream_tag)
    _connect(flowsheet, unit, obj)


def _ok(name: str) -> dict:
    return {"success": True, "unit_name": name, "error": None}


def _fail(msg: str) -> dict:
    return {"success": False, "unit_name": None, "error": msg}


def _ensure_clr():
    """Make sure CLR assemblies are loaded (idempotent)."""
    from dwsim_tools.clr_helpers import get_automation  # noqa: F401

    get_automation()


def _parse_names(value: str) -> list[str]:
    """Parse *value* as a JSON array or a comma-separated string.

    Returns a list of non-empty stripped strings.
    """
    import json as _json

    stripped = value.strip()
    if stripped.startswith("["):
        try:
            items = _json.loads(stripped)
            if isinstance(items, list):
                return [str(s).strip() for s in items if str(s).strip()]
        except _json.JSONDecodeError:
            pass
    return [s.strip() for s in value.split(",") if s.strip()]


def _ensure_material_stream(flowsheet, tag: str):
    """Return the object with *tag*, auto-creating a MaterialStream if missing."""
    obj = flowsheet.GetFlowsheetSimulationObject(tag)
    if obj is not None:
        return obj
    from DWSIM.Interfaces.Enums.GraphicObjects import ObjectType

    return flowsheet.AddObject(ObjectType.MaterialStream, 0, 0, tag)


def _normalize_rxn_def(reaction_set: str) -> dict:
    """Parse and normalise a reaction-set JSON string.

    Accepts a JSON object **or** a single-element array.  Also accepts
    ``"base"`` as an alias for ``"base_compound"`` and ``"Keq"`` as an
    alias for ``"Keq_expression"``.
    """
    import json as _json

    rxn = _json.loads(reaction_set)
    if isinstance(rxn, list):
        if len(rxn) == 0:
            raise ValueError("reaction_set array is empty")
        rxn = rxn[0]
    if "base" in rxn and "base_compound" not in rxn:
        rxn["base_compound"] = rxn.pop("base")
    if "Keq" in rxn and "Keq_expression" not in rxn:
        rxn["Keq_expression"] = rxn.pop("Keq")
    return rxn


_VALID_REACTION_PHASES = {"Liquid", "Vapor"}

_KNOWN_CONVERSION_KEYS = {
    "base_compound",
    "base",
    "conversion",
    "stoichiometry",
    "reaction_phase",
}
_KNOWN_EQUILIBRIUM_KEYS = {
    "base_compound",
    "base",
    "stoichiometry",
    "Keq_expression",
    "Keq",
    "reaction_phase",
}


def _validate_reaction_common(rxn_def: dict, context: str) -> str | None:
    """Validate keys shared by both reactor types.

    Returns an error message string, or ``None`` if valid.
    """
    if not rxn_def.get("base_compound"):
        return f"{context}: 'base_compound' is missing or empty in reaction_set. Got keys: {sorted(rxn_def.keys())}"

    stoich = rxn_def.get("stoichiometry")
    if not stoich or not isinstance(stoich, dict):
        return f"{context}: 'stoichiometry' is missing or empty in reaction_set."

    base = rxn_def["base_compound"]
    if base not in stoich:
        return f"{context}: base_compound '{base}' is not present in stoichiometry keys {sorted(stoich.keys())}."

    for compound, coeff in stoich.items():
        try:
            float(coeff)
        except (TypeError, ValueError):
            return f"{context}: stoichiometric coefficient for '{compound}' is not numeric: {coeff!r}"

    phase = rxn_def.get("reaction_phase")
    if phase is not None and phase not in _VALID_REACTION_PHASES:
        return f"{context}: invalid reaction_phase '{phase}'. Must be one of: {sorted(_VALID_REACTION_PHASES)}"

    return None


def _warn_unknown_keys(rxn_def: dict, known: set[str], context: str) -> str | None:
    """Return a warning string if *rxn_def* contains unrecognised keys."""
    unknown = set(rxn_def.keys()) - known
    if unknown:
        return (
            f"{context}: unrecognised key(s) in reaction_set: "
            f"{sorted(unknown)}.  These will be ignored.  "
            f"Valid keys: {sorted(known)}"
        )
    return None


# ---------------------------------------------------------------------------
# Mixer
# ---------------------------------------------------------------------------


def add_mixer(
    flowsheet: object,
    name: str,
    inlet_stream_names: str,
    outlet_stream_name: str,
) -> dict:
    """
    Add a stream mixer.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the mixer.
    inlet_stream_names : str
        Comma-separated inlet stream tags.
    outlet_stream_name : str
        Tag of the outlet stream.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()

        unit = flowsheet.AddObject(OT.NodeIn, 0, 0, name)

        for s in _parse_names(inlet_stream_names):
            _connect_inlet(flowsheet, unit, s)
        _connect_outlet(flowsheet, unit, outlet_stream_name, create_if_missing=True)

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add mixer: {e}")


# ---------------------------------------------------------------------------
# Splitter
# ---------------------------------------------------------------------------


def add_splitter(
    flowsheet: object,
    name: str,
    inlet_stream_name: str,
    outlet_stream_names: str,
    split_ratios: str,
) -> dict:
    """
    Add a stream splitter.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the splitter.
    inlet_stream_name : str
        Tag of the inlet stream.
    outlet_stream_names : str
        Comma-separated outlet stream tags.
    split_ratios : str
        Comma-separated split ratios (should sum to 1).

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()

        unit = flowsheet.AddObject(OT.NodeOut, 0, 0, name)

        _connect_inlet(flowsheet, unit, inlet_stream_name)

        outlets = _parse_names(outlet_stream_names)
        ratios = [float(r) for r in _parse_names(split_ratios)]

        if ratios and len(ratios) != len(outlets):
            return _fail(f"Split ratios length ({len(ratios)}) must match outlet count ({len(outlets)})")

        for i, r in enumerate(ratios):
            if not 0.0 <= r <= 1.0:
                return _fail(f"Split ratio {i + 1} is {r}; each ratio must be between 0 and 1.")

        if ratios:
            total = sum(ratios)
            if abs(total - 1.0) > 0.01:
                import logging

                logging.getLogger(__name__).warning(
                    "Split ratios sum to %.4f (expected 1.0) for splitter '%s'.",
                    total,
                    name,
                )

        for out_name in outlets:
            _connect_outlet(flowsheet, unit, out_name, create_if_missing=True)

        # Set split ratios on the concrete Splitter type
        if ratios:
            # The splitter exposes split ratio properties as SR1, SR2, …
            # (1-indexed, matching outlet order).
            for i, r in enumerate(ratios, start=1):
                unit.SetPropertyValue(f"SR{i}", float(r))

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add splitter: {e}")


# ---------------------------------------------------------------------------
# Heater
# ---------------------------------------------------------------------------


def add_heater(
    flowsheet: object,
    name: str,
    inlet_stream_name: str,
    outlet_stream_name: str,
    outlet_temperature: float,
    pressure_drop: float,
    energy_stream_name: str = "",
) -> dict:
    """
    Add a heater / heat-input unit.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the heater.
    inlet_stream_name, outlet_stream_name : str
        Inlet / outlet material-stream tags.
    outlet_temperature : float
        Desired outlet temperature in **Kelvin**.
    pressure_drop : float
        Pressure drop across the heater in **Pascal**.
    energy_stream_name : str
        Tag of the energy stream supplying heat to this unit.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()
        from DWSIM.UnitOperations.UnitOperations import Heater

        unit = flowsheet.AddObject(OT.Heater, 0, 0, name)

        _connect_inlet(flowsheet, unit, inlet_stream_name)
        _connect_outlet(flowsheet, unit, outlet_stream_name, create_if_missing=True)
        _connect_inlet(flowsheet, unit, energy_stream_name)

        # CalcMode enum must be set on concrete type
        unit.__implementation__.CalcMode = Heater.CalculationMode.OutletTemperature
        unit.SetPropertyValue("PROP_HT_2", float(outlet_temperature))  # Outlet T (K)
        unit.SetPropertyValue("PROP_HT_0", float(pressure_drop))  # dP (Pa)
        unit.SetPropertyValue("PROP_HT_1", 100.0)  # Efficiency (%)

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add heater: {e}")


# ---------------------------------------------------------------------------
# Cooler
# ---------------------------------------------------------------------------


def add_cooler(
    flowsheet: object,
    name: str,
    inlet_stream_name: str,
    outlet_stream_name: str,
    outlet_temperature: float,
    pressure_drop: float,
    energy_stream_name: str = "",
) -> dict:
    """
    Add a cooler / heat-removal unit.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the heater.
    inlet_stream_name, outlet_stream_name : str
        Inlet / outlet material-stream tags.
    outlet_temperature : float
        Desired outlet temperature in **Kelvin**.
    pressure_drop : float
        Pressure drop in **Pascal**.
    energy_stream_name : str
        Tag of the energy stream supplying heat to this unit.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()
        from DWSIM.UnitOperations.UnitOperations import Cooler

        unit = flowsheet.AddObject(OT.Cooler, 0, 0, name)

        _connect_inlet(flowsheet, unit, inlet_stream_name)
        _connect_outlet(flowsheet, unit, outlet_stream_name, create_if_missing=True)
        _connect_outlet(flowsheet, unit, energy_stream_name)

        # CalcMode enum must be set on concrete type
        unit.__implementation__.CalcMode = Cooler.CalculationMode.OutletTemperature
        unit.SetPropertyValue("PROP_CL_2", float(outlet_temperature))  # Outlet T (K)
        unit.SetPropertyValue("PROP_CL_0", float(pressure_drop))  # dP (Pa)
        unit.SetPropertyValue("PROP_CL_1", 100.0)  # Efficiency (%)

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add cooler: {e}")


# ---------------------------------------------------------------------------
# Pump
# ---------------------------------------------------------------------------


def add_pump(
    flowsheet: object,
    name: str,
    inlet_stream_name: str,
    outlet_stream_name: str,
    outlet_pressure: float,
    energy_stream_name: str = "",
) -> dict:
    """
    Add a liquid pump.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the heater.
    inlet_stream_name, outlet_stream_name : str
        Inlet / outlet material-stream tags.
    outlet_pressure : float
        Desired discharge pressure in **Pascal**.
    energy_stream_name : str
        Tag of the energy stream supplying heat to this unit.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()
        from DWSIM.UnitOperations.UnitOperations import Pump

        unit = flowsheet.AddObject(OT.Pump, 0, 0, name)

        _connect_inlet(flowsheet, unit, inlet_stream_name)
        _connect_outlet(flowsheet, unit, outlet_stream_name, create_if_missing=True)
        _connect_inlet(flowsheet, unit, energy_stream_name)

        # CalcMode: OutletPressure = 1
        impl = unit.__implementation__
        impl.CalcMode = Pump.CalculationMode.OutletPressure
        impl.POut = float(outlet_pressure)
        impl.Eficiencia = 75.0  # Efficiency (%)

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add pump: {e}")


# ---------------------------------------------------------------------------
# Valve
# ---------------------------------------------------------------------------


def add_valve(
    flowsheet: object,
    name: str,
    inlet_stream_name: str,
    outlet_stream_name: str,
    outlet_pressure: float,
) -> dict:
    """
    Add an expansion valve (isenthalpic).

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name, inlet_stream_name, outlet_stream_name : str
        Unit and stream tags.
    outlet_pressure : float
        Outlet pressure in **Pascal**.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()
        from DWSIM.UnitOperations.UnitOperations import Valve

        unit = flowsheet.AddObject(OT.Valve, 0, 0, name)

        _connect_inlet(flowsheet, unit, inlet_stream_name)
        _connect_outlet(flowsheet, unit, outlet_stream_name, create_if_missing=True)

        impl = unit.__implementation__
        impl.CalcMode = Valve.CalculationMode.OutletPressure
        impl.OutletPressure = float(outlet_pressure)  # Pa

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add valve: {e}")


# ---------------------------------------------------------------------------
# Compressor
# ---------------------------------------------------------------------------


def add_compressor(
    flowsheet: object,
    name: str,
    inlet_stream_name: str,
    outlet_stream_name: str,
    outlet_pressure: float,
    energy_stream_name: str = "",
) -> dict:
    """
    Add a gas compressor.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    inlet_stream_name, outlet_stream_name : str
        Inlet / outlet material-stream tags.
    outlet_temperature : float
        Desired outlet temperature in **Kelvin**.
    outlet_pressure : float
        Discharge pressure in **Pascal**.
    energy_stream_name : str
        Tag of the energy stream supplying heat to this unit.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()
        from DWSIM.UnitOperations.UnitOperations import Compressor

        unit = flowsheet.AddObject(OT.Compressor, 0, 0, name)

        _connect_inlet(flowsheet, unit, inlet_stream_name)
        _connect_outlet(flowsheet, unit, outlet_stream_name, create_if_missing=True)
        _connect_inlet(flowsheet, unit, energy_stream_name)

        # CalcMode: OutletPressure = 0
        impl = unit.__implementation__
        impl.CalcMode = Compressor.CalculationMode.OutletPressure
        impl.POut = float(outlet_pressure)
        impl.AdiabaticEfficiency = 75.0

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add compressor: {e}")


# ---------------------------------------------------------------------------
# Heat Exchanger
# ---------------------------------------------------------------------------


def add_heat_exchanger(
    flowsheet: object,
    name: str,
    hot_inlet: str,
    hot_outlet: str,
    cold_inlet: str,
    cold_outlet: str,
    hot_outlet_temperature: float,
    overall_u: float = 500.0,
    area: float = 10.0,
) -> dict:
    """
    Add a two-stream heat exchanger.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the heat exchanger.
    hot_inlet, hot_outlet : str
        Tags for hot-side material streams.
    cold_inlet, cold_outlet : str
        Tags for cold-side material streams.
    hot_outlet_temperature : float
        Target hot-side outlet temperature in **Kelvin**.
    overall_u : float
        Overall heat transfer coefficient in **W/m²·K** (default 500).
    area : float
        Heat exchange area in **m²** (default 10). Must be > 0 for the
        NTU calculation to succeed.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()

        unit = flowsheet.AddObject(OT.HeatExchanger, 0, 0, name)

        # Connection order matters: hot-in first, then cold-in.
        _connect_inlet(flowsheet, unit, hot_inlet)
        _connect_inlet(flowsheet, unit, cold_inlet)
        _connect_outlet(flowsheet, unit, hot_outlet, create_if_missing=True)
        _connect_outlet(flowsheet, unit, cold_outlet, create_if_missing=True)

        # Configure via __implementation__ — SetPropertyValue doesn't reliably
        # update the fields the solver reads for enums and numeric properties.
        hx = unit.__implementation__

        # CalculationMode: CalcTempHotOut (0) — specify hot outlet T
        cm_type = type(hx.CalculationMode)
        hx.CalculationMode = cm_type(0)

        # DefinedTemperature: Hot_Fluid (0) — the specified T is for the hot side
        dt_type = type(hx.DefinedTemperature)
        hx.DefinedTemperature = dt_type(0)

        hx.HotSideOutletTemperature = float(hot_outlet_temperature)
        hx.OverallCoefficient = float(overall_u)
        hx.Area = float(area)

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add heat exchanger: {e}")


# ---------------------------------------------------------------------------
# Flash Separator (Vessel)
# ---------------------------------------------------------------------------


def add_separator(
    flowsheet: object,
    name: str,
    inlet_stream_name: str,
    vapor_outlet_name: str,
    liquid_outlet_name: str,
    temperature: float,
    pressure: float,
) -> dict:
    """
    Add a flash separator (vessel).

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the separator.
    inlet_stream_name : str
        Tag of the inlet material stream.
    vapor_outlet_name, liquid_outlet_name : str
        Tags for the vapour and liquid outlet streams.
    temperature : float
        Operating temperature in **Kelvin** (for adiabatic flash set to 0).
    pressure : float
        Operating pressure in **Pascal**.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()

        unit = flowsheet.AddObject(OT.Vessel, 0, 0, name)

        _connect_inlet(flowsheet, unit, inlet_stream_name)
        _connect_outlet(flowsheet, unit, vapor_outlet_name, create_if_missing=True)
        _connect_outlet(flowsheet, unit, liquid_outlet_name, create_if_missing=True)

        # Vessel property codes: PROP_SV_0 = override T (K), PROP_SV_1 = override P (Pa)
        if temperature > 0:
            unit.SetPropertyValue("PROP_SV_0", float(temperature))
        if pressure > 0:
            unit.SetPropertyValue("PROP_SV_1", float(pressure))

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add separator: {e}")


# ---------------------------------------------------------------------------
# Conversion Reactor
# ---------------------------------------------------------------------------


def add_conversion_reactor(
    flowsheet: object,
    name: str,
    inlet_stream_name: str,
    vapor_outlet_name: str,
    liquid_outlet_name: str,
    reaction_set: str,
    energy_stream_name: str = "",
) -> dict:
    """
    Add a conversion reactor.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the reactor.
    inlet_stream_name : str
        Tag of the feed stream.
    vapor_outlet_name, liquid_outlet_name : str
        Tags for the vapour and liquid product streams.
    reaction_set : str
        JSON string describing the reaction. Expected schema::

            {
                "base_compound": "Ethanol",
                "conversion": 0.95,
                "stoichiometry": {"Ethanol": -1, "Water": 1, "Carbon Dioxide": 2},
                "reaction_phase": "Liquid",
            }

        ``reaction_phase`` (optional): ``"Liquid"`` (default) or ``"Vapor"``.
    energy_stream_name : str
        Tag of the energy stream.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()

        rxn_def = _normalize_rxn_def(reaction_set)

        # --- input validation ---
        warning = _warn_unknown_keys(rxn_def, _KNOWN_CONVERSION_KEYS, name)
        if warning:
            import logging

            logging.getLogger(__name__).warning(warning)

        err = _validate_reaction_common(rxn_def, name)
        if err:
            return _fail(err)

        conv_raw = rxn_def.get("conversion")
        if conv_raw is None:
            return _fail(f"{name}: 'conversion' is missing from reaction_set. Got keys: {sorted(rxn_def.keys())}")
        try:
            conv = float(conv_raw)
        except (TypeError, ValueError):
            return _fail(f"{name}: 'conversion' must be numeric, got {conv_raw!r}")
        if not 0.0 <= conv <= 1.0:
            return _fail(f"{name}: 'conversion' must be between 0 and 1, got {conv}")
        # --- end validation ---

        unit = flowsheet.AddObject(OT.RCT_Conversion, 0, 0, name)

        _connect_inlet(flowsheet, unit, inlet_stream_name)
        _connect_outlet(flowsheet, unit, vapor_outlet_name, create_if_missing=True)
        _connect_outlet(flowsheet, unit, liquid_outlet_name, create_if_missing=True)
        _connect_inlet(flowsheet, unit, energy_stream_name)

        # Build the conversion reaction using flowsheet helper methods
        base = rxn_def["base_compound"]
        stoich = rxn_def["stoichiometry"]
        phase = rxn_def.get("reaction_phase", "Liquid")

        from System.Collections.Generic import Dictionary as DotNetDict

        stoich_dict = DotNetDict[str, float]()
        for compound, coeff in stoich.items():
            stoich_dict.Add(compound, float(coeff))

        # DWSIM expects the conversion expression as a percentage string
        # (e.g. "60" for 60%), but users specify fractional (0.0–1.0).
        conv_pct = conv * 100.0

        rxn = flowsheet.CreateConversionReaction(
            name=f"{name}_rxn",
            description=f"Conversion reaction for {name}",
            compounds_and_stoichcoeffs=stoich_dict,
            basecompound=base,
            reactionphase=phase,
            basis="Molar",
            conversionExpression=str(conv_pct),
        )
        flowsheet.AddReaction(rxn)
        flowsheet.AddReactionToSet(
            reactionID=rxn.ID,
            reactionSetID="DefaultSet",
            enabled=True,
            rank=0,
        )

        # Assign the default reaction set to the reactor
        unit.__implementation__.ReactionSetID = "DefaultSet"

        # Set outlet T and P from the inlet stream so the flash calculation
        # has valid specifications.
        try:
            inlet = _lookup(flowsheet, inlet_stream_name).__implementation__
            impl = unit.__implementation__
            impl.OutletTemperature = inlet.Phases[0].Properties.temperature
            impl.OutletPressure = inlet.Phases[0].Properties.pressure
        except (ValueError, AttributeError, IndexError) as e:
            import logging

            logging.getLogger(__name__).debug("Could not initialize reactor outlet T/P from inlet: %s", e)

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add conversion reactor: {e}")


# ---------------------------------------------------------------------------
# Equilibrium Reactor
# ---------------------------------------------------------------------------


def add_equilibrium_reactor(
    flowsheet: object,
    name: str,
    inlet_stream_name: str,
    vapor_outlet_name: str,
    liquid_outlet_name: str,
    reaction_set: str,
    energy_stream_name: str = "",
) -> dict:
    """
    Add an equilibrium reactor.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the reactor.
    inlet_stream_name : str
        Tag of the feed stream.
    vapor_outlet_name, liquid_outlet_name : str
        Tags for product streams.
    reaction_set : str
        JSON string describing the reaction. Expected schema::

            {
                "base_compound": "Ethanol",
                "stoichiometry": {"Ethanol": -1, "Water": 1, "Carbon Dioxide": 2},
                "Keq_expression": "exp(-5000/T + 10)",
                "reaction_phase": "Vapor",
            }

        ``Keq_expression`` (optional, alias ``"Keq"``): expression for
        ln(Keq) as a function of temperature ``T`` (Kelvin).  If omitted,
        defaults to Keq = 1 (activity coefficient basis).  If set to an
        empty string ``""``, DWSIM computes Keq from Gibbs free energy
        (fugacity basis).

        ``reaction_phase`` (optional): ``"Vapor"`` (default) or ``"Liquid"``.
    energy_stream_name : str
        Tag of the energy stream.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()

        rxn_def = _normalize_rxn_def(reaction_set)

        # --- input validation ---
        warning = _warn_unknown_keys(rxn_def, _KNOWN_EQUILIBRIUM_KEYS, name)
        if warning:
            import logging

            logging.getLogger(__name__).warning(warning)

        err = _validate_reaction_common(rxn_def, name)
        if err:
            return _fail(err)
        # --- end validation ---

        unit = flowsheet.AddObject(OT.RCT_Equilibrium, 0, 0, name)

        _connect_inlet(flowsheet, unit, inlet_stream_name)
        _connect_outlet(flowsheet, unit, vapor_outlet_name, create_if_missing=True)
        _connect_outlet(flowsheet, unit, liquid_outlet_name, create_if_missing=True)
        _connect_inlet(flowsheet, unit, energy_stream_name)

        base = rxn_def["base_compound"]
        stoich = rxn_def["stoichiometry"]
        raw_keq = rxn_def.get("Keq_expression", None)
        if raw_keq is None:
            keq_expr = "1"  # Backwards-compatible default: Keq = 1
        else:
            keq_expr = str(raw_keq)
        phase = rxn_def.get("reaction_phase", "Vapor")

        from System.Collections.Generic import Dictionary as DotNetDict

        stoich_dict = DotNetDict[str, float]()
        for compound, coeff in stoich.items():
            stoich_dict.Add(compound, float(coeff))

        # When a Keq expression is provided, use activity coefficient basis
        # (dimensionless Keq in terms of mole fractions × activity coefficients).
        # When empty, use Fugacity basis to let DWSIM compute Keq from
        # Gibbs free energy (matching the reference implementation).
        if keq_expr:
            basis = "ActivityCoefficient"
            units = ""
        else:
            basis = "Fugacity"
            units = "Pa"

        rxn = flowsheet.CreateEquilibriumReaction(
            name=f"{name}_rxn",
            description=f"Equilibrium reaction for {name}",
            compounds_and_stoichcoeffs=stoich_dict,
            basecompound=base,
            reactionphase=phase,
            basis=basis,
            units=units,
            Tapproach=0.0,
            lnKeq_fT=keq_expr,
        )
        flowsheet.AddReaction(rxn)
        flowsheet.AddReactionToSet(
            reactionID=rxn.ID,
            reactionSetID="DefaultSet",
            enabled=True,
            rank=0,
        )

        unit.__implementation__.ReactionSetID = "DefaultSet"

        # Set outlet T and P from the inlet stream so the flash calculation
        # has valid specifications (without these, DWSIM gets NaN/zero values).
        try:
            inlet = _lookup(flowsheet, inlet_stream_name).__implementation__
            impl = unit.__implementation__
            impl.OutletTemperature = inlet.Phases[0].Properties.temperature
            impl.OutletPressure = inlet.Phases[0].Properties.pressure
        except (ValueError, AttributeError, IndexError) as e:
            import logging

            logging.getLogger(__name__).debug("Could not initialize reactor outlet T/P from inlet: %s", e)

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add equilibrium reactor: {e}")


# ---------------------------------------------------------------------------
# Distillation Column
# ---------------------------------------------------------------------------


def add_distillation_column(
    flowsheet: object,
    name: str,
    feed_stream_name: str,
    feed_stage: int,
    num_stages: int,
    condenser_type: str,
    distillate_stream_name: str,
    bottoms_stream_name: str,
    reflux_ratio: float,
    reboiler_duty: float = 0.0,
    bottoms_rate: float = 0.0,
    reboiler_spec_type: str = "Product_Molar_Flow_Rate",
) -> dict:
    """
    Add a rigorous distillation column.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the column.
    feed_stream_name : str
        Tag of the feed stream.
    feed_stage : int
        Feed tray number (1-based from the top, where stage 1 is the
        condenser).
    num_stages : int
        Total number of stages including condenser and reboiler.
        Clamped to the internal stage count (default 12).
    condenser_type : str
        ``"TotalCondenser"`` or ``"PartialCondenser"``.
    distillate_stream_name : str
        Tag for the distillate product stream.
    bottoms_stream_name : str
        Tag for the bottoms product stream.
    reflux_ratio : float
        Reflux ratio (L/D).
    reboiler_duty : float
        Reboiler duty in **Watts** (used when ``reboiler_spec_type``
        is ``"Heat_Duty"``).
    bottoms_rate : float
        Bottoms product molar flow in **mol/s** (used when
        ``reboiler_spec_type`` is ``"Product_Molar_Flow_Rate"``).
    reboiler_spec_type : str
        Reboiler specification type.  ``"Product_Molar_Flow_Rate"``
        (default — most robust) or ``"Heat_Duty"``.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()
        from DWSIM.UnitOperations.UnitOperations.Auxiliary.SepOps import (
            StreamInformation,
        )

        _si_proto = StreamInformation()
        BehaviorEnum = type(_si_proto.StreamBehavior)
        PhaseEnum = type(_si_proto.StreamPhase)

        unit = flowsheet.AddObject(OT.DistillationColumn, 0, 0, name)
        col = unit.__implementation__

        # The internal Stages collection is fixed at creation time (default 12).
        actual_stages = col.Stages.Count
        col.NumberOfStages = min(int(num_stages), actual_stages)

        # Condenser type (Python.NET 3.0+ requires explicit Enum conversion).
        ct_enum = type(col.CondenserType)
        if condenser_type == "PartialCondenser":
            col.CondenserType = ct_enum(1)
        else:
            col.CondenserType = ct_enum(0)  # TotalCondenser

        go = unit.GraphicObject
        energy_port_idx = go.InputConnectors.Count - 1

        # Feed connects at the port matching the feed stage (0-indexed).
        feed_port = max(0, min(int(feed_stage) - 1, energy_port_idx - 1))
        feed_obj = _lookup(flowsheet, feed_stream_name)
        flowsheet.ConnectObjects(
            feed_obj.GraphicObject,
            unit.GraphicObject,
            -1,
            feed_port,
        )

        # Distillate (OUT[0]) and bottoms (OUT[1])
        dist_obj = _ensure_material_stream(flowsheet, distillate_stream_name)
        flowsheet.ConnectObjects(unit.GraphicObject, dist_obj.GraphicObject, 0, -1)

        bott_obj = _ensure_material_stream(flowsheet, bottoms_stream_name)
        flowsheet.ConnectObjects(unit.GraphicObject, bott_obj.GraphicObject, 1, -1)

        # Condenser and reboiler energy streams
        out_energy_port = go.OutputConnectors.Count - 1
        cond_energy = flowsheet.AddObject(OT.EnergyStream, 0, 0, f"{name}_Q_cond")
        flowsheet.ConnectObjects(
            unit.GraphicObject,
            cond_energy.GraphicObject,
            out_energy_port,
            -1,
        )
        reb_energy = flowsheet.AddObject(OT.EnergyStream, 0, 0, f"{name}_Q_reb")
        flowsheet.ConnectObjects(
            reb_energy.GraphicObject,
            unit.GraphicObject,
            -1,
            energy_port_idx,
        )

        # ------------------------------------------------------------------
        # Populate internal stream registries (MaterialStreams / EnergyStreams)
        # DWSIM's column solver reads these dictionaries, NOT graphic ports.
        # ------------------------------------------------------------------
        def _register_stream(registry, obj, behavior_int, stage_idx):
            si = StreamInformation()
            si.StreamID = obj.Name
            si.ID = obj.Name
            si.StreamBehavior = BehaviorEnum(behavior_int)
            si.StreamPhase = PhaseEnum(0)  # Liquid
            si.AssociatedStage = col.Stages[stage_idx].ID
            registry[obj.Name] = si

        _register_stream(col.MaterialStreams, feed_obj, 2, feed_port)  # Feed
        _register_stream(col.MaterialStreams, dist_obj, 0, 0)  # Distillate
        _register_stream(col.MaterialStreams, bott_obj, 1, actual_stages - 1)  # Bottoms
        _register_stream(col.EnergyStreams, cond_energy, 0, 0)  # Cond. energy
        _register_stream(col.EnergyStreams, reb_energy, 1, actual_stages - 1)  # Reb. energy

        # ------------------------------------------------------------------
        # Column specifications
        # ------------------------------------------------------------------
        spec_type_enum = type(col.Specs["C"].SType)
        # Condenser: reflux ratio (Stream_Ratio = 7)
        col.Specs["C"].SType = spec_type_enum(7)
        col.Specs["C"].SpecValue = float(reflux_ratio)

        # Reboiler spec
        _REBOILER_SPEC_MAP = {
            "Heat_Duty": 0,
            "Product_Molar_Flow_Rate": 1,
            "Product_Mass_Flow_Rate": 3,
            "Component_Molar_Flow_Rate": 2,
            "Component_Fraction": 5,
            "Temperature": 8,
        }
        reb_spec_int = _REBOILER_SPEC_MAP.get(reboiler_spec_type, 1)
        col.Specs["R"].SType = spec_type_enum(reb_spec_int)
        if reboiler_spec_type == "Heat_Duty":
            col.Specs["R"].SpecValue = float(reboiler_duty)
        else:
            col.Specs["R"].SpecValue = float(bottoms_rate)

        col.MaxIterations = 500

        # Initialize all stage pressures to the feed pressure.
        try:
            feed_impl = _lookup(flowsheet, feed_stream_name).__implementation__
            feed_p = feed_impl.Phases[0].Properties.pressure
            for i in range(col.Stages.Count):
                col.Stages[i].P = float(feed_p)
        except Exception:
            pass

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add distillation column: {e}")


# ---------------------------------------------------------------------------
# Multi-Feed Distillation Column
# ---------------------------------------------------------------------------


def add_multi_feed_distillation_column(
    flowsheet: object,
    name: str,
    feeds_json: str,
    num_stages: int,
    condenser_type: str,
    distillate_stream_name: str,
    bottoms_stream_name: str,
    reflux_ratio: float,
    bottoms_rate: float = 0.0,
    reboiler_spec_type: str = "Product_Molar_Flow_Rate",
    condenser_pressure: float = 0.0,
    reboiler_pressure: float = 0.0,
) -> dict:
    """
    Add a rigorous distillation column with multiple feed streams.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the column.
    feeds_json : str
        JSON list of feed specifications.  Each element is an object with
        ``"stream_name"`` (tag of an existing material stream) and
        ``"stage"`` (1-based stage number where 1 = condenser).
        Example: ``'[{"stream_name": "Feed", "stage": 10},
        {"stream_name": "Solvent", "stage": 3}]'``
    num_stages : int
        Total number of stages including condenser and reboiler.
        The column is resized if *num_stages* exceeds the default 12.
    condenser_type : str
        ``"TotalCondenser"`` or ``"PartialCondenser"``.
    distillate_stream_name : str
        Tag for the distillate product stream.
    bottoms_stream_name : str
        Tag for the bottoms product stream.
    reflux_ratio : float
        Reflux ratio (L/D).
    bottoms_rate : float
        Bottoms product molar flow in **mol/s** (used when
        ``reboiler_spec_type`` is ``"Product_Molar_Flow_Rate"``).
    reboiler_spec_type : str
        Reboiler specification type.  ``"Product_Molar_Flow_Rate"``
        (default) or ``"Heat_Duty"``.
    condenser_pressure : float
        Pressure in **Pa** for the condenser stage.  If 0, uses the first
        feed's pressure.
    reboiler_pressure : float
        Pressure in **Pa** for the reboiler stage.  If 0, equals
        *condenser_pressure*.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        import json as _json

        OT = _get_object_type()
        from DWSIM.UnitOperations.UnitOperations.Auxiliary.SepOps import (
            Stage,
            StreamInformation,
        )

        _si_proto = StreamInformation()
        BehaviorEnum = type(_si_proto.StreamBehavior)
        PhaseEnum = type(_si_proto.StreamPhase)

        feeds = _json.loads(feeds_json)
        if not feeds:
            return _fail("feeds_json must contain at least one feed")

        unit = flowsheet.AddObject(OT.DistillationColumn, 0, 0, name)
        col = unit.__implementation__

        # Resize stages if needed (default is 12).
        actual_stages = col.Stages.Count
        target_stages = int(num_stages)
        if target_stages > actual_stages:
            for i in range(target_stages - actual_stages):
                s = Stage(str(i + actual_stages))
                s.Name = f"Stage_{actual_stages + i}"
                col.Stages.Add(s)
        col.NumberOfStages = min(target_stages, col.Stages.Count)

        # Condenser type
        ct_enum = type(col.CondenserType)
        if condenser_type == "PartialCondenser":
            col.CondenserType = ct_enum(1)
        else:
            col.CondenserType = ct_enum(0)

        go = unit.GraphicObject
        energy_port_idx = go.InputConnectors.Count - 1

        # ------------------------------------------------------------------
        # Register helper
        # ------------------------------------------------------------------
        def _register_stream(registry, obj, behavior_int, stage_idx):
            si = StreamInformation()
            si.StreamID = obj.Name
            si.ID = obj.Name
            si.StreamBehavior = BehaviorEnum(behavior_int)
            si.StreamPhase = PhaseEnum(0)
            si.AssociatedStage = col.Stages[stage_idx].ID
            registry[obj.Name] = si

        # ------------------------------------------------------------------
        # Wire feeds
        # ------------------------------------------------------------------
        first_feed_pressure = 101325.0
        for idx, feed_spec in enumerate(feeds):
            sname = feed_spec["stream_name"]
            stage = int(feed_spec["stage"])
            stage_idx = max(0, min(stage - 1, col.Stages.Count - 1))

            feed_obj = _lookup(flowsheet, sname)

            # Graphic port (clamped to available connectors)
            port = max(0, min(stage_idx, energy_port_idx - 1))
            flowsheet.ConnectObjects(
                feed_obj.GraphicObject, unit.GraphicObject, -1, port
            )
            _register_stream(col.MaterialStreams, feed_obj, 2, stage_idx)

            if idx == 0:
                try:
                    first_feed_pressure = float(
                        feed_obj.__implementation__.Phases[0].Properties.pressure
                    )
                except Exception:
                    pass

        # ------------------------------------------------------------------
        # Wire products
        # ------------------------------------------------------------------
        dist_obj = _ensure_material_stream(flowsheet, distillate_stream_name)
        flowsheet.ConnectObjects(
            unit.GraphicObject, dist_obj.GraphicObject, 0, -1
        )
        _register_stream(col.MaterialStreams, dist_obj, 0, 0)

        bott_obj = _ensure_material_stream(flowsheet, bottoms_stream_name)
        flowsheet.ConnectObjects(
            unit.GraphicObject, bott_obj.GraphicObject, 1, -1
        )
        _register_stream(
            col.MaterialStreams, bott_obj, 1, col.Stages.Count - 1
        )

        # ------------------------------------------------------------------
        # Energy streams
        # ------------------------------------------------------------------
        out_energy_port = go.OutputConnectors.Count - 1
        cond_energy = flowsheet.AddObject(
            OT.EnergyStream, 0, 0, f"{name}_Q_cond"
        )
        flowsheet.ConnectObjects(
            unit.GraphicObject, cond_energy.GraphicObject, out_energy_port, -1
        )
        reb_energy = flowsheet.AddObject(
            OT.EnergyStream, 0, 0, f"{name}_Q_reb"
        )
        flowsheet.ConnectObjects(
            reb_energy.GraphicObject, unit.GraphicObject, -1, energy_port_idx
        )
        _register_stream(col.EnergyStreams, cond_energy, 0, 0)
        _register_stream(
            col.EnergyStreams, reb_energy, 1, col.Stages.Count - 1
        )

        # ------------------------------------------------------------------
        # Column specifications
        # ------------------------------------------------------------------
        spec_type_enum = type(col.Specs["C"].SType)
        col.Specs["C"].SType = spec_type_enum(7)  # Stream_Ratio (reflux)
        col.Specs["C"].SpecValue = float(reflux_ratio)

        _REBOILER_SPEC_MAP = {
            "Heat_Duty": 0,
            "Product_Molar_Flow_Rate": 1,
            "Product_Mass_Flow_Rate": 3,
            "Component_Molar_Flow_Rate": 2,
            "Component_Fraction": 5,
            "Temperature": 8,
        }
        reb_spec_int = _REBOILER_SPEC_MAP.get(reboiler_spec_type, 1)
        col.Specs["R"].SType = spec_type_enum(reb_spec_int)
        col.Specs["R"].SpecValue = float(bottoms_rate)

        col.MaxIterations = 500

        # Stage pressures (linear profile from condenser to reboiler)
        p_cond = float(condenser_pressure) if condenser_pressure else first_feed_pressure
        p_reb = float(reboiler_pressure) if reboiler_pressure else p_cond
        n = col.Stages.Count
        for i in range(n):
            col.Stages[i].P = p_cond + (p_reb - p_cond) * i / max(n - 1, 1)

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add multi-feed distillation column: {e}")


# ---------------------------------------------------------------------------
# Recycle (Tear Stream)
# ---------------------------------------------------------------------------


def add_recycle(
    flowsheet: object,
    name: str,
    inlet_stream_name: str,
    outlet_stream_name: str,
    max_iterations: int = 100,
    tolerance_mass_flow: float = 1e-3,
    tolerance_temperature: float = 1e-3,
    tolerance_pressure: float = 1e-3,
    acceleration_method: str = "Wegstein",
) -> dict:
    """
    Add a recycle (tear stream) convergence block.

    The recycle block connects a downstream outlet to an upstream inlet,
    iterating until the assumed values (outlet) match the calculated
    values (inlet) within the specified tolerances.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the recycle block.
    inlet_stream_name : str
        Tag of the stream **entering** the recycle block (from downstream
        of the process — the "calculated" values).
    outlet_stream_name : str
        Tag of the stream **leaving** the recycle block (going upstream
        — the "assumed" values that the solver iterates on).
    max_iterations : int
        Maximum convergence iterations (default 100).
    tolerance_mass_flow : float
        Relative tolerance for mass flow convergence (default 1e-3).
    tolerance_temperature : float
        Relative tolerance for temperature convergence (default 1e-3).
    tolerance_pressure : float
        Relative tolerance for pressure convergence (default 1e-3).
    acceleration_method : str
        Convergence acceleration method. ``"Wegstein"`` (default) or
        ``"Direct"``.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()

        unit = flowsheet.AddObject(OT.OT_Recycle, 0, 0, name)

        _connect_inlet(flowsheet, unit, inlet_stream_name)
        _connect_outlet(flowsheet, unit, outlet_stream_name, create_if_missing=True)

        impl = unit.__implementation__
        impl.MaximumIterations = int(max_iterations)

        # Set convergence tolerances
        impl.ConvergenceParameters.MassFlow = float(tolerance_mass_flow)
        impl.ConvergenceParameters.Temperature = float(tolerance_temperature)
        impl.ConvergenceParameters.Pressure = float(tolerance_pressure)

        # Acceleration method
        if acceleration_method.lower() == "wegstein":
            impl.AccelMethod = 1  # Wegstein
        else:
            impl.AccelMethod = 0  # Direct substitution

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add recycle: {e}")


# ---------------------------------------------------------------------------
# Expander / Turbine
# ---------------------------------------------------------------------------


def add_expander(
    flowsheet: object,
    name: str,
    inlet_stream_name: str,
    outlet_stream_name: str,
    outlet_pressure: float,
    efficiency: float = 75.0,
    energy_stream_name: str = "",
) -> dict:
    """
    Add an expander (turbine) to extract work from a gas or steam stream.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the expander.
    inlet_stream_name, outlet_stream_name : str
        Inlet / outlet material-stream tags.
    outlet_pressure : float
        Discharge pressure in **Pascal**.
    efficiency : float
        Adiabatic efficiency in **percent** (default 75).
    energy_stream_name : str
        Tag of the energy stream receiving generated work.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()
        from DWSIM.UnitOperations.UnitOperations import Expander

        unit = flowsheet.AddObject(OT.Expander, 0, 0, name)

        _connect_inlet(flowsheet, unit, inlet_stream_name)
        _connect_outlet(flowsheet, unit, outlet_stream_name, create_if_missing=True)
        _connect_outlet(flowsheet, unit, energy_stream_name)

        impl = unit.__implementation__
        impl.CalcMode = Expander.CalculationMode.OutletPressure
        impl.POut = float(outlet_pressure)
        impl.AdiabaticEfficiency = float(efficiency)

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add expander: {e}")


# ---------------------------------------------------------------------------
# Absorption Column
# ---------------------------------------------------------------------------


def add_absorption_column(
    flowsheet: object,
    name: str,
    num_stages: int,
    gas_inlet_name: str,
    liquid_inlet_name: str,
    gas_outlet_name: str,
    liquid_outlet_name: str,
    operating_pressure: float = 0.0,
) -> dict:
    """
    Add an absorption (or stripping) column.

    The gas enters at the bottom and the liquid solvent enters at the top.
    No condenser or reboiler — separation relies on gas-liquid contacting.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the absorption column.
    num_stages : int
        Number of theoretical stages.
    gas_inlet_name : str
        Tag of the gas feed stream (enters at bottom).
    liquid_inlet_name : str
        Tag of the liquid solvent stream (enters at top).
    gas_outlet_name : str
        Tag of the treated gas outlet stream (exits at top).
    liquid_outlet_name : str
        Tag of the rich solvent outlet stream (exits at bottom).
    operating_pressure : float
        Column operating pressure in **Pascal**.  If 0, uses the feed
        stream pressure.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()
        from DWSIM.UnitOperations.UnitOperations.Auxiliary.SepOps import (
            StreamInformation,
        )

        _si_proto = StreamInformation()
        BehaviorEnum = type(_si_proto.StreamBehavior)
        PhaseEnum = type(_si_proto.StreamPhase)

        unit = flowsheet.AddObject(OT.AbsorptionColumn, 0, 0, name)
        col = unit.__implementation__

        # SetNumberOfStages properly resizes the Stages collection.
        col.SetNumberOfStages(int(num_stages))
        actual_stages = col.Stages.Count
        col.MaxIterations = 500

        go = unit.GraphicObject
        last_in = go.InputConnectors.Count - 1

        # Gas inlet → bottom port
        gas_in = _lookup(flowsheet, gas_inlet_name)
        gas_port = min(actual_stages - 1, last_in)
        flowsheet.ConnectObjects(gas_in.GraphicObject, go, -1, gas_port)

        # Liquid inlet → top port (port 0)
        liq_in = _lookup(flowsheet, liquid_inlet_name)
        flowsheet.ConnectObjects(liq_in.GraphicObject, go, -1, 0)

        # Gas outlet → top output (port 0)
        gas_out = _ensure_material_stream(flowsheet, gas_outlet_name)
        flowsheet.ConnectObjects(go, gas_out.GraphicObject, 0, -1)

        # Liquid outlet → bottom output
        liq_out = _ensure_material_stream(flowsheet, liquid_outlet_name)
        bot_port = go.OutputConnectors.Count - 1
        flowsheet.ConnectObjects(go, liq_out.GraphicObject, bot_port, -1)

        # ------------------------------------------------------------------
        # Populate internal stream registries (same fix as distillation)
        # ------------------------------------------------------------------
        def _register_stream(registry, obj, behavior_int, stage_idx):
            si = StreamInformation()
            si.StreamID = obj.Name
            si.ID = obj.Name
            si.StreamBehavior = BehaviorEnum(behavior_int)
            si.StreamPhase = PhaseEnum(0)
            si.AssociatedStage = col.Stages[stage_idx].ID
            registry[obj.Name] = si

        _register_stream(col.MaterialStreams, gas_in, 2, actual_stages - 1)  # Feed @ bottom
        _register_stream(col.MaterialStreams, liq_in, 2, 0)  # Feed @ top
        _register_stream(col.MaterialStreams, gas_out, 4, 0)  # OverheadVapor @ top
        _register_stream(col.MaterialStreams, liq_out, 1, actual_stages - 1)  # BottomsLiquid

        # Set stage pressures
        if operating_pressure > 0:
            for i in range(actual_stages):
                col.Stages[i].P = float(operating_pressure)
        else:
            try:
                p = float(gas_in.__implementation__.Phases[0].Properties.pressure)
                for i in range(actual_stages):
                    col.Stages[i].P = p
            except Exception:
                pass

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add absorption column: {e}")


# ---------------------------------------------------------------------------
# Three-Phase Separator / Decanter
# ---------------------------------------------------------------------------


def add_decanter(
    flowsheet: object,
    name: str,
    inlet_stream_name: str,
    light_liquid_outlet_name: str,
    heavy_liquid_outlet_name: str,
    temperature: float = 0.0,
    pressure: float = 0.0,
    energy_stream_name: str = "",
) -> dict:
    """
    Add a liquid-liquid decanter (three-phase separator).

    Separates a feed into a light liquid phase (organic) and a heavy
    liquid phase (aqueous), based on liquid-liquid equilibrium.  When
    temperature is set to 0, the separator operates adiabatically.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    name : str
        Tag for the decanter.
    inlet_stream_name : str
        Tag of the inlet material stream.
    light_liquid_outlet_name : str
        Tag for the light (organic) liquid outlet stream.
    heavy_liquid_outlet_name : str
        Tag for the heavy (aqueous) liquid outlet stream.
    temperature : float
        Operating temperature in **Kelvin** (0 for adiabatic).
    pressure : float
        Operating pressure in **Pascal** (0 to use feed pressure).
    energy_stream_name : str
        Optional energy stream tag.

    Returns
    -------
    dict
    """
    try:
        _ensure_clr()
        OT = _get_object_type()

        unit = flowsheet.AddObject(OT.TPVessel, 0, 0, name)

        _connect_inlet(flowsheet, unit, inlet_stream_name)

        # TPVessel outlets: OUT[0] = vapor, OUT[1] = light liquid, OUT[2] = heavy liquid
        # For a decanter operating below the boiling point, the vapor outlet
        # will carry negligible flow.  We still create it to satisfy DWSIM's
        # connector requirements.
        vapor_dummy = flowsheet.AddObject(OT.MaterialStream, 0, 0, f"{name}_VAP")
        flowsheet.ConnectObjects(unit.GraphicObject, vapor_dummy.GraphicObject, 0, -1)

        light_out = _ensure_material_stream(flowsheet, light_liquid_outlet_name)
        flowsheet.ConnectObjects(unit.GraphicObject, light_out.GraphicObject, 1, -1)

        heavy_out = _ensure_material_stream(flowsheet, heavy_liquid_outlet_name)
        flowsheet.ConnectObjects(unit.GraphicObject, heavy_out.GraphicObject, 2, -1)

        _connect_inlet(flowsheet, unit, energy_stream_name)

        if temperature > 0:
            unit.SetPropertyValue("PROP_SV_0", float(temperature))
        if pressure > 0:
            unit.SetPropertyValue("PROP_SV_1", float(pressure))

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add decanter: {e}")


def add_kinetic_reactor(
    flowsheet,
    name: str,
    inlet_stream_name: str,
    outlet_stream_name: str,
    energy_stream_name: str = "",
    reactor_type: str = "PFR",
    reactions_json: str = "[]",
    volume: float = 1.0,
    length: float = 5.0,
    number_of_tubes: int = 1,
    catalyst_loading: float = 0.0,
    catalyst_particle_diameter: float = 0.0,
    catalyst_void_fraction: float = 0.0,
    operation_mode: str = "Isothermic",
    outlet_temperature: float = 0.0,
    vapor_outlet_name: str = "",
) -> dict:
    """Add a kinetic reactor (PFR or CSTR) with kinetic or het-cat reactions.

    Parameters
    ----------
    flowsheet : DWSIM flowsheet object
    name : reactor block name
    inlet_stream_name : feed material stream
    outlet_stream_name : liquid / main product stream
    energy_stream_name : optional energy stream (connects to energy inlet)
    reactor_type : "PFR" or "CSTR"
    reactions_json : JSON list of reaction dicts, each with:
        - name, description (str)
        - stoichiometry: {compound: coeff} (negative=reactant)
        - direct_orders: {compound: order} (forward)
        - reverse_orders: {compound: order} (reverse, optional)
        - base_compound: str
        - reaction_phase: "Liquid" | "Vapor" | "Mixture"
        - basis: "Molar" | "Mass" | "PartialPress"
        - amount_units: e.g. "mol/L"
        - rate_units: e.g. "mol/[L.s]"
        - A_forward, E_forward: Arrhenius forward (pre-exp, Ea J/mol)
        - A_reverse, E_reverse: Arrhenius reverse (0 = irreversible)
        - expression_forward, expression_reverse: custom rate exprs (optional)
        - type: "kinetic" (default) or "hetcat"
        - numerator_expression, denominator_expression: for hetcat only
    volume : reactor volume [m³]
    length : reactor length [m] (PFR only)
    number_of_tubes : number of tubes (PFR only)
    catalyst_loading : catalyst loading [kg/m³] (PFR only, 0 = homogeneous)
    catalyst_particle_diameter : particle diameter [m] (PFR only)
    catalyst_void_fraction : bed void fraction (PFR only)
    operation_mode : "Isothermic", "Adiabatic", "OutletTemperature",
                     "NonIsothermalNonAdiabatic"
    outlet_temperature : outlet T [K] when mode is OutletTemperature or Isothermic
    vapor_outlet_name : CSTR only — optional vapor product stream
    """
    import json

    try:
        _ensure_clr()
        OT = _get_object_type()
        from System.Collections.Generic import Dictionary as DotNetDict

        rtype = reactor_type.upper()
        if rtype not in ("PFR", "CSTR"):
            return _fail(f"reactor_type must be PFR or CSTR, got {reactor_type}")

        ot = OT.RCT_PFR if rtype == "PFR" else OT.RCT_CSTR
        unit_obj = flowsheet.AddObject(ot, 0, 0, name)
        unit = unit_obj.__implementation__

        # --- connections ---
        feed = _ensure_material_stream(flowsheet, inlet_stream_name)
        flowsheet.ConnectObjects(feed.GraphicObject, unit_obj.GraphicObject, 0, 0)

        product = _ensure_material_stream(flowsheet, outlet_stream_name)
        flowsheet.ConnectObjects(unit_obj.GraphicObject, product.GraphicObject, 0, 0)

        # CSTR has 2 material outputs (liq=0, vap=1); PFR has only 1
        if rtype == "CSTR" and vapor_outlet_name:
            vap_out = _ensure_material_stream(flowsheet, vapor_outlet_name)
            flowsheet.ConnectObjects(
                unit_obj.GraphicObject, vap_out.GraphicObject, 1, 0
            )

        # Energy stream -> reactor energy inlet (port 1)
        if energy_stream_name:
            en = flowsheet.GetFlowsheetSimulationObject(energy_stream_name)
            if en is None:
                en = flowsheet.AddObject(OT.EnergyStream, 0, 0, energy_stream_name)
            flowsheet.ConnectObjects(en.GraphicObject, unit_obj.GraphicObject, 0, 1)

        # --- parse and create reactions ---
        rxns = json.loads(reactions_json) if isinstance(reactions_json, str) else reactions_json
        for rx in rxns:
            rx_type = rx.get("type", "kinetic").lower()
            rx_name = rx["name"]
            rx_desc = rx.get("description", "")
            stoich = rx["stoichiometry"]
            base = rx["base_compound"]
            phase = rx.get("reaction_phase", "Liquid")
            basis = rx.get("basis", "Molar")
            amt_units = rx.get("amount_units", "mol/L")
            rate_units = rx.get("rate_units", "mol/[L.s]")

            stoich_dict = DotNetDict[str, float]()
            for comp, coeff in stoich.items():
                stoich_dict.Add(comp, float(coeff))

            if rx_type == "hetcat":
                num_expr = rx.get("numerator", rx.get("numerator_expression", ""))
                den_expr = rx.get("denominator", rx.get("denominator_expression", "1"))
                rxn = flowsheet.CreateHetCatReaction(
                    rx_name, rx_desc, stoich_dict, base, phase, basis,
                    amt_units, rate_units, num_expr, den_expr,
                )
            else:
                direct = DotNetDict[str, float]()
                for comp, order in rx.get("direct_orders", {}).items():
                    direct.Add(comp, float(order))

                reverse = DotNetDict[str, float]()
                for comp, order in rx.get("reverse_orders", {}).items():
                    reverse.Add(comp, float(order))

                Af = float(rx.get("A_forward", 0.0))
                Ef = float(rx.get("E_forward", 0.0))
                Ar = float(rx.get("A_reverse", 0.0))
                Er = float(rx.get("E_reverse", 0.0))
                expr_f = rx.get("expression_forward", "")
                expr_r = rx.get("expression_reverse", "")

                rxn = flowsheet.CreateKineticReaction(
                    rx_name, rx_desc, stoich_dict, direct, reverse,
                    base, phase, basis, amt_units, rate_units,
                    Af, Ef, Ar, Er, expr_f, expr_r,
                )

            flowsheet.AddReaction(rxn)
            flowsheet.AddReactionToSet(rxn.ID, "DefaultSet", True, 0)

        # --- reactor configuration ---
        unit.ReactionSetID = "DefaultSet"

        op_modes = {
            "isothermic": 0, "adiabatic": 1,
            "outlettemperature": 2, "nonisothermalnonadiabatic": 3,
        }
        mode_val = op_modes.get(operation_mode.lower(), 0)
        rom_type = type(unit.ReactorOperationMode)
        unit.ReactorOperationMode = rom_type(mode_val)

        if outlet_temperature > 0 and mode_val in (0, 2):
            # Only meaningful for Isothermic (0) and OutletTemperature (2)
            unit.OutletTemperature = float(outlet_temperature)

        unit.Volume = float(volume)

        if rtype == "PFR":
            unit.Length = float(length)
            if number_of_tubes > 1:
                unit.NumberOfTubes = int(number_of_tubes)
            if catalyst_loading > 0:
                unit.CatalystLoading = float(catalyst_loading)
            if catalyst_particle_diameter > 0:
                unit.CatalystParticleDiameter = float(catalyst_particle_diameter)
            if catalyst_void_fraction > 0:
                unit.CatalystVoidFraction = float(catalyst_void_fraction)

        return _ok(name)
    except Exception as e:
        return _fail(f"Failed to add kinetic reactor: {e}")
