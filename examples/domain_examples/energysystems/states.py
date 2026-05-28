"""Energy systems domain state vocabulary.

Defines the canonical state tokens for the energy systems tool graph.
Each token represents a meaningful intermediate artifact that downstream
tools can consume.
"""

from enum import Enum


class EnergySystemsState(str, Enum):
    """State tokens for the energy systems domain tool graph.

    The graph flows:

        define_network ─────► NETWORK_DEFINED
                                    │
                    ┌───────────────┼───────────────────┐
                    ▼               ▼                   ▼
            add_components    add_components       add_components
                    │               │                   │
                    ▼               ▼                   ▼
            COMPONENTS_ADDED  COMPONENTS_ADDED    COMPONENTS_ADDED
                    │               │                   │
            ┌───────┼───────┐      │                   │
            ▼       ▼       ▼      ▼                   ▼
        run_pf  run_opf  analyze  add_time_series  analyze_topology
            │       │    _topology     │                │
            ▼       ▼       ▼         ▼                ▼
        PF_SOLVED OPF_SOLVED TOPO   TIME_SERIES    TOPOLOGY
                    │             _ATTACHED         _ANALYZED
                    ▼                 │
              analyze_costs    run_capacity
                    │          _expansion
                    ▼                │
              COSTS_ANALYZED         ▼
                              CAPACITY_EXPANSION
                                  _SOLVED
    """

    NETWORK_DEFINED = "energysystems.network_defined"
    COMPONENTS_ADDED = "energysystems.components_added"
    TIME_SERIES_ATTACHED = "energysystems.time_series_attached"
    POWER_FLOW_SOLVED = "energysystems.power_flow_solved"
    OPF_SOLVED = "energysystems.opf_solved"
    CAPACITY_EXPANSION_SOLVED = "energysystems.capacity_expansion_solved"
    COSTS_ANALYZED = "energysystems.costs_analyzed"
    TOPOLOGY_ANALYZED = "energysystems.topology_analyzed"


STATE_AFFORDANCES = {
    EnergySystemsState.NETWORK_DEFINED: [
        "create a power network",
        "define simulation time horizon",
        "set up snapshots for time-series analysis",
    ],
    EnergySystemsState.COMPONENTS_ADDED: [
        "add buses, generators, loads, and lines",
        "build a power system model",
        "define network topology with components",
    ],
    EnergySystemsState.TIME_SERIES_ATTACHED: [
        "attach load profiles to components",
        "set renewable capacity factor time series",
        "model time-varying demand or generation",
    ],
    EnergySystemsState.POWER_FLOW_SOLVED: [
        "compute voltage magnitudes and angles",
        "determine line power flows",
        "check network feasibility",
    ],
    EnergySystemsState.OPF_SOLVED: [
        "minimize generation cost",
        "compute optimal generator dispatch",
        "determine marginal prices at buses",
    ],
    EnergySystemsState.CAPACITY_EXPANSION_SOLVED: [
        "optimize investment in new generation or storage",
        "plan long-term capacity additions",
        "evaluate renewable integration scenarios",
    ],
    EnergySystemsState.COSTS_ANALYZED: [
        "break down system costs by technology",
        "analyze marginal pricing",
        "compare generation cost across buses",
    ],
    EnergySystemsState.TOPOLOGY_ANALYZED: [
        "check network connectivity",
        "identify electrical islands",
        "find bottleneck transmission lines",
    ],
}
