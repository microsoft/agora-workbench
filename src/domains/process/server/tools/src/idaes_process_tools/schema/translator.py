"""
Translator unit configuration for IDAES.

Defines configuration for the Translator unit that converts between property packages.
"""

from typing import List, Literal, Optional, Type

from idaes.models.unit_models import Translator
from pydantic import Field

from .base import UnitConfig


class TranslatorConfig(UnitConfig):
    """Configuration for an IDAES Translator unit.

    Translates between two property packages that may differ in state definitions and
    component sets. Constraint generation will be handled by the VariableManager.
    """

    unit_class: Literal["TranslatorConfig"] = "TranslatorConfig"
    unit_type: Type = Translator
    property_package: str = ""  # Override: not used for Translator

    # Names of property packages registered in the flowsheet
    property_package_in_name: str
    property_package_out_name: str

    # State definitions as strings (e.g., "FTPx", "FpcTP")
    state_definition_in: str
    state_definition_out: str

    # Optional stream labels
    inlet_streams: List[str] = Field(default_factory=lambda: ["inlet_stream"])
    outlet_streams: List[str] = Field(default_factory=lambda: ["outlet_stream"])

    # Component/state metadata
    components_in: List[str] = Field(default_factory=list)
    components_out: List[str] = Field(default_factory=list)

    # Optional: when mapping FTPx -> FpcTP, which output phase to allocate to
    target_phase_out: Optional[str] = None
