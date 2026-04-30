# Column Design Heuristics

## Minimum Stages (Fenske Equation)

For a binary separation at total reflux:

    N_min = ln[(x_D / (1 - x_D)) × ((1 - x_B) / x_B)] / ln(α_avg)

where:
- `x_D` = mole fraction of light key in distillate
- `x_B` = mole fraction of light key in bottoms
- `α_avg` = average relative volatility of light key to heavy key

## Minimum Reflux (Underwood Equation)

For a binary system, minimum reflux can be estimated from:

    R_min = (1 / (α - 1)) × [(x_D / x_F) - α × ((1 - x_D) / (1 - x_F))]

where `x_F` is the feed composition and `α` is the relative volatility.

For multi-component systems, the full Underwood method involves finding the root
θ from the feed equation and then computing R_min from the distillate equation.

## Actual Reflux and Stages

- Use R = 1.2–1.5 × R_min as a starting point.
- Use the Gilliland correlation to estimate actual stages from R and N_min.
- Typical rule: doubling the minimum stages gives a practical design.

## Feed Stage (Kirkbride Correlation)

    ln(N_R / N_S) = 0.206 × ln[(B/D) × (x_HK,F / x_LK,F) × (x_LK,B / x_HK,D)²]

where N_R = rectifying stages, N_S = stripping stages, B = bottoms flow,
D = distillate flow, and subscripts refer to heavy key (HK) and light key (LK).

## Typical Parameters by Separation

| Separation | Typical α | Typical R/R_min | Typical Stages |
|-----------|----------|----------------|---------------|
| Methanol / Water | 2–4 | 1.2–1.5 | 15–25 |
| Ethanol / Water | 1.5–2.5 | 1.3–1.5 | 20–30 |
| Benzene / Toluene | 2.3–2.5 | 1.1–1.3 | 15–20 |
| Propane / Butane | 2–3 | 1.2–1.4 | 20–30 |
| Ethanol / Water (near azeotrope) | ~1.0 | Very high | Cannot cross azeotrope by simple distillation |

## Practical Notes

- Always check whether an azeotrope exists before specifying high-purity targets.
- For azeotropic systems, consider pressure-swing distillation or extractive
  distillation (add an entrainer compound to the flowsheet).
- Start with a conservative (larger) number of stages and higher reflux to get
  convergence, then optimize.
