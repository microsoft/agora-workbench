# Reactor Selection Guide

## Reactor Concepts vs DWSIM Tools

| Conceptual Reactor | DWSIM Tool | When to Use |
|-------------------|-----------|-------------|
| CSTR (continuous stirred-tank) | `add_conversion_reactor` | Well-mixed, known conversion; model as a single-stage conversion reactor |
| PFR (plug flow reactor) | `add_conversion_reactor` | Tubular reactor with known overall conversion; approximate as conversion reactor |
| Equilibrium reactor | `add_equilibrium_reactor` | Reaction reaches equilibrium at given T, P; Keq determines conversion |

Note: DWSIM's conversion reactor does not distinguish between CSTR and PFR
geometry — it simply applies the specified conversion to the base compound.
For detailed kinetic modeling, use multiple conversion reactors in series to
approximate a PFR with incremental conversion steps.

## Common Reaction Examples

### Ammonia Synthesis (Haber-Bosch)

    N₂ + 3 H₂ ⇌ 2 NH₃

- Equilibrium reactor with Keq dependent on temperature.
- High pressure (100–300 atm), moderate temperature (400–500 °C).
- Property package: Peng-Robinson.
- Stoichiometry: `{"Nitrogen": -1, "Hydrogen": -3, "Ammonia": 2}`

### Ethanol Dehydration

    C₂H₅OH → C₂H₄ + H₂O

- Conversion reactor with 85–95% conversion.
- Temperature: 300–400 °C, atmospheric pressure.
- Property package: NRTL or Peng-Robinson.
- Stoichiometry: `{"Ethanol": -1, "Ethylene": 1, "Water": 1}`

### Methane Combustion

    CH₄ + 2 O₂ → CO₂ + 2 H₂O

- Conversion reactor with ~99% conversion (complete combustion).
- Property package: Peng-Robinson.
- Stoichiometry: `{"Methane": -1, "Oxygen": -2, "Carbon Dioxide": 1, "Water": 2}`

### Water-Gas Shift

    CO + H₂O ⇌ CO₂ + H₂

- Equilibrium reactor; equilibrium favored at lower temperatures.
- Property package: Peng-Robinson.
- Stoichiometry: `{"Carbon Monoxide": -1, "Water": -1, "Carbon Dioxide": 1, "Hydrogen": 2}`

## Tips

- Always verify compound names with `search_compounds` before defining the
  stoichiometry.
- Ensure stoichiometric coefficients are balanced — unbalanced reactions will give
  incorrect mass balances.
- For exothermic reactions, connect an energy stream to monitor the duty. Large
  exotherms may require inter-stage cooling in practice.
