"""Run capacity expansion optimization on a PyPSA network."""

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


def run_capacity_expansion(network_name: str) -> dict:
    """Run investment optimization for extendable components.

    Generators and storage units with ``p_nom_extendable=True`` are
    optimized for both dispatch and capacity simultaneously.

    Args:
        network_name: Name of a previously defined network with time
            series attached and extendable components.

    Returns:
        Dictionary with ``status``, ``total_system_cost``,
        ``optimal_capacities``, and ``investment_by_type``.

    Raises:
        ValueError: If the network is not found.
    """
    n = _get_network(network_name)

    status, _ = n.optimize(solver_name="highs")

    # Optimal capacities for extendable components
    optimal_capacities = []

    for gen_name in n.generators.index:
        gen = n.generators.loc[gen_name]
        if gen.get("p_nom_extendable", False):
            optimal_capacities.append({
                "component": gen_name,
                "type": "generator",
                "carrier": str(gen.get("carrier", "")),
                "p_nom_opt_mw": round(float(gen.get("p_nom_opt", 0)), 4),
                "capital_cost": round(float(gen.get("capital_cost", 0)), 4),
            })

    for su_name in n.storage_units.index:
        su = n.storage_units.loc[su_name]
        if su.get("p_nom_extendable", False):
            optimal_capacities.append({
                "component": su_name,
                "type": "storage_unit",
                "carrier": str(su.get("carrier", "")),
                "p_nom_opt_mw": round(float(su.get("p_nom_opt", 0)), 4),
                "capital_cost": round(float(su.get("capital_cost", 0)), 4),
            })

    # Investment breakdown by carrier
    investment_by_type: dict[str, float] = {}
    for cap in optimal_capacities:
        carrier = cap["carrier"] or cap["component"]
        cost = cap["p_nom_opt_mw"] * cap["capital_cost"]
        investment_by_type[carrier] = round(
            investment_by_type.get(carrier, 0) + cost, 4
        )

    return {
        "status": status,
        "total_system_cost": round(float(n.objective), 4),
        "optimal_capacities": optimal_capacities,
        "investment_by_type": investment_by_type,
    }
