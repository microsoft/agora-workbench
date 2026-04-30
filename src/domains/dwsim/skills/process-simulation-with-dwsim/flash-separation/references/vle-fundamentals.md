# Vapor-Liquid Equilibrium Fundamentals

## Phase Equilibrium

At equilibrium, the fugacity of each component is equal in both phases:

    f_i^V = f_i^L

This condition determines how a mixture distributes between vapor and liquid at
a given temperature and pressure.

## Bubble and Dew Points

- **Bubble point**: the temperature (at fixed P) or pressure (at fixed T) where
  the first bubble of vapor forms from a liquid mixture. Below the bubble point,
  the mixture is entirely liquid.
- **Dew point**: the temperature (at fixed P) or pressure (at fixed T) where the
  first drop of liquid condenses from a vapor mixture. Above the dew point, the
  mixture is entirely vapor.

Between the bubble and dew points, two phases coexist.

## Raoult's Law (Ideal Systems)

For ideal mixtures:

    y_i * P = x_i * P_i^sat(T)

where `y_i` is vapor mole fraction, `x_i` is liquid mole fraction, and
`P_i^sat(T)` is the pure-component saturation pressure at temperature T.

This model works for chemically similar, non-polar mixtures (e.g. pentane-hexane).

## Activity Coefficient Models (Non-Ideal Liquids)

For non-ideal liquid mixtures, an activity coefficient γ_i corrects for
liquid-phase non-ideality:

    y_i * P = x_i * γ_i * P_i^sat(T)

Models like NRTL and UNIQUAC provide γ_i as a function of composition and
temperature, using binary interaction parameters fitted to experimental data.

## Equation of State Models

Cubic equations of state (Peng-Robinson, SRK) calculate fugacity coefficients
for both phases from a single model. They work well for gas-phase non-ideality
and moderate liquid-phase non-ideality, particularly for hydrocarbon systems.

## Phase Diagrams

- **T-xy diagram**: temperature vs. composition at constant pressure. Shows
  bubble and dew curves — useful for understanding flash and distillation behavior.
- **P-xy diagram**: pressure vs. composition at constant temperature.
- **Azeotropes**: points where the bubble and dew curves meet, meaning the vapor
  and liquid have identical composition. Azeotropic mixtures cannot be separated
  by simple distillation beyond the azeotrope.
