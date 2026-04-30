"""
COCO FSD → DWSIM Converter Tool.

Parses COCO simulator .fsd flowsheet files and builds equivalent DWSIM
flowsheets programmatically.  The converted file is saved as a .dwxmz
archive that can be opened directly in DWSIM.

Limitations
-----------
* Kinetic reaction parameters (Arrhenius constants, rate expressions) are
  **not portable** from COCO's CAPE-OPEN reaction packages.  Reactors are
  rebuilt as **conversion reactors** whose conversion is back-calculated
  from the solved inlet/outlet compositions in the FSD.
* Only a subset of unit operation types is supported (see
  ``_SUPPORTED_UNIT_OPS``).  Unsupported types are reported in the result.
* COCO property packages (COM CAPE-OPEN) don't map 1-to-1 to DWSIM.
  A conservative mapping table is used; unrecognised packages default to
  Peng-Robinson with a warning.
"""

from __future__ import annotations

import json
import logging
import os
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

LOGGER = logging.getLogger(__name__)

# ── COCO → DWSIM property-package mapping table ──────────────────────

_PP_MAP: dict[str, str] = {
    # COCO TEA/CAPE-OPEN packages → DWSIM equivalents
    "Peng-Robinson": "Peng-Robinson",
    "SRK": "SRK",
    "NRTL": "NRTL",
    "UNIQUAC": "UNIQUAC",
    "Ideal": "Raoult's Law",
    "Steam Tables": "Steam Tables (IAPWS-IF97)",
    "CoolProp": "CoolProp",
}

_DEFAULT_PP = "Peng-Robinson"

# ── Supported COCO unit-operation type mappings ───────────────────────

_SUPPORTED_UNIT_OPS = {
    "CSTR",
    "PFR",
    "Mixer",
    "Splitter",
    "Heater",
    "Cooler",
    "HeatExchanger",
    "Separator",
    "Valve",
    "Pump",
    "Compressor",
}


# =====================================================================
# FSD XML parsing helpers
# =====================================================================


def _parse_fsd(fsd_path: str) -> dict:
    """
    Parse a COCO .fsd file and return a structured topology dict.

    Returns
    -------
    dict with keys:
        compounds : list[dict]   – name, cas
        streams   : list[dict]   – name, temperature, pressure, molar_flow,
                                    mole_fractions, solved_mole_fractions
        unit_ops  : list[dict]   – name, type, connections, params
        property_package : str   – COCO PP name (or "Unknown")
    """
    result: dict[str, Any] = {
        "compounds": [],
        "streams": [],
        "unit_ops": [],
        "property_package": "Unknown",
    }

    # FSD is a ZIP archive containing Flowsheet.xml
    try:
        with zipfile.ZipFile(fsd_path, "r") as zf:
            xml_bytes = zf.read("Flowsheet.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError(f"Cannot read Flowsheet.xml from '{fsd_path}': {exc}") from exc

    root = ET.fromstring(xml_bytes)

    # ── Compounds ─────────────────────────────────────────────────
    for comp_el in root.findall(".//compound"):
        name = comp_el.get("name", "")
        cas_el = comp_el.find("CAS")
        cas = cas_el.text.strip() if cas_el is not None and cas_el.text else ""
        result["compounds"].append({"name": name, "cas": cas})

    compound_names = [c["name"] for c in result["compounds"]]

    # ── Property Package ──────────────────────────────────────────
    pp_el = root.find(".//propertyPackage")
    if pp_el is not None:
        pp_name = pp_el.get("name", "Unknown")
        result["property_package"] = pp_name

    # ── Streams ───────────────────────────────────────────────────
    for stream_el in root.findall(".//stream"):
        obj_el = stream_el.find("object")
        stream_name = obj_el.get("name", "") if obj_el is not None else ""

        def _float(tag: str, default: float = 0.0) -> float:
            el = stream_el.find(tag)
            if el is not None and el.text:
                try:
                    return float(el.text.strip())
                except ValueError:
                    pass
            return default

        def _floats(tag: str) -> list[float]:
            el = stream_el.find(tag)
            if el is not None and el.text:
                return [float(v) for v in el.text.strip().split(";") if v.strip()]
            return []

        # Specified (input) values
        spec_fracs = _floats("specifiedMoleFraction")
        spec_T = _float("specifiedTemperature")
        spec_P = _float("specifiedPressure")
        spec_F = _float("specifiedFlowRate")

        # Solved overall composition (from COCO solution)
        solved_fracs: list[float] = []
        for phase_el in stream_el.findall(".//phase"):
            if phase_el.get("name") == "Overall":
                mf_el = phase_el.find("moleFraction")
                if mf_el is not None and mf_el.text:
                    solved_fracs = [float(v) for v in mf_el.text.strip().split(";") if v.strip()]
                break

        # Solved T, P, F
        solved_T = _float(".//overallTemperature", spec_T)
        solved_P = _float(".//overallPressure", spec_P)
        solved_F = _float(".//overallFlowRate", spec_F)

        # Build mole fraction dict (compound_name → fraction)
        mole_fracs: dict[str, float] = {}
        fracs_to_use = spec_fracs if spec_fracs else solved_fracs
        for i, cname in enumerate(compound_names):
            if i < len(fracs_to_use):
                mole_fracs[cname] = fracs_to_use[i]

        solved_mole_fracs: dict[str, float] = {}
        for i, cname in enumerate(compound_names):
            if i < len(solved_fracs):
                solved_mole_fracs[cname] = solved_fracs[i]

        stream_data = {
            "name": stream_name,
            "temperature": spec_T if spec_T > 0 else solved_T,
            "pressure": spec_P if spec_P > 0 else solved_P,
            "molar_flow": spec_F if spec_F > 0 else solved_F,
            "mole_fractions": mole_fracs,
            "solved_mole_fractions": solved_mole_fracs,
            "solved_temperature": solved_T,
            "solved_pressure": solved_P,
            "solved_molar_flow": solved_F,
        }
        result["streams"].append(stream_data)

    # ── Unit Operations ───────────────────────────────────────────
    for uo_el in root.findall(".//unitOperation"):
        obj_el = uo_el.find("object")
        uo_name = obj_el.get("name", "") if obj_el is not None else ""

        uo_type_el = uo_el.find("type")
        uo_type = uo_type_el.text.strip() if uo_type_el is not None and uo_type_el.text else "Unknown"

        connections: list[dict] = []
        for conn_el in uo_el.findall("connection"):
            connections.append({
                "port": conn_el.get("port", ""),
                "feed": conn_el.get("feed", "false").lower() == "true",
                "type": conn_el.get("type", "material"),
                "stream": conn_el.text.strip() if conn_el.text else "",
            })

        result["unit_ops"].append({
            "name": uo_name,
            "type": uo_type,
            "connections": connections,
        })

    return result


# =====================================================================
# Compound mapping: COCO name/CAS → DWSIM name
# =====================================================================


def _map_compounds_to_dwsim(
    coco_compounds: list[dict],
    available_compounds: Any,
) -> tuple[dict[str, str], list[str]]:
    """
    Map COCO compound names to DWSIM compound names.

    Parameters
    ----------
    coco_compounds : list[dict]
        Each dict has 'name' and 'cas' keys.
    available_compounds : .NET dictionary
        ``flowsheet.AvailableCompounds`` from the DWSIM runtime.

    Returns
    -------
    mapping : dict[str, str]
        COCO name → DWSIM name
    warnings : list[str]
        Any mapping issues encountered.
    """
    mapping: dict[str, str] = {}
    warnings: list[str] = []

    # Build lookup indices from DWSIM's database
    dwsim_by_name: dict[str, str] = {}   # lowercase → actual name
    dwsim_by_cas: dict[str, str] = {}    # CAS → actual name

    for key in available_compounds.Keys:
        name = str(key)
        dwsim_by_name[name.lower()] = name
        try:
            comp_obj = available_compounds[key]
            cas = str(comp_obj.CAS_Number) if hasattr(comp_obj, "CAS_Number") else ""
            if cas:
                dwsim_by_cas[cas] = name
        except Exception:
            pass

    for coco_comp in coco_compounds:
        coco_name = coco_comp["name"]
        cas = coco_comp.get("cas", "")

        # Strategy 1: exact name match (case-insensitive)
        dwsim_name = dwsim_by_name.get(coco_name.lower())
        if dwsim_name:
            mapping[coco_name] = dwsim_name
            continue

        # Strategy 2: CAS number lookup
        if cas and cas in dwsim_by_cas:
            dwsim_name = dwsim_by_cas[cas]
            mapping[coco_name] = dwsim_name
            warnings.append(
                f"Compound '{coco_name}' mapped to DWSIM '{dwsim_name}' via CAS {cas}"
            )
            continue

        # Strategy 3: partial name match (substring)
        matches = [v for k, v in dwsim_by_name.items() if coco_name.lower() in k]
        if len(matches) == 1:
            mapping[coco_name] = matches[0]
            warnings.append(
                f"Compound '{coco_name}' mapped to DWSIM '{matches[0]}' via partial name match"
            )
            continue

        warnings.append(
            f"Compound '{coco_name}' (CAS: {cas}) could not be mapped to any DWSIM compound"
        )

    return mapping, warnings


# =====================================================================
# Stoichiometry back-calculation from solved data
# =====================================================================


def _calculate_reactor_stoichiometry(
    inlet_streams: list[dict],
    outlet_streams: list[dict],
    compound_names: list[str],
) -> dict:
    """
    Back-calculate reaction stoichiometry and conversion from solved
    inlet/outlet stream data.

    Returns
    -------
    dict with keys:
        base_compound : str
        conversion : float
        stoichiometry : dict[str, float]
        warnings : list[str]
    """
    warnings: list[str] = []

    # Sum inlet and outlet molar flows per compound
    inlet_mols: dict[str, float] = {c: 0.0 for c in compound_names}
    outlet_mols: dict[str, float] = {c: 0.0 for c in compound_names}

    for s in inlet_streams:
        F = s.get("solved_molar_flow") or s.get("molar_flow", 0.0)
        fracs = s.get("solved_mole_fractions") or s.get("mole_fractions", {})
        for c in compound_names:
            inlet_mols[c] += F * fracs.get(c, 0.0)

    for s in outlet_streams:
        F = s.get("solved_molar_flow") or s.get("molar_flow", 0.0)
        fracs = s.get("solved_mole_fractions") or s.get("mole_fractions", {})
        for c in compound_names:
            outlet_mols[c] += F * fracs.get(c, 0.0)

    # Net change per compound
    deltas: dict[str, float] = {}
    for c in compound_names:
        deltas[c] = outlet_mols[c] - inlet_mols[c]

    # Find base compound (largest consumed species)
    consumed = {c: -d for c, d in deltas.items() if d < -1e-10}
    if not consumed:
        return {
            "base_compound": "",
            "conversion": 0.0,
            "stoichiometry": {},
            "warnings": ["No species consumed — cannot determine reaction stoichiometry"],
        }

    base = max(consumed, key=consumed.get)  # type: ignore[arg-type]
    base_delta = deltas[base]

    # Normalize stoichiometry relative to base compound = -1
    stoich: dict[str, float] = {}
    for c in compound_names:
        if abs(deltas[c]) > 1e-10:
            stoich[c] = round(deltas[c] / abs(base_delta), 4)

    # Conversion of base compound
    conversion = consumed[base] / inlet_mols[base] if inlet_mols[base] > 1e-10 else 0.0

    if abs(conversion) > 1.0:
        warnings.append(
            f"Calculated conversion of {base} is {conversion:.4f} (>1.0) — "
            "stoichiometry may be ambiguous due to recycle or side reactions"
        )
        conversion = min(conversion, 1.0)

    return {
        "base_compound": base,
        "conversion": round(conversion, 6),
        "stoichiometry": stoich,
        "warnings": warnings,
    }


# =====================================================================
# Main converter function
# =====================================================================


def convert_fsd_to_dwsim(
    fsd_file_path: str,
    output_file_path: str = "",
    property_package: str = "",
    solve: bool = True,
) -> dict:
    """
    Convert a COCO simulator ``.fsd`` flowsheet to DWSIM ``.dwxmz`` format.

    Parses the FSD file, maps compounds and property packages, builds
    the DWSIM flowsheet, saves it, and optionally solves it.

    Parameters
    ----------
    fsd_file_path : str
        Path to the ``.fsd`` file on the server filesystem.
    output_file_path : str
        Path for the output ``.dwxmz`` file.  Defaults to the same
        directory/name as the FSD file with a ``.dwxmz`` extension.
    property_package : str
        DWSIM property package to use.  If empty, the tool maps from
        the COCO property package or defaults to Peng-Robinson.
    solve : bool
        Whether to attempt solving the flowsheet after building it.

    Returns
    -------
    dict
        Keys: success, file_path, converged, compound_mapping,
              topology_report, warnings, unsupported_unit_ops, error.
    """
    result: dict[str, Any] = {
        "success": False,
        "file_path": None,
        "converged": None,
        "compound_mapping": {},
        "topology_report": {},
        "warnings": [],
        "unsupported_unit_ops": [],
        "error": None,
    }

    try:
        # ── 1. Parse the FSD file ────────────────────────────────
        if not os.path.isfile(fsd_file_path):
            result["error"] = f"FSD file not found: {fsd_file_path}"
            return result

        topology = _parse_fsd(fsd_file_path)
        LOGGER.info(
            "Parsed FSD: %d compounds, %d streams, %d unit ops",
            len(topology["compounds"]),
            len(topology["streams"]),
            len(topology["unit_ops"]),
        )

        result["topology_report"] = {
            "coco_compounds": [c["name"] for c in topology["compounds"]],
            "coco_streams": [s["name"] for s in topology["streams"]],
            "coco_unit_ops": [
                {"name": u["name"], "type": u["type"]}
                for u in topology["unit_ops"]
            ],
            "coco_property_package": topology["property_package"],
        }

        if not topology["compounds"]:
            result["error"] = "No compounds found in FSD file"
            return result

        # ── 2. Create DWSIM flowsheet and map compounds ──────────
        from dwsim_tools.clr_helpers import get_automation

        automation = get_automation()
        flowsheet = automation.CreateFlowsheet()

        compound_map, map_warnings = _map_compounds_to_dwsim(
            topology["compounds"],
            flowsheet.AvailableCompounds,
        )
        result["warnings"].extend(map_warnings)
        result["compound_mapping"] = compound_map

        unmapped = [
            c["name"] for c in topology["compounds"]
            if c["name"] not in compound_map
        ]
        if unmapped:
            result["error"] = (
                f"Could not map these COCO compounds to DWSIM: {unmapped}. "
                "Provide correct DWSIM names or check CAS numbers."
            )
            return result

        # Add mapped compounds to flowsheet
        for coco_name, dwsim_name in compound_map.items():
            comp = flowsheet.AvailableCompounds[dwsim_name]
            flowsheet.SelectedCompounds.Add(dwsim_name, comp)

        # ── 3. Attach property package ───────────────────────────
        if property_package:
            pp_name = property_package
        else:
            coco_pp = topology["property_package"]
            pp_name = _PP_MAP.get(coco_pp, "")
            if not pp_name:
                pp_name = _DEFAULT_PP
                result["warnings"].append(
                    f"COCO property package '{coco_pp}' has no direct DWSIM "
                    f"mapping. Using '{_DEFAULT_PP}' as default."
                )

        from dwsim_tools.tools.flowsheet import _PROPERTY_PACKAGES
        from DWSIM.Thermodynamics import PropertyPackages as PP

        pp_class = _PROPERTY_PACKAGES.get(pp_name)
        if pp_class is None:
            result["warnings"].append(
                f"Property package '{pp_name}' not found in DWSIM. "
                f"Falling back to '{_DEFAULT_PP}'."
            )
            pp_class = _PROPERTY_PACKAGES[_DEFAULT_PP]
            pp_name = _DEFAULT_PP

        pp_instance = getattr(PP, pp_class)()
        flowsheet.AddPropertyPackage(pp_instance)
        result["topology_report"]["dwsim_property_package"] = pp_name

        # ── 4. Identify feed vs. outlet streams ──────────────────
        from DWSIM.Interfaces.Enums.GraphicObjects import ObjectType

        # Build connectivity map from unit operations
        feed_stream_names: set[str] = set()
        outlet_stream_names: set[str] = set()

        for uo in topology["unit_ops"]:
            for conn in uo["connections"]:
                if conn["type"] != "material":
                    continue
                if conn["feed"]:
                    feed_stream_names.add(conn["stream"])
                else:
                    outlet_stream_names.add(conn["stream"])

        # Streams that are only feeds (not produced by any unit op)
        pure_feed_streams = feed_stream_names - outlet_stream_names

        # ── 5. Add feed streams ──────────────────────────────────
        stream_lookup: dict[str, dict] = {s["name"]: s for s in topology["streams"]}
        added_streams: set[str] = set()

        for sname in sorted(pure_feed_streams):
            sdata = stream_lookup.get(sname)
            if not sdata:
                result["warnings"].append(f"Feed stream '{sname}' referenced but not found in FSD")
                continue

            stream_obj = flowsheet.AddObject(ObjectType.MaterialStream, 0, 0, sname)
            stream_obj.SetPropertyValue("PROP_MS_0", float(sdata["temperature"]))
            stream_obj.SetPropertyValue("PROP_MS_1", float(sdata["pressure"]))
            stream_obj.SetPropertyValue("PROP_MS_3", float(sdata["molar_flow"]))

            for coco_name, frac in sdata["mole_fractions"].items():
                dwsim_name = compound_map.get(coco_name, coco_name)
                stream_obj.SetPropertyValue(f"PROP_MS_102/{dwsim_name}", float(frac))

            added_streams.add(sname)

        # ── 6. Add unit operations ───────────────────────────────
        for uo in topology["unit_ops"]:
            uo_name = uo["name"]
            uo_type = uo["type"]

            # Get inlet/outlet stream names for this unit
            uo_inlets = [c["stream"] for c in uo["connections"] if c["feed"] and c["type"] == "material"]
            uo_outlets = [c["stream"] for c in uo["connections"] if not c["feed"] and c["type"] == "material"]

            if uo_type in ("CSTR", "PFR"):
                # Back-calculate stoichiometry from solved data
                inlet_data = [stream_lookup[s] for s in uo_inlets if s in stream_lookup]
                outlet_data = [stream_lookup[s] for s in uo_outlets if s in stream_lookup]
                coco_names = [c["name"] for c in topology["compounds"]]

                rxn_info = _calculate_reactor_stoichiometry(inlet_data, outlet_data, coco_names)
                result["warnings"].extend(rxn_info["warnings"])

                if not rxn_info["base_compound"]:
                    result["warnings"].append(
                        f"Reactor '{uo_name}': could not determine reaction. "
                        "Adding as pass-through."
                    )
                    continue

                # Map stoichiometry to DWSIM compound names
                dwsim_stoich = {}
                for coco_name, coeff in rxn_info["stoichiometry"].items():
                    dwsim_name = compound_map.get(coco_name, coco_name)
                    dwsim_stoich[dwsim_name] = coeff

                base_dwsim = compound_map.get(rxn_info["base_compound"], rxn_info["base_compound"])

                # Create vapor and liquid outlet stream names
                vap_out = f"{uo_name}_VapOut"
                liq_out = f"{uo_name}_LiqOut"

                inlet_name = uo_inlets[0] if uo_inlets else ""

                # Build reaction set JSON
                rxn_set = {
                    "base_compound": base_dwsim,
                    "conversion": rxn_info["conversion"],
                    "stoichiometry": dwsim_stoich,
                    "reaction_phase": "Mixture",
                }

                from dwsim_tools.tools.unit_operations import add_conversion_reactor

                rx_result = add_conversion_reactor(
                    flowsheet=flowsheet,
                    name=uo_name,
                    inlet_stream_name=inlet_name,
                    vapor_outlet_name=vap_out,
                    liquid_outlet_name=liq_out,
                    reaction_set=json.dumps(rxn_set),
                )

                if not rx_result.get("success"):
                    result["warnings"].append(
                        f"Reactor '{uo_name}' failed: {rx_result.get('error')}"
                    )
                    continue

                added_streams.update([vap_out, liq_out])

                # Add mixer to recombine vapor+liquid outlets
                mixer_name = f"{uo_name}_Mix"
                from dwsim_tools.tools.unit_operations import add_mixer

                # The original outlet stream(s) become the mixer outlet
                original_outlet = uo_outlets[0] if uo_outlets else f"{uo_name}_Out"
                mix_out_name = f"{mixer_name}_Out"

                mix_result = add_mixer(
                    flowsheet=flowsheet,
                    name=mixer_name,
                    inlet_stream_names=f"{vap_out},{liq_out}",
                    outlet_stream_name=mix_out_name,
                )

                if not mix_result.get("success"):
                    result["warnings"].append(
                        f"Mixer '{mixer_name}' failed: {mix_result.get('error')}"
                    )

                added_streams.add(mix_out_name)

                # Add cooler to match outlet temperature if different from inlet
                if outlet_data and inlet_data:
                    outlet_T = outlet_data[0].get("solved_temperature") or outlet_data[0].get("temperature", 0)
                    inlet_T = inlet_data[0].get("temperature", 0)

                    if outlet_T > 0 and abs(outlet_T - inlet_T) > 1.0:
                        cooler_name = f"{uo_name}_TAdj"
                        outlet_stream_name = original_outlet
                        energy_name = f"Q-{cooler_name}"

                        from dwsim_tools.tools.unit_operations import add_cooler

                        cool_result = add_cooler(
                            flowsheet=flowsheet,
                            name=cooler_name,
                            inlet_stream_name=mix_out_name,
                            outlet_stream_name=outlet_stream_name,
                            outlet_temperature=float(outlet_T),
                            pressure_drop=0.0,
                            energy_stream_name=energy_name,
                        )

                        if cool_result.get("success"):
                            added_streams.add(outlet_stream_name)
                            result["warnings"].append(
                                f"Added temperature adjustment block '{cooler_name}' "
                                f"({inlet_T:.1f}K → {outlet_T:.1f}K) to match "
                                f"COCO outlet temperature for reactor '{uo_name}'."
                            )
                        else:
                            result["warnings"].append(
                                f"Temperature adjustment '{cooler_name}' failed: "
                                f"{cool_result.get('error')}"
                            )
                    else:
                        # No temperature adjustment needed — rename mixer output
                        # to match original outlet name
                        pass

            elif uo_type == "Mixer":
                from dwsim_tools.tools.unit_operations import add_mixer

                inlet_names = ",".join(uo_inlets)
                outlet_name = uo_outlets[0] if uo_outlets else f"{uo_name}_Out"

                add_mixer(
                    flowsheet=flowsheet,
                    name=uo_name,
                    inlet_stream_names=inlet_names,
                    outlet_stream_name=outlet_name,
                )
                added_streams.add(outlet_name)

            elif uo_type == "Splitter":
                from dwsim_tools.tools.unit_operations import add_splitter

                inlet_name = uo_inlets[0] if uo_inlets else ""
                # DWSIM splitter takes outlet names
                outlet_names = ",".join(uo_outlets) if uo_outlets else f"{uo_name}_Out1,{uo_name}_Out2"

                add_splitter(
                    flowsheet=flowsheet,
                    name=uo_name,
                    inlet_stream_name=inlet_name,
                    outlet_stream_names=outlet_names,
                )
                added_streams.update(uo_outlets)

            elif uo_type in ("Heater", "Cooler"):
                inlet_name = uo_inlets[0] if uo_inlets else ""
                outlet_name = uo_outlets[0] if uo_outlets else f"{uo_name}_Out"
                outlet_data = [stream_lookup[s] for s in uo_outlets if s in stream_lookup]
                outlet_T = outlet_data[0].get("solved_temperature", 0) if outlet_data else 0

                if uo_type == "Cooler":
                    from dwsim_tools.tools.unit_operations import add_cooler

                    add_cooler(
                        flowsheet=flowsheet,
                        name=uo_name,
                        inlet_stream_name=inlet_name,
                        outlet_stream_name=outlet_name,
                        outlet_temperature=float(outlet_T) if outlet_T > 0 else 300.0,
                        pressure_drop=0.0,
                        energy_stream_name=f"Q-{uo_name}",
                    )
                else:
                    from dwsim_tools.tools.unit_operations import add_heater

                    add_heater(
                        flowsheet=flowsheet,
                        name=uo_name,
                        inlet_stream_name=inlet_name,
                        outlet_stream_name=outlet_name,
                        outlet_temperature=float(outlet_T) if outlet_T > 0 else 300.0,
                        pressure_drop=0.0,
                        energy_stream_name=f"Q-{uo_name}",
                    )
                added_streams.add(outlet_name)

            elif uo_type == "Separator":
                from dwsim_tools.tools.unit_operations import add_separator

                inlet_name = uo_inlets[0] if uo_inlets else ""
                vap_out = uo_outlets[0] if len(uo_outlets) > 0 else f"{uo_name}_Vap"
                liq_out = uo_outlets[1] if len(uo_outlets) > 1 else f"{uo_name}_Liq"

                add_separator(
                    flowsheet=flowsheet,
                    name=uo_name,
                    inlet_stream_name=inlet_name,
                    vapor_outlet_name=vap_out,
                    liquid_outlet_name=liq_out,
                    energy_stream_name=f"Q-{uo_name}",
                )
                added_streams.update([vap_out, liq_out])

            elif uo_type == "Valve":
                from dwsim_tools.tools.unit_operations import add_valve

                inlet_name = uo_inlets[0] if uo_inlets else ""
                outlet_name = uo_outlets[0] if uo_outlets else f"{uo_name}_Out"
                outlet_data = [stream_lookup[s] for s in uo_outlets if s in stream_lookup]
                outlet_P = outlet_data[0].get("solved_pressure", 101325) if outlet_data else 101325

                add_valve(
                    flowsheet=flowsheet,
                    name=uo_name,
                    inlet_stream_name=inlet_name,
                    outlet_stream_name=outlet_name,
                    outlet_pressure=float(outlet_P),
                )
                added_streams.add(outlet_name)

            elif uo_type == "Pump":
                from dwsim_tools.tools.unit_operations import add_pump

                inlet_name = uo_inlets[0] if uo_inlets else ""
                outlet_name = uo_outlets[0] if uo_outlets else f"{uo_name}_Out"
                outlet_data = [stream_lookup[s] for s in uo_outlets if s in stream_lookup]
                outlet_P = outlet_data[0].get("solved_pressure", 101325) if outlet_data else 101325

                add_pump(
                    flowsheet=flowsheet,
                    name=uo_name,
                    inlet_stream_name=inlet_name,
                    outlet_stream_name=outlet_name,
                    outlet_pressure=float(outlet_P),
                    energy_stream_name=f"Q-{uo_name}",
                )
                added_streams.add(outlet_name)

            elif uo_type == "Compressor":
                from dwsim_tools.tools.unit_operations import add_compressor

                inlet_name = uo_inlets[0] if uo_inlets else ""
                outlet_name = uo_outlets[0] if uo_outlets else f"{uo_name}_Out"
                outlet_data = [stream_lookup[s] for s in uo_outlets if s in stream_lookup]
                outlet_P = outlet_data[0].get("solved_pressure", 101325) if outlet_data else 101325

                add_compressor(
                    flowsheet=flowsheet,
                    name=uo_name,
                    inlet_stream_name=inlet_name,
                    outlet_stream_name=outlet_name,
                    outlet_pressure=float(outlet_P),
                    energy_stream_name=f"Q-{uo_name}",
                )
                added_streams.add(outlet_name)

            else:
                result["unsupported_unit_ops"].append({
                    "name": uo_name,
                    "type": uo_type,
                    "inlets": uo_inlets,
                    "outlets": uo_outlets,
                })
                result["warnings"].append(
                    f"Unit operation '{uo_name}' (type: {uo_type}) is not "
                    "supported for automatic conversion."
                )

        # ── 7. Save the flowsheet ────────────────────────────────
        if not output_file_path:
            base = os.path.splitext(fsd_file_path)[0]
            output_file_path = f"{base}_converted.dwxmz"

        parent = os.path.dirname(output_file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        automation.SaveFlowsheet2(flowsheet, output_file_path)
        result["file_path"] = output_file_path
        result["success"] = True

        LOGGER.info("Saved converted flowsheet to %s", output_file_path)

        # ── 8. Optionally solve ──────────────────────────────────
        if solve:
            try:
                from dwsim_tools.tools.flowsheet import solve_flowsheet

                solve_result = solve_flowsheet(flowsheet)
                result["converged"] = solve_result.get("converged", False)

                if not result["converged"]:
                    result["warnings"].append(
                        "Flowsheet did not converge. Check stream specs and "
                        "unit operation parameters."
                    )
                else:
                    # Re-save after solving so results are persisted
                    automation.SaveFlowsheet2(flowsheet, output_file_path)

            except Exception as solve_exc:
                result["converged"] = False
                result["warnings"].append(f"Solve failed: {solve_exc}")

    except ValueError as ve:
        result["error"] = str(ve)
    except Exception as e:
        result["error"] = f"Conversion failed: {e}"

    return result
