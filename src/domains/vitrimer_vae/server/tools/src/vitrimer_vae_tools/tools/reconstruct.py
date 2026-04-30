"""
Reconstruct vitrimer molecules through the VAE encoder-decoder pipeline
to assess reconstruction fidelity.
"""

import torch

from vitrimer_vae_tools.model_loader import load_model, load_vocabs
from vitrimer_vae_tools.vae import MolGraph, common_atom_vocab
from vitrimer_vae_tools.vae.chemutils import canon_smiles_list


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


def reconstruct_molecules(
    acid_smiles: list[str],
    epoxide_smiles: list[str],
) -> dict:
    """
    Reconstruct vitrimer molecules by encoding through the VAE and decoding
    back to SMILES. Compares input vs output to measure reconstruction quality.

    Args:
        acid_smiles: List of acid SMILES strings.
        epoxide_smiles: List of epoxide SMILES strings.

    Returns:
        Dict with ``acid_original``, ``epoxide_original``,
        ``acid_reconstructed``, ``epoxide_reconstructed``,
        ``acid_match`` (list[bool]), ``epoxide_match`` (list[bool]),
        and ``reconstruction_accuracy`` (float).
    """
    model = load_model()
    vocab_aci, vocab_epo = load_vocabs()

    acid_orig_all = []
    epo_orig_all = []
    acid_recon_all = []
    epo_recon_all = []
    batch_size = 32

    for start in range(0, len(acid_smiles), batch_size):
        end = min(start + batch_size, len(acid_smiles))
        acid_batch = acid_smiles[start:end]
        epo_batch = epoxide_smiles[start:end]

        tensors = _tensorize(acid_batch, epo_batch, vocab_aci, vocab_epo)
        if tensors is None:
            acid_recon_all.extend([""] * (end - start))
            epo_recon_all.extend([""] * (end - start))
            acid_orig_all.extend(acid_batch)
            epo_orig_all.extend(epo_batch)
            continue

        with torch.no_grad():
            aci_dec, epo_dec = model.reconstruct(tensors)

        aci_dec = canon_smiles_list(aci_dec)
        epo_dec = canon_smiles_list(epo_dec)

        acid_orig_all.extend(acid_batch)
        epo_orig_all.extend(epo_batch)
        acid_recon_all.extend(aci_dec)
        epo_recon_all.extend(epo_dec)

    # Compute per-molecule match.
    acid_match = [a == r for a, r in zip(acid_orig_all, acid_recon_all)]
    epo_match = [e == r for e, r in zip(epo_orig_all, epo_recon_all)]
    both_match = [a and e for a, e in zip(acid_match, epo_match)]

    n = len(both_match)
    accuracy = sum(both_match) / n if n > 0 else 0.0

    return {
        "acid_original": acid_orig_all,
        "epoxide_original": epo_orig_all,
        "acid_reconstructed": acid_recon_all,
        "epoxide_reconstructed": epo_recon_all,
        "acid_match": acid_match,
        "epoxide_match": epo_match,
        "reconstruction_accuracy": accuracy,
    }
