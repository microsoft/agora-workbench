"""Attach time-varying profiles to network components."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    import pypsa


def add_time_series(
    network: pypsa.Network,
    profiles: list[dict],
) -> dict:
    """Attach time-varying profiles to existing network components.

    Args:
        network: Live PyPSA network object returned by ``define_network``.
        profiles: List of profile dicts, each with:
            - ``component_type``: PyPSA component type (e.g. "generators", "loads")
            - ``component_name``: Name of the component
            - ``attribute``: Time-varying attribute (e.g. "p_max_pu", "p_set")
            - ``values``: List of numeric values matching snapshot count

    Returns:
        Dictionary with ``num_profiles_attached``, ``snapshot_count``,
        and ``components`` (list of updated component names).

    Raises:
        ValueError: If values length mismatches snapshot count.
    """
    n = network
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
            raise ValueError(f"Unknown component_type {comp_type!r}. Expected one of: {sorted(type_map)}")

        t_df = getattr(n, t_attr)
        if attribute not in t_df:
            t_df[attribute] = pd.DataFrame(index=n.snapshots)
        df = t_df[attribute]
        df.loc[:, comp_name] = values
        t_df[attribute] = df

        updated_components.append(comp_name)

    return {
        "num_profiles_attached": len(profiles),
        "snapshot_count": snapshot_count,
        "components": updated_components,
    }
