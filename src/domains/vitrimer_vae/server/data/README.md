# Vitrimer VAE Data Artifacts

This directory is mounted into the Docker image at `/app/domains/vitrimer_vae/server/data/`.

## Required files

```
data/
├── ckpt/
│   └── prop49.model            # Pre-trained HierVAE checkpoint (from train_vae_prop.py epoch 49)
└── data/
    ├── vocab_acid.txt          # Tree decomposition vocabulary for acids (checked in)
    ├── vocab_epoxide.txt       # Tree decomposition vocabulary for epoxides (checked in)
    └── tg_calibration.csv      # Calibration data: smiles, tg_exp, tg_md (checked in)
```

## Optional files

```
data/
└── pca.pkl                     # PCA transformer for 2D latent-space visualization (from latent_vector.py)
```

If `pca.pkl` is absent, tools will return empty PCA coordinates.

## Not required

- **scaler.pkl** — The StandardScaler parameters (mean=373.097, scale=32.877) are
  hardcoded in `model_loader.py`, derived from the 7,424-row `prop_train.csv` training set.

## How to obtain the model checkpoint

The `.model` file (~107 MB) is too large for git and is excluded by `ckpt/` in
`.gitignore`.

### Automatic download (recommended)

Set the `VITRIMER_VAE_CHECKPOINT_URL` environment variable to an Azure Blob
Storage URL for the checkpoint (SAS-signed or publicly accessible).  On first
use `model_loader.py` will download and cache the file automatically.

Optionally set `VITRIMER_VAE_CHECKPOINT_SHA256` to the file's SHA-256 digest
for integrity verification.

```bash
# In docker-compose environment section or .env:
VITRIMER_VAE_CHECKPOINT_URL=https://<account>.blob.core.windows.net/<container>/prop49.model?<sas>
VITRIMER_VAE_CHECKPOINT_SHA256=<sha256hex>
```

To upload the checkpoint and generate the SHA-256:

```bash
sha256sum prop49.model
az storage blob upload \
    --account-name <account> --container-name <container> \
    --file prop49.model --name prop49.model
```

### Manual placement

Place the file at `data/ckpt/prop49.model` before building the Docker image,
or generate it by running `VAE/train_vae_prop.py` (epoch 49 checkpoint).
