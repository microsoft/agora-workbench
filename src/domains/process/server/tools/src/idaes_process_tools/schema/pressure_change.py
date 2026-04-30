"""
Pressure change unit operation configurations for IDAES.

Defines configurations for Pump, Turbine, and Compressor units.
"""

from typing import Literal, Optional, Type

from idaes.models.unit_models import Compressor, Pump, Turbine
from pydantic import model_validator

from .base import UnitConfig
from ..units import Quantity


class PumpConfig(UnitConfig):
    """Configuration for a Pump (liquid pressure increase)."""

    unit_class: Literal["PumpConfig"] = "PumpConfig"
    unit_type: Type = Pump
    efficiency_pump: float = 0.8
    efficiency_motor: float = 0.9
    outlet_pressure: Optional[Quantity] = None


class TurbineConfig(UnitConfig):
    """Configuration for a Turbine (expander/power recovery)."""

    unit_class: Literal["TurbineConfig"] = "TurbineConfig"
    unit_type: Type = Turbine
    thermodynamic_assumption: Literal["isothermal", "isentropic"] = "isentropic"

    # Specification parameters
    outlet_pressure: Optional[Quantity] = None
    deltaP: Optional[Quantity] = None
    efficiency_isentropic: Optional[Quantity] = None


class CompressorConfig(UnitConfig):
    """Configuration for a Compressor (gas pressure increase)."""

    unit_class: Literal["CompressorConfig"] = "CompressorConfig"
    unit_type: Type = Compressor
    compressor: bool = True
    has_phase_equilibrium: bool = False
    thermodynamic_assumption: Literal["isothermal", "isentropic"] = "isentropic"

    # Specification parameters
    outlet_pressure: Optional[Quantity] = None
    efficiency_isentropic: Optional[Quantity] = None

    @model_validator(mode="after")
    def validate_compressor_specifications(self):
        if self.thermodynamic_assumption == "isentropic" and self.efficiency_isentropic is None:
            raise ValueError("Must specify efficiency_isentropic when using isentropic assumption")
        return self
