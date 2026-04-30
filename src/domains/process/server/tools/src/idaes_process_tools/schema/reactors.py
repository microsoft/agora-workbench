"""
Reactor unit operation configurations for IDAES.

Defines configurations for CSTR, Stoichiometric, and Gibbs reactors.
"""

from typing import Dict, List, Literal, Optional, Tuple, Type, Union

from idaes.models.unit_models import CSTR, GibbsReactor, StoichiometricReactor
from pydantic import Field, model_validator

from .base import UnitConfig
from ..units import Quantity


class CSTRConfig(UnitConfig):
    """Configuration for a Continuous Stirred Tank Reactor (CSTR).

    Well-mixed reactor with optional heat and pressure change. Requires a reaction
    package when modeling reactions.
    """

    unit_class: Literal["CSTRConfig"] = "CSTRConfig"
    unit_type: Type = CSTR

    # Construction parameters
    reaction_package: Optional[str] = None
    has_heat_transfer: bool = False
    has_pressure_change: bool = False
    has_heat_of_reaction: bool = True

    # Specification parameters
    volume: Optional[Quantity] = None
    conversion: Optional[Quantity] = None
    limiting_reactant: Optional[Union[str, Tuple[str, str]]] = None

    # Heat specification
    heat_duty: Optional[Quantity] = None
    outlet_temperature: Optional[Quantity] = None


class StoichiometricReactorConfig(UnitConfig):
    """Configuration for a Stoichiometric Reactor.

    Models reactions via conversion or yields (no kinetics). Requires a reaction package.
    """

    unit_class: Literal["StoichiometricReactorConfig"] = "StoichiometricReactorConfig"
    unit_type: Type = StoichiometricReactor

    # Construction parameters
    reaction_package: str
    has_heat_transfer: bool = False
    has_pressure_change: bool = False
    has_heat_of_reaction: bool = True

    # Reaction specification
    reaction_yield: Optional[Dict[str, Quantity]] = Field(default_factory=dict)
    conversion: Optional[Quantity] = None
    limiting_reactant: Optional[Union[str, Tuple[str, str]]] = None

    # Heat specification
    heat_duty: Optional[Quantity] = None
    outlet_temperature: Optional[Quantity] = None

    # Pressure specification
    outlet_pressure: Optional[Quantity] = None
    deltaP: Optional[Quantity] = None

    @model_validator(mode="after")
    def validate_reaction_specification(self):
        has_yield = self.reaction_yield is not None and len(self.reaction_yield) > 0
        has_conversion = self.conversion is not None

        if has_yield and has_conversion:
            raise ValueError("Cannot specify both reaction_yield and conversion")
        if has_conversion and self.limiting_reactant is None:
            raise ValueError("Must specify limiting_reactant when using conversion")
        if not (has_yield or has_conversion):
            raise ValueError("Must specify either reaction_yield or conversion")
        return self

    @model_validator(mode="after")
    def validate_heat_specification(self):
        if not self.has_heat_transfer:
            if self.heat_duty is not None or self.outlet_temperature is not None:
                raise ValueError("Cannot specify heat_duty or outlet_temperature when has_heat_transfer=False")
            return self

        has_heat_duty = self.heat_duty is not None
        has_outlet_temp = self.outlet_temperature is not None

        if has_heat_duty and has_outlet_temp:
            raise ValueError("Cannot specify both heat_duty and outlet_temperature")
        return self


class GibbsReactorConfig(UnitConfig):
    """Configuration for a Gibbs (equilibrium) Reactor.

    Assumes chemical equilibrium (minimizes Gibbs free energy); no reaction package required.
    """

    unit_class: Literal["GibbsReactorConfig"] = "GibbsReactorConfig"
    unit_type: Type = GibbsReactor

    # Constructor parameters
    has_heat_transfer: bool = True
    has_pressure_change: bool = False
    inert_species: List[str] = Field(default_factory=list)

    # Heat specification
    heat_duty: Optional[Quantity] = None
    outlet_temperature: Optional[Quantity] = None

    # Conversion specification
    reaction_yield: Optional[Dict[str, Quantity]] = Field(default_factory=dict)
    conversion: Optional[Quantity] = None
    converted_reactant: Optional[Union[str, Tuple[str, str]]] = None

    # Pressure specification
    outlet_pressure: Optional[Quantity] = None
    deltaP: Optional[Quantity] = None

    @model_validator(mode="after")
    def validate_gibbs_specification(self):
        if self.outlet_temperature is not None and self.heat_duty is not None:
            raise ValueError("Cannot specify both outlet_temperature and heat_duty")
        elif self.outlet_temperature is None and self.heat_duty is None and self.conversion is None:
            raise ValueError("Must specify either outlet_temperature or heat_duty or conversion")
        elif (self.outlet_temperature is not None or self.heat_duty is not None) and self.conversion is not None:
            raise ValueError("Cannot specify both heat (outlet_temperature/heat_duty) and conversion")
        return self
