"""
Property and reaction package configurations for IDAES.

Defines configurations for thermodynamic property packages and reaction packages.
"""

from typing import Any, Dict, Union

from pydantic import Field

from .base import BaseModel


class PropertyPackageConfig(BaseModel):
    """Configuration for IDAES property packages.

    Property packages define thermodynamic/physical properties, including phase behavior,
    equations of state, and property methods. Typically used to construct an
    IDAES GenericParameterBlock.

    Attributes:
    - name: Name used to reference this property package in the flowsheet
    - config_dict: Dictionary passed to the property package factory (e.g., base_units,
        phases, components, state_definition, property and heat capacity methods, etc.)
        Can also be an IDAES property package that's already created
    """

    name: str  # Name used to reference this property package in the flowsheet
    config_dict: Union[Dict[str, Any], Any] = Field(default_factory=dict)


class ReactionPackageConfig(BaseModel):
    """Configuration for IDAES reaction packages.

    Reaction packages define reactions, kinetics, and stoichiometry and are linked to a
    specific property package.

    Attributes:
    - name: Identifier used to reference this reaction package in the flowsheet
    - property_package: Name of the associated property package
    - config_dict: Dictionary passed to the reaction package factory (e.g., base_units,
      rate_reactions/equilibrium_reactions, stoichiometry, kinetic parameters, etc.)
      Can also be an IDAES reaction package already created

    Example:
        rxn_config = ReactionPackageConfig(
            name="reaction_params",
            property_package="thermo_params",
            config_dict={...}
        )
    """

    name: str  # Name used to reference this reaction package in the flowsheet
    property_package: str  # Name of the property package to use
    config_dict: Union[Dict[str, Any], Any] = Field(default_factory=dict)
