"""
GIS Code Execution Server.

A code execution server with geospatial analysis packages including GeoPandas,
Shapely, Rasterio, Fiona, and Folium for spatial data processing and visualization.
"""

import asyncio
import logging
import os
from pathlib import Path

from code_execution import CodeExecutionServer, EnvironmentConfig

LOGGER = logging.getLogger(__name__)


def create_gis_config() -> EnvironmentConfig:
    """Create GIS environment configuration."""

    requirements_path = Path(__file__).parent / "requirements.yaml"
    dependency_file = requirements_path.read_text()

    return EnvironmentConfig(
        name="gis",
        description="Execute Python code for geospatial analysis with GeoPandas, Shapely, Rasterio, and Folium",
        type="uv",
        dependency_file=dependency_file,
        auto_build=True,
    )


async def main():
    """Run the GIS code execution server."""

    entra_client_id = os.getenv("ENTRA_CLIENT_ID")
    entra_tenant_id = os.getenv("ENTRA_TENANT_ID")
    port = int(os.getenv("PORT", "8006"))
    host = os.getenv("HOST", "0.0.0.0")

    config = create_gis_config()

    server = CodeExecutionServer(
        environment_config=config,
        entra_client_id=entra_client_id,
        entra_tenant_id=entra_tenant_id,
    )

    LOGGER.info(f"Starting GIS code execution server on {host}:{port}")
    LOGGER.info(f"Environment: {config.name} ({config.type})")

    await server.run_http(host=host, port=port)


if __name__ == "__main__":
    asyncio.run(main())
