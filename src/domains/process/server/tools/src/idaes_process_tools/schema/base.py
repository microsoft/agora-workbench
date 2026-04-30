"""
Base models and utilities for IDAES configuration.

Provides base Pydantic models used across all configuration models.
"""

from typing import List

from pydantic import BaseModel as PydanticBaseModel


class BaseModel(PydanticBaseModel):
    """Base Pydantic model with relaxed typing for IDAES config classes."""

    class Config:
        arbitrary_types_allowed = True


class UnitConfig(BaseModel):
    """Base configuration for all IDAES unit operations.

    Attributes (shared by all units):
        - name: Identifier for the unit operation
        - unit_class: Type discriminator for determining which unit config class to use
        - property_package: Name of the property package used to build the unit
        - inlet_streams: Names of inlet streams to connect to the unit
        - outlet_streams: Names of outlet streams produced by the unit
        - dynamic: Whether to construct a dynamic (True) or steady-state (False) unit
        - has_holdup: Whether to include material holdup (affects mass balances and DOF)
    """

    name: str
    property_package: str
    inlet_streams: List[str]
    outlet_streams: List[str]
    dynamic: bool = False
    has_holdup: bool = False
