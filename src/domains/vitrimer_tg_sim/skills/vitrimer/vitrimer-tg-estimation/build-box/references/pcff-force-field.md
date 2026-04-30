# PCFF Force Field Reference

## Overview

The Polymer Consistent Force Field (PCFF) is a class II force field designed
for polymeric systems. It is part of the CFF (Consistent Force Field) family
and provides good accuracy for thermomechanical properties of polymers,
including glass transition temperatures.

## LAMMPS Styles

| Interaction | LAMMPS Style | Cutoff |
|-------------|-------------|--------|
| Pair | `lj/class2/coul/long` | 9.5 Å (both LJ and Coulomb) |
| Bond | `class2` | — |
| Angle | `class2` | — |
| Dihedral | `class2` | — |
| Improper | `class2` | — |
| Kspace | `pppm/cg` | accuracy 0.001 |
| Pair mixing | `sixthpower` | with tail corrections |
| Special bonds | `lj/coul 0 0 1` | — |

## Units

PCFF uses LAMMPS `real` units:

| Quantity | Unit |
|----------|------|
| Distance | Ångströms (Å) |
| Energy | kcal/mol |
| Temperature | Kelvin (K) |
| Pressure | atmospheres (atm) |
| Time | femtoseconds (fs) |
| Mass | grams/mol |
| Density | g/cm³ |

## Recommended Timestep

- **0.5 fs** for equilibration and production runs
- Smaller timesteps (0.25 fs) may be needed for initial minimization of
  poorly constructed configurations

## Atom Types Covered

PCFF covers standard organic atom types including:
- Carbon: c, c1, c_1, cp (aromatic), c=, etc.
- Hydrogen: hc, ho, ho2, h, etc.
- Oxygen: o, o_1, o_2, oh, etc.
- Nitrogen: n, na, nh, etc.
- Sulfur, halogens, and selected metals

Exotic atom types (e.g., transition metals, rare earth elements) are
generally **not** covered by PCFF.

## References

1. Sun, H. "COMPASS: An ab Initio Force-Field Optimized for Condensed-Phase
   Applications." J. Phys. Chem. B 102, 7338–7364 (1998).
2. Sun, H. et al. "An ab Initio CFF93 All-Atom Force Field for Polycarbonates."
   J. Am. Chem. Soc. 116, 2978–2987 (1994).
