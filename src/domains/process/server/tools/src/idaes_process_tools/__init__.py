"""
Process Simulation Tools for IDAES
==================================

This package contains tools and configuration models for building and executing
IDAES process simulations within the AgoraAgentMAF code execution server.

Modules:
    schema: Pydantic configuration classes for defining IDAES flowsheets
    builder: IdaesFlowsheetBuilder for constructing IDAES models from configs
    variable_manager: VariableManager for managing unit specifications
    units: Pyomo unit utilities (PyomoUnit, Quantity, UnitWrapper)
"""

# Re-export all schema classes
from .schema import (
    # Base models
    BaseModel,
    UnitConfig,
    # Package configurations
    PropertyPackageConfig,
    ReactionPackageConfig,
    # Stream configurations
    MaterialBlockConfig,
    FeedConfig,
    ProductConfig,
    # Reactor configurations
    CSTRConfig,
    StoichiometricReactorConfig,
    GibbsReactorConfig,
    # Heat transfer configurations
    HeaterConfig,
    HeatExchangerConfig,
    # Separation configurations
    FlashConfig,
    DistillationColumnConfig,
    # Mixing/splitting configurations
    MixerConfig,
    SplitterConfig,
    # Pressure change configurations
    PumpConfig,
    TurbineConfig,
    CompressorConfig,
    # Translator configuration
    TranslatorConfig,
    # Top-level flowsheet configuration
    FlowsheetConfig,
)

__all__ = [
    # Base models
    "BaseModel",
    # Package configurations
    "PropertyPackageConfig",
    "ReactionPackageConfig",
    # Stream configurations
    "MaterialBlockConfig",
    "FeedConfig",
    "ProductConfig",
    # Unit operation base
    "UnitConfig",
    # Reactor configurations
    "CSTRConfig",
    "StoichiometricReactorConfig",
    "GibbsReactorConfig",
    # Heat transfer configurations
    "HeaterConfig",
    "HeatExchangerConfig",
    # Separation configurations
    "FlashConfig",
    "DistillationColumnConfig",
    # Mixing/splitting configurations
    "MixerConfig",
    "SplitterConfig",
    # Pressure change configurations
    "PumpConfig",
    "TurbineConfig",
    "CompressorConfig",
    # Translator configuration
    "TranslatorConfig",
    # Top-level flowsheet configuration
    "FlowsheetConfig",
]
