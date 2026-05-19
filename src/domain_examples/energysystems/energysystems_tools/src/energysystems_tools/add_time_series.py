"""Attach time-varying profiles to network components."""

from __future__ import annotations

import builtins

import pandas as pd


def _get_network(name: str):
    """Retrieve a previously created network by name."""
    networks = getattr(builtins, "_pypsa_networks", {})
    if name not in networks:
        raise ValueError(
            f"Network {name!r} not found. Call define_network first. "
            f"Available: {sorted(networks)}"
        )
    return networks[name]


def add_time_series(
    network_name: str,
    profiles: list[dict],
) -> dict:
    """Attach time-varying profiles to existing network components.

    Args:
        network_name: Name of a previously defined network.
        profiles: List of profile dicts, each with:
            - ``component_type``: PyPSA component type (e.g. "generators", "loads")
            - ``component_name``: Name of the component
            - ``attribute``: Time-varying attribute (e.g. "p_max_pu", "p_set")
            - ``values``: List of numeric values matching snapshot count

    Returns:
        Dictionary with ``num_profiles_attached``, ``snapshot_count``,
        and ``components`` (list of updated component names).

    Raises:
        ValueError: If the network is not found or values length mismatches
            snapshot count.
    """
    n = _get_network(network_name)
    snapshot_count = len(n.snapshots)
    updated_components = []

    for profile in profiles:
        comp_type = profile["component_type"]
        comp_name = profile["component_name"]
        attribute = profile["attribute"]
        values = profile["values"]

        if len(values) != snapshot_count:
            raise ValueError(
                f"Profile for {comp_name}.{attribute} has {len(values)} values "
                f"but network has {snapshot_count} snapshots."
            )

        # Map plural component type names to PyPSA's internal DataFrame names
        type_map = {
            "generators": "generators_t",
            "loads": "loads_t",
            "storage_units": "storage_units_t",
            "lines": "lines_t",
            "links": "links_t",
        }
        t_attr = type_map.get(comp_type)
        if t_attr is None:
            raise ValueError(
                f"Unknown component_type {comp_type!r}. "
                f"Expected one of: {sorted(type_map)}"
            )

        t_df = getattr(n, t_attr)
        if attribute not in t_df:
            t_df[attribute] = pd.DataFrame(index=n.snapshots)
        t_df[attribute][comp_name] = values

        updated_components.append(comp_name)

    return {
        "num_profiles_attached": len(profiles),
        "snapshot_count": snapshot_count,
        "components": updated_components,
    }
