---
name: reaction-engineering
parent_skill: process-simulation-with-dwsim
description: Model chemical reactions using conversion reactors (specified conversion), equilibrium reactors (thermodynamic equilibrium), or kinetic reactors (PFR/CSTR with Arrhenius rate expressions).
states: [dwsim.flowsheet_exists, dwsim.flowsheet_solved]
---

# Reaction Engineering

Use this skill when the user needs to model a chemical reaction — either with a
known target conversion, at chemical equilibrium, or with rate-based kinetics.

## Conversion Reactor

Call `add_conversion_reactor` when the user specifies a **target conversion** for
the reaction (e.g. "90% of ethanol is converted").

Parameters:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"RX-1"`.
- `inlet_stream_name`: feed stream tag.
- `vapor_outlet_name`: vapor product stream tag.
- `liquid_outlet_name`: liquid product stream tag.
- `reaction_set`: JSON string defining the reaction.
- `energy_stream_name` (optional): energy stream to read the heat duty.

### Reaction Set JSON Format

```json
{
  "base_compound": "Ethanol",
  "conversion": 0.95,
  "stoichiometry": {
    "Ethanol": -1,
    "Oxygen": -3,
    "Carbon Dioxide": 2,
    "Water": 3
  }
}
```

Key rules:
- **`base_compound`**: the limiting reactant whose conversion is specified.
- **`conversion`**: fractional (0 to 1), not percentage.
- **`stoichiometry`**: signed coefficients — **negative for reactants, positive
  for products**. Coefficients must be stoichiometrically balanced.
- All compound names must match exactly the names used in `create_flowsheet`.

## Equilibrium Reactor

Call `add_equilibrium_reactor` when operating conditions (T, P) determine the
extent of reaction rather than a fixed conversion.

Parameters are the same as the conversion reactor, except the `reaction_set` JSON
uses a `Keq_expression` instead of `conversion`:

```json
{
  "base_compound": "Nitrogen",
  "stoichiometry": {
    "Nitrogen": -1,
    "Hydrogen": -3,
    "Ammonia": 2
  },
  "Keq_expression": "exp(-5000/T + 10)"
}
```

- **`Keq_expression`**: a string expression for the equilibrium constant as a
  function of temperature `T` (in Kelvin). Use Python math syntax
  (e.g. `exp(...)`, `log(...)`, `T**2`).
- If `Keq_expression` is omitted, DWSIM defaults to Keq = 1.

## Choosing Between Conversion and Equilibrium Reactors

| Scenario | Reactor Type | Reason |
|----------|-------------|--------|
| User specifies "90% conversion" | Conversion | Fixed conversion is the specification |
| User asks "what conversion do I get at 500 K?" | Equilibrium | T determines the extent |
| Industrial process with known yield | Conversion | Simplest to model |
| Reversible reaction at high temperature | Equilibrium | Equilibrium limits the conversion |

## Vapor and Liquid Outlets

Both reactor types produce **two outlet streams** — a vapor outlet and a liquid
outlet. After solving:
- Gaseous products report to the vapor outlet.
- Liquid products and unreacted liquid feed report to the liquid outlet.
- Call `get_stream_results` on both outlets to see the full product distribution.

## Energy Stream Connection

Reactions are typically exothermic (release heat) or endothermic (absorb heat).
Connecting an energy stream allows you to read the heat duty after solving:
1. `add_energy_stream` with `name: "Q-RX-1"`.
2. Pass `energy_stream_name: "Q-RX-1"` when creating the reactor.
3. After solving, the duty appears in `get_unit_operation_results` for the reactor.
   Positive duty means heat is added (endothermic); negative means heat is released.

## Multiple Reactions and Reactor Chains

Each call to `add_conversion_reactor` or `add_equilibrium_reactor` models a
single reaction. For processes involving multiple reactions, chain separate
reactor units:

**Sequential reactions** (e.g. A→B→C, where B is the desired intermediate):
1. Add reactor `RX-1` converting A→B with the desired conversion for the first step.
2. Connect the liquid (or vapor) outlet of `RX-1` as the inlet to `RX-2`.
3. Add reactor `RX-2` converting B→C.
4. Solve and check `get_stream_results` on all outlets to track each species.

**Parallel reactions** (e.g. A→B and A→C simultaneously):
DWSIM's `add_conversion_reactor` accepts one `base_compound` per reactor. Model
parallel reactions by splitting the feed first:
1. `add_splitter` to divide the feed by the expected molar split.
2. `add_conversion_reactor` for each parallel reaction branch.
3. `add_mixer` to recombine the product streams.

Alternatively, use a single conversion reactor for the dominant reaction and
treat the side reaction as a post-processing correction on the product
composition.

**Recycle loops** (e.g. unreacted feed recycled to inlet):
1. Add a `add_splitter` on the reactor outlet to split off a recycle fraction.
2. Connect the recycle stream back to `add_mixer` upstream of the reactor.
3. The solver will iterate until the recycle stream converges. If convergence is
   slow, reduce the recycle fraction as an initial estimate and increase it
   incrementally.

See [references/reactor-selection.md](references/reactor-selection.md) for a
comparison of reactor concepts and common reaction examples.

## Kinetic Reactor (PFR / CSTR)

Call `add_kinetic_reactor` when the user needs **rate-based kinetics** — the
reaction rate depends on concentration and temperature via Arrhenius expressions,
or when the user specifies a Plug-Flow Reactor (PFR) or Continuous Stirred-Tank
Reactor (CSTR).

Parameters:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"PFR-1"`.
- `inlet_stream_name`: feed stream tag.
- `outlet_stream_name`: main (liquid) product stream tag.
- `reactor_type`: `"PFR"` or `"CSTR"`.
- `reactions_json`: JSON list of kinetic reaction definitions (see below).
- `energy_stream_name` (optional): energy stream tag.
- `volume`: reactor volume in m³ (default 1.0).
- `length`: tube length in m (PFR only, default 5.0).
- `number_of_tubes`: parallel tubes (PFR only, default 1).
- `catalyst_loading`: kg/m³ (0 for homogeneous).
- `catalyst_particle_diameter`: m.
- `catalyst_void_fraction`: 0–1.
- `operation_mode`: `"Isothermic"`, `"Adiabatic"`, `"OutletTemperature"`, or
  `"NonIsothermalNonAdiabatic"`.
- `outlet_temperature`: K (only for OutletTemperature mode).
- `vapor_outlet_name` (optional): CSTR vapor outlet tag.

### Kinetic Reaction JSON Format

```json
[
  {
    "name": "EO-Hydration",
    "stoichiometry": {"Ethylene oxide": -1, "Water": -1, "Ethylene glycol": 1},
    "direct_orders": {"Ethylene oxide": 1, "Water": 1},
    "reverse_orders": {},
    "base_compound": "Ethylene oxide",
    "reaction_phase": "Liquid",
    "basis": "Molar",
    "amount_units": "mol/L",
    "rate_units": "mol/[L.s]",
    "A_forward": 1.0e6,
    "E_forward": 50000.0,
    "A_reverse": 0,
    "E_reverse": 0
  }
]
```

Key rules:
- **`stoichiometry`**: signed coefficients — negative for reactants, positive for
  products. Must be stoichiometrically balanced.
- **`direct_orders`**: forward reaction orders for each species (power-law).
- **`reverse_orders`**: reverse reaction orders. Omit or `{}` for irreversible.
- **`A_forward`, `E_forward`**: Arrhenius pre-exponential factor and activation
  energy (J/mol) for the forward reaction. Rate = A·exp(-E/RT)·∏(C^n).
- **`A_reverse`, `E_reverse`**: set both to 0 for irreversible reactions.
- **`basis`**: `"Molar"` (concentration), `"Mass"`, or `"PartialPress"`.

### Heterogeneous Catalytic Reactions

For Langmuir-Hinshelwood or other het-cat rate forms, add `"type": "HetCat"` and
provide `"numerator"` and `"denominator"` rate expressions instead of Arrhenius
parameters:

```json
[
  {
    "type": "HetCat",
    "name": "Fischer-Tropsch",
    "stoichiometry": {"Carbon monoxide": -1, "Hydrogen": -2, "Methanol": 1},
    "base_compound": "Carbon monoxide",
    "reaction_phase": "Vapor",
    "basis": "PartialPress",
    "amount_units": "Pa",
    "rate_units": "mol/[kg.s]",
    "numerator": "k * pCO * pH2",
    "denominator": "(1 + K_CO * pCO + K_H2 * pH2)^2"
  }
]
```

### PFR vs CSTR Selection

| Scenario | Reactor | Reason |
|----------|---------|--------|
| High conversion needed, positive-order kinetics | PFR | Higher conversion per volume |
| Exothermic reaction needing temperature control | CSTR | Uniform temperature |
| Catalyst bed with axial profiles | PFR | Concentration/temperature gradients |
| Fast liquid-phase reaction | CSTR | Well-mixed assumption valid |
| Autocatalytic reaction | CSTR | Product promotes reaction rate |

### PFR Port Layout

- **Input 0**: material feed stream
- **Input 1**: energy stream (optional)
- **Output 0**: product stream

### CSTR Port Layout

- **Input 0**: material feed stream
- **Input 1**: energy stream (optional)
- **Output 0**: liquid product stream
- **Output 1**: vapor product stream (use `vapor_outlet_name`)

### Operation Modes

- **Isothermic**: reactor temperature equals feed temperature (default).
- **Adiabatic**: no heat transfer — temperature changes with reaction enthalpy.
- **OutletTemperature**: specify desired outlet T; solver calculates required duty.
- **NonIsothermalNonAdiabatic**: heat transfer with environment (advanced).
