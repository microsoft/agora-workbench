"""
Fit IDAES-ready coefficients from `thermo` for a single compound.

- Ideal gas:   RPP4 cp(T) (J/mol/K)  -> A,B,C,D
               h(T), s(T) from RPP4 integrals + formation offsets at Tref
- Liquid:      Perrys cp(T) (J/kmol/K) -> C1..C5
               h(T), s(T) from Perrys integrals + formation offsets at Tref
- Psat(T):     Antoine (log10), P in bar, T in K -> A,B,C

Outputs:
- Coefficients + basic error metrics
- IDAES Generic Property Package `configuration` dict

Author: (c) 2025
"""

from __future__ import annotations

import dataclasses as dc
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from numpy.typing import ArrayLike
from scipy.optimize import curve_fit

# ChEDL / thermo
from thermo import Chemical

# ----------------------------- Utilities ----------------------------- #


@dc.dataclass
class RPP4IG:
    A: float
    B: float
    C: float
    D: float  # J/mol/K, J/mol/K^2, J/mol/K^3, J/mol/K^4

    def cp(self, T: ArrayLike) -> np.ndarray:
        T = np.asarray(T)
        return self.A + self.B * T + self.C * T**2 + self.D * T**3

    def h(self, T: ArrayLike, Tref: float, h_form: float = 0.0) -> np.ndarray:
        """Absolute ideal-gas enthalpy (J/mol) using RPP4 integral + formation offset at Tref.
        h(Tref) = h_form
        """
        T = np.asarray(T)
        dT = T - Tref
        dT2 = T**2 - Tref**2
        dT3 = T**3 - Tref**3
        dT4 = T**4 - Tref**4
        h_rel = self.A * dT + 0.5 * self.B * dT2 + (1.0 / 3.0) * self.C * dT3 + 0.25 * self.D * dT4
        return h_form + h_rel

    def s(self, T: ArrayLike, Tref: float, s_form: float = 0.0) -> np.ndarray:
        """Absolute ideal-gas entropy (J/mol/K) using RPP4 integral + formation offset at Tref.
        s(Tref) = s_form
        """
        T = np.asarray(T)
        s_rel = (
            self.A * np.log(T / Tref)
            + self.B * (T - Tref)
            + 0.5 * self.C * (T**2 - Tref**2)
            + (1.0 / 3.0) * self.D * (T**3 - Tref**3)
        )
        return s_form + s_rel


@dc.dataclass
class Antoine:
    A: float
    B: float
    C: float  # log10(P) = A - B/(T + C)

    def log10p(self, T: ArrayLike) -> np.ndarray:
        return antoine_log10(T, self.A, self.B, self.C)


@dc.dataclass
class PerrysRho:
    C1: float
    C2: float
    C3: float
    C4: float
    eqn_type: int  # kmol/m3, etc.

    def density(self, T: ArrayLike) -> np.ndarray:
        if self.eqn_type == 1:
            return perry_eq1(T, self.C1, self.C2, self.C3, self.C4)
        elif self.eqn_type == 2:
            return perry_eq2(T, self.C1, self.C2, self.C3, self.C4)
        else:
            raise ValueError(f"Unknown equation type: {self.eqn_type}")


@dc.dataclass
class PerrysLiq:
    C1: float
    C2: float
    C3: float
    C4: float
    C5: float  # J/kmol/K, etc.

    def cp(self, T: ArrayLike) -> np.ndarray:
        T = np.asarray(T)
        return self.C1 + self.C2 * T + self.C3 * T**2 + self.C4 * T**3 + self.C5 * T**4  # J/kmol/K

    def h(self, T: ArrayLike, Tref: float, h_form_kmol: float = 0.0) -> np.ndarray:
        """Absolute liquid enthalpy (J/kmol) using Perrys integral + formation offset at Tref."""
        T = np.asarray(T)
        dT = T - Tref
        dT2 = T**2 - Tref**2
        dT3 = T**3 - Tref**3
        dT4 = T**4 - Tref**4
        dT5 = T**5 - Tref**5
        h_rel = (
            self.C1 * dT
            + 0.5 * self.C2 * dT2
            + (1.0 / 3.0) * self.C3 * dT3
            + 0.25 * self.C4 * dT4
            + 0.2 * self.C5 * dT5
        )
        return h_form_kmol + h_rel

    def s(self, T: ArrayLike, Tref: float, s_form_kmol: float = 0.0) -> np.ndarray:
        """Absolute liquid entropy (J/kmol/K) using Perrys integral + formation offset at Tref."""
        T = np.asarray(T)
        s_rel = (
            self.C1 * np.log(T / Tref)
            + self.C2 * (T - Tref)
            + 0.5 * self.C3 * (T**2 - Tref**2)
            + (1.0 / 3.0) * self.C4 * (T**3 - Tref**3)
            + 0.25 * self.C5 * (T**4 - Tref**4)
        )
        return s_form_kmol + s_rel


def rmse(y_true: ArrayLike, y_pred: ArrayLike) -> float:
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)
    return float(np.sqrt(np.mean((y_true - y_pred) ** 2)))


# ----------------------------- Fitting Routines ----------------------------- #


def fit_rpp4_cp(T: ArrayLike, Cp_ig: ArrayLike) -> RPP4IG:
    """Fit ideal-gas cp ~ A + B T + C T^2 + D T^3  (J/mol/K)."""
    T = np.asarray(T)
    y = np.asarray(Cp_ig)
    # Vandermonde columns for degrees 0..3
    X = np.vstack([np.ones_like(T), T, T**2, T**3]).T
    theta, *_ = np.linalg.lstsq(X, y, rcond=None)
    A, B, C, D = theta.tolist()
    return RPP4IG(A, B, C, D)


def fit_perrys_cp_liq(T: ArrayLike, Cp_liq_J_per_molK: ArrayLike) -> PerrysLiq:
    """Fit liquid cp quartic in T but with Perrys units (J/kmol/K).
    Convert y by 1000 to kmol basis before fitting.
    """
    T = np.asarray(T)
    y = 1e3 * np.asarray(Cp_liq_J_per_molK)  # -> J/kmol/K
    X = np.vstack([np.ones_like(T), T, T**2, T**3, T**4]).T
    theta, *_ = np.linalg.lstsq(X, y, rcond=None)
    C1, C2, C3, C4, C5 = theta.tolist()
    return PerrysLiq(C1, C2, C3, C4, C5)


# Perry Eqn 1: rho = C1 / (C2^(1 + (1 - T/C3)^C4))
def perry_eq1(T: ArrayLike, C1: float, C2: float, C3: float, C4: float) -> np.ndarray:
    T = np.asarray(T)
    return C1 / (C2 ** (1.0 + (1.0 - T / C3) ** C4))


def fit_perrys_density_eq1(T: ArrayLike, density_kmol_per_m3: ArrayLike, T_crit: float) -> PerrysRho:
    """
    Fit Perry's Eqn 1 for liquid density:
        rho = C1 / (C2^(1 + (1 - T/C3)^C4))
    Inputs:
        T: Temperature in K
        density_kmol_per_m3: density in kmol/m^3
    Returns:
        (C1, C2, C3, C4)
    """
    T = np.asarray(T)
    y = np.asarray(density_kmol_per_m3)

    # Initial guesses:
    rho_ref = float(np.median(y))
    _, T_max = float(np.min(T)), float(np.max(T))
    C1_0 = rho_ref
    C2_0 = 1.0
    # Ensure C3_0 > T_max to keep (1 - T/C3) positive in the fit window
    C3_0 = max(float(T_crit), T_max * 1.05)
    C4_0 = 1.0

    p0 = [C1_0, C2_0, C3_0, C4_0]

    # Bounds:
    # C1 > 0, C2 > 0, C3 > max(T), C4 > 0
    # First try without bounds
    lower_bounds = [0.1 * rho_ref, 0.1, T_max, 0.1]
    upper_bounds = [10 * rho_ref, 10.0, 5 * T_max, 10.0]

    popt, _ = curve_fit(perry_eq1, T, y, p0=p0, bounds=(lower_bounds, upper_bounds), maxfev=50000)
    # popt, _ = curve_fit(perry_eq1, T, y, p0=p0, maxfev=50000)

    return PerrysRho(C1=popt[0], C2=popt[1], C3=popt[2], C4=popt[3], eqn_type=1)


def perry_eq2(T: ArrayLike, C1: float, C2: float, C3: float, C4: float) -> np.ndarray:
    T = np.asarray(T)
    return C1 + C2 * T + C3 * T**2 + C4 * T**3


def fit_perrys_density_eq2(T: ArrayLike, density_kmol_per_m3: ArrayLike) -> PerrysRho:
    """Fit liquid density quartic in T but with Perrys units (kmol/m3).
    Convert y by 1e-3 to kmol basis before fitting.
    Two equations in are possible. We just use the one that applies more often here (eqn 2 in IDAES)
    (1)  rho = C1 / (C2^(1+(1-T/C3)^C4)) TODO: Implement eqn 1 as well
    (2)  rho = C1 + C2 T + C3 T^2 + C4 T^3

    """
    T = np.asarray(T)
    y = 1e-3 * np.asarray(density_kmol_per_m3)  # -> kmol/m3
    X = np.vstack([np.ones_like(T), T, T**2, T**3]).T
    theta, *_ = np.linalg.lstsq(X, y, rcond=None)
    C1, C2, C3, C4 = theta.tolist()
    return PerrysRho(C1, C2, C3, C4, eqn_type=2)


def antoine_log10(T: ArrayLike, A: float, B: float, C: float) -> np.ndarray:
    """log10(P[bar]) with T[K].  log10 P = A - B/(T + C)"""
    T = np.asarray(T)
    return A - B / (T + C)


def fit_antoine(T: ArrayLike, Psat: ArrayLike) -> Antoine:
    """Fit Antoine with P[bar], T[K]."""
    T = np.asarray(T)
    P_bar = np.asarray(Psat)  # already in bar (caller ensures)
    y = np.log10(P_bar)
    # Initial guesses: A ~ log10(P) mid, B ~ range scaling, C ~ small shift
    A0 = float(np.median(y))
    B0 = 1000.0
    C0 = -100.0
    popt, _ = curve_fit(antoine_log10, T, y, p0=[A0, B0, C0], maxfev=50000)
    A, B, C = popt.tolist()
    return Antoine(A, B, C)


# ----------------------------- Data Sampling from `thermo` ----------------------------- #


@dc.dataclass
class SampleGrids:
    Tref: float
    T_ranges: Dict[str, np.ndarray]


def select_best_ideal_method(ranked_methods, available_methods, exclude={"HEOS_FIT", "COOLPROP"}):
    for method in ranked_methods:
        if method in available_methods and method not in exclude:
            return method
    return None


def sample_from_thermo(
    identifier: str,
    grids: SampleGrids,
    properties: List[str] = ["cp_vap", "cp_liq", "density", "psat"],
    P_for_liq: float = 1e5,
) -> Tuple[Dict[str, np.ndarray], Dict[str, str]]:
    """
    Pull values from `thermo` for a compound:
      - Ideal-gas cp
      - Liquid cp
      - Psat(T)
      - Density
    Note: We sample cp only; h and s are formed via the fitted integrals.
    """
    chem = Chemical(identifier, T=grids.Tref, P=P_for_liq)

    samples = {}
    used_methods = {}
    for property in properties:
        if property == "cp_vap":
            # Ideal gas cp [J/mol/K]
            cp_igs = []
            method_g = select_best_ideal_method(chem.HeatCapacityGas.ranked_methods, chem.HeatCapacityGas.all_methods)
            used_methods["Cp_ig"] = method_g
            cp_igs = np.array([chem.HeatCapacityGas.calculate(T, method=method_g) for T in grids.T_ranges["T_ig"]])
            samples["Cp_ig"] = cp_igs

        if property == "cp_liq":
            # Liquid cp [J/mol/K] (only valid where liquid exists; choose range accordingly)
            method_l = select_best_ideal_method(
                chem.HeatCapacityLiquid.ranked_methods, chem.HeatCapacityLiquid.all_methods
            )
            used_methods["Cp_liq"] = method_l
            cp_liqs = np.array([chem.HeatCapacityLiquid.calculate(T, method=method_l) for T in grids.T_ranges["T_liq"]])
            samples["Cp_liq"] = cp_liqs

        if property == "density":
            # Density [kg/m^3]
            method_rho = select_best_ideal_method(chem.VolumeLiquid.ranked_methods, chem.VolumeLiquid.all_methods)
            used_methods["Density"] = method_rho
            density = np.array(
                [chem.VolumeLiquid.calculate(T, method=method_rho) for T in grids.T_ranges["T_liq"]]
            )  # m3/mol
            density = 1 / density  # mol/m3
            density = density / 1e3  # Convert to kmol/m3 for Perrys fit
            # density = np.where(density < 0, 0, density)  # Replace negative values with 0
            samples["Density"] = density

        if property == "psat":
            # Psat [Pa] -> convert to bar
            method_psat = select_best_ideal_method(chem.VaporPressure.ranked_methods, chem.VaporPressure.all_methods)
            used_methods["Psat_bar"] = method_psat
            Psat_bar = (
                np.array([chem.VaporPressure.calculate(T, method=method_psat) for T in grids.T_ranges["T_psat"]]) / 1e5
            )
            samples["Psat_bar"] = Psat_bar

    return samples, used_methods


# ----------------------------- Fit and test against ground truth coefficients ----------------------------- #


@dc.dataclass
class FitResults:
    rpp4_ig: RPP4IG
    perrys_liq: PerrysLiq
    perrys_rho: PerrysRho
    antoine: Antoine
    # formation offsets (absolute level) at Tref
    h_form_vap_ref: float  # J/mol at Tref
    s_form_vap_ref: float  # J/mol/K at Tref
    h_form_liq_ref: float  # J/kmol at Tref
    s_form_liq_ref: float  # J/kmol/K at Tref
    # predictions
    cp_ig_pred: np.ndarray
    cp_liq_pred: np.ndarray
    psat_pred: np.ndarray
    density_pred: np.ndarray
    # errors
    rmse_cp_ig: float
    rmse_cp_liq: float
    rmse_psat: float
    rmse_density: float


def fit_all(
    identifier: str,
    grids: SampleGrids,
    h_form_vap_ref: float = 0.0,
    s_form_vap_ref: float = 0.0,
    h_form_liq_ref_kmol: float = 0.0,
    s_form_liq_ref_kmol: float = 0.0,
) -> FitResults:
    # Identifier can be cas nr or compound name
    data, used_methods = sample_from_thermo(identifier, grids)

    # Ideal gas cp fit (RPP4)
    rpp4 = fit_rpp4_cp(grids.T_ranges["T_ig"], data["Cp_ig"])
    cp_ig_pred = rpp4.cp(grids.T_ranges["T_ig"])
    err_cp_ig = rmse(data["Cp_ig"], cp_ig_pred)

    # Liquid cp fit (Perrys quartic in J/kmol/K)
    perry = fit_perrys_cp_liq(grids.T_ranges["T_liq"], data["Cp_liq"])
    cp_liq_pred = perry.cp(grids.T_ranges["T_liq"]) / 1e3  # back to J/mol/K for RMSE
    err_cp_liq = rmse(data["Cp_liq"], cp_liq_pred)
    # print(data["Cp_liq"], cp_liq_pred)

    # Fit liquid density
    # Get T_crit which is used for initial parameter guess
    c = Chemical(identifier)
    T_crit = c.Tc
    perry_density = fit_perrys_density_eq1(grids.T_ranges["T_liq"], data["Density"], T_crit)
    density_pred = perry_density.density(grids.T_ranges["T_liq"])
    err_density = rmse(data["Density"], density_pred)

    # Psat fit (Antoine), P in bar
    antoine = fit_antoine(grids.T_ranges["T_psat"], data["Psat_bar"])
    psat_bar_pred = 10.0 ** antoine.log10p(grids.T_ranges["T_psat"])
    err_psat = rmse(data["Psat_bar"], psat_bar_pred)

    return FitResults(
        rpp4_ig=rpp4,
        perrys_liq=perry,
        perrys_rho=perry_density,
        antoine=antoine,
        h_form_vap_ref=h_form_vap_ref,
        s_form_vap_ref=s_form_vap_ref,
        h_form_liq_ref=h_form_liq_ref_kmol,
        s_form_liq_ref=s_form_liq_ref_kmol,
        cp_ig_pred=cp_ig_pred,
        cp_liq_pred=cp_liq_pred,
        psat_pred=psat_bar_pred,
        density_pred=density_pred,
        rmse_cp_ig=err_cp_ig,
        rmse_cp_liq=err_cp_liq,
        rmse_psat=err_psat,
        rmse_density=err_density,
    )


def get_perrys_ground_truth(cas_nr):
    # Load the Excel file using the openpyxl engine
    df = pd.read_excel("earthshots/dac/process/idaes/flowsheet/heat-capacities-liquids.xlsx", engine="openpyxl")
    # Clean numeric columns: remove commas and convert to float where possible
    for col in df.columns:
        try:
            # Remove commas and convert to float
            df[col] = df[col].astype(str).str.replace(",", "")
            df[col] = df[col].astype(float)
        except ValueError:
            # Skip columns that cannot be converted to float
            continue

    row = df[df["CAS no."] == cas_nr]
    if not row.empty:
        C1_GT = float(row["C1"].values[0])
        C2_GT = float(row["C2"].values[0])
        C3_GT = float(row["C3"].values[0])
        C4_GT = float(row["C4"].values[0])
        C5_GT = float(row["C5"].values[0])
        # Set nan values to 0.0
        C1_GT = C1_GT if not pd.isna(C1_GT) else 0.0
        C2_GT = C2_GT if not pd.isna(C2_GT) else 0.0
        C3_GT = C3_GT if not pd.isna(C3_GT) else 0.0
        C4_GT = C4_GT if not pd.isna(C4_GT) else 0.0
        C5_GT = C5_GT if not pd.isna(C5_GT) else 0.0
    else:
        C1_GT = C2_GT = C3_GT = C4_GT = C5_GT = 0.0

    return C1_GT, C2_GT, C3_GT, C4_GT, C5_GT


# ----------------------------- Example usage ----------------------------- #

if __name__ == "__main__":
    # Ideal gas cp methods
    cpig_method_ranking = ["WEBBOOK_SHOMATE", "POLING_POLY", "TRCIG", "JOBACK", "LASTOVKA_SHAW"]
    # liquid ideal cp methods
    # cp_liq_method_ranking = ["PERRYS", 'ZABRANSKY_SPLINE', 'ZABRANSKY_QUASIPOLYNOMIAL', 'ZABRANSKY_SPLINE_C', 'ZABRANSKY_QUASIPOLYNOMIAL_C', 'ZABRANSKY_SPLINE_SAT', 'ZABRANSKY_QUASIPOLYNOMIAL_SAT', 'WEBBOOK_SHOMATE', 'JANAF', 'UNARY', 'VDI_TABULAR', 'COOLPROP', 'DADGOSTAR_SHAW', 'ROWLINSON_POLING', 'ROWLINSON_BONDI', 'POLING_CONST', 'CRCSTD'
    # Create the Chemical object
    ethanol = Chemical("ethanol")

    # See what method it's using now
    print("Default method:", ethanol.HeatCapacityGas.method)

    # See all available methods for Cp_g
    print(
        "Available methods:",
    )

    # Pick one (e.g. 'POLY', 'DIPPR_PERRY_8E')
    # This will be an ideal-gas method
    T = 350.0  # Kelvin

    print(ethanol.HeatCapacityGas(T))
    print(list(ethanol.HeatCapacityGas.all_methods))
    # ethanol.HeatCapacityGas.method = "POLING_POLY"

    # Now get Cp_g at a certain temperature
    cpg = ethanol.HeatCapacityGas(T)
    print("Cp_g [J/mol/K]:", cpg)

    # Now get Cp_g at a certain temperature
    # ---- Choose the compound and temperature windows ----
    compound_name = "formic acid"  # name known to `thermo` (CAS strings work too)
    Tref = 298.15
    grids = SampleGrids(
        Tref=Tref,
        T_ranges={
            "T_ig": np.linspace(250.0, 1200.0, 120),  # ideal-gas Cp fit window
            "T_liq": np.linspace(250.0, 360.0, 60),  # liquid Cp fit window (subcooled)
            "T_psat": np.linspace(
                273.0, 351.6, 100
            ),  # Psat fit below Tc, test if antoine coefficients equivalent to https://webbook.nist.gov/cgi/cbook.cgi?ID=C64175&Units=SI&Mask=4#Thermo-Phase
        },
    )

    # Optional: if you have absolute reference formation values at Tref (vap & liq), set them:
    h_form_vap = 0.0  # J/mol @ Tref
    s_form_vap = 0.0  # J/mol/K @ Tref
    h_form_liq = 0.0  # J/kmol @ Tref
    s_form_liq = 0.0  # J/kmol/K @ Tref

    # ---- Fit all coefficients ----
    results = fit_all(
        identifier=compound_name,
        grids=grids,
        h_form_vap_ref=h_form_vap,
        s_form_vap_ref=s_form_vap,
        h_form_liq_ref_kmol=h_form_liq,
        s_form_liq_ref_kmol=s_form_liq,
    )

    cas = Chemical(compound_name).CAS
    C1_GT, C2_GT, C3_GT, C4_GT, C5_GT = get_perrys_ground_truth(cas)
    perry_GT = PerrysLiq(C1_GT, C2_GT, C3_GT, C4_GT, C5_GT)
    cp_liq_GT = perry_GT.cp(grids.T_ranges["T_liq"]) / 1e3  # back to J/mol/K for RMSE

    print("\n=== Fit Summary ===")
    print(
        f"RPP4 (ig cp): A={results.rpp4_ig.A:.6e}, B={results.rpp4_ig.B:.6e}, "
        f"C={results.rpp4_ig.C:.6e}, D={results.rpp4_ig.D:.6e}  [J/mol/K, ...]"
    )
    print(f"  RMSE cp_ig: {results.rmse_cp_ig:.4e} J/mol/K over {len(grids.T_ranges['T_ig'])} T-points")

    print(
        f"Perrys (liq cp): C1={results.perrys_liq.C1:.6e}, C2={results.perrys_liq.C2:.6e}, "
        f"C3={results.perrys_liq.C3:.6e}, C4={results.perrys_liq.C4:.6e}, C5={results.perrys_liq.C5:.6e}  [J/kmol/K, ...]"
    )
    print(f"  RMSE cp_liq: {results.rmse_cp_liq:.4e} J/mol/K over {len(grids.T_ranges['T_liq'])} T-points")

    print(
        f"Perrys (liq density): C1={results.perrys_rho.C1:.6e}, C2={results.perrys_rho.C2:.6e}, "
        f"C3={results.perrys_rho.C3:.6e}, C4={results.perrys_rho.C4:.6e}  [kmol/m3, ...]"
    )
    print(f"  RMSE density: {results.rmse_density:.4e} kmol/m3 over {len(grids.T_ranges['T_liq'])} T-points")

    print(
        f"Antoine (Psat): A={results.antoine.A:.6f}, B={results.antoine.B:.3f}, C={results.antoine.C:.3f}  "
        f"(log10 P[bar] = A - B/(T[K]+C))"
    )
    print(f"  RMSE Psat: {results.rmse_psat:.4e} bar over {len(grids.T_ranges['T_psat'])} T-points")

    # Loop through perry xlsx and get the predictions from fits from thermo and compare
    # Load the Excel file using the openpyxl engine
    df = pd.read_excel("earthshots/dac/process/idaes/flowsheet/heat-capacities-liquids.xlsx", engine="openpyxl")
    skipped_not_available = []
    for id, row in df.iterrows():
        cas = str(row["CAS no."])
        try:
            chem = Chemical(cas)
        except Exception:
            try:
                # CAS lookup failed; retry using compound name
                chem = Chemical(row["Name"])
                cas = chem.CAS
            except Exception as e:
                print(
                    f"Skipping component {row['Name']}, because it's not available in chemicals database: ERROR: {str(e)}"
                )
                skipped_not_available.append(row["Name"])
        # 1. Ground truth perry coefficients and fit
        C1_GT, C2_GT, C3_GT, C4_GT, C5_GT = get_perrys_ground_truth(cas)
        perry_GT = PerrysLiq(C1_GT, C2_GT, C3_GT, C4_GT, C5_GT)
        cp_liq_GT = perry_GT.cp(grids.T_ranges["T_liq"]) / 1e3  # back to J/mol/K for RMSE
        # 2. Approximation via polynomial fit
        skipped_failed = []
        try:
            results = fit_all(
                identifier=cas,
                grids=grids,
                h_form_vap_ref=h_form_vap,
                s_form_vap_ref=s_form_vap,
                h_form_liq_ref_kmol=h_form_liq,
                s_form_liq_ref_kmol=s_form_liq,
            )
        except Exception as e:
            print(f"Error occurred while fitting {row['Name']}: {str(e)}")
            skipped_failed.append(row["Name"])
            continue
        cp_liq_poly = results.perrys_liq.cp(grids.T_ranges["T_liq"]) / 1e3  # back to J/mol/K for RMSE
        # 3. Compare the fit with the GT (MAE) and plot
        print(f"Compound name: {row['Name']}")
        print(
            f"Perrys (liq cp): C1={results.perrys_liq.C1:.6e}, C2={results.perrys_liq.C2:.6e}, "
            f"C3={results.perrys_liq.C3:.6e}, C4={results.perrys_liq.C4:.6e}, C5={results.perrys_liq.C5:.6e}  [J/kmol/K, ...]"
        )
        print(f"  RMSE cp_liq: {results.rmse_cp_liq:.4e} J/mol/K over {len(grids.T_ranges['T_liq'])} T-points")
        """ import matplotlib.pyplot as plt

        plt.figure(figsize=(10, 6))
        plt.plot(grids.T_liq, cp_liq_GT, label="Ground Truth", linestyle="--")
        plt.plot(grids.T_liq, cp_liq_poly, label="Polynomial Fit", linestyle="-")
        plt.xlabel("Temperature [K]")
        plt.ylabel("Heat Capacity [J/mol/K]")
        plt.title(f"Comparison of Heat Capacities for {compound_name}")
        plt.legend()
        plt.grid()
        plt.show() """
