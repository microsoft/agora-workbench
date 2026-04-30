"""
Generate novel vitrimer molecules by sampling from the VAE latent space.
"""

import torch

from vitrimer_vae_tools.model_loader import load_model, load_scaler, load_vocabs
from vitrimer_vae_tools.vae import MolGraph, common_atom_vocab
from vitrimer_vae_tools.vae.chemutils import check_acid, check_epoxide, canon_smiles_list


def sample_molecules(num_samples: int = 20, seed: int = 1) -> dict:
    """
    Generate novel vitrimer molecules by sampling z ~ N(0, I) in latent space
    and decoding to acid/epoxide SMILES pairs.

    Args:
        num_samples: Number of valid molecules to generate.
        seed: Random seed for reproducibility.

    Returns:
        Dict with ``acids`` (list[str]), ``epoxides`` (list[str]),
        ``tg_predicted`` (list[float]), ``num_valid`` (int),
        and ``num_attempted`` (int).
    """
    torch.manual_seed(seed)
    model = load_model()
    scaler = load_scaler()
    vocab_aci, vocab_epo = load_vocabs()

    acids = []
    epoxides = []
    tg_values = []
    attempted = 0
    batch_size = 50

    while len(acids) < num_samples and attempted < num_samples * 30:
        n = min(batch_size, num_samples * 3)
        try:
            aci_dec, epo_dec = model.sample(n)
        except KeyError:
            # Decoder can generate out-of-vocabulary fragment combinations;
            # skip this batch and retry with fresh random state.
            attempted += n
            continue
        aci_dec = canon_smiles_list(aci_dec)
        epo_dec = canon_smiles_list(epo_dec)
        attempted += n

        for i in range(len(aci_dec)):
            if len(acids) >= num_samples:
                break
            if check_acid(aci_dec[i]) and check_epoxide(epo_dec[i]):
                acids.append(aci_dec[i])
                epoxides.append(epo_dec[i])

    # Predict Tg by re-encoding the decoded molecules so predictions
    # correspond to the actual molecular structures, not random z vectors.
    if acids:
        with torch.no_grad():
            for a, e in zip(acids, epoxides):
                try:
                    x_aci = MolGraph.tensorize([a], vocab_aci, common_atom_vocab)
                    x_epo = MolGraph.tensorize([e], vocab_epo, common_atom_vocab)
                    z_mean, _ = model.encode((x_aci, x_epo))  # type: ignore[misc]
                    tg_norm = model.predict(z_mean).detach().cpu().numpy().reshape(-1, 1)
                    tg = float(scaler.inverse_transform(tg_norm).squeeze())
                    tg_values.append(tg)
                except (KeyError, RuntimeError):
                    # KeyError: molecule fragment not in vocabulary
                    # RuntimeError: tensorization/encoding failure
                    tg_values.append(float("nan"))

    return {
        "acids": acids,
        "epoxides": epoxides,
        "tg_predicted": tg_values,
        "num_valid": len(acids),
        "num_attempted": attempted,
    }
