"""
DWSIM CLR Helpers.

Shared utilities for initializing the .NET CLR via pythonnet and loading
DWSIM assemblies. All tool modules import from here to ensure the CLR is
initialized exactly once per process.
"""

import os
from functools import lru_cache
from pathlib import Path


def _dwsim_root() -> Path:
    """Return the DWSIM installation directory."""
    return Path(os.getenv("DWSIM_PATH", "/usr/local/lib/dwsim/"))


@lru_cache(maxsize=1)
def _load_clr():
    """Initialize the .NET CLR via pythonnet (coreclr runtime). Cached."""
    os.environ["PYTHONNET_RUNTIME"] = "coreclr"
    import pythonnet

    pythonnet.load()
    import clr

    return clr


@lru_cache(maxsize=1)
def get_automation():
    """
    Return a DWSIM ``Automation3`` instance with required assemblies loaded.

    The instance is cached so all tools in the same session share the same
    automation object and assembly references.

    Returns
    -------
    Automation3
        Ready-to-use DWSIM automation interface.
    """
    clr = _load_clr()
    root = _dwsim_root()

    clr.AddReference(str(root / "DWSIM.Automation.dll"))
    clr.AddReference(str(root / "DWSIM.Interfaces.dll"))
    clr.AddReference(str(root / "DWSIM.Thermodynamics.dll"))
    clr.AddReference(str(root / "DWSIM.UnitOperations.dll"))
    clr.AddReference(str(root / "DWSIM.FlowsheetSolver.dll"))
    clr.AddReference(str(root / "DWSIM.GlobalSettings.dll"))
    clr.AddReference(str(root / "CapeOpen.dll"))

    from DWSIM.Automation import Automation3

    return Automation3()
