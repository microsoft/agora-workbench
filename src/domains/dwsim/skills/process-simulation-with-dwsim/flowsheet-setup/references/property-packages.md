# Property Package Selection Guide

## Overview

The property package (thermodynamic model) determines how phase equilibria,
enthalpies, and densities are calculated. Choosing the wrong package can lead to
incorrect phase splits, unrealistic temperatures, or convergence failures.

## Decision Table

| Chemistry Type | Recommended Package | Typical Examples | Caveats |
|---------------|-------------------|-----------------|---------|
| Ideal gas mixtures at low pressure | Peng-Robinson | Air separation, flue gas | Less accurate for liquids with strong non-ideality |
| Hydrocarbon gas processing | SRK | Natural gas, refinery gas | Similar to PR; slightly different mixing rules |
| Polar liquid mixtures | NRTL | Water-ethanol, water-amine | Requires binary interaction parameters (DWSIM has many built-in) |
| Polar liquid mixtures (alternative) | UNIQUAC | Liquid-liquid extraction | Better for systems with molecules of very different sizes |
| Unknown binary parameters | UNIFAC | Novel mixtures | Predictive group-contribution; less accurate than fitted NRTL |
| Unknown binary parameters (improved) | Modified UNIFAC (Dortmund) | Wide range of organics | Improved temperature dependence over original UNIFAC |
| Pure water / steam cycles | Steam Tables (IAPWS-IF97) | Boilers, steam turbines | Only valid for pure water; do not use for mixtures |
| Refrigerants | CoolProp | R-134a, ammonia refrigeration | Covers many industrial fluids with high accuracy |
| Simple ideal solutions | Raoult's Law | Chemically similar hydrocarbons | Assumes ideal liquid and ideal gas; very limited |
| Light hydrocarbons | Lee-Kesler-Plocker | LPG, ethylene plants | Correlation-based; not for polar compounds |

## Tips

- When mixing polar and non-polar compounds (e.g. water + hydrocarbons), NRTL is
  usually the best starting point.
- If NRTL fails to converge, try switching to UNIQUAC or UNIFAC — the issue may
  be missing binary parameters.
- For high-pressure systems (> 50 bar) with non-ideal gases, Peng-Robinson with
  appropriate binary interaction parameters is preferred.
- Steam Tables should only be used for pure water systems. Adding any other
  compound requires a different package.
