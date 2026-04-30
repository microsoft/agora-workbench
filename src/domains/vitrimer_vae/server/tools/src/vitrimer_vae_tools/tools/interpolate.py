"""
Interpolate between two vitrimer molecules in latent space using
linear or spherical (great-circle) interpolation.
"""

import logging

import numpy as np
import torch

from vitrimer_vae_tools.model_loader import (
    load_model,
    load_pca,
    load_scaler,
    load_vocabs,
)
from vitrimer_vae_tools.vae import MolGraph, common_atom_vocab
from vitrimer_vae_tools.vae.chemutils import check_acid, check_epoxide

LOGGER = logging.getLogger(__name__)


def _to_numpy(tensors):
    def convert(x):
        return x.numpy() if isinstance(x, torch.Tensor) else x

    a, b, c = tensors
    b = [convert(x) for x in b[0]], [convert(x) for x in b[1]]
    return a, b, c


def _tensorize_pair(acid, epoxide, vocab_aci, vocab_epo):
    try:
        x_aci = MolGraph.tensorize([acid], vocab_aci, common_atom_vocab)
        x_epo = MolGraph.tensorize([epoxide], vocab_epo, common_atom_vocab)
        return _to_numpy(x_aci), _to_numpy(x_epo)
    except Exception:
        return None


def _decode_points(z_array, z_origin, model, pca, scaler):
    """Decode latent vectors, returning only valid unique vitrimer pairs."""
    z_tensor = torch.tensor(z_array).float()
    if torch.cuda.is_available():
        z_tensor = z_tensor.cuda()

    with torch.no_grad():
        try:
            acid_dec, epo_dec = model.decode(z_tensor)
        except KeyError:
            LOGGER.warning("Decoding failed due to out-of-vocabulary fragment combination")
            return [], [], [], [], []
        tg_norm = model.predict(z_tensor).detach().cpu().numpy().reshape(-1, 1)
    tg = scaler.inverse_transform(tg_norm).squeeze()

    acids, epoxides, dists, tgs, pca_list = [], [], [], [], []
    seen = set()

    for i in range(len(acid_dec)):
        a, e = acid_dec[i], epo_dec[i]
        if not (check_acid(a) and check_epoxide(e)):
            continue
        pair = (a, e)
        if pair in seen:
            continue
        seen.add(pair)
        acids.append(a)
        epoxides.append(e)
        dists.append(float(np.linalg.norm(z_origin - z_array[i])))
        tgs.append(float(tg[i]) if tg.ndim > 0 else float(tg))
        pca_list.append(pca.transform(z_array[i].reshape(1, -1))[0].tolist() if pca is not None else [])

    return acids, epoxides, dists, tgs, pca_list


def interpolate_molecules(
    acid1: str,
    epoxide1: str,
    acid2: str,
    epoxide2: str,
    method: str = "linear",
    num_points: int = 20,
    seed: int = 5,
) -> dict:
    """
    Generate intermediate vitrimer molecules by interpolating between two
    endpoints in the VAE latent space.

    Args:
        acid1: Start-point acid SMILES.
        epoxide1: Start-point epoxide SMILES.
        acid2: End-point acid SMILES.
        epoxide2: End-point epoxide SMILES.
        method: Interpolation method — ``"linear"`` or ``"spherical"``.
        num_points: Number of intermediate points to generate.
        seed: Random seed.

    Returns:
        Dict with ``acids``, ``epoxides``, ``tg_predicted``, ``distances``,
        and ``pca_coords`` (sorted by distance from start).
    """
    torch.manual_seed(seed)

    model = load_model()
    scaler = load_scaler()
    pca = load_pca()
    vocab_aci, vocab_epo = load_vocabs()

    # Encode both endpoints.
    t1 = _tensorize_pair(acid1, epoxide1, vocab_aci, vocab_epo)
    t2 = _tensorize_pair(acid2, epoxide2, vocab_aci, vocab_epo)
    if t1 is None or t2 is None:
        return {"error": "Failed to tensorize one or both input molecules."}

    with torch.no_grad():
        z_start, _ = model.encode(t1)  # type: ignore[reportAssignmentType]
        z_end, _ = model.encode(t2)  # type: ignore[reportAssignmentType]
    z_start = z_start.detach().cpu().numpy().squeeze()
    z_end = z_end.detach().cpu().numpy().squeeze()

    # Compute Tg for endpoints.
    with torch.no_grad():
        tg_start = float(
            scaler.inverse_transform(
                model.predict(torch.tensor(z_start).float().unsqueeze(0).to(next(model.parameters()).device))
                .detach()
                .cpu()
                .numpy()
                .reshape(-1, 1)
            ).squeeze()
        )
        tg_end = float(
            scaler.inverse_transform(
                model.predict(torch.tensor(z_end).float().unsqueeze(0).to(next(model.parameters()).device))
                .detach()
                .cpu()
                .numpy()
                .reshape(-1, 1)
            ).squeeze()
        )

    # Generate intermediate latent points.
    z_inter = []
    for i in range(num_points):
        alpha = (i + 1) / (num_points + 1)
        if method == "spherical":
            theta = np.arccos(
                np.clip(
                    np.dot(z_start, z_end) / (np.linalg.norm(z_start) * np.linalg.norm(z_end)),
                    -1.0,
                    1.0,
                )
            )
            if theta < 1e-8:
                z = z_start * (1 - alpha) + z_end * alpha
            else:
                z = (z_end * np.sin(alpha * theta) + z_start * np.sin((1 - alpha) * theta)) / np.sin(theta)
        else:  # linear
            z = z_end * alpha + z_start * (1 - alpha)
        z_inter.append(z)

    z_inter = np.array(z_inter)

    acids, epoxides, dists, tgs, pca_coords = _decode_points(z_inter, z_start, model, pca, scaler)

    # Add endpoints.
    z_pca_start = pca.transform(z_start.reshape(1, -1))[0].tolist() if pca is not None else []
    z_pca_end = pca.transform(z_end.reshape(1, -1))[0].tolist() if pca is not None else []

    acids.extend([acid1, acid2])
    epoxides.extend([epoxide1, epoxide2])
    tgs.extend([tg_start, tg_end])
    dists.extend([0.0, float(np.linalg.norm(z_start - z_end))])
    pca_coords.extend([z_pca_start, z_pca_end])

    # Sort by distance from start.
    order = sorted(range(len(dists)), key=lambda i: dists[i])
    return {
        "acids": [acids[i] for i in order],
        "epoxides": [epoxides[i] for i in order],
        "tg_predicted": [tgs[i] for i in order],
        "distances": [dists[i] for i in order],
        "pca_coords": [pca_coords[i] for i in order],
    }
