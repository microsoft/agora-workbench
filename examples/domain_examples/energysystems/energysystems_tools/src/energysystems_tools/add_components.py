"""Add buses, generators, loads, lines, and storage units to a network."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pypsa

def add_components(
    network: pypsa.Network,
    buses: list[dict] | None = None,
    generators: list[dict] | None = None,
    loads: list[dict] | None = None,
    lines: list[dict] | None = None,
    storage_units: list[dict] | None = None,
) -> dict:
    """Add components to an existing PyPSA network.

    Each component type is a list of dicts whose keys match PyPSA's
    component parameters (e.g. ``name``, ``bus``, ``p_nom``).

    Args:
        network: Live PyPSA network object returned by ``define_network``.
        buses: List of bus parameter dicts.
        generators: List of generator parameter dicts.
        loads: List of load parameter dicts.
        lines: List of line parameter dicts.
        storage_units: List of storage unit parameter dicts.

    Returns:
        Dictionary with counts per component type and a summary string.

    Raises:
        ValueError: If component parameter dictionaries are missing required keys.
    """
    n = network

    counts: dict[str, Any] = {
        "num_buses": 0,
        "num_generators": 0,
        "num_loads": 0,
        "num_lines": 0,
        "num_storage_units": 0,
    }

    for bus in buses or []:
        params = dict(bus)
        bus_name = params.pop("name")
        n.add("Bus", bus_name, **params)
        counts["num_buses"] += 1

    for gen in generators or []:
        params = dict(gen)
        gen_name = params.pop("name")
        n.add("Generator", gen_name, **params)
        counts["num_generators"] += 1

    for load in loads or []:
        params = dict(load)
        load_name = params.pop("name")
        n.add("Load", load_name, **params)
        counts["num_loads"] += 1

    for line in lines or []:
        params = dict(line)
        line_name = params.pop("name")
        n.add("Line", line_name, **params)
        counts["num_lines"] += 1

    for su in storage_units or []:
        params = dict(su)
        su_name = params.pop("name")
        n.add("StorageUnit", su_name, **params)
        counts["num_storage_units"] += 1

    parts = []
    if counts["num_buses"]:
        parts.append(f"{counts['num_buses']} bus(es)")
    if counts["num_generators"]:
        parts.append(f"{counts['num_generators']} generator(s)")
    if counts["num_loads"]:
        parts.append(f"{counts['num_loads']} load(s)")
    if counts["num_lines"]:
        parts.append(f"{counts['num_lines']} line(s)")
    if counts["num_storage_units"]:
        parts.append(f"{counts['num_storage_units']} storage unit(s)")

    counts["summary"] = f"Added {', '.join(parts)} to network {n.name!r}."
    return counts
