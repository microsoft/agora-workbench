---
name: powerflow
description: Run AC and DC power flow analysis on PyPSA and PYPOWER networks to calculate bus voltages, line flows, and power losses.
---

# Power Flow Analysis

Use this skill when the user asks to run power flow, load flow, or wants to
calculate bus voltages, line flows, or power losses on a network.

## PyPSA Power Flow

### AC Power Flow (Newton-Raphson)

```python
import pypsa

network = pypsa.Network("path/to/network.nc")
network.pf()  # AC power flow using Newton-Raphson
```

After solving, inspect results:

- **Bus voltages**: network.buses_t.v_mag_pu (magnitude), network.buses_t.v_ang (angle in radians)
- **Line flows**: network.lines_t.p0 (sending end), network.lines_t.p1 (receiving end)
- **Power losses**: network.lines_t.p0 + network.lines_t.p1 (positive = loss)
- **Convergence**: Check the return value of network.pf() for solver status

### DC Power Flow (Linear Approximation)

```python
network.lpf()  # DC (linear) power flow
```

DC power flow assumes flat voltage profiles and ignores reactive power.
Use it for quick estimates on large networks or when voltage profiles are not critical.

## PYPOWER Power Flow

```python
from pypower.api import case9, runpf, printpf
from pypower.idx_bus import PD, QD, VM, VA
from pypower.idx_gen import PG, QG

ppc = case9()
results, success = runpf(ppc)

if success:
    print("Bus voltages:", results["bus"][:, VM])
    print("Bus angles:", results["bus"][:, VA])
    print("Generator output:", results["gen"][:, PG])
else:
    print("Power flow did not converge")
```

Built-in test cases: `case9()`, `case14()`, `case30()`, `case118()`, `case300()`.

## Choosing AC vs DC Power Flow

See [references/ac-vs-dc.md](references/ac-vs-dc.md) for guidance on when to use each method.

## Convergence Checklist

1. Verify at least one slack/reference bus exists (`bus_type = 3` in PYPOWER, or `control = "Slack"` in PyPSA)
2. Check that all loads and generation are within reasonable ranges
3. Ensure the network is fully connected (no isolated buses)
4. For AC power flow, ensure reactive power limits are not severely binding
5. If diverging, try DC power flow first to verify the base case is feasible
