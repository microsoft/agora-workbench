"""
IDAES Flowsheet Builder
----------------------
This module builds IDAES flowsheets from configuration objects.
"""

from typing import Any, Dict, Type

import idaes.logger as idaeslog
from idaes.core import Component, FlowsheetBlock, Phase
from idaes.core.util.initialization import propagate_state
from idaes.core.util.model_statistics import degrees_of_freedom
from idaes.models.properties.modular_properties.base.generic_property import GenericParameterBlock
from idaes.models.properties.modular_properties.base.generic_reaction import (
    GenericReactionParameterBlock,
)
from idaes.models.properties.modular_properties.eos.ideal import Ideal
from idaes.models.properties.modular_properties.state_definitions import FTPx
from idaes.models.unit_models import Feed, Product, Translator
from idaes.models.unit_models.pressure_changer import ThermodynamicAssumption
from idaes.models_extra.column_models.condenser import CondenserType, TemperatureSpec
from pyomo.environ import (
    ConcreteModel,
    TransformationFactory,
)
from pyomo.network import Arc

from .schema import (
    DistillationColumnConfig,
    FeedConfig,
    FlowsheetConfig,
    MixerConfig,
    ProductConfig,
    PropertyPackageConfig,
    SplitterConfig,
    TranslatorConfig,
    UnitConfig,
)
from .units import PyomoUnit
from .variable_manager import VariableManager


class IdaesFlowsheetBuilder:
    """Builder class for IDAES flowsheets from configuration objects."""

    def __init__(self, config: FlowsheetConfig):
        """Initialize with a flowsheet configuration."""
        self.config = config
        self.model = ConcreteModel()
        self.property_packages: Dict[str, Any] = {}
        self.reaction_packages: Dict[str, Any] = {}
        self.unit_operations: Dict[str, Any] = {}
        self.material_blocks: Dict[str, Any] = {}

    # Internal helper
    def _require_model(self) -> ConcreteModel:
        if self.model is None:
            raise RuntimeError("Model has not been built. Call build() first.")
        return self.model

    def build(self) -> ConcreteModel:
        """Build the IDAES flowsheet from configuration."""

        # Create the flowsheet block
        self.model.fs = FlowsheetBlock(
            dynamic=self.config.dynamic,
            time_set=self.config.time_set,
            time_units=self.config.time_units,
        )

        # Build all the components
        self._build_property_packages()
        self._build_reaction_packages()
        self._build_material_blocks()
        self._build_unit_operations()
        self._connect_streams()

        # Expand arcs
        TransformationFactory("network.expand_arcs").apply_to(self.model)

        return self.model

    def specify_feed_and_units(self):
        """Specify feed conditions and units for the flowsheet."""
        # Set inputs from stream configs for feed conditions
        # print("\nDegrees of freedom before fixing feed:")
        self._apply_stream_specifications()
        # print("\nDegrees of freedom before fixing unit operations:")
        # self.print_degrees_of_freedom()
        self.variable_manager = VariableManager(
            self
        )  # instantiate the variable manager with reference to flowsheet object
        self._apply_unit_specifications()
        self.print_degrees_of_freedom()

        return self.model

    def _build_property_packages(self):
        """Build the property packages defined in the configuration."""
        for pp_config in self.config.property_packages:
            if isinstance(pp_config.config_dict, Dict):
                # Determine which configuration to use
                if pp_config.config_dict:
                    # Use the user-supplied config_dict directly
                    param_dict = pp_config.config_dict
                else:
                    # Fall back to the legacy method of building the dictionary
                    param_dict = self._build_property_package_dict(pp_config)

                # Create the property package
                setattr(self.model.fs, pp_config.name, GenericParameterBlock(**param_dict))

                # Store reference
                self.property_packages[pp_config.name] = getattr(self.model.fs, pp_config.name)

            else:
                # Option: Already pass an IDAES Property package
                setattr(self.model.fs, pp_config.name, pp_config.config_dict)
                self.property_packages[pp_config.name] = getattr(self.model.fs, pp_config.name)

    def _build_property_package_dict(self, pp_config: PropertyPackageConfig) -> Dict[str, Any]:
        """Build a parameter dictionary for GenericParameterBlock."""
        # TODO: This still needs to be implemented

        raise NotImplementedError(
            "Property package dictionary building is not yet implemented. "
            "This should convert the PropertyPackageConfig into a dict for GenericParameterBlock."
        )
        # Map string names to actual classes
        eos_map = {"Ideal": Ideal}
        state_def_map = {"FTPx": FTPx}

        # Build components dict
        components = {}
        for comp_name in pp_config.components:
            components[comp_name] = {
                "type": Component,
                # Simplified component properties
                "parameter_data": {
                    "mw": pp_config.additional_properties.get(f"{comp_name}_mw", 1.0),
                },
            }

        # Build phases dict
        phases = {}
        for phase_name in pp_config.phases:
            phases[phase_name] = {
                "type": Phase,
                "equation_of_state": eos_map.get(pp_config.equation_of_state, Ideal),
            }

        # Build the parameter dictionary
        param_dict = {
            "components": components,
            "phases": phases,
            "state_definition": state_def_map.get(pp_config.state_definition, FTPx),
            "base_units": pp_config.base_units,
            "pressure_ref": pp_config.pressure_ref,
            "temperature_ref": pp_config.temperature_ref,
        }

        return param_dict

    def _build_reaction_packages(self):
        """Build the reaction packages defined in the configuration."""
        for rxn_config in self.config.reaction_packages:
            # Get the associated property package
            prop_package = self.property_packages.get(rxn_config.property_package)
            if not prop_package:
                raise ValueError(f"Property package {rxn_config.property_package} not found")

            # Set up the reaction arguments based on the configuration
            if rxn_config.config_dict:
                # Use the user-supplied config_dict directly
                reaction_args = rxn_config.config_dict

                # The property_package should always be overridden with our reference
                reaction_args["property_package"] = prop_package

                # Create the reaction package with the config_dict
                setattr(self.model.fs, rxn_config.name, GenericReactionParameterBlock(**reaction_args))
            else:
                # Fall back to the legacy way of constructing the args
                # Base units - use the same as the property package
                base_units = {
                    "time": PyomoUnit.s.unit,
                    "length": PyomoUnit.m.unit,
                    "mass": PyomoUnit.kg.unit,  # Assuming there's a kg unit, might need adjustment
                    "amount": PyomoUnit.mol.unit,
                    "temperature": PyomoUnit.K.unit,
                }

                # Set up the reaction arguments based on the reaction type
                reaction_args = {}

                # Prioritize explicit reaction dictionaries
                if rxn_config.equilibrium_reactions:
                    reaction_args["equilibrium_reactions"] = rxn_config.equilibrium_reactions
                elif rxn_config.rate_reactions:
                    reaction_args["rate_reactions"] = rxn_config.rate_reactions
                # Fall back to the legacy reactions field with type
                elif rxn_config.reactions:
                    if rxn_config.reaction_type == "equilibrium":
                        reaction_args["equilibrium_reactions"] = rxn_config.reactions
                    else:  # rate_based
                        reaction_args["rate_reactions"] = rxn_config.reactions

                # Create the reaction package with the correct arguments
                setattr(
                    self.model,
                    rxn_config.name,
                    GenericReactionParameterBlock(
                        property_package=prop_package, base_units=base_units, **reaction_args
                    ),
                )

            # Store reference
            self.reaction_packages[rxn_config.name] = getattr(self.model.fs, rxn_config.name)

    def _build_material_blocks(self):
        """Build the material blocks defined in the configuration."""
        for material_block in self.config.material_blocks:
            # Get the associated property package
            prop_package = self.property_packages.get(material_block.property_package)
            if not prop_package:
                raise ValueError(f"Property package {material_block.property_package} not found")

            if isinstance(material_block, FeedConfig):
                self._build_feed(material_block, prop_package)
            elif isinstance(material_block, ProductConfig):
                self._build_product(material_block, prop_package)

    def _build_unit_operations(self):
        """Build the unit operations defined in the configuration."""
        for unit_config in self.config.unit_operations:
            # Special handling for Translator which has two property packages
            if isinstance(unit_config, TranslatorConfig):
                self._build_translator(unit_config)
                continue

            # Get the associated property package for standard units
            prop_package = self.property_packages.get(unit_config.property_package)
            if not prop_package:
                raise ValueError(f"Property package {unit_config.property_package} not found")

            # Get unit type directly from the config
            unit_type = unit_config.unit_type

            # Use the generic builder for all unit types
            self._build_unit(unit_config, unit_type, prop_package)

    def _build_feed(self, config: FeedConfig, prop_package):
        """Build a Feed unit operation."""
        setattr(self.model.fs, config.name, Feed(property_package=prop_package))
        self.material_blocks[config.name] = getattr(self.model.fs, config.name)

    def _build_product(self, config: ProductConfig, prop_package):
        """Build a Product unit operation."""
        setattr(self.model.fs, config.name, Product(property_package=prop_package))
        self.material_blocks[config.name] = getattr(self.model.fs, config.name)

    def _build_unit(self, config: UnitConfig, unit_type: Type, prop_package):
        """Build a generic unit operation based on its type."""
        # Extract basic parameters common to all unit operations
        basic_params = {
            "dynamic": config.dynamic,
            "has_holdup": config.has_holdup,
            "property_package": prop_package,
        }

        # Extract initialization parameters
        init_params = self._extract_initialization_params(config, prop_package)

        # Combine parameters
        all_params = {**basic_params, **init_params}

        # Remove any None values to avoid errors with ConfigDict
        all_params = {k: v for k, v in all_params.items() if v is not None}

        # Create the unit operation
        setattr(self.model.fs, config.name, unit_type(**all_params))

        # Initialize the unit operation

        # Get a reference to the created unit
        unit = getattr(self.model.fs, config.name)

        # If mixer, manually add inlet list from config
        # TODO: Currently not the right check.
        if isinstance(config, MixerConfig) and hasattr(config, "inlet_list"):
            unit.inlet_list = config.inlet_list

        # Variables are fixed in a separate step after all units are created
        # Store the unit
        self.unit_operations[config.name] = unit

        return unit

    def _build_translator(self, config: TranslatorConfig):
        """Build a Translator unit with separate inlet and outlet property packages."""
        # Resolve in/out property packages by name
        pp_in = self.property_packages.get(config.property_package_in_name)
        if not pp_in:
            raise ValueError(
                f"Translator '{config.name}': inlet property package '{config.property_package_in_name}' not found"
            )
        pp_out = self.property_packages.get(config.property_package_out_name)
        if not pp_out:
            raise ValueError(
                f"Translator '{config.name}': outlet property package '{config.property_package_out_name}' not found"
            )

        # Instantiate Translator
        setattr(
            self.model.fs,
            config.name,
            Translator(inlet_property_package=pp_in, outlet_property_package=pp_out),
        )
        unit = getattr(self.model.fs, config.name)
        self.unit_operations[config.name] = unit
        return unit

    def _apply_unit_specifications(self):
        """Apply variable specifications after unit creation."""

        # Loop through all unit operations in the model and apply specifications
        for unit_name, unit in self.unit_operations.items():
            unit_config = self._get_unit_config(unit_name)
            if unit_config and isinstance(unit, unit_config.unit_type):
                # Use the variable manager's apply_unit_specifications method
                self.variable_manager.apply_unit_specifications(unit, unit_config)

    def _apply_stream_specifications(self):
        """
        Apply specifications from stream configs to the appropriate feed blocks,
        depending on whether state_definition is FTPx or FpcTP. Raise an error
        if required attributes are not present on the feed block.
        """
        for material in self.config.material_blocks:
            if isinstance(material, FeedConfig):
                feed_unit = self.material_blocks[material.name]
                if material.feed_specification:
                    stream_config = material.feed_specification

                    for t in self.model.fs.time:
                        # Handle FpcTP specification
                        if stream_config.state_definition == "FpcTP":
                            if not hasattr(feed_unit, "flow_mol_phase_comp"):
                                raise AttributeError(
                                    f"Double check consistency of state_definitions for {material.name}"
                                )
                            for phase, comps in stream_config.flow_mol_phase_comp.items():
                                for comp, flow_rate in comps.items():
                                    feed_unit.properties[t].flow_mol_phase_comp[phase, comp].fix(flow_rate.value)

                        # Handle FTPx specification
                        elif stream_config.state_definition == "FTPx":
                            if not (hasattr(feed_unit, "flow_mol") and hasattr(feed_unit, "mole_frac_comp")):
                                raise AttributeError(
                                    f"Double check consistency of state_definitions for {material.name}"
                                )
                            if stream_config.flow_rate is not None:
                                feed_unit.flow_mol.fix(stream_config.flow_rate.value)
                            if stream_config.compositions:
                                for comp, value in stream_config.compositions.items():
                                    feed_unit.mole_frac_comp[t, comp].fix(value.value)

                        else:
                            raise ValueError(
                                f"Unsupported state_definition {stream_config.state_definition} for feed {material.name}"
                            )

                        # Temperature and Pressure (common)
                        if stream_config.temperature is not None:
                            feed_unit.temperature.fix(stream_config.temperature.value)
                        if stream_config.pressure is not None:
                            feed_unit.pressure.fix(stream_config.pressure.value)

    def _extract_initialization_params(self, config: UnitConfig, prop_package):
        """
        Extract initialization parameters from a unit configuration.
        These are parameters required for the unit's constructor.

        Args:
            config: The unit operation configuration
            prop_package: The property package to use

        Returns:
            Dict of keyword arguments for the unit's constructor
        """
        # Start with common parameters all units have
        init_kwargs = {
            "dynamic": config.dynamic,
            "has_holdup": config.has_holdup,
            "property_package": prop_package,
        }

        # Get reaction package if needed
        if hasattr(config, "reaction_package") and config.reaction_package:
            reaction_package = self.reaction_packages.get(config.reaction_package)
            if not reaction_package:
                raise ValueError(f"Reaction package {config.reaction_package} not found for unit {config.name}")
            init_kwargs["reaction_package"] = reaction_package

        # Add special boolean flags that vary by unit type
        for key in dir(config):
            # Only consider boolean parameters that start with "has_"
            if key.startswith("has_") and isinstance(getattr(config, key), bool):
                init_kwargs[key] = getattr(config, key)

        # Special handling for units with custom init parameters
        if hasattr(config, "momentum_mixing_type"):
            init_kwargs["momentum_mixing_type"] = config.momentum_mixing_type

        # Handle inlet_list for Mixer units
        if hasattr(config, "inlet_list") and config.inlet_list:
            init_kwargs["inlet_list"] = config.inlet_list

        # Handle outlet_list for Splitter units
        if isinstance(config, SplitterConfig):
            # If outlet_list not provided, default to outlet_streams for 1:1 mapping
            outlet_list = config.outlet_list if config.outlet_list else config.outlet_streams
            if outlet_list:
                init_kwargs["outlet_list"] = outlet_list

        # Handle side options for heat exchangers
        if hasattr(config, "hot_side_name"):
            init_kwargs["hot_side_name"] = config.hot_side_name
            init_kwargs["cold_side_name"] = config.cold_side_name

        # Pressure changers handling
        if hasattr(config, "thermodynamic_assumption"):
            if config.thermodynamic_assumption == "isothermal":
                init_kwargs["thermodynamic_assumption"] = ThermodynamicAssumption.isothermal
            elif config.thermodynamic_assumption == "isentropic":
                init_kwargs["thermodynamic_assumption"] = ThermodynamicAssumption.isentropic

        if hasattr(config, "compressor"):
            init_kwargs["compressor"] = config.compressor

        if hasattr(config, "has_phase_equilibrium"):
            init_kwargs["has_phase_equilibrium"] = config.has_phase_equilibrium

        # Handle distillation column parameters
        if isinstance(config, DistillationColumnConfig):
            # Number of trays
            if hasattr(config, "number_of_trays"):
                init_kwargs["number_of_trays"] = config.number_of_trays

            # Feed tray location
            if hasattr(config, "feed_tray_location"):
                init_kwargs["feed_tray_location"] = config.feed_tray_location

            # Condenser type
            if hasattr(config, "condenser_type"):
                if config.condenser_type == "total":
                    init_kwargs["condenser_type"] = CondenserType.totalCondenser
                elif config.condenser_type == "partial":
                    init_kwargs["condenser_type"] = CondenserType.partialCondenser

            if hasattr(config, "condenser_temperature_spec"):
                if config.condenser_temperature_spec == "atBubblePoint":
                    init_kwargs["condenser_temperature_spec"] = TemperatureSpec.atBubblePoint
                elif config.condenser_temperature_spec == "atCustomTemperature":
                    init_kwargs["condenser_temperature_spec"] = TemperatureSpec.customTemperature

        # Handle inert species for Gibbs reactor
        if hasattr(config, "inert_species"):
            init_kwargs["inert_species"] = config.inert_species

        return init_kwargs

    def _connect_streams(self):
        """Connect unit operations with arcs based on inlet/outlet stream specifications."""
        # First go through feed and product blocks
        for material_block in self.config.material_blocks:
            # Go through unit operations and build arcs
            # Feed first
            if isinstance(material_block, FeedConfig):
                feed_unit = self.material_blocks[material_block.name]
                for unit_config in self.config.unit_operations:
                    if material_block.outlet_stream in unit_config.inlet_streams:
                        # Build an arc from the feed to the unit operation
                        destination_name = unit_config.name
                        destination_unit = self.unit_operations[destination_name]
                        self._build_arc(feed_unit, destination_unit, source_unit_config=material_block)
            # Don't do products actively, they are sinks and should be connected automatically from the previous units
            elif isinstance(material_block, ProductConfig):
                """product_unit = self.material_blocks[material_block.name]
                for unit_config in self.config.unit_operations:
                    if material_block.inlet_stream in unit_config.outlet_streams:
                        # Build an arc from the product to the unit operation
                        source_name = unit_config.name
                        source_unit = self.unit_operations[source_name]
                        self._build_arc(source_unit, product_unit)"""
                # This is handled automatically in the unit connection loop below
                pass

        for unit_config in self.config.unit_operations:
            # Connect outlet streams
            source_unit = self.unit_operations[unit_config.name]

            for outlet_stream in unit_config.outlet_streams:
                # Find the unit operation that has this as an inlet
                destination_unit = None
                destination_name = None

                for other_unit_config in self.config.unit_operations:
                    if outlet_stream in other_unit_config.inlet_streams:
                        destination_name = other_unit_config.name
                        destination_unit = self.unit_operations[destination_name]
                        destination_unit_config = other_unit_config
                        break

                if destination_unit is None:
                    # If no destination found, check if it's a product block
                    for material_block in self.config.material_blocks:
                        if isinstance(material_block, ProductConfig) and outlet_stream == material_block.inlet_stream:
                            destination_name = material_block.name
                            destination_unit = self.material_blocks[destination_name]
                            destination_unit_config = material_block
                            break

                if destination_unit:
                    # Create an arc connecting the two units
                    self._build_arc(
                        source_unit,
                        destination_unit,
                        source_unit_config=unit_config,
                        destination_unit_config=destination_unit_config,
                    )
            print(unit_config.name, destination_name)

    def _build_arc(self, source, destination, source_unit_config, destination_unit_config=None):
        """Build an arc between two unit operations.

        Parameters:
            source: The source unit operation (model object)
            destination: The destination unit operation (model object)
        """
        # Get the base unit class name, removing any _Scalar prefix
        dest_unit_type = destination.__class__.__name__
        if dest_unit_type.startswith("_Scalar"):
            dest_unit_type = dest_unit_type[7:]  # Remove '_Scalar' prefix

        source_unit_type = source.__class__.__name__
        if source_unit_type.startswith("_Scalar"):
            source_unit_type = source_unit_type[7:]  # Remove '_Scalar' prefix

        # Create a unique name for the arc
        source_name = source.name.split(".")[-1]
        dest_name = destination.name.split(".")[-1]
        arc_name = f"arc_{source_name}_{dest_name}"
        # --- Determine destination inlet port (defer arc creation until end) ---
        if dest_unit_type == "Mixer":
            if not hasattr(destination, "inlet_list"):
                raise ValueError(
                    f"Mixer {destination.name} does not have an inlet_list. Please specify in the configuration."
                )
            destination_inlet_port = None
            for inlet_name in destination.inlet_list:
                candidate = getattr(destination, inlet_name, None)
                if candidate is None:
                    continue
                # Check if already used as an Arc destination
                used = False
                for arc in self.model.fs.component_objects(Arc, descend_into=False):
                    if arc.destination is candidate:
                        used = True
                        break
                if not used:
                    destination_inlet_port = candidate
                    break
            if destination_inlet_port is None:
                raise ValueError(
                    f"All mixer inlet ports on {destination.name} are already connected. Provide more inlets or adjust config."
                )
        elif dest_unit_type == "TrayColumn":
            destination_inlet_port = destination.feed_tray.feed
        else:
            # Generic single-inlet unit
            destination_inlet_port = destination.inlet

        # --- Determine source outlet port ---
        outlet_port = None
        if source_unit_type == "Flash":
            # Identify which outlet stream feeds the destination
            destination_inlet_stream = None
            if destination_unit_config and isinstance(destination_unit_config, UnitConfig):
                for inlet_stream in destination_unit_config.inlet_streams:
                    if inlet_stream in source_unit_config.outlet_streams:
                        destination_inlet_stream = inlet_stream
                        break
            elif destination_unit_config and isinstance(destination_unit_config, ProductConfig):
                if destination_unit_config.inlet_stream in source_unit_config.outlet_streams:
                    destination_inlet_stream = destination_unit_config.inlet_stream

            if (
                hasattr(source_unit_config, "vapor_outlet_stream")
                and source_unit_config.vapor_outlet_stream == destination_inlet_stream
            ):
                outlet_port = source.vap_outlet
                print(f"Connecting Flash vapor outlet to {destination.name}")
            elif (
                hasattr(source_unit_config, "liquid_outlet_stream")
                and source_unit_config.liquid_outlet_stream == destination_inlet_stream
            ):
                outlet_port = source.liq_outlet
                print(f"Connecting Flash liquid outlet to {destination.name}")
            else:
                raise ValueError(
                    f"Could not determine which Flash outlet to connect from {source.name} to {destination.name}"
                )

        elif source_unit_type == "DistillationColumn":
            destination_inlet_stream = None
            if destination_unit_config and isinstance(destination_unit_config, UnitConfig):
                for inlet_stream in destination_unit_config.inlet_streams:
                    if inlet_stream in source_unit_config.outlet_streams:
                        destination_inlet_stream = inlet_stream
                        break
            elif destination_unit_config and isinstance(destination_unit_config, ProductConfig):
                if destination_unit_config.inlet_stream in source_unit_config.outlet_streams:
                    destination_inlet_stream = destination_unit_config.inlet_stream

            if (
                hasattr(source_unit_config, "vapor_outlet_stream")
                and source_unit_config.vapor_outlet_stream == destination_inlet_stream
            ):
                outlet_port = source.condenser.distillate
                print(f"Connecting Distillation column distillate outlet to {destination.name}")
            elif (
                hasattr(source_unit_config, "liquid_outlet_stream")
                and source_unit_config.liquid_outlet_stream == destination_inlet_stream
            ):
                outlet_port = source.reboiler.bottoms
                print(f"Connecting Distillation column bottoms outlet to {destination.name}")
            else:
                raise ValueError(
                    f"Could not determine which DistillationColumn outlet to connect from {source.name} to {destination.name}"
                )

        elif source_unit_type == "Separator":
            destination_inlet_stream = None
            if destination_unit_config and isinstance(destination_unit_config, UnitConfig):
                for inlet_stream in destination_unit_config.inlet_streams:
                    if inlet_stream in source_unit_config.outlet_streams:
                        destination_inlet_stream = inlet_stream
                        break
            elif destination_unit_config and isinstance(destination_unit_config, ProductConfig):
                if destination_unit_config.inlet_stream in source_unit_config.outlet_streams:
                    destination_inlet_stream = destination_unit_config.inlet_stream
            if destination_inlet_stream is None:
                raise ValueError(f"Could not determine which Separator outlet connects to {destination.name}")
            outlet_port = getattr(source, destination_inlet_stream, None)
            if outlet_port is None:
                raise ValueError(f"Could not find outlet port '{destination_inlet_stream}' on {source.name}")

        elif source_unit_type == "TrayColumn":
            if hasattr(source, "reboiler"):
                outlet_port = source.reboiler.bottoms
            elif hasattr(source, "condenser"):
                outlet_port = source.condenser.distillate
            else:
                raise ValueError(
                    f"TrayColumn {source.name} missing both reboiler and condenser references for outlet resolution"
                )
        else:
            # Generic single-outlet unit
            outlet_port = source.outlet

        # --- Create Arc ---
        setattr(self.model.fs, arc_name, Arc(source=outlet_port, destination=destination_inlet_port))

    def print_degrees_of_freedom(self):
        for block in self.model.fs.block_data_objects(active=True):
            dof = degrees_of_freedom(block)
            print(f"{block.name} DOF: {dof}")

    def print_degrees_of_freedom_deprecated(self):
        """
        Print detailed degrees of freedom for the model and each block.

        For standard process units, calculates:
        Effective DOF = (DOF internal) - (DOF feeds)

        For feed units and product units (sinks), only shows the raw DOF
        without any feed DOF subtraction.

        This gives a more accurate view of how many variables need to be fixed for each unit or per feed stream.
        TODO: this is currently only correct for FcpTP state definition.
        TODO: depending on when this is called it should result to 0 degrees of freedom -> assert statements
        """
        if not self.model:
            print("Model has not been built yet")
            return

        print(f"Overall model DOF: {degrees_of_freedom(self.model)}")

        # First, identify all units and their types
        unit_types = {}
        for unit_name, unit in self.unit_operations.items():
            # Determine the unit type based on the class name
            unit_class = unit.__class__.__name__
            unit_types[unit_name] = unit_class

        # Add material blocks (feeds and products)
        for block_name, block in self.material_blocks.items():
            block_class = block.__class__.__name__
            unit_types[block_name] = block_class

        # Build a graph of connections to identify feed streams for each unit
        connections = {}
        for arc in self.model.fs.component_objects(Arc, descend_into=False):
            try:
                source_name = arc.source.parent_block().name.split(".")[-1]
                dest_name = arc.destination.parent_block().name.split(".")[-1]

                # Initialize if needed
                if dest_name not in connections:
                    connections[dest_name] = []

                # Add the source as a feed to the destination
                connections[dest_name].append(source_name)
            except Exception as e:
                print(f"Warning: Could not determine connection for arc: {e}")

        # Get the number of phases and components from property packages
        phase_comp_counts = {}
        for pp_name, pp in self.property_packages.items():
            try:
                # Get component list
                components = list(pp.component_list)
                # Get phase list
                phases = list(pp.phase_list)

                phase_comp_counts[pp_name] = {
                    "components": components,
                    "phases": phases,
                    "num_components": len(components),
                    "num_phases": len(phases),
                }

                print(f"\nProperty package '{pp_name}':")
                print(f"  Components ({len(components)}): {', '.join(str(c) for c in components)}")
                print(f"  Phases ({len(phases)}): {', '.join(str(p) for p in phases)}")
            except Exception as e:
                print(f"Warning: Could not determine components/phases for property package {pp_name}: {e}")
                phase_comp_counts[pp_name] = {
                    "components": [],
                    "phases": [],
                    "num_components": 0,
                    "num_phases": 0,
                }

        # Determine the property package used by each unit
        unit_property_packages = {}
        for unit_config in self.config.unit_operations:
            unit_property_packages[unit_config.name] = unit_config.property_package

        for material_config in self.config.material_blocks:
            unit_property_packages[material_config.name] = material_config.property_package

        # Identify feed and product units (to handle DOF differently)
        product_units = set()
        feed_units = set()
        for material_block in self.config.material_blocks:
            if isinstance(material_block, ProductConfig):
                product_units.add(material_block.name)
            elif isinstance(material_block, FeedConfig):
                feed_units.add(material_block.name)

        # Print detailed DOF for each unit
        print("\nUnit degrees of freedom (DOF internal - DOF feeds):")
        print("--------------------------------------------------")
        print(f"{'Unit':<15} {'Type':<15} {'Raw DOF':<10} {'Feed DOFs':<10} {'Effective DOF':<15}")
        print("-" * 65)

        for name in sorted(unit_types.keys()):
            unit = getattr(self.model.fs, name)
            unit_type = unit_types[name]
            raw_dof = degrees_of_freedom(unit)

            # Calculate feed DOFs based on unit type, number of feeds, phases, and components
            feed_dof = 0

            # For product units or feed units, we don't show/calculate feed DOFs
            if name not in product_units and name not in feed_units and name in connections:
                num_feeds = len(connections[name])

                # Get the property package for this unit
                pp_name = unit_property_packages.get(name, "")
                pp_info = phase_comp_counts.get(pp_name, {"num_components": 0, "num_phases": 0})

                num_components = pp_info["num_components"]
                num_phases = pp_info["num_phases"]

                # For a single feed, DOF = F + T + P + (num_components - 1) mole fractions per phase
                # F: 1 total flow rate or num_phases*num_components individual flows
                # T: 1 temperature
                # P: 1 pressure
                # For multiple feeds (e.g., mixer), multiply by number of feeds
                base_specs = 2  # T and P are always needed once per feed

                # If we're specifying component-phase flows directly:
                # We need (num_phases * num_components) flow specifications per feed
                # If we're using mole fractions:
                # We need 1 total flow rate + (num_components - 1) mole fractions per phase
                # This is because mole fractions sum to 1, so only n-1 are independent
                component_specs = min(num_phases * num_components, 1 + num_phases * (num_components - 1))

                # Total specs per feed
                specs_per_feed = base_specs + component_specs

                feed_dof = specs_per_feed * num_feeds

                # Calculate effective DOF
                effective_dof = raw_dof - feed_dof

                print(f"{name:<15} {unit_type:<15} {raw_dof:<10} {feed_dof:<10} {effective_dof:<15}")

            elif name in feed_units:
                # For feed units, we only show the raw DOF
                feed_dof = degrees_of_freedom(unit)
                effective_dof = feed_dof  # No feed DOF subtraction for feeds
                print(f"{name:<15} {unit_type:<15} {raw_dof:<10} {feed_dof:<10} {effective_dof:<15}")

        # Print additional notes about DOF calculation
        print("\nNotes:")
        print("- Feed DOFs calculation depends on the number of phases and components in each feed")
        print("- For feed and product units, only the raw DOF is shown (no feed DOF subtraction)")
        print("- For process units, effective DOF = raw DOF - feed DOFs")
        print("- For each feed stream, specifications include: temperature, pressure, and")
        print("  either component-phase flow rates or total flow rate + mole fractions")
        print("- Mixers with multiple feeds have correspondingly higher feed DOFs")
        print("- Effective DOF represents the remaining variables to be specified for each unit")

    def initialize_flowsheet(self, solver=None, outlvl="info"):
        """
        Initialize the flowsheet in the correct sequence, starting from feed units.
        TODO: Use solver for initialization if provided?

        Args:
            solver: Solver to use for initialization (default: None, which uses idaes default)
            outlvl: Output level for initialization (info or debug)

        Returns:
            model: The initialized flowsheet model
        """

        print("Starting flowsheet initialization sequence...")
        if outlvl == "debug":
            outlvl = idaeslog.DEBUG
        elif outlvl == "info":
            outlvl = idaeslog.INFO
        else:
            outlvl = idaeslog.INFO

        # Step 1: Build initialization sequence
        init_sequence = self._build_initialization_sequence()
        print(f"Initialization sequence: {' → '.join([unit.name for unit in init_sequence])}")

        # Track already initialized units
        initialized_units = set()

        # Step 2: Initialize each unit in sequence and propagate state
        for i, unit in enumerate(init_sequence):
            print(f"\nInitializing unit {i + 1}/{len(init_sequence)}: {unit.name}")

            # Check DOF before initialization
            unit_dof = degrees_of_freedom(unit)
            print(f"Degrees of freedom before initialization: {unit_dof}")

            # Is this a feed unit? Feeds are always initialized directly
            is_feed = False
            for material_block in self.config.material_blocks:
                if isinstance(material_block, FeedConfig) and self.material_blocks[material_block.name] == unit:
                    is_feed = True
                    break

            # Initialize based on unit type and connections
            if is_feed or i == 0:
                print(f"Direct initialization of {'feed' if is_feed else 'first'} unit {unit.name}")
                unit.initialize(outlvl=outlvl)
                initialized_units.add(unit.name)

                # Propagate state from feed to downstream units
                for arc in self.model.fs.component_objects(Arc, descend_into=False):
                    if arc.source.parent_block() == unit:
                        dest_unit = arc.destination.parent_block()
                        print(f"Propagating state from {unit.name} to {dest_unit.name}")
                        propagate_state(arc=arc)
            else:
                # Find all inlet arcs to this unit
                inlet_arcs = []
                for arc in self.model.fs.component_objects(Arc, descend_into=False):
                    if arc.destination.parent_block() == unit:
                        inlet_arcs.append(arc)

                # Check if all upstream units are already initialized
                all_upstream_initialized = True
                for arc in inlet_arcs:
                    source_unit = arc.source.parent_block()
                    if source_unit.name not in initialized_units:
                        all_upstream_initialized = False
                        print(f"Warning: Upstream unit {source_unit.name} not yet initialized for {unit.name}")

                if inlet_arcs and all_upstream_initialized:
                    print(f"Initializing {unit.name} with state propagated from upstream units")

                    # Initialize with state already propagated from upstream
                    unit.initialize(outlvl=outlvl)
                    initialized_units.add(unit.name)

                    # Propagate state to downstream units
                    outlet_arcs = []
                    for arc in self.model.fs.component_objects(Arc, descend_into=False):
                        if arc.source.parent_block() == unit:
                            outlet_arcs.append(arc)

                    for outlet_arc in outlet_arcs:
                        dest_unit = outlet_arc.destination.parent_block()
                        print(f"Propagating state from {unit.name} to {dest_unit.name}")
                        propagate_state(arc=outlet_arc)

                else:
                    print(f"No inlet arcs found for {unit.name} or upstream not initialized, initializing directly")
                    try:
                        unit.initialize(outlvl=outlvl)
                        initialized_units.add(unit.name)
                    except Exception as e:
                        print(f"Error initializing {unit.name}: {str(e)}")

            # Check DOF after initialization
            unit_dof = degrees_of_freedom(unit)
            print(f"Degrees of freedom after initialization: {unit_dof}")

        print("\nFlowsheet initialization complete!")
        return self.model

    def _build_initialization_sequence(self):
        """
        TODO: Not robust enough yet, needs more testing.
        Build a sequence of units for initialization:
        1. All feed units first
        2. Process units in DFS order from feeds (following the process flow)
        3. Products at the end

        Returns:
            List of unit operations in initialization order
        """
        print("Building initialization sequence...")

        # Get all model units
        all_units = {}
        for unit_name, unit in self.material_blocks.items():
            all_units[unit.name] = unit
        for unit_name, unit in self.unit_operations.items():
            all_units[unit.name] = unit

        # Build a directed graph of the connections
        # Key: unit name, Value: list of unit names it connects to
        graph = {}
        for unit_name, unit in all_units.items():
            graph[unit_name] = []

        # Fill the graph with arc connections
        for arc in self.model.fs.component_objects(Arc, descend_into=False):
            try:
                source_name = arc.source.parent_block().name
                dest_name = arc.destination.parent_block().name
                if source_name in graph:
                    graph[source_name].append(dest_name)
                    print(f"Connection: {source_name} -> {dest_name}")
            except Exception as e:
                print(f"Warning: Could not determine connection for arc: {e}")

        # Step 1: Identify feed units, product units, and process units
        feed_units = []
        feed_unit_objects = []
        product_units = []
        product_unit_objects = []

        for material_block in self.config.material_blocks:
            if isinstance(material_block, FeedConfig):
                feed_unit = self.material_blocks[material_block.name]
                feed_units.append(feed_unit.name)
                feed_unit_objects.append(feed_unit)
            elif isinstance(material_block, ProductConfig):
                product_unit = self.material_blocks[material_block.name]
                product_units.append(product_unit.name)
                product_unit_objects.append(product_unit)

        if not feed_units:
            print("Warning: No feed units found in the flowsheet")
            # Return all units in any order if no feeds are found
            return list(all_units.values())

        # Step 2: Start building the sequence with all feed units
        sequence = []
        sequence.extend(feed_unit_objects)

        # Step 3: DFS from feed units through process units
        visited = set(feed_units + product_units)  # Mark feeds and products as visited
        process_units = []

        def dfs_traverse(unit_name):
            """DFS traversal of process units."""
            if unit_name in visited:
                return

            visited.add(unit_name)
            unit = all_units.get(unit_name)

            # Only add to sequence if it's not a product (products come last)
            if unit and unit_name not in product_units:
                process_units.append(unit)
                print(f"Added process unit to sequence: {unit_name}")

            # Visit all downstream units
            for next_unit in graph.get(unit_name, []):
                dfs_traverse(next_unit)

        # Start DFS from each feed unit
        for feed_unit in feed_units:
            # Visit direct downstream units from feeds
            for next_unit in graph.get(feed_unit, []):
                dfs_traverse(next_unit)

        # Add process units after feeds
        sequence.extend(process_units)

        # Step 4: Add product units at the end
        sequence.extend(product_unit_objects)

        # Check if all units were visited
        all_in_sequence = set(unit.name for unit in sequence)
        if len(all_in_sequence) < len(all_units):
            print("Warning: Not all units were reached in the initialization sequence")
            # Add any unvisited units at the end (except products which are already there)
            for unit_name, unit in all_units.items():
                if unit_name not in all_in_sequence and unit_name not in product_units:
                    sequence.append(unit)
                    print(f"Added unconnected unit to sequence: {unit_name}")

        print(f"Final initialization sequence: {' → '.join([unit.name for unit in sequence])}")
        return sequence

    def _get_unit_config(self, unit_name: str) -> UnitConfig:
        """Get the configuration for a specific unit operation."""
        for config in self.config.unit_operations:
            if config.name == unit_name:
                return config
        raise ValueError(f"Unit configuration for {unit_name} not found")

    def _validate_feed_specification_against_state_definition(self, feed_unit, material_config, prop_package):
        """
        Validates that the feed specification variables match the required state variables
        for the chosen property package's state definition.

        Args:
            feed_unit: The IDAES feed unit
            material_config: The MaterialBlockConfig for this feed
            prop_package: The property package object

        Returns:
            Boolean indicating if validation passed and a message with any issues
        """
        # Get the state definition from the property package
        if not hasattr(prop_package, "config") or not hasattr(prop_package.config, "state_definition"):
            return True, "Could not determine state definition"

        state_definition = prop_package.config.state_definition.__name__

        # Check if the right variables are fixed based on the state definition
        if state_definition == "FTPx":
            # Needs temperature, pressure, and either flow_mol+mole_frac or flow_mol_phase_comp
            required_vars = ["temperature", "pressure"]
            required_flow = (
                ["flow_mol", "mole_frac_comp"]
                if not material_config.fix_flow_mol_phase_comp
                else ["flow_mol_phase_comp"]
            )

            missing = []
            if not material_config.fix_temperature:
                missing.append("temperature")
            if not material_config.fix_pressure:
                missing.append("pressure")

            if not material_config.fix_flow_mol_phase_comp:
                if not material_config.fix_flow_rate:
                    missing.append("flow_mol")
                if not material_config.fix_compositions:
                    missing.append("mole_frac_comp")

            if missing:
                return (
                    False,
                    f"State definition {state_definition} requires fixing: {', '.join(required_vars)} and {' + '.join(required_flow)}. Missing: {', '.join(missing)}",
                )

        elif state_definition == "FPhx":
            # Needs pressure, enthalpy, and either flow_mol+mole_frac or flow_mol_phase_comp
            required_vars = ["pressure", "enthalpy"]
            required_flow = (
                ["flow_mol", "mole_frac_comp"]
                if not material_config.fix_flow_mol_phase_comp
                else ["flow_mol_phase_comp"]
            )

            missing = []
            if not material_config.fix_pressure:
                missing.append("pressure")

            # Enthalpy isn't directly in MaterialBlockConfig, would need to be added
            missing.append("enthalpy")

            if not material_config.fix_flow_mol_phase_comp:
                if not material_config.fix_flow_rate:
                    missing.append("flow_mol")
                if not material_config.fix_compositions:
                    missing.append("mole_frac_comp")

            if missing:
                return (
                    False,
                    f"State definition {state_definition} requires fixing: {', '.join(required_vars)} and {' + '.join(required_flow)}. Missing: {', '.join(missing)}",
                )

        elif state_definition == "FpcTP":
            # Needs temperature, pressure, and component flows
            required_vars = ["temperature", "pressure", "flow_mol_phase_comp"]

            missing = []
            if not material_config.fix_temperature:
                missing.append("temperature")
            if not material_config.fix_pressure:
                missing.append("pressure")
            if not material_config.fix_flow_mol_phase_comp:
                missing.append("flow_mol_phase_comp")

            if missing:
                return (
                    False,
                    f"State definition {state_definition} requires fixing: {', '.join(required_vars)}. Missing: {', '.join(missing)}",
                )

        # If we get here, the validation passed
        return True, f"Feed specification matches state definition {state_definition}"
