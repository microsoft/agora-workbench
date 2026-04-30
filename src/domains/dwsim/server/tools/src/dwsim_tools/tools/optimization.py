"""
DWSIM Sensitivity Analysis & Optimization Tools.

Run parametric sweeps and numerical optimizations on solved flowsheets.

All variable/objective access goes through the ``SetPropertyValue`` /
``GetPropertyValue`` interface using PROP_XX_N codes, **not** direct
attribute access, because pythonnet wraps DWSIM objects as
``ISimulationObject`` interfaces.
"""

from __future__ import annotations

import json
import logging

LOGGER = logging.getLogger(__name__)


def run_sensitivity_analysis(
    flowsheet: object,
    variable_object: str,
    variable_property: str,
    min_value: float,
    max_value: float,
    num_points: int,
    objective_object: str,
    objective_property: str,
) -> dict:
    """
    Sweep a single variable across a range and record an objective.

    At each point the flowsheet is re-solved and the objective is sampled.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle (should already be solved at the base case).
    variable_object : str
        Tag of the flowsheet object whose property is varied (e.g. a stream
        or unit-operation tag).
    variable_property : str
        DWSIM property code to vary (e.g. ``"PROP_MS_0"`` for temperature,
        ``"PROP_HT_2"`` for heater outlet T).
    min_value, max_value : float
        Lower and upper bounds for the sweep.
    num_points : int
        Number of evenly spaced points (including endpoints).
    objective_object : str
        Tag of the flowsheet object from which the objective is read.
    objective_property : str
        DWSIM property code to read as the objective (e.g. ``"PROP_MS_0"``,
        ``"PROP_HT_3"``).

    Returns
    -------
    dict
        ``{"success", "variable_values", "objective_values", "error"}``
    """
    result = {
        "success": False,
        "variable_values": [],
        "objective_values": [],
        "error": None,
    }

    try:
        from dwsim_tools.clr_helpers import get_automation

        automation = get_automation()

        # --- input validation ---
        if not isinstance(num_points, int) and not (isinstance(num_points, float) and num_points == int(num_points)):
            result["error"] = f"num_points must be an integer, got {num_points!r} (type {type(num_points).__name__})."
            return result
        num_points = int(num_points)
        if num_points < 2:
            result["error"] = f"num_points must be >= 2, got {num_points}."
            return result
        if min_value >= max_value:
            result["error"] = f"min_value ({min_value}) must be less than max_value ({max_value})."
            return result
        # --- end validation ---

        var_obj = flowsheet.GetFlowsheetSimulationObject(variable_object)
        obj_obj = flowsheet.GetFlowsheetSimulationObject(objective_object)

        if var_obj is None:
            result["error"] = f"Variable object '{variable_object}' not found."
            return result
        if obj_obj is None:
            result["error"] = f"Objective object '{objective_object}' not found."
            return result

        step = (max_value - min_value) / max(num_points - 1, 1)
        var_values = [min_value + i * step for i in range(int(num_points))]
        obj_values: list[float | None] = []

        for val in var_values:
            var_obj.SetPropertyValue(variable_property, float(val))
            automation.CalculateFlowsheet4(flowsheet)
            try:
                obj_val = float(obj_obj.GetPropertyValue(objective_property))
            except Exception:
                LOGGER.debug("Could not read objective property '%s' at val=%s", objective_property, val, exc_info=True)
                obj_val = None
            obj_values.append(obj_val)

        result["variable_values"] = var_values
        result["objective_values"] = obj_values
        result["success"] = True

    except Exception as e:
        result["error"] = f"Sensitivity analysis failed: {e}"

    return result


def run_optimization(
    flowsheet: object,
    objective_object: str,
    objective_property: str,
    minimize: bool,
    variables: str,
    constraints: str,
) -> dict:
    """
    Run a numerical optimization on the flowsheet.

    Uses SciPy's ``minimize`` (or ``maximize`` by negation) with the
    Nelder-Mead method. Each evaluation re-solves the flowsheet.

    Parameters
    ----------
    flowsheet : object
        Flowsheet handle.
    objective_object : str
        Tag of the object whose property is the objective.
    objective_property : str
        DWSIM property code on that object (e.g. ``"PROP_HT_3"``).
    minimize : bool
        ``True`` to minimise, ``False`` to maximise.
    variables : str
        JSON list of decision-variable specs::

            [{"object": "HTR-1", "property": "PROP_HT_2", "min": 350, "max": 500, "initial": 400}, ...]

    constraints : str
        JSON list of inequality constraints (``value >= 0`` convention)::

            [{"object": "S-OUT", "property": "PROP_MS_0", "type": ">=", "value": 300}, ...]

        Pass ``"[]"`` if there are no constraints.

    Returns
    -------
    dict
        ``{"success", "optimal_values", "objective_value", "error"}``
    """
    result = {
        "success": False,
        "optimal_values": None,
        "objective_value": None,
        "error": None,
    }

    try:
        from dwsim_tools.clr_helpers import get_automation
        from scipy.optimize import minimize as sp_minimize

        automation = get_automation()

        var_specs = json.loads(variables)
        con_specs = json.loads(constraints)

        # --- input validation ---
        if not isinstance(var_specs, list):
            result["error"] = f"'variables' must be a JSON array of objects, got {type(var_specs).__name__}."
            return result
        if not isinstance(con_specs, list):
            result["error"] = f"'constraints' must be a JSON array of objects, got {type(con_specs).__name__}."
            return result

        _OPT_VAR_REQUIRED = {"object", "property", "min", "max"}
        for i, v in enumerate(var_specs):
            if not isinstance(v, dict):
                result["error"] = f"Variable spec [{i}] must be an object/dict, got {type(v).__name__}."
                return result
            missing = _OPT_VAR_REQUIRED - set(v.keys())
            if missing:
                result["error"] = (
                    f"Variable spec [{i}] is missing required key(s): "
                    f"{sorted(missing)}. Required: {sorted(_OPT_VAR_REQUIRED)}"
                )
                return result
            for key in ("min", "max"):
                try:
                    v[key] = float(v[key])
                except (TypeError, ValueError):
                    result["error"] = f"Variable spec [{i}]: '{key}' must be numeric, got {v[key]!r}."
                    return result
            if "initial" in v:
                try:
                    v["initial"] = float(v["initial"])
                except (TypeError, ValueError):
                    result["error"] = f"Variable spec [{i}]: 'initial' must be numeric, got {v['initial']!r}."
                    return result
            if v["min"] >= v["max"]:
                result["error"] = (
                    f"Variable spec [{i}] ({v['object']}.{v['property']}): "
                    f"'min' ({v['min']}) must be less than 'max' ({v['max']})."
                )
                return result

        _OPT_CON_REQUIRED = {"object", "property", "type", "value"}
        _OPT_CON_TYPES = {">=", "<="}
        for i, c in enumerate(con_specs):
            if not isinstance(c, dict):
                result["error"] = f"Constraint spec [{i}] must be an object/dict, got {type(c).__name__}."
                return result
            missing = _OPT_CON_REQUIRED - set(c.keys())
            if missing:
                result["error"] = (
                    f"Constraint spec [{i}] is missing required key(s): "
                    f"{sorted(missing)}. Required: {sorted(_OPT_CON_REQUIRED)}"
                )
                return result
            try:
                c["value"] = float(c["value"])
            except (TypeError, ValueError):
                result["error"] = f"Constraint spec [{i}]: 'value' must be numeric, got {c['value']!r}."
                return result
            if c["type"] not in _OPT_CON_TYPES:
                result["error"] = (
                    f"Constraint spec [{i}]: invalid type '{c['type']}'. Must be one of: {sorted(_OPT_CON_TYPES)}"
                )
                return result
        # --- end validation ---

        # Resolve .NET objects
        var_objs: list[tuple] = []
        x0: list[float] = []
        bounds: list[tuple[float, float]] = []
        for v in var_specs:
            obj = flowsheet.GetFlowsheetSimulationObject(v["object"])
            if obj is None:
                result["error"] = f"Variable object '{v['object']}' not found."
                return result
            var_objs.append((obj, v["property"]))
            x0.append(v.get("initial", (v["min"] + v["max"]) / 2))
            bounds.append((v["min"], v["max"]))

        obj_obj = flowsheet.GetFlowsheetSimulationObject(objective_object)
        if obj_obj is None:
            result["error"] = f"Objective object '{objective_object}' not found."
            return result

        def evaluate(x):
            # Apply decision variables via SetPropertyValue
            for (obj, prop), val in zip(var_objs, x):
                obj.SetPropertyValue(prop, float(val))
            automation.CalculateFlowsheet4(flowsheet)

            try:
                obj_val = float(obj_obj.GetPropertyValue(objective_property))
            except Exception:
                LOGGER.debug("Could not read objective property '%s' during evaluation", objective_property, exc_info=True)
                obj_val = 0.0
            sign = 1.0 if minimize else -1.0

            # Penalty for constraint violations
            penalty = 0.0
            for c in con_specs:
                c_obj = flowsheet.GetFlowsheetSimulationObject(c["object"])
                if c_obj is None:
                    continue
                try:
                    c_val = float(c_obj.GetPropertyValue(c["property"]))
                except Exception:
                    LOGGER.debug("Could not read constraint property '%s' on '%s'", c["property"], c["object"], exc_info=True)
                    c_val = 0.0
                bound = c["value"]
                if c["type"] == ">=" and c_val < bound:
                    penalty += (bound - c_val) ** 2 * 1e6
                elif c["type"] == "<=" and c_val > bound:
                    penalty += (c_val - bound) ** 2 * 1e6

            return sign * obj_val + penalty

        opt_result = sp_minimize(
            evaluate,
            x0,
            method="Nelder-Mead",
            bounds=bounds,
            options={"maxiter": 200, "xatol": 1e-3, "fatol": 1e-3},
        )

        optimal_vals = {}
        for spec, val in zip(var_specs, opt_result.x):
            optimal_vals[f"{spec['object']}.{spec['property']}"] = float(val)

        result["optimal_values"] = optimal_vals
        try:
            result["objective_value"] = float(obj_obj.GetPropertyValue(objective_property))
        except Exception:
            LOGGER.debug("Could not read final objective value for '%s'", objective_property, exc_info=True)
            result["objective_value"] = float(opt_result.fun)
        result["success"] = opt_result.success

    except Exception as e:
        result["error"] = f"Optimization failed: {e}"

    return result
