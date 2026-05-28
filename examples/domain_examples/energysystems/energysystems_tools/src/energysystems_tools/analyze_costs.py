"""Analyze costs from a solved optimal power flow."""

from __future__ import annotations

import builtins


def _get_network(name: str):
    """Retrieve a previously created network by name."""
    networks = getattr(builtins, "_pypsa_networks", {})
    if name not in networks:
        raise ValueError(f"Network {name!r} not found. Call define_network first. Available: {sorted(networks)}")
    return networks[name]


def analyze_costs(network_name: str) -> dict:
    """Analyze costs from a solved OPF: total cost, breakdown, marginal prices.

    Args:
        network_name: Name of a previously defined network with a solved OPF.

    Returns:
        Dictionary with ``total_cost``, ``cost_by_carrier``,
        ``marginal_price_stats``, and ``most_expensive_bus``.

    Raises:
        ValueError: If the network is not found or OPF has not been solved.
    """
    n = _get_network(network_name)

    # Check whether OPF dispatch results exist rather than testing
    # objective == 0 — a zero objective is valid (e.g. all-wind dispatch).
    has_dispatch = not n.generators_t.p.empty and len(n.generators_t.p.columns) > 0
    if not has_dispatch:
        raise ValueError(f"Network {network_name!r} has no solved OPF. Run run_optimal_power_flow first.")

    total_cost = round(float(n.objective), 4)

    # Cost by carrier — sum marginal_cost * dispatch for each generator
    cost_by_carrier: dict[str, float] = {}
    for gen_name in n.generators.index:
        gen = n.generators.loc[gen_name]
        carrier = str(gen.get("carrier", "other")) or "other"
        mc = float(gen.get("marginal_cost", 0))
        if "p" in n.generators_t and gen_name in n.generators_t.p.columns:
            total_gen = float(n.generators_t.p[gen_name].sum())
        else:
            total_gen = 0
        gen_cost = mc * total_gen
        cost_by_carrier[carrier] = round(cost_by_carrier.get(carrier, 0) + gen_cost, 4)

    # Marginal price stats per bus
    marginal_price_stats: dict[str, dict] = {}
    most_expensive_bus = ""
    max_avg_price = float("-inf")

    for bus_name in n.buses.index:
        if bus_name in n.buses_t.marginal_price.columns:
            mp = n.buses_t.marginal_price[bus_name]
            avg = float(mp.mean())
            marginal_price_stats[bus_name] = {
                "mean": round(avg, 4),
                "min": round(float(mp.min()), 4),
                "max": round(float(mp.max()), 4),
            }
            if avg > max_avg_price:
                max_avg_price = avg
                most_expensive_bus = bus_name

    return {
        "total_cost": total_cost,
        "cost_by_carrier": cost_by_carrier,
        "marginal_price_stats": marginal_price_stats,
        "most_expensive_bus": most_expensive_bus,
    }
