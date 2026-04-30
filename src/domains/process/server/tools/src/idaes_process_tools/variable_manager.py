"""
IDAES Variable Manager
----------------------
This module provides a class for managing variables and constraints on IDAES unit operations.
It allows checking if a variable exists, and if not, creating it along with its associated constraint.
It also provides unit-specific handlers for applying specifications from configuration objects.
"""

from typing import Any, Callable, Dict, Optional, Tuple, Union

from pyomo.environ import Constraint, Var

# Import unit models for type checking
from .schema import (
    CompressorConfig,
    CSTRConfig,
    DistillationColumnConfig,
    FlashConfig,
    GibbsReactorConfig,
    HeaterConfig,
    HeatExchangerConfig,
    MixerConfig,
    PumpConfig,
    SplitterConfig,
    StoichiometricReactorConfig,
    TranslatorConfig,
    TurbineConfig,
    UnitConfig,
)
from .units import PyomoUnit, Quantity

# Type alias for constraint generation functions
ConstraintGenerator = Callable[[Any, Var, Dict[str, Any]], Constraint]


class VariableDefinition:
    """Helper class for definition of a variable that can be created if it doesn't exist."""

    def __init__(
        self,
        name: str,
        initialize: float,
        bounds: Tuple[Optional[float], Optional[float]],
        units: Optional[PyomoUnit] = None,
        constraint_generator: Optional[ConstraintGenerator] = None,
        constraint_kwargs: Optional[Dict[str, Any]] = None,
    ):
        """Initialize a variable definition.

        Args:
            name: The name of the variable
            initialize: Initial value for the variable
            bounds: Bounds for the variable as a tuple (lower, upper)
            units: Optional units for the variable
            constraint_generator: Optional function that generates a constraint for this variable
            constraint_kwargs: Optional kwargs to pass to the constraint generator
        """
        self.name = name
        self.initialize = initialize
        self.bounds = bounds
        self.units = units
        self.constraint_generator = constraint_generator
        self.constraint_kwargs = constraint_kwargs or {}

    def create_variable(self, unit) -> Var:
        """Create the variable on the given unit."""
        var_args = {"initialize": self.initialize, "bounds": self.bounds}

        if self.units is not None:
            var_args["units"] = self.units

        var = Var(**var_args)
        setattr(unit, self.name, var)
        return var

    def create_constraint(self, unit, var) -> Optional[Constraint]:
        """Create the constraint for this variable if a generator is provided."""
        if self.constraint_generator is None:
            return None

        constraint = self.constraint_generator(unit, var, self.constraint_kwargs)
        constraint_name = f"{self.name}_constraint"
        setattr(unit, constraint_name, constraint)
        return constraint


class VariableManager:
    """
    Manager for IDAES variables and constraints.
    Provides methods for creating, checking, and managing variables and constraints
    on IDAES unit operations.
    """

    def __init__(self, flowsheet=None):
        """
        Initialize the variable manager.

        Args:
            flowsheet: The IDAES flowsheet object to manage
        """
        self.flowsheet = flowsheet
        self.unit_operations = {}

        # If a flowsheet is provided, gather all unit operations
        if flowsheet is not None:
            self._gather_unit_operations()

    def _gather_unit_operations(self):
        """Gather all unit operations from the flowsheet."""
        # This method collects all unit operations from the flowsheet
        # for easier access
        if not self.flowsheet:
            return

        # Get unit operations from the flowsheet's builder
        if hasattr(self.flowsheet, "unit_operations"):
            self.unit_operations.update(self.flowsheet.unit_operations)

        # Get material blocks from the flowsheet's builder
        if hasattr(self.flowsheet, "material_blocks"):
            self.unit_operations.update(self.flowsheet.material_blocks)

    def set_flowsheet(self, flowsheet):
        """
        Set the flowsheet to manage.

        Args:
            flowsheet: The IDAES flowsheet object
        """
        self.flowsheet = flowsheet
        self._gather_unit_operations()

    def apply_unit_specifications(self, unit: Any, config: UnitConfig):
        """
        Apply specifications to a unit based on its type and configuration.
        This is a dispatch function that calls the appropriate handler.

        Args:
            unit: The IDAES unit operation
            config: The configuration object for the unit
        """

        # Dispatch to the appropriate handler based on unit config type
        if isinstance(config, CSTRConfig):
            self.apply_cstr_specifications(unit, config)
        elif isinstance(config, StoichiometricReactorConfig):
            self.apply_stoichiometric_reactor_specifications(unit, config)
        elif isinstance(config, GibbsReactorConfig):
            self.apply_gibbs_reactor_specifications(unit, config)
        elif isinstance(config, HeaterConfig):
            self.apply_heater_specifications(unit, config)
        elif isinstance(config, HeatExchangerConfig):
            self.apply_heat_exchanger_specifications(unit, config)
        elif isinstance(config, FlashConfig):
            self.apply_flash_specifications(unit, config)
        elif isinstance(config, DistillationColumnConfig):
            self.apply_distillation_column_specifications(unit, config)
        elif (
            isinstance(config, PumpConfig) or isinstance(config, CompressorConfig) or isinstance(config, TurbineConfig)
        ):
            self.apply_pressure_changer_specifications(unit, config)
        elif isinstance(config, MixerConfig):
            self.apply_mixer_specifications(unit, config)
        elif isinstance(config, SplitterConfig):
            self.apply_splitter_specifications(unit, config)
        elif isinstance(config, TranslatorConfig):
            self.apply_translator_specifications(unit, config)
        else:
            # Default handler for other unit types
            self.apply_basic_specifications(unit, config)

    def apply_all_specifications(self, unit_configs):
        """
        Apply specifications to all units in the flowsheet.

        Args:
            unit_configs: Dictionary of {unit_name: config} pairs
        """
        for unit_name, config in unit_configs.items():
            if unit_name in self.unit_operations:
                unit = self.unit_operations[unit_name]
                self.apply_unit_specifications(unit, config)
            else:
                print(f"Warning: Unit {unit_name} not found in flowsheet")

    def apply_basic_specifications(self, unit, config):
        """
        Apply basic specifications common to many unit types.

        Args:
            unit: The IDAES unit operation
            config: The configuration object for the unit
        """
        # Handle volume for units that have it
        if hasattr(config, "volume") and config.volume is not None and hasattr(unit, "volume"):
            unit.volume.fix(config.volume.value)

        # Handle outlet pressure for units that have it
        if hasattr(config, "outlet_pressure") and config.outlet_pressure is not None and hasattr(unit, "outlet"):
            unit.outlet.pressure.fix(config.outlet_pressure.value)

        # Handle heat duty for units that have it
        if hasattr(config, "heat") and config.heat is not None and hasattr(unit, "heat"):
            unit.heat.fix(config.heat.value)

        # Handle outlet temperature for units that have it
        if hasattr(config, "outlet_temperature") and config.outlet_temperature is not None and hasattr(unit, "outlet"):
            unit.outlet.temperature.fix(config.outlet_temperature.value)

    def apply_cstr_specifications(self, unit, config):
        """
        Apply specifications for CSTR units.
        So far only volume should be fixed with reaction package defined.

        Args:
            unit: The IDAES CSTR unit
            config: The configuration object for the unit
        """
        # First apply basic specifications
        self.apply_basic_specifications(unit, config)

        # Handle conversion if specified
        if hasattr(config, "conversion") and config.conversion is not None:
            # Create or access the conversion variable and set the constraint
            limiting_reactant = getattr(config, "limiting_reactant", None)
            if limiting_reactant is None:
                print("Warning: No limiting_reactant specified for conversion. Using first component.")
                # Try to find a component to use
                if hasattr(unit.config.property_package, "component_list"):
                    components = list(unit.config.property_package.component_list)
                    if components:
                        limiting_reactant = components[0]

            if limiting_reactant:
                var = self.create_and_fix_variable(
                    unit,
                    "conversion",
                    config.conversion,
                )
                # Create the conversion constraint
                constraint = self.create_conversion_constraint(unit, var, limiting_reactant=limiting_reactant)
                setattr(unit, "conversion_constraint", constraint)
            else:
                print("Error: Cannot create conversion constraint without limiting_reactant")

        # Handle heat specifications
        if unit.config.has_heat_transfer:
            if hasattr(config, "heat_duty") and config.heat_duty is not None:
                if hasattr(unit, "heat"):
                    unit.heat.fix(config.heat_duty.value)
                else:
                    print(f"Warning: Unit {unit.name} has has_heat_transfer=True but no heat variable")

            if hasattr(config, "outlet_temperature") and config.outlet_temperature is not None:
                if hasattr(unit, "outlet") and hasattr(unit.outlet, "temperature"):
                    unit.outlet.temperature.fix(config.outlet_temperature.value)
                else:
                    print(f"Warning: Unit {unit.name} has no outlet.temperature variable")

        # Handle pressure specification
        if unit.config.has_pressure_change:
            if hasattr(config, "deltaP") and config.deltaP is not None:
                if hasattr(unit, "pressure_change"):
                    unit.pressure_change.fix(config.deltaP.value)
                else:
                    print(f"Warning: Unit {unit.name} has has_pressure_change=True but no pressure_change variable")
            if hasattr(config, "outlet_pressure") and config.outlet_pressure is not None:
                if hasattr(unit, "outlet") and hasattr(unit.outlet, "pressure"):
                    unit.outlet.pressure.fix(config.outlet_pressure.value)
                else:
                    print(f"Warning: Unit {unit.name} has no outlet.pressure variable")

    def apply_stoichiometric_reactor_specifications(self, unit, config):
        """
        Apply specifications for StoichiometricReactor units.

        Args:
            unit: The IDAES StoichiometricReactor unit
            config: The configuration object for the unit
        """
        # First apply basic specifications
        self.apply_basic_specifications(unit, config)

        # Handle reaction yield if specified
        if hasattr(config, "reaction_yield") and config.reaction_yield is not None:
            raise NotImplementedError(
                "Reaction yield handling is not implemented yet. Please use the conversion parameter."
            )

        # Handle conversion if specified
        if hasattr(config, "conversion") and config.conversion is not None:
            # Create or access the conversion variable and set the constraint
            limiting_reactant = getattr(config, "limiting_reactant", None)
            if limiting_reactant is None:
                print("Warning: No limiting_reactant specified for conversion. Using first component.")
                # Try to find a component to use
                if hasattr(unit.config.property_package, "component_list"):
                    components = list(unit.config.property_package.component_list)
                    if components:
                        limiting_reactant = components[0]

            if limiting_reactant:
                var = self.create_and_fix_variable(
                    unit,
                    "conversion",
                    config.conversion,
                )
                # Create the conversion constraint
                constraint = self.create_conversion_constraint(unit, var, limiting_reactant=limiting_reactant)
                setattr(unit, "conversion_constraint", constraint)
            else:
                print("Error: Cannot create conversion constraint without limiting_reactant")

        # Handle heat specifications
        if unit.config.has_heat_transfer:
            if hasattr(config, "heat_duty") and config.heat_duty is not None:
                if hasattr(unit, "heat"):
                    unit.heat.fix(config.heat_duty.value)
                else:
                    print(f"Warning: Unit {unit.name} has has_heat_transfer=True but no heat variable")

            if hasattr(config, "outlet_temperature") and config.outlet_temperature is not None:
                if hasattr(unit, "outlet") and hasattr(unit.outlet, "temperature"):
                    unit.outlet.temperature.fix(config.outlet_temperature.value)
                else:
                    print(f"Warning: Unit {unit.name} has no outlet.temperature variable")

        # Handle pressure specification
        if unit.config.has_pressure_change:
            if hasattr(config, "deltaP") and config.deltaP is not None:
                if hasattr(unit, "pressure_change"):
                    unit.pressure_change.fix(config.deltaP.value)
                else:
                    print(f"Warning: Unit {unit.name} has has_pressure_change=True but no pressure_change variable")
            if hasattr(config, "outlet_pressure") and config.outlet_pressure is not None:
                if hasattr(unit, "outlet") and hasattr(unit.outlet, "pressure"):
                    unit.outlet.pressure.fix(config.outlet_pressure.value)
                else:
                    print(f"Warning: Unit {unit.name} has no outlet.pressure variable")

    def apply_gibbs_reactor_specifications(self, unit, config):
        """
        Apply specifications for GibbsReactor units.

        Args:
            unit: The IDAES GibbsReactor unit
            config: The configuration object for the unit
        """
        # First apply basic specifications
        self.apply_basic_specifications(unit, config)

        # Handle specifications
        if unit.config.has_heat_transfer:
            if hasattr(config, "heat_duty") and config.heat_duty is not None:
                if hasattr(unit, "heat_duty"):
                    unit.heat_duty.fix(config.heat_duty.value)
                else:
                    print(f"Warning: Unit {unit.name} has has_heat_transfer=True but no heat_duty variable")

            elif hasattr(config, "outlet_temperature") and config.outlet_temperature is not None:
                if hasattr(unit, "outlet") and hasattr(unit.outlet, "temperature"):
                    unit.outlet.temperature.fix(config.outlet_temperature.value)
                else:
                    print(f"Warning: Unit {unit.name} has no outlet.temperature variable")

            if hasattr(config, "conversion") and config.conversion is not None:
                # Create or access the conversion variable and set the constraint
                converted_reactant = getattr(config, "converted_reactant", None)
                if converted_reactant:
                    var = self.create_and_fix_variable(
                        unit,
                        "conversion",
                        config.conversion,
                    )
                    # Create the conversion constraint
                    constraint = self.create_conversion_constraint(unit, var, limiting_reactant=converted_reactant)
                    setattr(unit, "conversion_constraint", constraint)
                else:
                    print("Error: Cannot create conversion constraint without converted_reactant")
                    print("Warning: Using first component in property package which might not be a reactant.")
                    # Try to find a component to use
                    if hasattr(unit.config.property_package, "component_list"):
                        components = list(unit.config.property_package.component_list)
                        if components:
                            converted_reactant = components[0]

        # Handle pressure specification
        if unit.config.has_pressure_change:
            if hasattr(config, "deltaP") and config.deltaP is not None:
                if hasattr(unit, "deltaP"):
                    unit.pressure_change.fix(config.deltaP.value)
                else:
                    print(f"Warning: Unit {unit.name} has has_pressure_change=True but no pressure_change variable")
            if hasattr(config, "outlet_pressure") and config.outlet_pressure is not None:
                if hasattr(unit, "outlet") and hasattr(unit.outlet, "pressure"):
                    unit.outlet.pressure.fix(config.outlet_pressure.value)
                else:
                    print(f"Warning: Unit {unit.name} has no outlet.pressure variable")

    def apply_heater_specifications(self, unit, config):
        """
        Apply specifications for Heater units.

        Args:
            unit: The IDAES Heater unit
            config: The configuration object for the unit
        """
        # First apply basic specifications
        self.apply_basic_specifications(unit, config)

        # Handle heat duty
        if hasattr(config, "heat_duty") and config.heat_duty is not None:
            if hasattr(unit, "heat_duty"):
                unit.heat_duty.fix(config.heat_duty.value)
            else:
                print(f"Warning: Unit {unit.name} has no heat_duty variable")

        # Handle outlet temperature
        if hasattr(config, "outlet_temperature") and config.outlet_temperature is not None:
            if hasattr(unit, "outlet") and hasattr(unit.outlet, "temperature"):
                unit.outlet.temperature.fix(config.outlet_temperature.value)
            else:
                print(f"Warning: Unit {unit.name} has no outlet.temperature variable")

    def apply_heat_exchanger_specifications(self, unit, config):
        """
        Apply specifications for HeatExchanger units.

        Args:
            unit: The IDAES HeatExchanger unit
            config: The configuration object for the unit
        """
        # First apply basic specifications
        self.apply_basic_specifications(unit, config)

        # Handle delta temperature approach
        if hasattr(config, "delta_temperature_approach") and config.delta_temperature_approach is not None:
            if hasattr(unit, "delta_temperature_approach"):
                unit.delta_temperature_approach.fix(config.delta_temperature_approach.value)
            else:
                print(f"Warning: Unit {unit.name} has no delta_temperature_approach variable")

    def apply_flash_specifications(self, unit, config):
        """
        Apply specifications for Flash units.
        # TODO: Cannot handle time-dependent variable fixing yet.

        Args:
            unit: The IDAES Flash unit
            config: The configuration object for the unit
        """
        # First apply basic specifications
        self.apply_basic_specifications(unit, config)

        # Handle outlet temperature as constraint on control volume not one of outlet streams
        # https://idaes-pse.readthedocs.io/en/latest/reference_guides/model_libraries/generic/unit_models/flash.htm
        if hasattr(config, "outlet_temperature") and config.outlet_temperature is not None:
            # Add constraint for unit.control_volume.outlet.temperature
            if hasattr(unit, "control_volume") and hasattr(unit.control_volume, "properties_out"):
                unit.outlet_temp_constraint = Constraint(
                    expr=unit.control_volume.properties_out[0].temperature == config.outlet_temperature.value
                )
        # Handle outlet pressure similarly (was previously ignored because Flash has no single 'outlet' Port)
        if hasattr(config, "outlet_pressure") and config.outlet_pressure is not None:
            if hasattr(unit, "control_volume") and hasattr(unit.control_volume, "properties_out"):
                unit.outlet_pressure_constraint = Constraint(
                    expr=unit.control_volume.properties_out[0].pressure == config.outlet_pressure.value
                )
        # pressure drop instead of outlet pressure
        if hasattr(config, "deltaP") and config.deltaP is not None:
            if hasattr(unit, "deltaP"):
                unit.deltaP.fix(config.deltaP.value)
            else:
                print(f"Warning: Unit {unit.name} has no deltaP variable, but should have one")

    def apply_mixer_specifications(self, unit, config):
        """
        Apply specifications for Mixer units.

        Args:
            unit: The IDAES Mixer unit
            config: The configuration object for the unit
        """
        # Mixers don't typically have specifications beyond constructor parameters
        self.apply_basic_specifications(unit, config)

    def apply_splitter_specifications(self, unit, config):
        """
        Apply specifications for Splitter units.

        Args:
            unit: The IDAES Splitter unit
            config: The configuration object for the unit
        """
        # First apply basic specifications
        self.apply_basic_specifications(unit, config)
        if hasattr(config, "split_fractions") and config.split_fractions:
            # Check if dict items length is length of outlet_list -1:
            if len(config.split_fractions) != len(config.outlet_list) - 1:
                print(
                    f"Warning: Splitter '{unit.name}': Splitter needs split fractions to be specified for N_outlet_streams-1"
                )
            for stream_name, split_fraction in config.split_fractions.items():
                if hasattr(unit, "split_fraction"):
                    unit.split_fraction[0, stream_name].fix(split_fraction)
                else:
                    print(f"Warning: Splitter '{unit.name}' has no split_fraction variable")

    def apply_translator_specifications(self, unit, config: TranslatorConfig):
        """Apply automatic mapping constraints for Translator blocks.

        Handles common state mappings and component set transformations.
        Supported state mappings:
        - FpcTP -> FTPx
        - FTPx -> FpcTP (allocate to a single target phase if target_phase_out is set)
        - FTPx -> FTPx
        If mole fractions are used in the outlet state they need to sum up to 1
        """
        t = 0

        state_in = config.state_definition_in
        state_out = config.state_definition_out

        # comps_in = list(config.components_in) if config.components_in else []
        comps_out = list(config.components_out) if config.components_out else []

        phases_in = list(unit.config.inlet_property_package.phase_list)
        phases_out = list(unit.config.outlet_property_package.phase_list)

        # Temperature and pressure equality for all mappings
        unit.eq_temperature = Constraint(expr=unit.outlet.temperature[t] == unit.inlet.temperature[t])
        unit.eq_pressure = Constraint(expr=unit.outlet.pressure[t] == unit.inlet.pressure[t])

        # === Check state definitions on both sides ===

        # === Handle FpcTP-to-FTPx mapping ===
        if state_in == "FpcTP" and state_out == "FTPx":
            # Total mole flow equals the sum of mole flows in all phases of inlet
            unit.eq_total_flow = Constraint(
                expr=unit.outlet.flow_mol[t]
                == sum(
                    unit.inlet.flow_mol_phase_comp[t, ph, comp]
                    for ph in phases_in
                    for comp in comps_out
                    if (t, ph, comp) in unit.inlet.flow_mol_phase_comp
                )
            )
            # For each component in the outlet spec, we divide the mole flow by the total flow
            for j in comps_out:
                unit.add_component(
                    f"eq_mole_frac_{j}",
                    Constraint(
                        expr=unit.outlet.mole_frac_comp[t, j]
                        == sum(
                            unit.inlet.flow_mol_phase_comp[t, ph, j]
                            for ph in phases_in
                            if (t, ph, j) in unit.inlet.flow_mol_phase_comp
                        )
                        / sum(
                            unit.inlet.flow_mol_phase_comp[t, ph, comp]
                            for ph in phases_in
                            for comp in comps_out
                            if (t, ph, comp) in unit.inlet.flow_mol_phase_comp
                        )
                    ),
                )

        # === Handle FpcTP-to-FpcTP mapping ===
        elif state_in == "FpcTP" and state_out == "FpcTP":
            # Direct one-to-one phase-component mapping for common indices
            for ph in phases_out:
                for j in comps_out:
                    if (t, ph, j) in unit.inlet.flow_mol_phase_comp and (
                        t,
                        ph,
                        j,
                    ) in unit.outlet.flow_mol_phase_comp:
                        cname = f"eq_flow_{ph}_{j}"
                        unit.add_component(
                            cname,
                            Constraint(
                                expr=unit.outlet.flow_mol_phase_comp[t, ph, j]
                                == unit.inlet.flow_mol_phase_comp[t, ph, j]
                            ),
                        )

        # === Handle FTPx-to-FTPx mapping ===
        elif state_in == "FTPx" and state_out == "FTPx":
            unit.eq_total_flow = Constraint(expr=unit.outlet.flow_mol[t] == unit.inlet.flow_mol[t])
            for j in comps_out:
                if (t, j) in unit.inlet.mole_frac_comp:
                    cname = f"eq_mole_frac_{j}"
                    unit.add_component(
                        cname,
                        Constraint(expr=unit.outlet.mole_frac_comp[t, j] == unit.inlet.mole_frac_comp[t, j]),
                    )

        # === Handle FTPx-to-FpcTP mapping ===
        # Only proceed with components specified in the outlet list
        elif state_in == "FTPx" and state_out == "FpcTP":
            # Map overall component flow to a single chosen phase; leave others unconstrained.
            if getattr(config, "target_phase_out", None) in phases_out:
                target_phase = config.target_phase_out
            elif "Vap" in phases_out:
                target_phase = "Vap"
            else:
                target_phase = phases_out[0]
            for j in comps_out:
                if (t, target_phase, j) not in unit.outlet.flow_mol_phase_comp:
                    continue
                cname = f"eq_flow_{target_phase}_{j}"
                unit.add_component(
                    cname,
                    Constraint(
                        expr=unit.outlet.flow_mol_phase_comp[t, target_phase, j]
                        == unit.inlet.flow_mol[t] * unit.inlet.mole_frac_comp[t, j]
                    ),
                )

        else:
            raise NotImplementedError("Translator only supports mappings between FTPx and FpcTP.")

    def apply_pressure_changer_specifications(self, unit, config):
        """
        Apply specifications for Compressor and Turbine units.

        Args:
            unit: The IDAES Compressor or Turbine unit
            config: The configuration object for the unit
        """
        # First apply basic specifications
        self.apply_basic_specifications(unit, config)

        # Handle outlet pressure
        if hasattr(config, "outlet_pressure") and config.outlet_pressure is not None:
            if hasattr(unit, "outlet") and hasattr(unit.outlet, "pressure"):
                unit.outlet.pressure.fix(config.outlet_pressure.value)

        elif hasattr(config, "deltaP") and config.deltaP is not None:
            if hasattr(unit, "deltaP"):
                unit.deltaP.fix(config.deltaP.value)
        else:
            print(f"Warning: Unit {unit.name} has neither outlet pressure nor deltaP defined")

        # Handle thermodynamic assumption - this may require additional constraints
        # based on the thermodynamic assumption (isothermal or isentropic)
        if hasattr(config, "thermodynamic_assumption"):
            if config.thermodynamic_assumption == "isentropic":
                # For isentropic compression, set the isentropic efficiency
                if hasattr(unit, "efficiency_isentropic") and config.efficiency_isentropic is not None:
                    unit.efficiency_isentropic.fix(config.efficiency_isentropic.value)
                else:
                    print(
                        f"Warning: Unit {unit.name} has isentropic assumption but efficiency_isentropic is not specified"
                    )

    def apply_distillation_column_specifications(self, unit, config):
        """
        Apply specifications for Distillation Column units.

        Args:
            unit: The IDAES TrayColumn unit
            config: The configuration object for the unit
        """
        # Apply basic specifications first
        self.apply_basic_specifications(unit, config)

        # Set reflux ratio
        if hasattr(config, "reflux_ratio") and config.reflux_ratio is not None:
            if hasattr(unit, "condenser") and hasattr(unit.condenser, "reflux_ratio"):
                unit.condenser.reflux_ratio.fix(config.reflux_ratio.value)
            else:
                print(f"Warning: Unit {unit.name} has no condenser.reflux_ratio variable")

        # Set boilup ratio
        if hasattr(config, "boilup_ratio") and config.boilup_ratio is not None:
            if hasattr(unit, "reboiler") and hasattr(unit.reboiler, "boilup_ratio"):
                unit.reboiler.boilup_ratio.fix(config.boilup_ratio.value)
            else:
                print(f"Warning: Unit {unit.name} has no reboiler.boilup_ratio variable")

        # Set condenser pressure
        if hasattr(config, "condenser_pressure") and config.condenser_pressure is not None:
            if hasattr(unit, "condenser") and hasattr(unit.condenser, "condenser_pressure"):
                unit.condenser.condenser_pressure.fix(config.condenser_pressure.value)
            else:
                print(f"Warning: Unit {unit.name} has no condenser.pressure variable")

        # Set pressure drop if has_pressure_change is True
        # TODO: Implement pressure drop per tray if has_pressure_change is True

    def create_conversion_constraint(self, unit, var, limiting_reactant: Union[str, Tuple[str, str]]):
        """
        Create a conversion constraint that works with either flow_mol_phase_comp
        or flow_mol and mole_frac_comp, depending on what's available.

        Args:
            unit: The reactor unit
            var: The conversion variable
            limiting_reactant: limiting_reactant

        Returns:
            A constraint for the conversion
        """
        # Get the limiting reactant from kwargs
        if not limiting_reactant:
            raise ValueError("Must specify limiting_reactant for conversion constraint")

        # Get time point (usually 0)
        time_point = 0

        # Check what state variables are available
        if hasattr(unit.inlet, "flow_mol_phase_comp") and hasattr(unit.outlet, "flow_mol_phase_comp"):
            # We need to use phase,component format
            if isinstance(limiting_reactant, tuple) and len(limiting_reactant) == 2:
                phase, component = limiting_reactant
            else:
                raise ValueError(
                    "When using flow_mol_phase_comp (state definition), "
                    "limiting_reactant must be a tuple (phase, component)"
                )
            # Use flow_mol_phase_comp directly
            inlet_flow = unit.inlet.flow_mol_phase_comp[time_point, phase, component]
            outlet_flow = unit.outlet.flow_mol_phase_comp[time_point, phase, component]

            return Constraint(expr=var * inlet_flow == (inlet_flow - outlet_flow))

        elif (
            hasattr(unit.inlet, "flow_mol")
            and hasattr(unit.inlet, "mole_frac_comp")
            and hasattr(unit.outlet, "flow_mol")
            and hasattr(unit.outlet, "mole_frac_comp")
        ):
            # Determine if we need to use phase,component format
            if isinstance(limiting_reactant, str):
                component = limiting_reactant
                phase = None
            elif isinstance(limiting_reactant, tuple) and len(limiting_reactant) == 2:
                raise ValueError(
                    "When using flow_mol and mole fractions (state definition), "
                    "limiting_reactant must be a string (component name only)"
                )
            # Use flow_mol and mole_frac_comp
            inlet_flow = unit.inlet.flow_mol[time_point] * unit.inlet.mole_frac_comp[time_point, component]
            outlet_flow = unit.outlet.flow_mol[time_point] * unit.outlet.mole_frac_comp[time_point, component]

            return Constraint(expr=var * inlet_flow == (inlet_flow - outlet_flow))

        else:
            raise ValueError(
                "Cannot create conversion constraint: "
                "Neither flow_mol_phase_comp nor flow_mol and mole_frac_comp are available for both inlet and outlet."
            )

    def create_and_fix_variable(
        self,
        unit,
        variable_name: str,
        quantity: Optional[Quantity] = None,
        initialize: float = 0.5,
        bounds: Tuple[Optional[float], Optional[float]] = (None, None),
        units: Optional[PyomoUnit] = None,
    ) -> Var:
        """
        Create a variable on a unit and fix it to a value if specified.

        Args:
            unit: The unit operation
            variable_name: Name of the variable to create
            quantity: Optional Quantity object with value and units
            initialize: Initial value for the variable (used if not specified in quantity)
            bounds: Bounds for the variable as a tuple (lower, upper)
            units: Optional units for the variable (used if not specified in quantity)

        Returns:
            The created or existing variable
        """
        # Check if the variable already exists
        if hasattr(unit, variable_name):
            var = getattr(unit, variable_name)
            # If a quantity is provided, fix the variable to that value
            if quantity is not None:
                var.fix(quantity.value)
            return var

        # Create the variable
        var_args = {
            "initialize": quantity.value if quantity is not None else initialize,
            "bounds": bounds,
        }

        # Apply units if provided
        if quantity is not None and hasattr(quantity, "units") and quantity.unit is not None:
            var_args["units"] = quantity.unit
        elif units is not None:
            var_args["units"] = units

        # Create the variable
        var = Var(**var_args)
        setattr(unit, variable_name, var)

        # Fix the variable if quantity is provided
        if quantity is not None:
            var.fix(quantity.value)

        return var
