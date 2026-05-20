"""Run linear optimal power flow (LOPF) on a PyPSA network."""

from __future__ import annotations

import builtins


def _get_network(name: str):
    """Retrieve a previously created network by name."""
    networks = getattr(builtins, "_pypsa_networks", {})
    if name not in networks:
        raise ValueError(
            f"Network {name!r} not found. Call define_network first. "
            f"Available: {sorted(networks)}"
        )
    return networks[name]


def run_optimal_power_flow(network_name: str) -> dict:
    """Run linear optimal power flow to minimize total generation cost.

    Args:
        network_name: Name of a previously defined network with generators
            that have ``marginal_cost`` set.

    Returns:
        Dictionary with ``status``, ``objective_value``,
        ``generator_dispatch``, ``line_flows``, and ``marginal_prices``.

    Raises:
        ValueError: If the network is not found.
    """
    n = _get_network(network_name)

    status, _ = n.optimize(solver_name="highs")

    # Generator dispatch
    generator_dispatch = []
    for gen_name in n.generators.index:
        entry = {"generator": gen_name}
        if "p" in n.generators_t:
            entry["p_mean_mw"] = round(float(n.generators_t.p[gen_name].mean()), 4)
            entry["p_max_mw"] = round(float(n.generators_t.p[gen_name].max()), 4)
        entry["carrier"] = str(n.generators.loc[gen_name].get("carrier", ""))
        generator_dispatch.append(entry)

    # Line flows
    line_flows = []
    for line_name in n.lines.index:
        entry = {"line": line_name}
        if "p0" in n.lines_t:
            entry["p0_mean_mw"] = round(float(n.lines_t.p0[line_name].mean()), 4)
            s_nom = float(n.lines.loc[line_name, "s_nom"]) if "s_nom" in n.lines.columns else 0
            if s_nom > 0:
                entry["loading_pct"] = round(
                    abs(float(n.lines_t.p0[line_name].mean())) / s_nom * 100, 2
                )
        line_flows.append(entry)

    # Marginal prices
    marginal_prices = []
    if "marginal_price" in n.buses_t:
        for bus_name in n.buses.index:
            mp_series = n.buses_t.marginal_price[bus_name]
            marginal_prices.append({
                "bus": bus_name,
                "mean": round(float(mp_series.mean()), 4),
                "min": round(float(mp_series.min()), 4),
                "max": round(float(mp_series.max()), 4),
            })

    return {
        "status": status,
        "objective_value": round(float(n.objective), 4),
        "generator_dispatch": generator_dispatch,
        "line_flows": line_flows,
        "marginal_prices": marginal_prices,
    }
