# VAE Latent Space Reference

## Architecture

The HierVAE (Hierarchical Graph Neural Network VAE) uses separate encoders
for acid and epoxide molecules, then combines their latent representations
into a shared 128-dimensional vector.

## Latent Space Layout (128 dimensions)

```
z = [ acid-specific │ shared │ epoxide-specific ]
      8 dims          48 dims   8 dims
```

**Detailed breakdown:**

| Subspace | Dimensions | Index range | Controls |
|----------|-----------|-------------|----------|
| Acid-specific | 8 | 0–7 | Acid structure only |
| Shared (acid + epoxide) | 48 | 8–55 | Both components jointly |
| Epoxide-specific | 8 | 56–63 | Epoxide structure only |

Note: The model parameters define `acid_size=112` and `epoxide_size=112`
with `share_size=96`. These overlap: the acid encoder produces 112 dims,
the epoxide encoder produces 112 dims, and they share 96 of those dims.
After merging, the unique acid dims (112-96=16→8 after projection),
shared dims (96→48), and unique epoxide dims (16→8) yield the final 128.

## Encoding Process

1. **Tree decomposition**: Each molecule is decomposed into a tree of
   chemical clusters using hierarchical substructure vocabulary
2. **Hierarchical message passing**: Three levels of message passing
   (tree → intermediate → graph) with LSTM cells (hidden=250, depth=15)
3. **Bottleneck**: Separate linear projections produce means and log-variances
   for acid (112d) and epoxide (112d) latent vectors
4. **Merging**: The shared dimensions are averaged between acid and epoxide
   encoders to form the final 128-dim z_mean

## Property Prediction

A 2-layer MLP maps the full 128-dim latent vector to Tg:

```
z (128) → Linear(128, 64) → ReLU → Linear(64, 1) → normalized Tg
```

The output is denormalized using: `Tg = z_pred * 32.877 + 373.097` (K)

## Targeted Search

The subspace structure enables controlled exploration:

- **`search_type="acid"`**: Perturb only dims 0–7
  → Acid changes, epoxide stays approximately fixed
- **`search_type="epoxide"`**: Perturb only dims 56–63
  → Epoxide changes, acid stays approximately fixed
- **`search_type="both"`**: Perturb all 128 dims
  → Both components may change

The shared subspace (dims 8–55) captures properties that depend on the
acid-epoxide *interaction*, so perturbing it affects both molecules.

## Training Details

- **Dataset**: 7,424 acid–epoxide vitrimer pairs with MD-simulated Tg
- **Vocabulary**: Separate PairVocab files for acids and epoxides
  (tree decomposition patterns)
- **Checkpoint**: Epoch 49 (`prop49.model`, 107 MB)
- **Tg scaler**: StandardScaler(mean=373.097, std=32.877)
  fitted on training Tg values
