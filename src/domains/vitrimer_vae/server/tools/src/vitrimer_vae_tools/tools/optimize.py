"""
Bayesian optimization over the VAE latent space to discover vitrimers
with target glass transition temperatures.

Re-implements the BO loop from VitrimerVAE using scikit-learn's
GaussianProcessRegressor instead of the Theano-based SparseGP.
"""

import numpy as np
import torch
from scipy.stats import norm
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import Matern

from vitrimer_vae_tools.model_loader import (
    load_model,
    load_pca,
    load_scaler,
    load_vocabs,
)
from vitrimer_vae_tools.vae import MolGraph, common_atom_vocab
from vitrimer_vae_tools.vae.chemutils import check_acid, check_epoxide


def _to_numpy(tensors):
    def convert(x):
        return x.numpy() if isinstance(x, torch.Tensor) else x

    a, b, c = tensors
    b = [convert(x) for x in b[0]], [convert(x) for x in b[1]]
    return a, b, c


def _tensorize(acid_list, epoxide_list, vocab_aci, vocab_epo):
    try:
        x_aci = MolGraph.tensorize(acid_list, vocab_aci, common_atom_vocab)
        x_epo = MolGraph.tensorize(epoxide_list, vocab_epo, common_atom_vocab)
        return _to_numpy(x_aci), _to_numpy(x_epo)
    except Exception:
        return None


def _decode_and_validate(z, model, vocab_aci, vocab_epo):
    """Decode a single latent vector, re-encode to get consistent Tg."""
    z_tensor = torch.tensor(z).float()
    if torch.cuda.is_available():
        z_tensor = z_tensor.cuda()

    try:
        acid, epoxide = model.decode(z_tensor)
        if not acid[0] or not epoxide[0]:
            return None, None, None
    except Exception:
        return None, None, None

    tensors = _tensorize(acid, epoxide, vocab_aci, vocab_epo)
    if tensors is None:
        return None, None, None

    with torch.no_grad():
        z_recon, _ = model.encode(tensors)  # type: ignore[reportAssignmentType]
        tg_norm = model.predict(z_recon).squeeze().detach().cpu().numpy()

    return acid[0], epoxide[0], tg_norm


def _expected_improvement(X_cand, gp, y_best, xi=0.01):
    """Compute expected improvement at candidate points."""
    mu, sigma = gp.predict(X_cand, return_std=True)
    sigma = np.maximum(sigma, 1e-8)
    Z = (y_best - mu) / sigma
    ei = (y_best - mu) * norm.cdf(Z) + sigma * norm.pdf(Z)
    return ei


def _generate_initial_pool(model, scaler, pca, vocab_aci, vocab_epo, seed, pool_size=1000):
    """Generate an initial pool of valid vitrimers by random sampling."""
    torch.manual_seed(seed)
    np.random.seed(seed)

    vitrimer_set = []
    for _ in range(30):
        try:
            aci_dec, epo_dec = model.sample(50)
        except KeyError:
            continue
        for i in range(len(aci_dec)):
            if check_acid(aci_dec[i]) and check_epoxide(epo_dec[i]):
                pair = (aci_dec[i], epo_dec[i])
                if pair not in vitrimer_set:
                    vitrimer_set.append(pair)
        if len(vitrimer_set) >= pool_size:
            break

    vitrimer_set = vitrimer_set[:pool_size]
    acids, epoxides = zip(*vitrimer_set) if vitrimer_set else ([], [])
    acids, epoxides = list(acids), list(epoxides)

    # Encode and predict Tg for the pool.
    z_list, tg_norm_list = [], []
    valid_acids, valid_epoxides = [], []
    batch_size = 32

    for start in range(0, len(acids), batch_size):
        end = min(start + batch_size, len(acids))
        tensors = _tensorize(acids[start:end], epoxides[start:end], vocab_aci, vocab_epo)
        if tensors is None:
            continue
        with torch.no_grad():
            z_recon, _ = model.encode(tensors)  # type: ignore[reportAssignmentType]
            tg_recon = model.predict(z_recon).squeeze()
        z_list.append(z_recon.detach().cpu().numpy())
        tg_vals = tg_recon.detach().cpu().tolist()
        if isinstance(tg_vals, float):
            tg_vals = [tg_vals]
        tg_norm_list.extend(tg_vals)
        valid_acids.extend(acids[start:end])
        valid_epoxides.extend(epoxides[start:end])

    z_pool = np.vstack(z_list) if z_list else np.empty((0, model.latent_size))
    tg_norm_pool = np.array(tg_norm_list)

    return z_pool, tg_norm_pool, valid_acids, valid_epoxides


def bayesian_optimize(
    target_tg: float = 373.0,
    maximize: bool = False,
    num_iterations: int = 50,
    candidates_per_iteration: int = 50,
    pool_size: int = 1000,
    seed: int = 1,
) -> dict:
    """
    Run Bayesian optimization in the VAE latent space to discover vitrimers
    with a target glass transition temperature.

    Args:
        target_tg: Target Tg in Kelvin. Ignored if ``maximize=True``.
        maximize: If True, maximize Tg instead of targeting a specific value.
        num_iterations: Number of BO iterations.
        candidates_per_iteration: Number of candidate points to evaluate per iteration.
        pool_size: Size of initial random molecule pool.
        seed: Random seed.

    Returns:
        Dict with ``acids``, ``epoxides``, ``tg_predicted``, ``iterations``,
        ``pca_coords``, and ``best_tg``.
    """
    model = load_model()
    scaler = load_scaler()
    pca = load_pca()
    vocab_aci, vocab_epo = load_vocabs()

    # Generate initial pool.
    z_pool, tg_norm_pool, _, _ = _generate_initial_pool(model, scaler, pca, vocab_aci, vocab_epo, seed, pool_size)

    if len(z_pool) == 0:
        return {"error": "Failed to generate initial molecule pool."}

    # Compute objective values.
    target_norm = scaler.transform(np.array([[target_tg]])).squeeze().item()

    def objective(tg_norm_vals):
        if maximize:
            return -tg_norm_vals  # Minimize negative = maximize
        return (tg_norm_vals - target_norm) ** 2

    X_train = z_pool.copy()
    y_train = objective(tg_norm_pool).reshape(-1, 1)

    discovered = []
    np.random.seed(seed)

    for iteration in range(num_iterations):
        # Fit GP surrogate.
        kernel = Matern(nu=2.5)
        gp = GaussianProcessRegressor(kernel=kernel, alpha=1e-2, normalize_y=True)
        gp.fit(X_train, y_train.ravel())

        # Generate random candidate points around the current data distribution.
        lower = np.min(X_train, axis=0)
        upper = np.max(X_train, axis=0)
        n_cand = candidates_per_iteration * 20
        candidates = lower + np.random.rand(n_cand, X_train.shape[1]) * (upper - lower)

        # Select top candidates by expected improvement.
        ei = _expected_improvement(candidates, gp, np.min(y_train))
        top_idx = np.argsort(ei)[-candidates_per_iteration:]
        z_next = candidates[top_idx]

        # Decode and validate candidates.
        z_new, y_new = [], []
        for i in range(len(z_next)):
            z = z_next[i].reshape(1, -1)
            acid, epoxide, tg_norm = _decode_and_validate(z, model, vocab_aci, vocab_epo)

            if acid and epoxide and tg_norm is not None:
                if check_acid(acid) and check_epoxide(epoxide):
                    tg_real = float(scaler.inverse_transform(np.array(tg_norm).reshape(-1, 1)).squeeze())
                    z_pca = pca.transform(z)[0].tolist() if pca is not None else []

                    discovered.append(
                        {
                            "iteration": iteration,
                            "acid": acid,
                            "epoxide": epoxide,
                            "tg_predicted": tg_real,
                            "pca_coords": z_pca,
                        }
                    )

                    z_new.append(z)
                    y_new.append(objective(np.array([tg_norm])))

        if z_new:
            X_train = np.concatenate([X_train, np.vstack(z_new)], axis=0)
            y_train = np.concatenate([y_train, np.array(y_new).reshape(-1, 1)], axis=0)

    # Compile results.
    if not discovered:
        return {
            "acids": [],
            "epoxides": [],
            "tg_predicted": [],
            "iterations": [],
            "pca_coords": [],
            "best_tg": None,
        }

    acids = [d["acid"] for d in discovered]
    epoxides = [d["epoxide"] for d in discovered]
    tgs = [d["tg_predicted"] for d in discovered]
    iters = [d["iteration"] for d in discovered]
    pca_coords = [d["pca_coords"] for d in discovered]

    best_idx = np.argmax(tgs) if maximize else np.argmin([abs(t - target_tg) for t in tgs])

    return {
        "acids": acids,
        "epoxides": epoxides,
        "tg_predicted": tgs,
        "iterations": iters,
        "pca_coords": pca_coords,
        "best_tg": tgs[best_idx],
    }
