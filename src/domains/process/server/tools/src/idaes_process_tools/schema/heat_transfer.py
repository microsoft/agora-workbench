"""
Heat transfer unit operation configurations for IDAES.

Defines configurations for Heater and HeatExchanger units.
"""

from typing import Literal, Optional, Type

from idaes.models.unit_models import Heater, HeatExchanger
from pydantic import model_validator

from .base import UnitConfig
from ..units import Quantity


class HeaterConfig(UnitConfig):
    """Configuration for a Heater (or cooler)."""

    unit_class: Literal["HeaterConfig"] = "HeaterConfig"
    unit_type: Type = Heater
    has_pressure_change: bool = False
    has_phase_equilibrium: bool = False

    # Specification parameters
    heat_duty: Optional[Quantity] = None
    outlet_temperature: Optional[Quantity] = None

    @model_validator(mode="after")
    def validate_mutually_exclusive_parameters(self):
        if self.heat_duty is not None and self.outlet_temperature is not None:
            raise ValueError("Cannot specify both heat_duty and outlet_temperature")
        return self


class HeatExchangerConfig(UnitConfig):
    """Configuration for a Heat Exchanger (two-stream heat transfer).

    TODO: Not yet implemented properly!
    """

    unit_class: Literal["HeatExchangerConfig"] = "HeatExchangerConfig"
    unit_type: Type = HeatExchanger
    delta_temperature_approach: Optional[Quantity] = None
    has_pressure_change: bool = False
    hot_side_name: str = "hot"
    cold_side_name: str = "cold"
