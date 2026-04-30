"""
Calibrate MD-simulated glass transition temperatures using a Gaussian Process
with a Tanimoto kernel, re-implemented with scikit-learn (no TensorFlow/GPflow
dependency).
"""

import numpy as np
import pandas as pd
from rdkit import Chem
from vitrimer_vae_tools.model_loader import get_calibration_csv_path
from rdkit.Chem import AllChem
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import Kernel, Hyperparameter
from sklearn.preprocessing import StandardScaler


class TanimotoKernel(Kernel):
    """
    Tanimoto (Jaccard) similarity kernel for molecular fingerprints.

    K(X, X') = variance * (X · X') / (||X||² + ||X'||² - X · X')
    """

    def __init__(self, variance=1.0, variance_bounds=(1e-5, 1e5)):
        self.variance = variance
        self.variance_bounds = variance_bounds

    @property
    def hyperparameter_variance(self):
        return Hyperparameter("variance", "numeric", self.variance_bounds)

    def __call__(self, X, Y=None, eval_gradient=False):
        if Y is None:
            Y = X

        XY = X @ Y.T
        X_sq = np.sum(X**2, axis=1)
        Y_sq = np.sum(Y**2, axis=1)

        denom = X_sq[:, None] + Y_sq[None, :] - XY
        # Avoid division by zero for identical zero-vectors.
        denom = np.maximum(denom, 1e-12)

        K = self.variance * XY / denom

        if eval_gradient:
            K_gradient = (XY / denom)[:, :, np.newaxis]
            return K, K_gradient

        return K

    def diag(self, X):
        return np.full(X.shape[0], self.variance)

    def is_stationary(self):
        return False

    def __repr__(self):
        return f"TanimotoKernel(variance={self.variance:.3g})"


def _vitrimerize(acid: str, epoxide: str) -> str:
    """
    Perform the acid + epoxide coupling reaction to form a vitrimer SMILES.

    Uses the same reaction SMARTS as the original VitrimerVAE calibration code.
    Returns an empty string if the reaction fails (invalid SMILES or no products).
    """
    try:
        acid_mol = Chem.MolFromSmiles(acid)
        epoxide_mol = Chem.MolFromSmiles(epoxide)
        if acid_mol is None or epoxide_mol is None:
            return ""

        rxn1 = AllChem.ReactionFromSmarts("[CX3:1](=O)[OX2H1:2]>>[CX3:1](=O)[OX2:2][*]")
        products = rxn1.RunReactants((acid_mol,))
        if not products:
            return ""
        acid_mol = products[0][0]
        Chem.SanitizeMol(acid_mol)

        rxn2 = AllChem.ReactionFromSmarts("[OD2r3:1]1[#6D2r3:2][#6r3:3]1>>[#6:3]([OD2:1])[#6D2:2][*]")
        products = rxn2.RunReactants((epoxide_mol,))
        if not products:
            return ""
        epoxide_mol = products[0][0]
        Chem.SanitizeMol(epoxide_mol)

        rxn = AllChem.ReactionFromSmarts(
            "[CX3:1](=O)[OX2H1:2].[OD2r3:3]1[#6D2r3:4][#6r3:5]1>>[CX3:1](=O)[OX2:2][#6D2:4][#6:5]([OD2:3])"
        )
        products = rxn.RunReactants((acid_mol, epoxide_mol))
        if not products:
            return ""
        vitrimer_mol = products[0][0]
        Chem.SanitizeMol(vitrimer_mol)
        return Chem.CanonSmiles(Chem.MolToSmiles(vitrimer_mol))
    except Exception:
        return ""


def _fingerprint(smiles_list: list[str]) -> np.ndarray:
    """Compute Morgan fingerprints (radius=3, 2048 bits) for a list of SMILES."""
    fps = []
    for smi in smiles_list:
        mol = AllChem.MolFromSmiles(smi)
        if mol is None:
            raise ValueError(f"Failed to parse SMILES for fingerprinting: {smi}")
        fp = AllChem.GetMorganFingerprintAsBitVect(mol, radius=3, nBits=2048)
        fps.append(np.array(fp, dtype=np.float64))
    return np.array(fps)


def calibrate_tg(
    acid_smiles: list[str],
    epoxide_smiles: list[str],
    tg_md: list[float],
) -> dict:
    """
    Calibrate MD-simulated Tg values against experimental Tg using a GP with
    Tanimoto kernel on Morgan fingerprints.

    The GP learns the correction (tg_exp - tg_md) from calibration data, then
    applies that correction to new vitrimer predictions.

    Args:
        acid_smiles: List of acid SMILES strings.
        epoxide_smiles: List of epoxide SMILES strings.
        tg_md: List of MD-simulated Tg values (in Kelvin).

    Returns:
        Dict with ``tg_calibrated`` (list of calibrated Tg values) and
        ``vitrimer_smiles`` (list of vitrimer SMILES).
    """
    # Validate input lengths.
    if len(acid_smiles) != len(epoxide_smiles) or len(acid_smiles) != len(tg_md):
        raise ValueError(
            f"Input lists must have the same length: got {len(acid_smiles)} acids, "
            f"{len(epoxide_smiles)} epoxides, {len(tg_md)} tg_md values."
        )

    # Load calibration training data.
    cal_df = pd.read_csv(get_calibration_csv_path())
    cal_smiles = cal_df["smiles"].to_numpy()
    cal_tg_exp = cal_df["tg_exp"].to_numpy()
    cal_tg_md = cal_df["tg_md"].to_numpy()

    # Filter out entries with non-positive MD Tg (following original code).
    valid = cal_tg_md > 0
    cal_smiles = cal_smiles[valid]
    cal_tg_exp = cal_tg_exp[valid]
    cal_tg_md = cal_tg_md[valid]

    # Train on the correction: tg_exp - tg_md.
    y_train = (cal_tg_exp - cal_tg_md).reshape(-1, 1)
    y_scaler = StandardScaler()
    y_train_scaled = y_scaler.fit_transform(y_train)

    X_train = _fingerprint(cal_smiles.tolist())

    # Build vitrimer SMILES for test molecules and compute fingerprints.
    vitrimer_smiles = [_vitrimerize(acid_smiles[i], epoxide_smiles[i]) for i in range(len(acid_smiles))]
    X_test = _fingerprint(vitrimer_smiles)

    # Fit GP with Tanimoto kernel.
    kernel = TanimotoKernel(variance=1.0)
    gp = GaussianProcessRegressor(kernel=kernel, alpha=1.0, normalize_y=False)
    gp.fit(X_train, y_train_scaled)

    # Predict correction and apply.
    y_pred_scaled = gp.predict(X_test)
    correction = y_scaler.inverse_transform(y_pred_scaled.reshape(-1, 1)).squeeze()
    tg_calibrated = (np.array(tg_md) + correction).tolist()

    return {
        "tg_calibrated": tg_calibrated,
        "vitrimer_smiles": vitrimer_smiles,
    }
