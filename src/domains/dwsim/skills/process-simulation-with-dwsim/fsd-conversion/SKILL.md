---
name: fsd-conversion
parent_skill: process-simulation-with-dwsim
description: Convert COCO simulator .fsd flowsheets to DWSIM .dwxmz format, mapping compounds, property packages, and unit operations automatically.
states: [dwsim.flowsheet_exists, dwsim.flowsheet_solved]
---

# FSD-to-DWSIM Conversion

Use this skill when the user has a COCO simulator `.fsd` file and wants to
convert it to a DWSIM `.dwxmz` flowsheet.

## When to Use

- User mentions a `.fsd` file or COCO/COFE flowsheet
- User asks to "convert", "import", or "migrate" a COCO simulation to DWSIM
- User has CAPE-OPEN flowsheets they want to open in DWSIM

## Tool

Call `convert_fsd_to_dwsim` with the path to the `.fsd` file:

```
convert_fsd_to_dwsim(
    fsd_file_path="/path/to/flowsheet.fsd",
    output_file_path="",           # auto-generates _converted.dwxmz
    property_package="",           # auto-maps from COCO PP
    solve=True                     # attempt solving after build
)
```

## What the Tool Does

1. **Parses** the FSD file (ZIP archive containing `Flowsheet.xml`)
2. **Maps compounds** from COCO names to DWSIM names (by name match, then CAS)
3. **Maps property package** from COCO CAPE-OPEN to DWSIM equivalent
4. **Builds** the DWSIM flowsheet with streams and unit operations
5. **Saves** the `.dwxmz` file (even if solve fails)
6. **Solves** the flowsheet (optional, default on)

## Supported Unit Operations

| COCO Type | DWSIM Equivalent | Notes |
|-----------|------------------|-------|
| CSTR / PFR | Conversion Reactor | Stoichiometry back-calculated from solved data |
| Mixer | Mixer | Direct mapping |
| Splitter | Splitter | Direct mapping |
| Heater | Heater | Temperature from solved outlet |
| Cooler | Cooler | Temperature from solved outlet |
| Separator | Separator | Flash separator |
| Valve | Valve | Outlet pressure from solved data |
| Pump | Pump | Outlet pressure from solved data |
| Compressor | Compressor | Outlet pressure from solved data |

Unsupported types (distillation columns, absorption columns, custom CAPE-OPEN
units) are reported in the result but not converted.

## Limitations

- **Kinetic parameters are not portable.** COCO uses CAPE-OPEN reaction
  packages with proprietary rate expressions. Reactors are rebuilt as
  conversion reactors using the solved inlet/outlet data to back-calculate
  the stoichiometry and conversion.
- **Property packages don't map 1-to-1.** COCO uses COM-based CAPE-OPEN
  thermodynamic packages. The tool maps known packages (PR, SRK, NRTL, etc.)
  and defaults to Peng-Robinson for unrecognised ones.
- **Reactor topology changes.** DWSIM conversion reactors produce separate
  vapor and liquid outlets. The converter adds a Mixer to recombine them and
  optionally a temperature-adjustment block (Cooler/Heater) to match the COCO
  outlet temperature. These additions are reported in the warnings.
- **Single-reaction limitation.** Each reactor is modelled with one net
  reaction. If the COCO flowsheet has multiple parallel reactions in a single
  reactor, the back-calculated stoichiometry represents the net effect only.

## Interpreting Results

The tool returns a detailed report:

- **compound_mapping**: COCO → DWSIM name mapping used
- **topology_report**: Parsed streams, unit ops, and property package
- **warnings**: All transformations, mapping notes, and issues
- **unsupported_unit_ops**: Unit operations that could not be converted
- **converged**: Whether the DWSIM flowsheet solved successfully

## Workflow

1. Upload or locate the `.fsd` file on the server filesystem.
2. Call `convert_fsd_to_dwsim` with the file path.
3. Review the `warnings` and `compound_mapping` in the result.
4. If the flowsheet didn't converge, load the saved `.dwxmz` file with
   `load_flowsheet` and use `troubleshooting` to diagnose issues.
5. Use `get_stream_results` and `get_unit_operation_results` to compare
   with the original COCO results.

## Property Package Override

If the automatic property package mapping produces incorrect results, specify
the DWSIM property package explicitly:

```
convert_fsd_to_dwsim(
    fsd_file_path="/path/to/flowsheet.fsd",
    property_package="NRTL"
)
```

See [references/fsd-format.md](references/fsd-format.md) for details on the
FSD file structure.
