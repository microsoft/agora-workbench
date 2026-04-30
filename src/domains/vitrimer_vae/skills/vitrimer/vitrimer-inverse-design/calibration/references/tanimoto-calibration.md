# Tanimoto Kernel and Calibration Reference

## Tanimoto Kernel

The calibration GP uses a custom Tanimoto kernel on binary Morgan
fingerprints, which is a standard molecular similarity measure in
cheminformatics.

### Definition

For binary fingerprint vectors x and x':

```
K(x, x') = σ² · (x · x') / (||x||² + ||x'||² − x · x')
```

Where:
- `x · x'` = number of shared "on" bits (intersection)
- `||x||²` = number of "on" bits in x
- The denominator equals the union of "on" bits
- σ² is a learned variance hyperparameter (bounds: 1e-5 to 1e5)

### Intuition

The Tanimoto coefficient equals |A ∩ B| / |A ∪ B| for bit sets A, B.
It ranges from 0 (completely dissimilar) to 1 (identical fingerprints).
This is the standard similarity metric for comparing molecular structures.

## Morgan Fingerprints

The calibration tool uses Morgan fingerprints with:
- **Radius**: 3 (captures up to 6-bond substructures)
- **Bits**: 2048 (binary vector)
- **Library**: RDKit `GetMorganFingerprintAsBitVect`

These capture circular substructure features around each atom, providing
a rich structural representation for the GP.

## Vitrimerization Reactions

Before fingerprinting, acid and epoxide monomers are combined into a
full vitrimer polymer repeat unit using SMARTS reaction patterns:

1. **Acid activation**: Opens the carboxylic acid for coupling
   ```
   [CX3](=O)[OX2H1] >> [CX3](=O)[OX2][*]
   ```

2. **Epoxide ring-opening**: Opens the oxirane ring
   ```
   [OD2r3]1[#6D2r3][#6r3]1 >> [#6:3]([OD2:1])[#6D2:2][*]
   ```

3. **Coupling**: Joins the activated acid with the opened epoxide
   ```
   [CX3](=O)[OX2H1].[OD2r3]1[#6D2r3][#6r3]1 >>
   [CX3](=O)[OX2][#6D2][#6]([OD2])
   ```

## Calibration Dataset

The GP is trained on `tg_calibration.csv` containing:
- ~100+ polymers with both experimental and MD-simulated Tg
- The GP learns the correction function: `Δ = Tg_exp − Tg_md`
- Predictions: `Tg_calibrated = Tg_md + GP(fingerprint)`
