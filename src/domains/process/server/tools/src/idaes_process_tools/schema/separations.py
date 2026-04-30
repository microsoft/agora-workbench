"""
Separation unit operation configurations for IDAES.

Defines configurations for Flash and DistillationColumn units.
"""

from typing import Literal, Optional, Type

from idaes.models.unit_models import Flash
from idaes.models_extra.column_models import TrayColumn

from .base import UnitConfig
from ..units import Quantity


class FlashConfig(UnitConfig):
    """Configuration for a Flash (vapor–liquid equilibrium separator)."""

    unit_class: Literal["FlashConfig"] = "FlashConfig"
    unit_type: Type = Flash
    has_heat_transfer: bool = True
    has_pressure_change: bool = True

    # Specification parameters
    outlet_temperature: Optional[Quantity] = None
    outlet_pressure: Optional[Quantity] = None
    deltaP: Optional[Quantity] = None

    # Stream mapping
    vapor_outlet_stream: Optional[str] = None
    liquid_outlet_stream: Optional[str] = None


class DistillationColumnConfig(UnitConfig):
    """Configuration for Distillation unit"""

    unit_class: Literal["DistillationColumnConfig"] = "DistillationColumnConfig"
    unit_type: Type = TrayColumn
    has_pressure_change: bool = False
    number_of_trays: int = 6
    feed_tray_location: int = 3
    condenser_type: Literal["partial", "total"] = "total"
    condenser_temperature_spec: Literal["atBubblePoint", "atCustomTemperature"] = "atBubblePoint"

    # Specification parameters
    reflux_ratio: Optional[Quantity] = None
    boilup_ratio: Optional[Quantity] = None
    condenser_pressure: Optional[Quantity] = None
    deltaP: Optional[Quantity] = None

    # Stream mapping
    vapor_outlet_stream: Optional[str] = None
    liquid_outlet_stream: Optional[str] = None
