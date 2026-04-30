"""
DWSIM domain state vocabulary.

Defines the controlled set of state tokens that DWSIM tools can require
or produce, along with human-readable affordance phrases for each state.
These affordances are merged with tool-specific affordances at catalog-
build time to improve search recall.

The DWSIM workflow has a small number of meaningful phase transitions:

    COMPOUNDS_AVAILABLE ──▶ FLOWSHEET_EXISTS ──▶ FLOWSHEET_SOLVED
                                  │  ▲                   │
                                  └──┘                   ▼
                              (self-loop:          RESULTS_AVAILABLE
                            add streams,                 │
                           add unit ops,                 ▼
                         set properties)        OPTIMIZATION_COMPLETE

Building a flowsheet (adding streams, unit operations, changing
properties) is a self-loop on FLOWSHEET_EXISTS — the tools require
and produce the same state because these operations are order-
independent and interleaved in practice.
"""

from enum import Enum, unique


@unique
class DwsimState(str, Enum):
    """Controlled vocabulary of DWSIM process simulation states."""

    COMPOUNDS_AVAILABLE = "dwsim.compounds_available"
    FLOWSHEET_EXISTS = "dwsim.flowsheet_exists"
    FLOWSHEET_SOLVED = "dwsim.flowsheet_solved"
    RESULTS_AVAILABLE = "dwsim.results_available"
    OPTIMIZATION_COMPLETE = "dwsim.optimization_complete"


STATE_AFFORDANCES: dict[DwsimState, list[str]] = {
    DwsimState.COMPOUNDS_AVAILABLE: [
        "find chemical species",
        "look up compound",
        "discover available chemicals",
    ],
    DwsimState.FLOWSHEET_EXISTS: [
        "set up simulation",
        "create process model",
        "build flowsheet",
        "add streams and equipment",
    ],
    DwsimState.FLOWSHEET_SOLVED: [
        "run steady-state simulation",
        "converge process",
        "calculate mass and energy balances",
    ],
    DwsimState.RESULTS_AVAILABLE: [
        "read simulation results",
        "get outlet conditions",
        "extract stream properties",
    ],
    DwsimState.OPTIMIZATION_COMPLETE: [
        "optimise process conditions",
        "complete sensitivity study",
        "find optimal operating point",
    ],
}
