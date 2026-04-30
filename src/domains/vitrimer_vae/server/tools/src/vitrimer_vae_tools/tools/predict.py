"""
Predict glass transition temperature (Tg) for given acid/epoxide pairs
by encoding through the VAE and using the property prediction head.
"""

import torch

from vitrimer_vae_tools.model_loader import load_model, load_scaler, load_vocabs
from vitrimer_vae_tools.vae import MolGraph, common_atom_vocab


def _to_numpy(tensors):
    """Convert tensorized molecule batch to numpy-compatible format."""

    def convert(x):
        return x.numpy() if isinstance(x, torch.Tensor) else x

    a, b, c = tensors
    b = [convert(x) for x in b[0]], [convert(x) for x in b[1]]
    return a, b, c


def _tensorize(acid_list, epoxide_list, vocab_aci, vocab_epo):
    """Tensorize lists of acid and epoxide SMILES for model encoding."""
    try:
        x_aci = MolGraph.tensorize(acid_list, vocab_aci, common_atom_vocab)
        x_epo = MolGraph.tensorize(epoxide_list, vocab_epo, common_atom_vocab)
        return _to_numpy(x_aci), _to_numpy(x_epo)
    except Exception:
        return None


def _tensorize_single(acid, epoxide, vocab_aci, vocab_epo):
    """Tensorize a single acid/epoxide pair for model encoding."""
    try:
        x_aci = MolGraph.tensorize([acid], vocab_aci, common_atom_vocab)
        x_epo = MolGraph.tensorize([epoxide], vocab_epo, common_atom_vocab)
        return _to_numpy(x_aci), _to_numpy(x_epo)
    except (KeyError, RuntimeError):
        return None


def predict_tg(
    acid_smiles: list[str],
    epoxide_smiles: list[str],
) -> dict:
    """
    Predict the glass transition temperature (Tg) for acid/epoxide pairs.

    Encodes each pair through the VAE encoder, then applies the property
    prediction head to obtain a Tg estimate.

    Args:
        acid_smiles: List of acid SMILES strings.
        epoxide_smiles: List of epoxide SMILES strings.

    Returns:
        Dict with ``tg_predicted`` (list[float]) and
        ``latent_vectors`` (list[list[float]]).
    """
    model = load_model()
    scaler = load_scaler()
    vocab_aci, vocab_epo = load_vocabs()

    tg_all = []
    z_all = []
    batch_size = 32

    for start in range(0, len(acid_smiles), batch_size):
        end = min(start + batch_size, len(acid_smiles))
        acid_batch = acid_smiles[start:end]
        epo_batch = epoxide_smiles[start:end]

        tensors = _tensorize(acid_batch, epo_batch, vocab_aci, vocab_epo)
        if tensors is None:
            # Batch tensorization failed; fall back to per-molecule processing
            # so one bad molecule doesn't poison the whole batch.
            for a, e in zip(acid_batch, epo_batch):
                single = _tensorize_single(a, e, vocab_aci, vocab_epo)
                if single is None:
                    tg_all.append(float("nan"))
                    z_all.append([float("nan")] * model.latent_size)
                    continue
                with torch.no_grad():
                    z_mean, _ = model.encode(single)  # type: ignore[reportAssignmentType]
                    tg_norm = model.predict(z_mean).detach().cpu().numpy().reshape(-1, 1)
                    tg = float(scaler.inverse_transform(tg_norm).squeeze())
                tg_all.append(tg)
                z_all.append(z_mean.detach().cpu().numpy().tolist()[0])
            continue

        with torch.no_grad():
            z_mean, _ = model.encode(tensors)  # type: ignore[reportAssignmentType]
            tg_norm = model.predict(z_mean).detach().cpu().numpy().reshape(-1, 1)
            tg = scaler.inverse_transform(tg_norm).squeeze()

        if tg.ndim == 0:
            tg = [float(tg)]
        else:
            tg = tg.tolist()

        tg_all.extend(tg)
        z_all.extend(z_mean.detach().cpu().numpy().tolist())

    return {
        "tg_predicted": tg_all,
        "latent_vectors": z_all,
    }
