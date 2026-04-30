# AC vs DC Power Flow

## When to Use AC Power Flow

- You need accurate **voltage magnitudes and angles** at each bus
- **Reactive power** analysis is required (VAR planning, capacitor placement)
- Studying **voltage stability** or voltage collapse scenarios
- Final validation of results before real-world decisions
- Small to medium networks (< 2000 buses) where computation time is acceptable

## When to Use DC Power Flow

- Quick **screening studies** on large networks (> 2000 buses)
- **Active power flow** patterns and congestion identification
- **Locational marginal price** (LMP) calculations where reactive power is secondary
- **Contingency analysis** (N-1 screening) where speed matters more than precision
- Initial feasibility checks before running full AC analysis

## Key Differences

| Aspect | AC Power Flow | DC Power Flow |
|--------|--------------|---------------|
| Voltage magnitudes | Calculated | Assumed 1.0 p.u. |
| Reactive power | Included | Ignored |
| Losses | Calculated | Ignored |
| Nonlinear | Yes (Newton-Raphson) | No (linear system) |
| Speed | Slower | Much faster |
| Accuracy | High | Approximate (±5-10%) |

## Rule of Thumb

Start with DC power flow to understand the general flow patterns, then refine
with AC power flow for the scenarios that matter most.
