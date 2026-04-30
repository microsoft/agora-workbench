"""
Mixing and splitting unit operation configurations for IDAES.

Defines configurations for Mixer and Splitter units.
"""

from typing import Dict, List, Literal, Type

from idaes.models.unit_models import Mixer, Separator
from pydantic import Field, model_validator

from .base import UnitConfig


class MixerConfig(UnitConfig):
    """Configuration for a Mixer (multiple inlets → single outlet)."""

    unit_class: Literal["MixerConfig"] = "MixerConfig"
    unit_type: Type = Mixer
    momentum_mixing_type: str = "minimize"  # Will be converted to MomentumMixingType enum in builder
    inlet_list: List[str] = Field(default_factory=list)
    has_phase_equilibrium: bool = False

    @model_validator(mode="after")
    def set_inlet_list(self) -> "MixerConfig":
        self.inlet_list = self.inlet_streams
        return self


class SplitterConfig(UnitConfig):
    """Configuration for a Splitter (single inlet → multiple outlets).

    TODO: Implement splitter properly
    """

    unit_class: Literal["SplitterConfig"] = "SplitterConfig"
    unit_type: Type = Separator
    outlet_list: List[str] = Field(default_factory=list)
    split_fractions: Dict[str, float] = Field(default_factory=dict)
