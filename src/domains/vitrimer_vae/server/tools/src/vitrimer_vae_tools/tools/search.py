"""
Search for similar molecules in the neighborhood of a given vitrimer
by adding Gaussian noise in targeted latent subspaces.
"""

import logging
import random

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


def _decode_neighbors(z_array, z_origin, model, pca, scaler, filter_mode):
    """Decode latent vectors and filter for valid, unique molecules."""
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

    acids, epoxides, dists, tgs, pca_coords = [], [], [], [], []
    seen_acids, seen_epoxides = set(), set()

    for i in range(len(acid_dec)):
        a, e = acid_dec[i], epo_dec[i]
        if not (check_acid(a) and check_epoxide(e)):
            continue

        if filter_mode == "acid" and a in seen_acids:
            continue
        elif filter_mode == "epoxide" and e in seen_epoxides:
            continue
        elif filter_mode == "both" and (a in seen_acids or e in seen_epoxides):
            continue

        seen_acids.add(a)
        seen_epoxides.add(e)
        acids.append(a)
        epoxides.append(e)
        dists.append(float(np.linalg.norm(z_origin - z_array[i])))
        tgs.append(float(tg[i]) if tg.ndim > 0 else float(tg))
        pca_coords.append(pca.transform(z_array[i].reshape(1, -1))[0].tolist() if pca is not None else [])

    return acids, epoxides, dists, tgs, pca_coords


def search_neighbors(
    acid_smiles: str,
    epoxide_smiles: str,
    search_type: str = "both",
    num_neighbors: int = 100,
    max_noise: float = 20.0,
    seed: int = 1,
) -> dict:
    """
    Search for similar vitrimers near a query molecule by perturbing its
    latent vector with Gaussian noise in targeted subspaces.

    Args:
        acid_smiles: Query acid SMILES.
        epoxide_smiles: Query epoxide SMILES.
        search_type: Which latent subspace to perturb:
            ``"acid"`` (acid-specific dims only),
            ``"epoxide"`` (epoxide-specific dims only),
            or ``"both"`` (full latent space).
        num_neighbors: Number of noise samples to generate.
        max_noise: Maximum noise magnitude.
        seed: Random seed.

    Returns:
        Dict with ``acids``, ``epoxides``, ``tg_predicted``, ``distances``,
        and ``pca_coords``.
    """
    torch.manual_seed(seed)
    random.seed(seed)
    np.random.seed(seed)

    model = load_model()
    scaler = load_scaler()
    pca = load_pca()
    vocab_aci, vocab_epo = load_vocabs()

    tensors = _tensorize_pair(acid_smiles, epoxide_smiles, vocab_aci, vocab_epo)
    if tensors is None:
        return {"error": "Failed to tensorize input molecules."}

    with torch.no_grad():
        z, _ = model.encode(tensors)  # type: ignore[reportAssignmentType]
    z = z.detach().cpu().numpy()

    latent_size = model.latent_size
    epoxide_size = model.epoxide_size
    share_size = model.share_size

    z1 = z[:, : (latent_size - epoxide_size)]
    z2 = z[:, (latent_size - epoxide_size) : (latent_size - epoxide_size + share_size)]
    z3 = z[:, (latent_size - epoxide_size + share_size) :]

    neighbors = []
    for _ in range(num_neighbors):
        noise_level = random.uniform(0, 1) * max_noise
        if search_type == "acid":
            noise_dir = np.random.normal(0, 1, size=z1.shape)
            noise_dir /= np.linalg.norm(noise_dir)
            neighbors.append(np.concatenate((z1 + noise_dir * noise_level, z2, z3), axis=1))
        elif search_type == "epoxide":
            noise_dir = np.random.normal(0, 1, size=z3.shape)
            noise_dir /= np.linalg.norm(noise_dir)
            neighbors.append(np.concatenate((z1, z2, z3 + noise_dir * noise_level), axis=1))
        else:  # "both"
            noise_dir = np.random.normal(0, 1, size=z.shape)
            noise_dir /= np.linalg.norm(noise_dir)
            neighbors.append(z + noise_dir * noise_level)

    z_neighbors = np.vstack(neighbors)

    filter_map = {"acid": "acid", "epoxide": "epoxide", "both": "both"}
    acids, epoxides, dists, tgs, pca_coords = _decode_neighbors(
        z_neighbors, z, model, pca, scaler, filter_map.get(search_type, "both")
    )

    # Sort by distance from query.
    order = sorted(range(len(dists)), key=lambda i: dists[i])
    return {
        "acids": [acids[i] for i in order],
        "epoxides": [epoxides[i] for i in order],
        "tg_predicted": [tgs[i] for i in order],
        "distances": [dists[i] for i in order],
        "pca_coords": [pca_coords[i] for i in order],
    }
