"""
IDAES Property Package Generation Utilities

This module provides tools for generating IDAES property package configurations
from thermodynamic data sources (thermo library, fitted correlations).

Main Components:
    - build_property_config: Generate complete IDAES property config dict
    - HEOSFitGAS/HEOSFitLIQ: Custom IDAES property classes for fitted data
    - Polynomial fitting utilities: RPP4, Perrys, Antoine correlations
    - PropertyMethodRankings: Configure property method priorities

Example:
    >>> from property_generation import build_property_config
    >>> components = {"H2O": ["Vap", "Liq"], "CO2": ["Vap"]}
    >>> temp_range = (300, 500)  # K
    >>> config = build_property_config(
    ...     comp_phases=components, temperature_range=temp_range, eos_config={"type": "ideal"}
    ... )
    >>> # Use with IDAES
    >>> from idaes.models.properties.modular_properties.base.generic_property import GenericParameterBlock
    >>> thermo_params = GenericParameterBlock(**config)

Note:
    NIST WebBook LLM extraction has been disabled in this version.
    The module falls back to thermo library data for all property correlations.
"""

from .build_property_config import (
    build_property_config,
    PropertyMethodRankings,
    PropertyMethodSelector,
    PropertyMethodApplicator,
    PropertyDataProcessor,
)
from .heos_fit import HEOSFitGAS, HEOSFitLIQ
from .polyfit_thermo import (
    RPP4IG,
    Antoine,
    PerrysLiq,
    PerrysRho,
    fit_rpp4_cp,
    fit_perrys_cp_liq,
    fit_perrys_density_eq1,
    fit_antoine,
    SampleGrids,
    sample_from_thermo,
)
from ..units import Quantity

__all__ = [
    # Main function
    "build_property_config",
    # Configuration classes
    "PropertyMethodRankings",
    "PropertyMethodSelector",
    "PropertyMethodApplicator",
    "PropertyDataProcessor",
    # Custom IDAES property classes
    "HEOSFitGAS",
    "HEOSFitLIQ",
    # Correlation dataclasses
    "RPP4IG",
    "Antoine",
    "PerrysLiq",
    "PerrysRho",
    "Quantity",
    # Fitting functions
    "fit_rpp4_cp",
    "fit_perrys_cp_liq",
    "fit_perrys_density_eq1",
    "fit_antoine",
    # Sampling utilities
    "SampleGrids",
    "sample_from_thermo",
]
