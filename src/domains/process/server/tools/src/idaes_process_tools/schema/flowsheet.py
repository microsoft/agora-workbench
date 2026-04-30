"""
Flowsheet configuration for IDAES.

Defines the top-level FlowsheetConfig that combines all components.
"""

from typing import Any, Annotated, List, Optional, Union

from pydantic import Field, Discriminator

from .base import BaseModel
from .packages import PropertyPackageConfig, ReactionPackageConfig
from .streams import FeedConfig, ProductConfig
from .reactors import CSTRConfig, GibbsReactorConfig, StoichiometricReactorConfig
from .heat_transfer import HeatExchangerConfig, HeaterConfig
from .separations import DistillationColumnConfig, FlashConfig
from .mixing import MixerConfig, SplitterConfig
from .pressure_change import CompressorConfig, PumpConfig, TurbineConfig
from .translator import TranslatorConfig

# Union of all unit config types with discriminator
UnitConfigTypes = Annotated[
    Union[
        FlashConfig,
        HeaterConfig,
        HeatExchangerConfig,
        CSTRConfig,
        StoichiometricReactorConfig,
        GibbsReactorConfig,
        MixerConfig,
        SplitterConfig,
        PumpConfig,
        TurbineConfig,
        CompressorConfig,
        DistillationColumnConfig,
        TranslatorConfig,
    ],
    Discriminator("unit_class"),
]


class FlowsheetConfig(BaseModel):
    """Main configuration for an IDAES flowsheet (top-level container).

    Combines property/reaction packages, streams, and unit operations into a complete
    flowsheet configuration consumable by the flowsheet builder.

    Attributes:
        - name: Flowsheet name
        - property_packages: List of PropertyPackageConfig
        - material_blocks: List of FeedConfig/ProductConfig (streams)
        - unit_operations: List of UnitConfig (unit operation configurations)
        - reaction_packages: Optional list of ReactionPackageConfig
        - time_units: Units for dynamic time (default None, will use pyunits.s in builder)
        - time_set: Time points for dynamic models (default [0])
        - dynamic: Whether to construct a dynamic model (True) or steady-state (False)
    """

    name: str
    property_packages: List[PropertyPackageConfig]
    material_blocks: List[Union[FeedConfig, ProductConfig]]
    unit_operations: List[UnitConfigTypes] = Field(default_factory=list)
    reaction_packages: List[ReactionPackageConfig] = Field(default_factory=list)
    time_units: Optional[Any] = None  # Will be set to pyunits.s in builder if None
    time_set: List[float] = Field(default_factory=lambda: [0])
    dynamic: bool = False
