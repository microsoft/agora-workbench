"""
Pyomo unit utilities for IDAES process simulation.

Provides wrappers and utilities for working with Pyomo units in a type-safe manner.

NOTE: This module defers pyomo import until first use to avoid requiring pyomo
in the server environment. The classes can be imported for type hints without
triggering the pyomo import.
"""

# Lazy import - only loaded when _get_pyunits() is called
_pyunits = None


def _get_pyunits():
    """Lazy-load pyomo units on first use."""
    global _pyunits
    if _pyunits is None:
        from pyomo.environ import units as pyunits

        _pyunits = pyunits
    return _pyunits


class UnitWrapper:
    """
    Wrapper for correction of Pyomo unit typing issues.

    unit: a Pyomo unit to wrap
    """

    def __init__(self, unit):
        self._unit = unit

    @property
    def unit(self):
        return self._unit

    def __mul__(self, other):
        if isinstance(other, (int, float)):
            return self.unit * other
        elif isinstance(other, UnitWrapper):
            return UnitWrapper(self.unit * other.unit)
        else:
            return self.unit * other

    def __rmul__(self, other):
        if isinstance(other, (int, float)):
            return other * self.unit
        elif isinstance(other, UnitWrapper):
            return UnitWrapper(other.unit * self.unit)
        else:
            return other * self.unit

    def __truediv__(self, other):
        if isinstance(other, (int, float)):
            return self.unit / other
        elif isinstance(other, UnitWrapper):
            return UnitWrapper(self.unit / other.unit)
        else:
            return self.unit / other

    def __rtruediv__(self, other):
        if isinstance(other, (int, float)):
            return other / self.unit
        elif isinstance(other, UnitWrapper):
            return UnitWrapper(other.unit / self.unit)
        else:
            return other / self.unit

    def __pow__(self, power):
        return UnitWrapper(self.unit**power)

    def __repr__(self):
        return f"UnitWrapper({self.unit})"

    def is_expression_type(self):
        raise RuntimeError(
            f"{str(self)} is being used in place of a Pyomo unit object. Please access wrapped unit directly with `UnitWrapper.unit`"
        )


class _PyomoUnitMeta(type):
    """Metaclass for lazy initialization of PyomoUnit attributes."""

    _initialized = False

    def __getattribute__(cls, name):
        # Don't intercept special/private attributes
        if name.startswith("_"):
            return type.__getattribute__(cls, name)

        # Initialize on first access to a unit
        if not cls._initialized:
            cls._initialize_units()

        return type.__getattribute__(cls, name)

    def _initialize_units(cls):
        """Initialize all pyomo units."""
        if cls._initialized:
            return

        pyunits = _get_pyunits()

        cls.dimensionless = UnitWrapper(pyunits.dimensionless)
        cls.g_per_mol = UnitWrapper(pyunits.g) / UnitWrapper(pyunits.mol)
        cls.J = UnitWrapper(pyunits.J)
        cls.J_per_s = UnitWrapper(pyunits.J) / UnitWrapper(pyunits.s)
        cls.W = UnitWrapper(pyunits.W)
        cls.J_per_mol = UnitWrapper(pyunits.J) / UnitWrapper(pyunits.mol)
        cls.J_per_mol_per_K = UnitWrapper(pyunits.J) / UnitWrapper(pyunits.mol) / UnitWrapper(pyunits.K)
        cls.K = UnitWrapper(pyunits.K)
        cls.K_per_s = UnitWrapper(pyunits.K) / UnitWrapper(pyunits.s)
        cls.m = UnitWrapper(pyunits.m)
        cls.m2 = UnitWrapper(pyunits.m) ** 2
        cls.m3 = UnitWrapper(pyunits.m) ** 3
        cls.m3_per_s = UnitWrapper(pyunits.m) ** 3 / UnitWrapper(pyunits.s)
        cls.m3_per_mol = UnitWrapper(pyunits.m) ** 3 / UnitWrapper(pyunits.mol)
        cls.m3_per_mol_per_s = UnitWrapper(pyunits.m) ** 3 / UnitWrapper(pyunits.mol) / UnitWrapper(pyunits.s)
        cls.kg = UnitWrapper(pyunits.kg)
        cls.mol = UnitWrapper(pyunits.mol)
        cls.mol_per_m3 = UnitWrapper(pyunits.mol) / UnitWrapper(pyunits.m) ** 3
        cls.mol_per_m3_per_s = UnitWrapper(pyunits.mol) / UnitWrapper(pyunits.m) ** 3 / UnitWrapper(pyunits.s)
        cls.mol_per_s = UnitWrapper(pyunits.mol) / UnitWrapper(pyunits.s)
        cls.Pa = UnitWrapper(pyunits.Pa)
        cls.Pa_per_s = UnitWrapper(pyunits.Pa) / UnitWrapper(pyunits.s)
        cls.Pa_m6_per_mol2 = UnitWrapper(pyunits.Pa) * UnitWrapper(pyunits.m) ** 6 / UnitWrapper(pyunits.mol) ** 2
        cls.per_s = 1.0 / UnitWrapper(pyunits.s)
        cls.s = UnitWrapper(pyunits.s)

        cls._initialized = True


class PyomoUnit(metaclass=_PyomoUnitMeta):
    """
    Collection of wrapped Pyomo units.

    Units are lazily initialized on first access to avoid loading pyomo
    when this module is imported for type hints only.
    """

    pass


class Quantity:
    """
    Represents a Pyomo unit in a pint-style unit format.

    The numerical portion of the unit is accessible via `self.magnitude`.
    The unit label is accessible via `self.unit`.
    The complete Pyomo unit is accessible via `self.value`.

    magnitude (float): the magnitude (with sign) of the quantity
    wrapped_unit (UnitWrapper): a Pyomo unit (or combination of units) wrapped with the UnitWrapper class
    """

    def __init__(self, magnitude: float, wrapped_unit: UnitWrapper):
        self.magnitude = magnitude
        self.wrapped_unit = wrapped_unit

    @property
    def unit(self):
        return self.wrapped_unit.unit

    @property
    def value(self):
        return self.magnitude * self.wrapped_unit.unit
