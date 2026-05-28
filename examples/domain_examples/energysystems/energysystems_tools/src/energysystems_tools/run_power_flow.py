"""Run AC or DC power flow on a PyPSA network."""

from __future__ import annotations

import builtins


def _get_network(name: str):
    """Retrieve a previously created network by name."""
    networks = getattr(builtins, "_pypsa_networks", {})
    if name not in networks:
        raise ValueError(f"Network {name!r} not found. Call define_network first. Available: {sorted(networks)}")
    return networks[name]


def run_power_flow(
    network_name: str,
    method: str = "ac",
) -> dict:
    """Run power flow analysis on the network.

    Args:
        network_name: Name of a previously defined network.
        method: ``"ac"`` for Newton-Raphson or ``"dc"`` for linear
            approximation (default: ``"ac"``).

    Returns:
        Dictionary with ``converged``, ``method``, ``bus_results``
        (per-bus voltages and power), and ``line_loading`` (per-line
        loading and flows).

    Raises:
        ValueError: If the network is not found or method is invalid.
    """
    n = _get_network(network_name)

    if method not in ("ac", "dc"):
        raise ValueError(f"method must be 'ac' or 'dc', got {method!r}")

    if method == "ac":
        result = n.pf()
        # pf() returns a dict with convergence info
        if hasattr(result, "__getitem__") and "converged" in result:
            converged = bool(result["converged"].all())
        else:
            converged = True
    else:
        # lpf() returns None in modern PyPSA; check buses_t for results
        n.lpf()
        converged = hasattr(n.buses_t, "p") and len(n.buses_t.p.columns) > 0

    # Extract bus results
    bus_results = []
    for bus_name in n.buses.index:
        entry = {"bus": bus_name}
        if method == "ac":
            if bus_name in n.buses_t.v_mag_pu.columns:
                entry["v_mag_pu"] = round(float(n.buses_t.v_mag_pu[bus_name].mean()), 4)
            if bus_name in n.buses_t.v_ang.columns:
                entry["v_ang"] = round(float(n.buses_t.v_ang[bus_name].mean()), 4)
        if hasattr(n.buses_t, "p") and bus_name in n.buses_t.p.columns:
            entry["p"] = round(float(n.buses_t.p[bus_name].mean()), 4)
        if hasattr(n.buses_t, "q") and bus_name in n.buses_t.q.columns:
            entry["q"] = round(float(n.buses_t.q[bus_name].mean()), 4)
        bus_results.append(entry)

    # Extract line loading
    line_loading = []
    for line_name in n.lines.index:
        entry = {"line": line_name}
        if hasattr(n.lines_t, "p0") and line_name in n.lines_t.p0.columns:
            p0_mean = float(n.lines_t.p0[line_name].mean())
            entry["p0_mean_mw"] = round(p0_mean, 4)
            s_nom = float(n.lines.loc[line_name, "s_nom"]) if "s_nom" in n.lines.columns else 0
            if s_nom > 0:
                entry["loading_pct"] = round(abs(p0_mean) / s_nom * 100, 2)
        line_loading.append(entry)

    return {
        "converged": converged,
        "method": method,
        "bus_results": bus_results,
        "line_loading": line_loading,
    }
