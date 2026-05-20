"""Compute NDVI from a red + NIR raster pair."""

from __future__ import annotations

import os
import tempfile
import uuid
from typing import Optional

_DEFAULT_MAX_PIXELS = 1_000_000  # ~1000x1000


def compute_ndvi(
    red_href: str,
    nir_href: str,
    bbox: Optional[list] = None,
    max_pixels: int = _DEFAULT_MAX_PIXELS,
    output_path: Optional[str] = None,
) -> dict:
    """Compute NDVI = (NIR − Red) / (NIR + Red) and write it to a GeoTIFF.

    Reads the two band hrefs (typically signed Planetary Computer URLs from
    ``search_stac_items``), optionally subsets to a bbox, downsamples so the
    output stays under ``max_pixels``, computes NDVI, and writes the result
    to ``output_path`` (or a unique ``/tmp`` path if not provided).

    NDVI is clipped to the natural range ``[-1.0, 1.0]``; pixels where
    ``NIR + Red == 0`` are masked as NaN.

    Args:
        red_href: URL or local path to the red-band raster (e.g. Sentinel-2
            ``B04``, Landsat ``red``).
        nir_href: URL or local path to the NIR-band raster (Sentinel-2
            ``B08``, Landsat ``nir08``).
        bbox: Optional ``[west, south, east, north]`` in EPSG:4326. Reads
            are restricted to the intersection of the source raster and
            this bbox.
        max_pixels: Approximate cap on the total pixel count read.
            Defaults to 1,000,000 (~1000×1000). Use a smaller value for
            quick previews; ``0`` disables downsampling (full resolution).
        output_path: Where to write the NDVI GeoTIFF. If ``None``, a unique
            file is created under ``/tmp``.

    Returns:
        Dictionary with ``output_path``, ``shape`` ``[height, width]``,
        ``crs``, ``bounds``, ``ndvi_min``, ``ndvi_max``, ``ndvi_mean``,
        ``ndvi_std``, ``valid_pixels``, ``total_pixels``.
    """
    import numpy as np
    import rasterio
    from rasterio.windows import from_bounds

    if output_path is None:
        output_path = os.path.join(
            tempfile.gettempdir(), f"ndvi_{uuid.uuid4().hex[:12]}.tif"
        )

    with rasterio.open(red_href) as red_src, rasterio.open(nir_href) as nir_src:
        # Determine read window
        if bbox is not None:
            from rasterio.warp import transform_bounds

            west, south, east, north = bbox
            src_west, src_south, src_east, src_north = transform_bounds(
                "EPSG:4326", red_src.crs, west, south, east, north
            )
            window = from_bounds(
                src_west, src_south, src_east, src_north, transform=red_src.transform
            ).round_offsets().round_lengths()
            window = window.intersection(
                rasterio.windows.Window(0, 0, red_src.width, red_src.height)
            )
        else:
            window = rasterio.windows.Window(0, 0, red_src.width, red_src.height)

        h, w = int(window.height), int(window.width)
        if h <= 0 or w <= 0:
            raise ValueError(
                f"Empty read window for bbox={bbox!r}; bbox does not intersect raster."
            )

        # Downsample to honour max_pixels
        if max_pixels and h * w > max_pixels:
            scale = (max_pixels / (h * w)) ** 0.5
            out_h = max(1, int(h * scale))
            out_w = max(1, int(w * scale))
        else:
            out_h, out_w = h, w

        red = red_src.read(
            1, window=window, out_shape=(out_h, out_w), masked=True
        ).astype("float32")
        nir = nir_src.read(
            1, window=window, out_shape=(out_h, out_w), masked=True
        ).astype("float32")

        denom = nir + red
        with np.errstate(divide="ignore", invalid="ignore"):
            ndvi = np.ma.divide(nir - red, denom)
        ndvi = np.ma.masked_where(np.isclose(denom, 0.0), ndvi)
        ndvi = np.ma.masked_invalid(ndvi)
        ndvi = np.ma.clip(ndvi, -1.0, 1.0)

        # Build transform for the (possibly resampled) output grid.
        window_transform = red_src.window_transform(window)
        out_transform = window_transform * window_transform.scale(
            w / out_w, h / out_h
        )

        ndvi_arr = ndvi.filled(np.nan).astype("float32")
        finite_mask = np.isfinite(ndvi_arr)
        valid_pixels = int(finite_mask.sum())
        total_pixels = int(ndvi_arr.size)

        if valid_pixels == 0:
            stats = {
                "ndvi_min": None,
                "ndvi_max": None,
                "ndvi_mean": None,
                "ndvi_std": None,
            }
        else:
            valid = ndvi_arr[finite_mask]
            stats = {
                "ndvi_min": float(valid.min()),
                "ndvi_max": float(valid.max()),
                "ndvi_mean": float(valid.mean()),
                "ndvi_std": float(valid.std()),
            }

        with rasterio.open(
            output_path,
            "w",
            driver="GTiff",
            height=out_h,
            width=out_w,
            count=1,
            dtype="float32",
            crs=red_src.crs,
            transform=out_transform,
            nodata=float("nan"),
            compress="deflate",
            tiled=True,
        ) as dst:
            dst.write(ndvi_arr, 1)

        bounds = rasterio.windows.bounds(window, red_src.transform)

    return {
        "output_path": output_path,
        "shape": [out_h, out_w],
        "crs": str(red_src.crs),
        "bounds": [float(b) for b in bounds],
        "valid_pixels": valid_pixels,
        "total_pixels": total_pixels,
        **stats,
    }
