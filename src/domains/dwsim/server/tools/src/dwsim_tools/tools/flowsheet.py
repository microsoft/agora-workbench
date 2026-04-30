"""
DWSIM Flowsheet Lifecycle Tools.

Create, load, and solve chemical process flowsheets using the
DWSIM Automation3 API.
"""

# ---------------------------------------------------------------------------
# Property-package name → DWSIM class mapping
# ---------------------------------------------------------------------------
_PROPERTY_PACKAGES = {
    "Peng-Robinson": "PengRobinsonPropertyPackage",
    "SRK": "SoaveRedlichKwongPropertyPackage",
    "NRTL": "NRTLPropertyPackage",
    "UNIQUAC": "UNIQUACPropertyPackage",
    "Raoult's Law": "RaoultPropertyPackage",
    "Lee-Kesler-Plocker": "LeeKeslerPlockerPropertyPackage",
    "UNIFAC": "UNIFACPropertyPackage",
    "Modified UNIFAC (Dortmund)": "MODFACPropertyPackage",
    "Steam Tables (IAPWS-IF97)": "SteamTablesPropertyPackage",
    "CoolProp": "CoolPropPropertyPackage",
}


def search_compounds(query: str = "") -> dict:
    """
    Search the DWSIM compound database.

    With no query, returns every available compound name.  With a query,
    returns only compounds whose name contains the query string
    (case-insensitive).

    Parameters
    ----------
    query : str, optional
        Substring to filter compound names (e.g. ``"ethanol"``,
        ``"acet"``, ``"methyl"``).  Empty string returns all compounds.

    Returns
    -------
    dict
        ``{"success": bool, "compounds": list[str], "count": int,
        "query": str, "error": str | None}``
    """
    result = {"success": False, "compounds": [], "count": 0, "query": query, "error": None}

    try:
        from dwsim_tools.clr_helpers import get_automation

        automation = get_automation()
        flowsheet = automation.CreateFlowsheet()

        all_names = sorted(flowsheet.AvailableCompounds.Keys)

        if query:
            q = query.lower()
            matches = [n for n in all_names if q in n.lower()]
        else:
            matches = all_names

        result["compounds"] = matches
        result["count"] = len(matches)
        result["success"] = True

    except Exception as e:
        result["error"] = f"Failed to search compounds: {e}"

    return result


def create_flowsheet(compounds: str, property_package: str) -> dict:
    """
    Create a new DWSIM flowsheet with the specified compounds and
    thermodynamic property package.

    Parameters
    ----------
    compounds : str
        Compound names as they appear in the DWSIM compound database.
        Use semicolons as delimiters when any name contains a comma
        (e.g. ``"2,2,4-Trimethylpentane;Water;Ethanol"``), otherwise
        commas work too (e.g. ``"Water,Ethanol,Methanol"``).
    property_package : str
        Name of the thermodynamic property package to attach. Supported
        values: ``Peng-Robinson``, ``SRK``, ``NRTL``, ``UNIQUAC``,
        ``Raoult's Law``, ``Lee-Kesler-Plocker``, ``UNIFAC``,
        ``Modified UNIFAC (Dortmund)``, ``Steam Tables (IAPWS-IF97)``,
        ``CoolProp``.

    Returns
    -------
    dict
        ``{"success": bool, "flowsheet": <handle>, "error": str | None}``
    """
    result = {"success": False, "flowsheet": None, "error": None}

    try:
        from dwsim_tools.clr_helpers import get_automation

        automation = get_automation()
        flowsheet = automation.CreateFlowsheet()

        # Add compounds from the DWSIM database.
        # Use semicolons as delimiter when compound names contain commas
        # (e.g. "2,2,4-trimethylpentane"); fall back to commas otherwise.
        sep = ";" if ";" in compounds else ","
        for name in (c.strip() for c in compounds.split(sep)):
            if not name:
                continue
            comp = flowsheet.AvailableCompounds[name]
            flowsheet.SelectedCompounds.Add(name, comp)

        # Attach a thermodynamic property package
        pp_class = _PROPERTY_PACKAGES.get(property_package)
        if pp_class is None:
            supported = ", ".join(sorted(_PROPERTY_PACKAGES.keys()))
            result["error"] = f"Unknown property package '{property_package}'. Supported: {supported}"
            return result

        from DWSIM.Thermodynamics import PropertyPackages as PP

        pp_instance = getattr(PP, pp_class)()
        flowsheet.AddPropertyPackage(pp_instance)

        result["flowsheet"] = flowsheet
        result["success"] = True

    except Exception as e:
        result["error"] = f"Failed to create flowsheet: {e}"

    return result


def load_flowsheet(file_path: str) -> dict:
    """
    Load an existing DWSIM flowsheet from a ``.dwxmz`` or ``.dwxml`` file.

    Parameters
    ----------
    file_path : str
        Absolute path to the flowsheet file on the server file-system.

    Returns
    -------
    dict
        ``{"success": bool, "flowsheet": <handle>, "error": str | None}``
    """
    result = {"success": False, "flowsheet": None, "error": None}

    try:
        from dwsim_tools.clr_helpers import get_automation

        automation = get_automation()
        flowsheet = automation.LoadFlowsheet(file_path)

        result["flowsheet"] = flowsheet
        result["success"] = True

    except Exception as e:
        result["error"] = f"Failed to load flowsheet: {e}"

    return result


def save_flowsheet(flowsheet: object, file_path: str) -> dict:
    """
    Save a DWSIM flowsheet to a file.

    Uses the DWSIM Automation3 interface to persist the flowsheet.
    The file format is determined by the extension:
    ``.dwxmz`` produces a compressed XML archive, ``.dwxml`` produces
    plain XML.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle returned by :func:`create_flowsheet` or
        :func:`load_flowsheet`.
    file_path : str
        Absolute destination path (e.g. ``"/tmp/my_process.dwxmz"``).

    Returns
    -------
    dict
        ``{"success": bool, "file_path": str | None, "error": str | None}``
    """
    result = {"success": False, "file_path": None, "error": None}

    try:
        import os

        from dwsim_tools.clr_helpers import get_automation

        automation = get_automation()

        # Ensure the parent directory exists
        parent = os.path.dirname(file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # Choose compressed or plain XML based on extension
        if file_path.lower().endswith(".dwxmz"):
            automation.SaveFlowsheet2(flowsheet, file_path)
        else:
            automation.SaveFlowsheet(flowsheet, file_path)

        result["file_path"] = file_path
        result["success"] = True

    except Exception as e:
        result["error"] = f"Failed to save flowsheet: {e}"

    return result


def solve_flowsheet(flowsheet: object) -> dict:
    """
    Solve (calculate) a DWSIM flowsheet.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.

    Returns
    -------
    dict
        ``{"success": bool, "converged": bool, "error_messages": list[str], "error": str | None}``
    """
    result = {
        "success": False,
        "converged": False,
        "error_messages": [],
        "error": None,
    }

    try:
        from dwsim_tools.clr_helpers import get_automation

        automation = get_automation()
        automation.CalculateFlowsheet4(flowsheet)

        # Collect per-object error messages, if any
        errors = []
        for key in flowsheet.SimulationObjects.Keys:
            obj = flowsheet.SimulationObjects[key]
            err_msg = obj.ErrorMessage
            if err_msg:
                tag = obj.GraphicObject.Tag if obj.GraphicObject else str(key)
                errors.append(f"{tag}: {err_msg}")

        result["error_messages"] = errors
        result["converged"] = len(errors) == 0
        result["success"] = True

    except Exception as e:
        result["error"] = f"Failed to solve flowsheet: {e}"

    return result
