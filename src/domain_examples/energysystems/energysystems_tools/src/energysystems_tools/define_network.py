"""Create a PyPSA network with time snapshots."""

import pandas as pd
import pypsa


def define_network(
    name: str,
    snapshots: int = 24,
    start: str = "2025-01-01",
    freq: str = "h",
) -> dict:
    """Create a PyPSA network with the given name and snapshot range.

    Args:
        name: Name for the network.
        snapshots: Number of time steps (default: 24).
        start: Start datetime as ISO string (default: "2025-01-01").
        freq: Pandas frequency string (default: "h" for hourly).

    Returns:
        Dictionary with ``name``, ``num_snapshots``, ``frequency``,
        ``start``, and ``end``.

    Raises:
        ValueError: If snapshots < 1.
    """
    if snapshots < 1:
        raise ValueError(f"snapshots must be >= 1, got {snapshots}")

    index = pd.date_range(start, periods=snapshots, freq=freq)
    network = pypsa.Network()
    network.name = name
    network.set_snapshots(index)

    # Store in the global namespace so subsequent tools can retrieve it
    import builtins

    if not hasattr(builtins, "_pypsa_networks"):
        builtins._pypsa_networks = {}
    builtins._pypsa_networks[name] = network

    return {
        "name": name,
        "num_snapshots": len(index),
        "frequency": freq,
        "start": str(index[0]),
        "end": str(index[-1]),
    }
