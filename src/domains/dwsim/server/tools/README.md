# DWSIM Tools Package

Domain-specific tools for chemical process simulation using DWSIM, installed in
the isolated code execution environment via `requirements.yaml`.

## Prerequisites

- .NET Runtime 8.0 (installed in Docker image)
- DWSIM 9.0.5 (installed via `.deb` package at `/usr/local/lib/dwsim/`)
- `pythonnet==3.0.5` for CLR interop

## Installation

This package is automatically installed in the DWSIM execution environment.
For local development:

```bash
pip install -e domains/dwsim/server/tools
```

## Available Tools

### Compound Database
| Tool | Description |
|------|-------------|
| `search_compounds` | Search / list available DWSIM compounds by name |

### Flowsheet Lifecycle
| Tool | Description |
|------|-------------|
| `create_flowsheet` | Create a new flowsheet with compounds and a property package |
| `load_flowsheet` | Load a `.dwxmz`/`.dwxml` file |
| `solve_flowsheet` | Calculate the flowsheet and report convergence |

### Streams
| Tool | Description |
|------|-------------|
| `add_material_stream` | Add a material stream (T, P, composition, flow) |
| `add_energy_stream` | Add an energy stream (heat / work) |

### Unit Operations
| Tool | Description |
|------|-------------|
| `add_mixer` | Combine multiple streams |
| `add_splitter` | Split a stream by ratio |
| `add_heater` | Heat a stream to a target temperature |
| `add_cooler` | Cool a stream to a target temperature |
| `add_pump` | Raise liquid pressure |
| `add_valve` | Isenthalpic expansion |
| `add_compressor` | Raise gas pressure |
| `add_heat_exchanger` | Two-stream heat exchange |
| `add_separator` | Flash separation (vapour / liquid) |
| `add_conversion_reactor` | Reaction with specified conversion |
| `add_equilibrium_reactor` | Reaction at chemical equilibrium |
| `add_distillation_column` | Rigorous column with condenser and reboiler |

### Results Extraction
| Tool | Description |
|------|-------------|
| `get_stream_results` | T, P, flows, compositions of a stream |
| `get_unit_operation_results` | Duty, efficiency, and details of a unit |
| `get_flowsheet_summary` | Object list, convergence, mass & energy balance |
| `list_object_properties` | List available PROP_* codes for any object tag |
| `get_object_property` | Read a single PROP_* value by code |
| `set_object_property` | Set a single PROP_* value by code |

### Optimization
| Tool | Description |
|------|-------------|
| `run_sensitivity_analysis` | Sweep a variable and record an objective |
| `run_optimization` | Nelder-Mead minimization / maximization |
