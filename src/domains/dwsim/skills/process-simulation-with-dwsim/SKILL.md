---
name: process-simulation-with-dwsim
description: Build, solve, and analyze steady-state chemical process simulations using the DWSIM simulator. Covers flowsheet setup, unit operations, results extraction, and optimization.
states: [dwsim.compounds_available, dwsim.flowsheet_exists, dwsim.flowsheet_solved, dwsim.results_available, dwsim.optimization_complete]
---

# Process Simulation with DWSIM

Use this skill when the user wants to build or analyze a chemical process simulation
using DWSIM. It orchestrates the full simulation workflow from flowsheet creation
through to results reporting and optimization.

## Sub-Skills

Load the appropriate sub-skill for the specific task at hand:

| Sub-skill | When to use |
|-----------|-------------|
| `flowsheet-setup` | Create a new flowsheet, select compounds and property packages, add feed streams, save flowsheets, configure recycle loops |
| `fsd-conversion` | Convert a COCO simulator .fsd flowsheet to DWSIM .dwxmz format |
| `distillation` | Design a rigorous multi-stage distillation column, extractive distillation with multiple feeds, or absorption/stripping columns |
| `flash-separation` | Model vapor-liquid equilibrium flash separators or liquid-liquid decanters |
| `heat-and-fluid-transport` | Add heaters, coolers, pumps, compressors, valves, expanders/turbines, heat exchangers |
| `reaction-engineering` | Configure conversion, equilibrium, or kinetic (PFR/CSTR) reactors |
| `results-interpretation` | Extract stream and unit-operation results and summarize the flowsheet |
| `sensitivity-and-optimization` | Run sensitivity studies or optimize an objective using DWSIM property codes |
| `troubleshooting` | Diagnose and fix convergence failures or unexpected results |

## Typical Workflow

Most DWSIM tasks follow this sequence:

1. **Setup** — load `flowsheet-setup` to create the flowsheet and add feed streams.
2. **Build** — load the relevant unit-operation sub-skills to add process equipment.
3. **Solve** — call `solve_flowsheet` and verify convergence.
4. **Inspect** — load `results-interpretation` to extract and present results.
5. **Optimize** (optional) — load `sensitivity-and-optimization` for parameter studies.

If the flowsheet fails to converge, load `troubleshooting` for diagnostic guidance.

## General Rules

- Never fabricate simulation results. Every number you report must come from a
  solved DWSIM flowsheet.
- Always verify convergence (`converged: true`) before reading results.
- Use exact compound names returned by `search_compounds`; do not guess.
- Keep stream and unit tags short, uppercase, and descriptive (e.g. `FEED`,
  `HTR-1`, `Q-HTR-1`).
