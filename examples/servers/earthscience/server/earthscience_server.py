"""
Earth Science MCP Server — Planetary Computer geospatial analysis environment.

Ships a single ``execute_earthscience_code`` MCP tool backed by a conda
environment with pystac-client, planetary-computer, rasterio, xarray, and
the rest of the geospatial Python stack. There are no domain-specific
wrapper tools — the agent writes geospatial code directly against the
auto-imported libraries, guided by the SKILL.md recipes under ``skills/``.

This server exists as the canonical example of a "minimal BYOA" domain:
the deliverable is the *environment* (conda spec + prelude + skill
markdown), not a catalogue of pre-canned wrappers.

The Microsoft Planetary Computer STAC API is free and publicly accessible —
no API keys or accounts are required.

Usage:
    python -m servers.earthscience.server.earthscience_server

Requires the base image (mcp-server-base:local) to be built first.
See the README for full instructions.
"""

import asyncio
import os
import sys
from pathlib import Path

from agora_workbench.code_execution import CodeExecutionServer, ServerConfig, discover_skills
from agora_workbench.code_execution.auth import create_noop_auth_config

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

ENVIRONMENT_YML = """\
name: earthscience
channels:
  - conda-forge
dependencies:
  - python=3.11
  - pip
  - pystac-client
  - planetary-computer
  - rasterio
  - xarray
  - rioxarray
  - geopandas
  - shapely
  - numpy
  - pandas
  - scipy
  - matplotlib
"""

EARTHSCIENCE_PRELUDE = """\
import planetary_computer
import pystac_client
import rasterio
import xarray as xr
import rioxarray
import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import box, Point, Polygon
"""

config = ServerConfig(
    name="earthscience",
    description=(
        "Execute Python code for earth science and remote sensing analysis. "
        "Available libraries: pystac-client, planetary-computer, rasterio, "
        "xarray, rioxarray, geopandas, shapely, numpy, pandas, scipy, matplotlib. "
        "Use this for satellite imagery search via STAC, raster analysis, "
        "NDVI computation, land cover classification, and time-series analysis. "
        "Data is accessed from the free Microsoft Planetary Computer catalog."
    ),
    type="conda",
    dependency_file=ENVIRONMENT_YML,
    auto_build=True,
)


class EarthScienceServer(CodeExecutionServer):
    """Earth science MCP server with auto-imported geospatial modules."""

    def preprocess_code(self, code: str) -> str:
        """Inject common geospatial imports before user code."""
        return EARTHSCIENCE_PRELUDE + code


_skills = discover_skills(_SKILLS_DIR, domain="earthscience")

server = EarthScienceServer(
    server_config=config,
    auth_config=create_noop_auth_config(),
    skills=_skills,
)

if __name__ == "__main__":
    if "--warm" in sys.argv:
        asyncio.run(server.warm())
    else:
        host = os.getenv("HOST", "0.0.0.0")
        port = int(os.getenv("PORT", "8000"))
        asyncio.run(server.run_http(host=host, port=port))
