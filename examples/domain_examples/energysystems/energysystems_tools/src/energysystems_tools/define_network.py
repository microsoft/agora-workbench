"""Create a PyPSA network with time snapshots."""

import pandas as pd
import pypsa


def define_network(
    name: str,
    snapshots: int = 24,
    start: str = "2025-01-01",
    freq: str = "h",
) -> pypsa.Network:
    """Create a PyPSA network with the given name and snapshot range.

    Args:
        name: Name for the network.
        snapshots: Number of time steps (default: 24).
        start: Start datetime as ISO string (default: "2025-01-01").
        freq: Pandas frequency string (default: "h" for hourly).

    Returns:
        A live ``pypsa.Network`` object with snapshots configured.

    Raises:
        ValueError: If snapshots < 1.
    """
    if snapshots < 1:
        raise ValueError(f"snapshots must be >= 1, got {snapshots}")

    index = pd.date_range(start, periods=snapshots, freq=freq)
    network = pypsa.Network()
    network.name = name
    network.set_snapshots(index)

    return network
