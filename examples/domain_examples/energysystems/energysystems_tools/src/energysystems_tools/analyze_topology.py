"""Analyze network graph topology using networkx."""

from __future__ import annotations

import builtins

import networkx as nx


def _get_network(name: str):
    """Retrieve a previously created network by name."""
    networks = getattr(builtins, "_pypsa_networks", {})
    if name not in networks:
        raise ValueError(f"Network {name!r} not found. Call define_network first. Available: {sorted(networks)}")
    return networks[name]


def analyze_topology(network_name: str) -> dict:
    """Analyze the network graph: connectivity, islands, bottlenecks.

    Args:
        network_name: Name of a previously defined network with components.

    Returns:
        Dictionary with ``num_buses``, ``num_lines``, ``is_connected``,
        ``num_islands``, ``degree_distribution``, and ``bottleneck_lines``.

    Raises:
        ValueError: If the network is not found.
    """
    n = _get_network(network_name)

    # Build networkx graph from lines
    G = nx.Graph()
    G.add_nodes_from(n.buses.index)
    for line_name in n.lines.index:
        line = n.lines.loc[line_name]
        G.add_edge(line["bus0"], line["bus1"], name=line_name)

    # Also include links if any
    if len(n.links) > 0:
        for link_name in n.links.index:
            link = n.links.loc[link_name]
            G.add_edge(link["bus0"], link["bus1"], name=link_name)

    num_buses = G.number_of_nodes()
    num_edges = G.number_of_edges()
    is_connected = nx.is_connected(G) if num_buses > 0 else True
    num_islands = nx.number_connected_components(G) if num_buses > 0 else 0

    # Degree distribution
    degree_counts: dict[int, int] = {}
    for _, degree in G.degree():
        degree_counts[degree] = degree_counts.get(degree, 0) + 1

    # Bottleneck lines by edge betweenness centrality
    bottleneck_lines = []
    if num_edges > 0:
        edge_bc = nx.edge_betweenness_centrality(G)
        sorted_edges = sorted(edge_bc.items(), key=lambda x: x[1], reverse=True)
        for (u, v), centrality in sorted_edges[:5]:
            edge_data = G.edges[u, v]
            bottleneck_lines.append(
                {
                    "line": edge_data.get("name", f"{u}-{v}"),
                    "bus0": u,
                    "bus1": v,
                    "betweenness_centrality": round(centrality, 4),
                }
            )

    return {
        "num_buses": num_buses,
        "num_lines": num_edges,
        "is_connected": is_connected,
        "num_islands": num_islands,
        "degree_distribution": degree_counts,
        "bottleneck_lines": bottleneck_lines,
    }
