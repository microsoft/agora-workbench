"""
Stream and material block configurations for IDAES.

Defines configurations for material streams, feeds, and products.
"""

from typing import Dict, List, Optional, Type

from idaes.models.unit_models import Feed, Product
from pydantic import Field, model_validator

from ..units import PyomoUnit, Quantity
from .base import BaseModel


class MaterialBlockConfig(BaseModel):
    """Configuration for material streams in IDAES (feeds, products, etc).

    Specifies state variables for a stream. Supports either total flow with global
    compositions or detailed phase-component flow specifications.

    Attributes:
        - property_package: Name of the property package used to build the material block
        - state_definition: State variable set expected by the property package (e.g., "FTPx",
            "FPhx", "FpcTP"); determines which variables must be fixed
        - temperature: Stream temperature (required by most state definitions)
        - pressure: Stream pressure (required by most state definitions)
        - flow_rate: Total molar flow of the stream (simple specification)
        - compositions: Component mole fractions (dimensionless); used with flow_rate
        - components: Optional list of component names (for documentation/validation)
        - phases: Optional list of phase names (for documentation/validation)
        - flow_mol_phase_comp: Detailed phase-component molar flows when using a phase-component
            state definition; format {phase: {component: Quantity}}

    Exactly one of the following specifications must be provided:
    - flow_rate + compositions
    - flow_mol_phase_comp
    """

    property_package: str
    state_definition: str = "FTPx"

    # Temperature and pressure settings
    temperature: Optional[Quantity] = None
    pressure: Optional[Quantity] = None

    # Simple approach: Total flow rate + compositions
    flow_rate: Optional[Quantity] = None
    compositions: Dict[str, Quantity] = Field(default_factory=dict)

    # Detailed approach: Component-phase specific flow rates
    components: List[str] = Field(default_factory=list)
    phases: List[str] = Field(default_factory=list)
    flow_mol_phase_comp: Dict[str, Dict[str, Quantity]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_flow_rates(self):
        """Ensure that exactly one flow specification method is used."""
        has_flow_rate = self.flow_rate is not None
        has_compositions = len(self.compositions) > 0
        has_flow_mol_phase_comp = len(self.flow_mol_phase_comp) > 0

        if not (has_flow_rate and has_compositions) and not has_flow_mol_phase_comp:
            raise ValueError("Specify either flow_rate+compositions OR flow_mol_phase_comp.")
        if has_flow_mol_phase_comp and (has_flow_rate or has_compositions):
            raise ValueError("Cannot specify both flow_rate+compositions AND flow_mol_phase_comp.")
        return self

    @model_validator(mode="after")
    def validate_fixed_variables_match_state_definition(self):
        """Validates that the fixed variables match the state definition of the property package."""
        if self.state_definition == "FTPx":
            if self.temperature is None:
                raise ValueError("Temperature must be provided for FTPx state definition")
            if self.pressure is None:
                raise ValueError("Pressure must be provided for FTPx state definition")
            if not (self.compositions and self.flow_rate):
                if not self.flow_mol_phase_comp:
                    raise ValueError(
                        "Either flow_mol_phase_comp or flow_rate and compositions must be provided for FTPx"
                    )
                else:
                    # Convert flow_mol_phase_comp to flow_rate and compositions
                    total_flow = 0.0
                    for phase_dict in self.flow_mol_phase_comp.values():
                        for flow_rate in phase_dict.values():
                            total_flow += flow_rate.value
                    self.flow_rate = Quantity(total_flow, PyomoUnit.mol_per_s)

                    self.compositions = {}
                    for phase, comp_dict in self.flow_mol_phase_comp.items():
                        for comp, flow_rate in comp_dict.items():
                            if comp not in self.compositions:
                                self.compositions[comp] = Quantity(0.0, PyomoUnit.dimensionless)
                            self.compositions[comp] += flow_rate.value / total_flow  # type: ignore

                    for comp in self.compositions:
                        self.compositions[comp] = Quantity(self.compositions[comp].value, PyomoUnit.dimensionless)
                    self.flow_mol_phase_comp = {}

        elif self.state_definition == "FPhx":
            if self.pressure is None:
                raise ValueError("Pressure must be provided for FPhx state definition")
            if self.temperature is None:
                raise ValueError("Enthalpy must be provided for FPhx state definition")
            if not self.flow_mol_phase_comp and not (self.compositions and self.flow_rate):
                raise ValueError("Either flow_mol_phase_comp or flow_rate and compositions must be provided for FPhx")
        elif self.state_definition == "FpcTP":
            if self.temperature is None:
                raise ValueError("Temperature must be provided for FpcTP state definition")
            if self.pressure is None:
                raise ValueError("Pressure must be provided for FpcTP state definition")
            if not self.flow_mol_phase_comp and not (self.compositions and self.flow_rate):
                raise ValueError("Either flow_mol_phase_comp or flow_rate and compositions must be provided for FpcTP")
        return self


class FeedConfig(BaseModel):
    """Configuration for an IDAES Feed unit (material inflow).

    Attributes:
        - name: Identifier for this feed unit
        - unit_type: Underlying IDAES unit model class (Feed)
        - property_package: Name of the property package used to build the unit
        - outlet_stream: Name of the outlet stream produced by this unit
        - feed_specification: MaterialBlockConfig describing the inlet stream conditions
    """

    name: str
    unit_type: Type = Feed
    property_package: str
    outlet_stream: str = "feed_stream"
    feed_specification: MaterialBlockConfig


class ProductConfig(BaseModel):
    """Configuration for an IDAES Product unit (material outflow).

    Attributes:
        - name: Identifier for this product unit
        - unit_type: Underlying IDAES unit model class (Product)
        - property_package: Name of the property package used to build the unit
        - inlet_stream: Name of the inlet stream feeding this product unit
        - product_specification: Optional MaterialBlockConfig for fixing product specs
    """

    name: str
    unit_type: Type = Product
    property_package: str
    inlet_stream: str = "product_stream"
    product_specification: Optional[MaterialBlockConfig] = None
