"""
Shared model loading utilities for Vitrimer VAE tools.

Provides cached loading of the HierVAE model, scaler, PCA transformer,
and vocabulary files to avoid redundant loading across tool calls
within a single session.

The model checkpoint (``prop49.model``, ~107 MB) is too large for git.
On first use, if the file is missing locally, it is automatically
downloaded from Azure Blob Storage using the URL in the
``VITRIMER_VAE_CHECKPOINT_URL`` environment variable.
"""

import hashlib
import logging
import os
import pickle
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

import torch

from vitrimer_vae_tools.vae import HierVAE, PairVocab, common_atom_vocab

LOGGER = logging.getLogger(__name__)

# Data directory bundled into the Docker image.
DATA_DIR = Path(
    os.environ.get(
        "VITRIMER_VAE_DATA_DIR",
        "/app/domains/vitrimer_vae/server/data",
    )
)

# Azure Blob Storage URL (with SAS token if needed) for the model
# checkpoint.  Set this in docker-compose or .env.
CHECKPOINT_URL = os.environ.get("VITRIMER_VAE_CHECKPOINT_URL", "")

# SHA-256 digest of the canonical prop49.model checkpoint.  Used to
# verify download integrity.  Set to "" to skip verification.
CHECKPOINT_SHA256 = os.environ.get(
    "VITRIMER_VAE_CHECKPOINT_SHA256",
    "",  # populate after first upload
)

# Default model hyperparameters matching the published VitrimerVAE checkpoint.
DEFAULT_HPARAMS = dict(
    rnn_type="LSTM",
    hidden_size=250,
    embed_size=250,
    batch_size=32,
    latent_size=128,
    acid_size=112,
    epoxide_size=112,
    share_size=96,
    depthT=15,
    depthG=15,
    diterT=1,
    diterG=3,
    dropout=0.0,
    prop_hidden_size=64,
)

_cache: dict = {}


def _get_device() -> torch.device:
    """Return CUDA device if available, else CPU."""
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _load_vocab(path: str) -> PairVocab:
    """Load a PairVocab from a whitespace-delimited text file."""
    pairs = [line.strip().split() for line in open(path)]
    return PairVocab(pairs, cuda=torch.cuda.is_available())


def _ensure_checkpoint(checkpoint: str) -> Path:
    """
    Return the local path to the checkpoint, downloading it first if absent.

    The download URL is read from ``VITRIMER_VAE_CHECKPOINT_URL``.  If the
    env var is unset and the file is missing, a clear error is raised.
    After download the SHA-256 digest is verified when
    ``VITRIMER_VAE_CHECKPOINT_SHA256`` is set.
    """
    ckpt_path = DATA_DIR / "ckpt" / checkpoint
    if ckpt_path.exists():
        return ckpt_path

    if not CHECKPOINT_URL:
        raise FileNotFoundError(
            f"Model checkpoint '{checkpoint}' not found at {ckpt_path} and "
            "VITRIMER_VAE_CHECKPOINT_URL is not set.  Either place the file "
            "manually or set the environment variable to an Azure Blob "
            "Storage URL (SAS-signed or publicly accessible)."
        )

    LOGGER.info("Checkpoint %s not found locally — downloading from blob storage", checkpoint)
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)

    # Download to a temporary file first, then rename for atomicity.
    tmp_path = ckpt_path.with_suffix(".downloading")
    try:
        # Acquire a bearer token for Azure Blob Storage when the URL
        # points to *.blob.core.windows.net and has no SAS query string.
        headers: dict[str, str] = {}
        from urllib.parse import urlparse

        parsed = urlparse(CHECKPOINT_URL)
        has_sas = parsed.query and "sig=" in parsed.query
        if parsed.hostname and parsed.hostname.endswith(".blob.core.windows.net") and not has_sas:
            try:
                from azure.identity import AzureCliCredential
                import tempfile

                # The mounted .azure directory may be read-only, but the
                # az CLI needs to write log files.  Create a writable
                # copy containing only the essential token-cache files.
                home_azure = Path.home() / ".azure"
                if home_azure.is_dir():
                    writable_cfg = Path(tempfile.mkdtemp(prefix="azure_cfg_"))
                    for name in (
                        "azureProfile.json",
                        "msal_token_cache.json",
                        "msal_token_cache.bin",
                        "clouds.config",
                        "az.json",
                        "az.sess",
                        "config",
                    ):
                        src = home_azure / name
                        if src.exists():
                            (writable_cfg / name).write_bytes(src.read_bytes())
                    # Also create writable commands/ dir for CLI logging.
                    (writable_cfg / "commands").mkdir(exist_ok=True)
                    os.environ["AZURE_CONFIG_DIR"] = str(writable_cfg)

                credential = AzureCliCredential()
                token = credential.get_token("https://storage.azure.com/.default")
                headers["Authorization"] = f"Bearer {token.token}"
                headers["x-ms-version"] = "2023-11-03"
            except Exception as auth_exc:
                LOGGER.warning(
                    "Could not obtain Azure credential for blob download, falling back to anonymous access: %s",
                    auth_exc,
                )

        download_timeout = int(os.environ.get("VITRIMER_VAE_DOWNLOAD_TIMEOUT", "600"))
        req = Request(CHECKPOINT_URL, headers=headers)
        with urlopen(req, timeout=download_timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status not in (200, 206):
                raise RuntimeError(f"Checkpoint download failed with HTTP status {status} from {CHECKPOINT_URL}")
            with open(tmp_path, "wb") as out_f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out_f.write(chunk)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to download checkpoint from {CHECKPOINT_URL}: {exc}") from exc

    # Verify integrity when a hash is provided.
    if CHECKPOINT_SHA256:
        sha = hashlib.sha256()
        with open(tmp_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                sha.update(chunk)
        actual = sha.hexdigest()
        if actual != CHECKPOINT_SHA256:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Checkpoint SHA-256 mismatch: expected {CHECKPOINT_SHA256}, "
                f"got {actual}.  The download may be corrupt."
            )

    tmp_path.rename(ckpt_path)
    LOGGER.info("Checkpoint saved to %s", ckpt_path)
    return ckpt_path


def load_model(checkpoint: str = "prop49.model") -> HierVAE:
    """
    Load a pre-trained HierVAE model with caching.

    Args:
        checkpoint: Model checkpoint filename inside ``ckpt/``.

    Returns:
        The loaded HierVAE model in eval mode.
    """
    cache_key = ("model", checkpoint)
    if cache_key in _cache:
        return _cache[cache_key]

    device = _get_device()

    args = SimpleNamespace(
        **DEFAULT_HPARAMS,
        atom_vocab=common_atom_vocab,
        vocab_aci=_load_vocab(str(DATA_DIR / "data" / "vocab_acid.txt")),
        vocab_epo=_load_vocab(str(DATA_DIR / "data" / "vocab_epoxide.txt")),
    )

    model = HierVAE(args)
    ckpt_path = _ensure_checkpoint(checkpoint)
    state_dict = torch.load(
        str(ckpt_path),
        map_location=device,
        weights_only=False,
    )
    # Checkpoints are stored as (state_dict, optimizer_state) tuples.
    if isinstance(state_dict, (list, tuple)):
        state_dict = state_dict[0]
    model.load_state_dict(state_dict)
    model.to(device)
    model.eval()

    _cache[cache_key] = model
    return model


def load_vocabs() -> tuple[PairVocab, PairVocab]:
    """Load acid and epoxide PairVocab objects (cached)."""
    cache_key = "vocabs"
    if cache_key in _cache:
        return _cache[cache_key]

    vocab_aci = _load_vocab(str(DATA_DIR / "data" / "vocab_acid.txt"))
    vocab_epo = _load_vocab(str(DATA_DIR / "data" / "vocab_epoxide.txt"))
    result = (vocab_aci, vocab_epo)
    _cache[cache_key] = result
    return result


def load_scaler():
    """
    Return a StandardScaler fitted on the prop_train.csv Tg values.

    Parameters are derived from the 7,424-row training set and hardcoded
    here so the scaler.pkl artifact is not required at runtime.
    """
    cache_key = "scaler"
    if cache_key in _cache:
        return _cache[cache_key]

    from sklearn.preprocessing import StandardScaler
    import numpy as np

    scaler = StandardScaler()
    # Fitted on prop_train.csv "tg" column (n=7424, ddof=0).
    scaler.mean_ = np.array([373.0971700027])
    scaler.scale_ = np.array([32.8766526526])
    scaler.var_ = np.array([32.8766526526**2])
    scaler.n_features_in_ = 1
    scaler.n_samples_seen_ = 7424

    _cache[cache_key] = scaler
    return scaler


def load_pca():
    """
    Load the PCA transformer for latent-space visualization (cached).

    Returns None if pca.pkl is not present — callers should handle this
    gracefully by omitting PCA coordinates from results.
    """
    cache_key = "pca"
    if cache_key in _cache:
        return _cache[cache_key]

    pca_path = DATA_DIR / "pca.pkl"
    if not pca_path.exists():
        _cache[cache_key] = None
        return None

    with open(pca_path, "rb") as f:
        pca = pickle.load(f)
    _cache[cache_key] = pca
    return pca


def get_calibration_csv_path() -> str:
    """Return the path to the bundled calibration CSV."""
    return str(DATA_DIR / "data" / "tg_calibration.csv")
