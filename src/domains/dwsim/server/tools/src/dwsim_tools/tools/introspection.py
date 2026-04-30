"""DWSIM Introspection Tools.

Small utilities to make the domain more usable without expanding the unit-op
surface area:
- discover available PROP_* codes on any flowsheet object
- get/set a single PROP_* value by code

These tools intentionally work through the ISimulationObject interface.
"""

from __future__ import annotations

import logging

LOGGER = logging.getLogger(__name__)


def list_object_properties(flowsheet: object, object_tag: str) -> dict:
    """List available property codes (PROP_*) for a flowsheet object."""
    result = {
        "success": False,
        "object_tag": object_tag,
        "properties": None,
        "error": None,
    }

    try:
        from dwsim_tools.clr_helpers import get_automation  # noqa: F401 – ensure CLR
        from DWSIM.Interfaces.Enums import PropertyType

        obj = flowsheet.GetFlowsheetSimulationObject(object_tag)
        if obj is None:
            result["error"] = f"Object '{object_tag}' not found in flowsheet."
            return result

        props = list(obj.GetProperties(PropertyType.ALL))
        result["properties"] = [str(p) for p in props]
        result["success"] = True

    except Exception as e:
        result["error"] = f"Failed to list properties: {e}"

    return result


def get_object_property(flowsheet: object, object_tag: str, property_code: str) -> dict:
    """Read a single PROP_* value from an object via GetPropertyValue."""
    result = {
        "success": False,
        "object_tag": object_tag,
        "property_code": property_code,
        "value": None,
        "error": None,
    }

    try:
        from dwsim_tools.clr_helpers import get_automation  # noqa: F401 – ensure CLR

        obj = flowsheet.GetFlowsheetSimulationObject(object_tag)
        if obj is None:
            result["error"] = f"Object '{object_tag}' not found in flowsheet."
            return result

        val = obj.GetPropertyValue(property_code)
        result["value"] = None if val is None else float(val)
        result["success"] = True

    except Exception as e:
        result["error"] = f"Failed to get property '{property_code}' on '{object_tag}': {e}"

    return result


def set_object_property(flowsheet: object, object_tag: str, property_code: str, value: float) -> dict:
    """Set a single PROP_* value on an object via SetPropertyValue."""
    result = {
        "success": False,
        "object_tag": object_tag,
        "property_code": property_code,
        "old_value": None,
        "new_value": None,
        "error": None,
    }

    try:
        from dwsim_tools.clr_helpers import get_automation  # noqa: F401 – ensure CLR

        obj = flowsheet.GetFlowsheetSimulationObject(object_tag)
        if obj is None:
            result["error"] = f"Object '{object_tag}' not found in flowsheet."
            return result

        try:
            old = obj.GetPropertyValue(property_code)
            result["old_value"] = None if old is None else float(old)
        except Exception:
            LOGGER.debug("Could not read old value for %s.%s", object_tag, property_code, exc_info=True)
            result["old_value"] = None

        obj.SetPropertyValue(property_code, float(value))

        try:
            new = obj.GetPropertyValue(property_code)
            result["new_value"] = None if new is None else float(new)
        except Exception:
            LOGGER.debug("Could not read new value for %s.%s", object_tag, property_code, exc_info=True)
            result["new_value"] = float(value)

        result["success"] = True

    except Exception as e:
        result["error"] = f"Failed to set property '{property_code}' on '{object_tag}': {e}"

    return result
