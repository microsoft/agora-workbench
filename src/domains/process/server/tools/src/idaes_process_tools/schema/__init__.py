"""
IDAES Process Configuration Schema

This package provides Pydantic-based configuration models for IDAES process simulation.
Configurations are organized by functionality:

- base: Core base models (BaseModel, UnitConfig)
- packages: Property and reaction package configurations
- streams: Material streams (feeds, products)
- reactors: Reactor unit operations (CSTR, Stoichiometric, Gibbs)
- heat_transfer: Heat transfer units (Heater, HeatExchanger)
- separations: Separation units (Flash, DistillationColumn)
- mixing: Mixing/splitting units (Mixer, Splitter)
- pressure_change: Pressure change units (Pump, Turbine, Compressor)
- translator: Property package translation
- flowsheet: Top-level flowsheet configuration
"""

# Base utilities
from .base import BaseModel, UnitConfig

# Property and reaction packages
from .packages import PropertyPackageConfig, ReactionPackageConfig

# Stream configurations
from .streams import FeedConfig, MaterialBlockConfig, ProductConfig

# Reactor units
from .reactors import CSTRConfig, GibbsReactorConfig, StoichiometricReactorConfig

# Heat transfer units
from .heat_transfer import HeatExchangerConfig, HeaterConfig

# Separation units
from .separations import DistillationColumnConfig, FlashConfig

# Mixing/splitting units
from .mixing import MixerConfig, SplitterConfig

# Pressure change units
from .pressure_change import CompressorConfig, PumpConfig, TurbineConfig

# Translator
from .translator import TranslatorConfig

# Top-level flowsheet
from .flowsheet import FlowsheetConfig

__all__ = [
    # Base
    "BaseModel",
    "UnitConfig",
    # Packages
    "PropertyPackageConfig",
    "ReactionPackageConfig",
    # Streams
    "FeedConfig",
    "MaterialBlockConfig",
    "ProductConfig",
    # Reactors
    "CSTRConfig",
    "GibbsReactorConfig",
    "StoichiometricReactorConfig",
    # Heat transfer
    "HeatExchangerConfig",
    "HeaterConfig",
    # Separations
    "DistillationColumnConfig",
    "FlashConfig",
    # Mixing/splitting
    "MixerConfig",
    "SplitterConfig",
    # Pressure change
    "CompressorConfig",
    "PumpConfig",
    "TurbineConfig",
    # Translator
    "TranslatorConfig",
    # Flowsheet
    "FlowsheetConfig",
]
