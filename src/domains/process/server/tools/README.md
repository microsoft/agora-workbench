# IDAES Process Tools Package

Installable Python package (`idaes_process_tools`) containing IDAES/Pyomo process simulation tools for the code execution server.

## Structure

- `schema/` — Pydantic configuration models (no IDAES imports, safe for server-side use)
- `property_generation/` — property package generation and fitting utilities
- `builder.py` — `IdaesFlowsheetBuilder` class
- `variable_manager.py` — variable specification manager
- `units.py` — lazy-loading Pyomo unit wrappers
- Tool entry points: `flowsheet_builder.py`, `flowsheet_specification.py`, `flowsheet_initialization.py`, `flowsheet_solver.py`, `results_extractor.py`

## Usage

The package is installed in the execution environment via the server's dependency file. Tool functions are imported in generated execution code:

```python
from idaes_process_tools.flowsheet_builder import build_idaes_flowsheet
from idaes_process_tools.property_generation.build_property_config import build_property_config
```

To rebuild after changes: `docker compose build process-server`
