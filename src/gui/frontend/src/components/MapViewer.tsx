import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import {
  MapContainer,
  TileLayer,
  GeoJSON,
  CircleMarker,
  Popup,
  Tooltip,
  useMap,
} from "react-leaflet";
import L from "leaflet";
import type { LatLngExpression } from "leaflet";
import "leaflet/dist/leaflet.css";
import html2canvas from "html2canvas";
import type { MapState, MapLayer, LayerStyle, ClassifyBy, Legend, MapAnnotation, DrawingMode } from "../types";
import { fetchLayerGeoJSON, fetchRasterTileUrl } from "../api";
import "@geoman-io/leaflet-geoman-free";
import "@geoman-io/leaflet-geoman-free/dist/leaflet-geoman.css";

// ---------------------------------------------------------------------------
// HTML escaping utility
// ---------------------------------------------------------------------------

function escapeHtml(value: unknown): string {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

/** Parse a CSS text string into a React CSSProperties object. */
function parseCssToReactStyle(css: string): React.CSSProperties {
  return Object.fromEntries(
    css.split(";").filter(Boolean).map((s) => {
      const [k, ...v] = s.split(":");
      const camel = k.trim().replace(/-([a-z])/g, (_, c: string) => c.toUpperCase());
      return [camel, v.join(":").trim()];
    })
  ) as React.CSSProperties;
}

// ---------------------------------------------------------------------------
// Scale bar control
// ---------------------------------------------------------------------------

function ScaleBar({ show }: { show: boolean }) {
  const map = useMap();
  useEffect(() => {
    if (!show) return;
    const ctrl = L.control.scale({ imperial: true, metric: true });
    ctrl.addTo(map);
    return () => { ctrl.remove(); };
  }, [map, show]);
  return null;
}

// ---------------------------------------------------------------------------
// Sub-component: handles agent capture requests (fly to location + screenshot)
// ---------------------------------------------------------------------------

function CaptureHandler({
  request,
  onComplete,
}: {
  request: { requestId: string; center: [number, number]; zoom: number } | null | undefined;
  onComplete?: (requestId: string, pngBlob: Blob) => void;
}) {
  const map = useMap();
  const handledRef = useRef<string>("");

  useEffect(() => {
    if (!request || !onComplete) return;
    if (request.requestId === handledRef.current) return;
    handledRef.current = request.requestId;

    const { requestId, center, zoom } = request;

    // Fly to the requested location
    map.flyTo([center[0], center[1]], zoom, { duration: 1.5 });

    // Wait for the fly animation + tile loading, then capture
    const timer = setTimeout(async () => {
      // Wait a bit more for tiles to fully render
      await new Promise((r) => setTimeout(r, 1500));

      const container = map.getContainer();
      try {
        const canvas = await html2canvas(container, {
          useCORS: true,
          allowTaint: false,
          width: 1024,
          height: 1024,
          scale: 1,
          logging: false,
        });
        canvas.toBlob((blob) => {
          if (blob) {
            onComplete(requestId, blob);
          } else {
            console.error("Failed to create blob from canvas");
          }
        }, "image/png");
      } catch (err) {
        console.error("Map capture failed:", err);
      }
    }, 2000); // 2s for fly animation to complete

    return () => clearTimeout(timer);
  }, [request, onComplete, map]);

  return null;
}

// ---------------------------------------------------------------------------
// Sub-component: applies view changes (center/zoom) to the map
// ---------------------------------------------------------------------------

function ViewUpdater({
  center,
  zoom,
  onViewportChange,
}: {
  center: LatLngExpression;
  zoom: number;
  onViewportChange?: (center: [number, number], zoom: number) => void;
}) {
  const map = useMap();
  const prevRef = useRef<string>("");

  useEffect(() => {
    const key = JSON.stringify({ center, zoom });
    if (key !== prevRef.current) {
      prevRef.current = key;
      map.flyTo(center, zoom, { duration: 1.2 });
    }
  }, [map, center, zoom]);

  // Report the live viewport whenever the user pans/zooms
  useEffect(() => {
    if (!onViewportChange) return;
    const handler = () => {
      const c = map.getCenter();
      onViewportChange([c.lat, c.lng], map.getZoom());
    };
    map.on("moveend", handler);
    // Report initial viewport
    handler();
    return () => { map.off("moveend", handler); };
  }, [map, onViewportChange]);

  return null;
}

// ---------------------------------------------------------------------------
// Sub-component: renders a raster layer via titiler tile URL
// ---------------------------------------------------------------------------

function RasterTileLayer({ layer }: { layer: MapLayer }) {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchRasterTileUrl(layer.id)
      .then((u) => { if (!cancelled) setUrl(u); })
      .catch((err) => console.error(`Failed to get tile URL for ${layer.id}:`, err));
    return () => { cancelled = true; };
  }, [layer.id, layer.tif_file, layer.colormap, layer.value_range]);

  if (!url) return null;

  return (
    <TileLayer
      url={url}
      opacity={layer.opacity ?? 0.7}
    />
  );
}

// ---------------------------------------------------------------------------
// Choropleth classification helpers
// ---------------------------------------------------------------------------

function classifyValue(
  value: unknown,
  classify: ClassifyBy
): number {
  if (classify.method === "categorical") {
    const cats = classify.categories ?? [];
    const idx = cats.indexOf(String(value));
    return idx >= 0 ? idx : -1;
  }

  const num = Number(value);
  if (isNaN(num)) return -1;

  const breaks = classify.breaks ?? [];
  for (let i = 0; i < breaks.length; i++) {
    if (num <= breaks[i]) return i;
  }
  return breaks.length;
}

function computeBreaks(
  features: GeoJSON.Feature[],
  classify: ClassifyBy
): number[] {
  if (classify.method === "manual" && classify.breaks) return classify.breaks;
  if (classify.method === "categorical") return [];

  const values = features
    .map((f) => Number(f.properties?.[classify.field]))
    .filter((v) => !isNaN(v))
    .sort((a, b) => a - b);

  if (values.length === 0) return [];
  const n = classify.num_classes ?? classify.colors.length;

  if (classify.method === "equal_interval") {
    const min = values[0], max = values[values.length - 1];
    const step = (max - min) / n;
    return Array.from({ length: n - 1 }, (_, i) => min + step * (i + 1));
  }

  // quantile
  return Array.from({ length: n - 1 }, (_, i) => {
    const idx = Math.floor(((i + 1) / n) * values.length) - 1;
    return values[Math.max(0, idx)];
  });
}

// ---------------------------------------------------------------------------
// Sub-component: renders one GeoJSON layer with styling
// ---------------------------------------------------------------------------

function GeoJSONLayer({
  layer,
  visible,
}: {
  layer: MapLayer;
  visible: boolean;
}) {
  const [geojson, setGeojson] = useState<GeoJSON.GeoJsonObject | null>(null);
  const [dataKey, setDataKey] = useState(0);

  // Serialise the layer config so we re-fetch GeoJSON whenever _any_ property
  // changes (e.g. fill_color_field added after an in-place file update).
  const layerJson = JSON.stringify(layer);

  useEffect(() => {
    const file = layer.geojson_file;
    if (!file) return;
    let cancelled = false;
    fetchLayerGeoJSON(file).then((data) => {
      if (!cancelled) {
        setGeojson(data);
        setDataKey((k) => k + 1);
      }
    }).catch((err) => console.error(`Failed to load ${file}:`, err));
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layerJson]);

  // Pre-compute choropleth breaks from loaded data
  const choroplethBreaks = useMemo(() => {
    if (!layer.classify_by || !geojson) return [];
    const fc = geojson as GeoJSON.FeatureCollection;
    if (fc.type !== "FeatureCollection") return [];
    return computeBreaks(fc.features, layer.classify_by);
  }, [geojson, layer.classify_by]);

  if (!geojson || !visible) return null;

  const style: LayerStyle = layer.style ?? {};
  const popupFields = layer.popup_fields;
  const classify = layer.classify_by;
  const labelField = layer.label_field;
  const labelStyle = layer.label_style;

  // Resolve per-feature field names from both formats
  const sbp = layer.style_by_property;
  const rbp = layer.radius_by_property;

  const colorField = layer.color_field
    ?? (sbp?.target === "color" ? sbp.property : undefined);
  const fillColorField = layer.fill_color_field
    ?? (sbp?.target === "fillColor" ? sbp.property : undefined);
  const radiusField = layer.radius_field ?? rbp?.property;
  const radiusMin = rbp?.min;
  const radiusMax = rbp?.max;

  const resolveRadius = (props: Record<string, unknown>): number => {
    if (!radiusField) return style.radius ?? 6;
    const raw = Number(props[radiusField]);
    if (isNaN(raw)) return style.radius ?? 6;
    if (radiusMin != null && radiusMax != null) {
      return Math.max(radiusMin, Math.min(radiusMax, raw));
    }
    return raw;
  };

  const resolveColor = (props: Record<string, unknown>, target: "color" | "fill"): string => {
    // 1. Choropleth classification
    if (classify) {
      const cls = classifyValue(props[classify.field], { ...classify, breaks: choroplethBreaks });
      if (cls >= 0 && cls < classify.colors.length) return classify.colors[cls];
    }
    // 2. Per-feature field
    if (target === "fill") {
      const val = fillColorField && props[fillColorField];
      if (val) return String(val);
      return style.fillColor || resolveColor(props, "color");
    }
    const val = colorField && props[colorField];
    if (val) return String(val);
    return style.color || "#3388ff";
  };

  const tooltipStyle = labelField ? {
    className: "feature-label",
    direction: "center" as const,
    permanent: true,
    ...(labelStyle?.minZoom != null ? {} : {}),
  } : null;

  const tooltipCss = labelStyle ? [
    labelStyle.fontSize ? `font-size:${labelStyle.fontSize}px` : "",
    labelStyle.color ? `color:${labelStyle.color}` : "",
    labelStyle.haloColor || labelStyle.haloWidth
      ? `text-shadow:${[[-1,0],[1,0],[0,-1],[0,1]].map(([x,y])=>`${x*(labelStyle.haloWidth??2)}px ${y*(labelStyle.haloWidth??2)}px 0 ${labelStyle.haloColor??"#fff"}`).join(",")}`
      : "",
  ].filter(Boolean).join(";") : "";

  if (layer.point_style === "circle") {
    const fc = geojson as GeoJSON.FeatureCollection;
    if (fc.type !== "FeatureCollection") return null;

    const nullCount = fc.features.filter((f) => !f.geometry).length;
    if (nullCount > 0) {
      console.warn(`[${layer.id}] ${nullCount} feature(s) with null geometry skipped`);
    }

    return (
      <>
        {fc.features
          .filter((f) => f.geometry?.type === "Point")
          .map((f, i) => {
            const [lng, lat] = (f.geometry as GeoJSON.Point).coordinates;
            const props = (f.properties ?? {}) as Record<string, unknown>;
            const featureColor = resolveColor(props, "color");
            const featureFill = resolveColor(props, "fill");
            const featureRadius = resolveRadius(props);
            return (
              <CircleMarker
                key={`${layer.id}-${i}`}
                center={[lat, lng]}
                radius={featureRadius}
                pathOptions={{
                  color: featureColor,
                  fillColor: featureFill,
                  fillOpacity: style.fillOpacity ?? 0.7,
                  weight: style.weight ?? 1,
                  dashArray: style.dashArray,
                }}
              >
                {popupFields && f.properties && (
                  <Popup>
                    {popupFields
                      .filter((k) => f.properties![k] != null)
                      .map((k) => (
                        <div key={k}>
                          <strong>{k}:</strong> {String(f.properties![k])}
                        </div>
                      ))}
                  </Popup>
                )}
                {labelField && props[labelField] != null && tooltipStyle && (
                  <Tooltip {...tooltipStyle}>
                    <span style={tooltipCss ? parseCssToReactStyle(tooltipCss) : undefined}>
                      {String(props[labelField])}
                    </span>
                  </Tooltip>
                )}
              </CircleMarker>
            );
          })}
      </>
    );
  }

  // Default: render using the GeoJSON component with line/polygon styling
  return (
    <GeoJSON
      key={`${layer.id}-${dataKey}`}
      data={geojson as GeoJSON.GeoJsonObject}
      style={(feature) => {
        const props = (feature?.properties ?? {}) as Record<string, unknown>;
        const featureColor = resolveColor(props, "color");
        const featureFill = resolveColor(props, "fill");
        return {
          color: featureColor,
          weight: style.weight ?? 2,
          opacity: style.opacity ?? 1,
          fillColor: featureFill,
          fillOpacity: style.fillOpacity ?? 0.2,
          dashArray: style.dashArray,
        };
      }}
      onEachFeature={(feature, leafletLayer) => {
        if (popupFields && feature.properties) {
          const html = popupFields
            .filter((k) => feature.properties![k] != null)
            .map((k) => `<b>${escapeHtml(k)}:</b> ${escapeHtml(feature.properties![k])}`)
            .join("<br/>");
          if (html) leafletLayer.bindPopup(html);
        }
        if (labelField && feature.properties?.[labelField] != null) {
          leafletLayer.bindTooltip(
            `<span style="${tooltipCss}">${escapeHtml(feature.properties[labelField])}</span>`,
            { permanent: true, direction: "center", className: "feature-label" }
          );
        }
      }}
    />
  );
}

// ---------------------------------------------------------------------------
// Sub-component: legend overlay
// ---------------------------------------------------------------------------

function LegendPanel({ layers, visibility }: { layers: MapLayer[]; visibility: Record<string, boolean> }) {
  // Collect legends from visible layers
  const legends: { layerName: string; legend: Legend }[] = [];

  for (const l of layers) {
    if (visibility[l.id] === false) continue;
    if (l.legend) {
      legends.push({ layerName: l.name, legend: l.legend });
    } else if (l.classify_by) {
      // Auto-generate from classify_by (breaks computed at render time, show colors/labels)
      const classify = l.classify_by;
      const items = classify.colors.map((color, i) => {
        let label: string;
        if (classify.method === "categorical") {
          label = classify.categories?.[i] ?? `Class ${i + 1}`;
        } else if (classify.breaks) {
          if (i === 0) label = `≤ ${classify.breaks[0]}`;
          else if (i >= classify.breaks.length) label = `> ${classify.breaks[classify.breaks.length - 1]}`;
          else label = `${classify.breaks[i - 1]} – ${classify.breaks[i]}`;
        } else {
          label = `Class ${i + 1}`;
        }
        return { label, color };
      });
      legends.push({ layerName: l.name, legend: { title: classify.field, items } });
    }
  }

  if (legends.length === 0) return null;

  return (
    <div className="map-legend">
      {legends.map(({ layerName, legend }, li) => (
        <div key={li} className="map-legend-section">
          <div className="map-legend-title">{legend.title || layerName}</div>
          {legend.items.map((item, ii) => (
            <div key={ii} className="map-legend-item">
              {item.dashArray ? (
                <svg className="map-legend-line" viewBox="0 0 24 4">
                  <line x1="0" y1="2" x2="24" y2="2" stroke={item.color} strokeWidth="2"
                    strokeDasharray={item.dashArray} />
                </svg>
              ) : item.radius ? (
                <svg className="map-legend-circle" viewBox="0 0 20 20">
                  <circle cx="10" cy="10" r={Math.min(item.radius, 8)} fill={item.color} />
                </svg>
              ) : (
                <span className="map-legend-swatch" style={{ background: item.color }} />
              )}
              <span className="map-legend-label">{item.label}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-component: handles pin-drop mode click events
// ---------------------------------------------------------------------------

function PinDropHandler({
  active,
  onDrop,
}: {
  active: boolean;
  onDrop: (latlng: L.LatLng) => void;
}) {
  const map = useMap();

  useEffect(() => {
    if (!active) {
      map.getContainer().style.cursor = "";
      return;
    }
    map.getContainer().style.cursor = "crosshair";
    const handler = (e: L.LeafletMouseEvent) => onDrop(e.latlng);
    map.on("click", handler);
    return () => {
      map.off("click", handler);
      map.getContainer().style.cursor = "";
    };
  }, [map, active, onDrop]);

  return null;
}

// ---------------------------------------------------------------------------
// Sub-component: handles polygon drawing via leaflet-geoman
// ---------------------------------------------------------------------------

function PolygonDrawHandler({
  active,
  onComplete,
}: {
  active: boolean;
  onComplete: (coords: number[][][]) => void;
}) {
  const map = useMap();

  useEffect(() => {
    if (!active) {
      if ((map as any).pm?.globalDrawModeEnabled()) {
        map.pm.disableDraw();
      }
      return;
    }

    // Hide the geoman toolbar — we use our own buttons
    map.pm.addControls({ position: "topleft" });
    map.pm.removeControls();

    map.pm.enableDraw("Polygon", {
      snappable: true,
      snapDistance: 20,
      templineStyle: { color: "#3b82f6", weight: 2, dashArray: "5 5" },
      hintlineStyle: { color: "#3b82f6", weight: 2, dashArray: "5 5" },
      pathOptions: { color: "#3b82f6", fillColor: "#3b82f6", fillOpacity: 0.15, weight: 2 },
    });

    const handler = (e: any) => {
      const layer = e.layer as L.Polygon;
      const coords = (layer.toGeoJSON() as GeoJSON.Feature<GeoJSON.Polygon>).geometry.coordinates;
      // Remove the temporary drawn layer — we render our own
      map.removeLayer(layer);
      onComplete(coords);
    };
    map.on("pm:create", handler);

    return () => {
      map.off("pm:create", handler);
      if ((map as any).pm?.globalDrawModeEnabled()) {
        map.pm.disableDraw();
      }
    };
  }, [map, active, onComplete]);

  return null;
}

// ---------------------------------------------------------------------------
// Sub-component: renders annotations (pins and polygons) on the map
// ---------------------------------------------------------------------------

const PIN_ICON = L.divIcon({
  className: "annotation-pin-icon",
  html: `<svg width="24" height="36" viewBox="0 0 24 36" xmlns="http://www.w3.org/2000/svg">
    <path d="M12 0C5.4 0 0 5.4 0 12c0 9 12 24 12 24s12-15 12-24C24 5.4 18.6 0 12 0z" fill="#ef4444"/>
    <circle cx="12" cy="11" r="5" fill="white"/>
  </svg>`,
  iconSize: [24, 36],
  iconAnchor: [12, 36],
  popupAnchor: [0, -36],
});

function AnnotationLayers({
  annotations,
}: {
  annotations: MapAnnotation[];
}) {
  const map = useMap();
  const layersRef = useRef<L.Layer[]>([]);

  useEffect(() => {
    // Clear previous
    for (const l of layersRef.current) map.removeLayer(l);
    layersRef.current = [];

    for (const a of annotations) {
      if (a.type === "pin" && a.geometry.type === "Point") {
        const [lng, lat] = a.geometry.coordinates;
        const marker = L.marker([lat, lng], { icon: PIN_ICON })
          .bindTooltip(a.label, { permanent: true, direction: "top", offset: [0, -36], className: "annotation-tooltip" })
          .addTo(map);
        layersRef.current.push(marker);
      } else if (a.type === "polygon" && a.geometry.type === "Polygon") {
        const coords = a.geometry.coordinates[0].map(([lng, lat]) => [lat, lng] as [number, number]);
        const polygon = L.polygon(coords, {
          color: "#3b82f6",
          fillColor: "#3b82f6",
          fillOpacity: 0.15,
          weight: 2,
        })
          .bindTooltip(a.label, { permanent: true, direction: "center", className: "annotation-tooltip" })
          .addTo(map);
        layersRef.current.push(polygon);
      }
    }

    return () => {
      for (const l of layersRef.current) map.removeLayer(l);
      layersRef.current = [];
    };
  }, [map, annotations]);

  return null;
}

// ---------------------------------------------------------------------------
// Basemap options
// ---------------------------------------------------------------------------

const BASEMAPS = {
  street: {
    label: "Street",
    url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>',
  },
  satellite: {
    label: "Satellite",
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attribution: '&copy; <a href="https://www.esri.com">Esri</a>',
  },
  topo: {
    label: "Topographic",
    url: "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
    attribution: '&copy; <a href="https://opentopomap.org">OpenTopoMap</a>',
  },
} as const;

type BasemapKey = keyof typeof BASEMAPS;

// ---------------------------------------------------------------------------
// Main MapViewer component
// ---------------------------------------------------------------------------

interface Props {
  mapState: MapState | null;
  basemap: string;
  onBasemapChange: (key: string) => void;
  annotations: MapAnnotation[];
  drawingMode: DrawingMode;
  onDrawingModeChange: (mode: DrawingMode) => void;
  onAddAnnotation: (type: "pin" | "polygon", geometry: GeoJSON.Point | GeoJSON.Polygon) => void;
  onRemoveAnnotation: (id: string) => void;
  onClearAnnotations: () => void;
  captureRequest?: { requestId: string; center: [number, number]; zoom: number } | null;
  onCaptureComplete?: (requestId: string, pngBlob: Blob) => void;
  onViewportChange?: (center: [number, number], zoom: number) => void;
}

const DEFAULT_CENTER: LatLngExpression = [39.8283, -98.5795]; // center of US
const DEFAULT_ZOOM = 5;

export default function MapViewer({ mapState, basemap, onBasemapChange, annotations, drawingMode, onDrawingModeChange, onAddAnnotation, onRemoveAnnotation, onClearAnnotations, captureRequest, onCaptureComplete, onViewportChange }: Props) {
  const layers = mapState?.layers ?? [];
  const basemapKey = (basemap in BASEMAPS ? basemap : "street") as BasemapKey;
  const showScaleBar = mapState?.scale_bar !== false;

  // Track visibility per layer (agent can set defaults, user can toggle)
  const [visibility, setVisibility] = useState<Record<string, boolean>>({});

  // Sync agent-provided visibility defaults
  useEffect(() => {
    if (!layers.length) return;
    setVisibility((prev) => {
      const next = { ...prev };
      for (const l of layers) {
        if (!(l.id in next)) {
          next[l.id] = l.visible !== false;
        }
      }
      return next;
    });
  }, [layers]);

  const toggleLayer = useCallback((id: string) => {
    setVisibility((prev) => ({ ...prev, [id]: !prev[id] }));
  }, []);

  const [datasetsOpen, setDatasetsOpen] = useState(false);

  // --- Annotation callbacks (stable refs) ---
  const handlePinDrop = useCallback((latlng: L.LatLng) => {
    const geom: GeoJSON.Point = { type: "Point", coordinates: [latlng.lng, latlng.lat] };
    onAddAnnotation("pin", geom);
  }, [onAddAnnotation]);

  const handlePolygonComplete = useCallback((coords: number[][][]) => {
    const geom: GeoJSON.Polygon = { type: "Polygon", coordinates: coords };
    onAddAnnotation("polygon", geom);
  }, [onAddAnnotation]);

  // Esc key to cancel drawing
  useEffect(() => {
    if (!drawingMode) return;
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onDrawingModeChange(null); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [drawingMode, onDrawingModeChange]);

  const center: LatLngExpression = mapState?.view?.center ?? DEFAULT_CENTER;
  const zoom = mapState?.view?.zoom ?? DEFAULT_ZOOM;

  // Sort layers by z_index for rendering order
  const sortedLayers = useMemo(() => {
    return [...layers].sort((a, b) => (a.z_index ?? 0) - (b.z_index ?? 0));
  }, [layers]);

  return (
    <div className="map-panel">
      {/* Layer controls toolbar (only when layers exist) */}
      {layers.length > 0 && (
        <div className="map-toolbar">
          <div className="layer-control">
            {layers.map((l) => (
              <label key={l.id} className="layer-toggle">
                <input
                  type="checkbox"
                  checked={visibility[l.id] !== false}
                  onChange={() => toggleLayer(l.id)}
                />
                <span
                  className="layer-swatch"
                  style={{ background: l.style?.color ?? "#3388ff" }}
                />
                {l.name}
              </label>
            ))}
          </div>
        </div>
      )}

      {/* Datasets info */}
      {layers.length > 0 && (
        <div className="datasets-panel">
          <button
            className={`datasets-toggle${datasetsOpen ? " datasets-toggle--open" : ""}`}
            onClick={() => setDatasetsOpen((o) => !o)}
          >
            <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
              <path d="M9 6l6 6-6 6" />
            </svg>
            Datasets ({layers.length})
          </button>
          {datasetsOpen && (
            <div className="datasets-list">
              {layers.map((l) => (
                <div key={l.id} className="dataset-item">
                  <span
                    className="layer-swatch"
                    style={{ background: l.style?.color ?? "#3388ff" }}
                  />
                  <div className="dataset-info">
                    <strong>{l.name}</strong>
                    {l.source && <div className="dataset-source">{l.source}</div>}
                    {l.description && <div className="dataset-desc">{l.description}</div>}
                    {l.feature_count != null && (
                      <div className="dataset-count">{l.feature_count} features</div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Map */}
      <div className="map-container">
        {/* Basemap sidebar */}
        <div className="basemap-sidebar">
          {(Object.keys(BASEMAPS) as BasemapKey[]).map((key) => (
            <button
              key={key}
              className={`basemap-btn${basemapKey === key ? " basemap-btn--active" : ""}`}
              onClick={() => onBasemapChange(key)}
              title={BASEMAPS[key].label}
            >
              {BASEMAPS[key].label}
            </button>
          ))}
        </div>

        {/* Legend */}
        <LegendPanel layers={layers} visibility={visibility} />

        {/* Drawing toolbar */}
        <div className="drawing-toolbar">
          <button
            className={`drawing-btn${drawingMode === "pin" ? " drawing-btn--active" : ""}`}
            onClick={() => onDrawingModeChange(drawingMode === "pin" ? null : "pin")}
            title="Drop a pin on the map"
          >
            <svg viewBox="0 0 24 36" width="16" height="24">
              <path d="M12 0C5.4 0 0 5.4 0 12c0 9 12 24 12 24s12-15 12-24C24 5.4 18.6 0 12 0z" fill="currentColor"/>
              <circle cx="12" cy="11" r="5" fill="white"/>
            </svg>
            <span>Pin</span>
          </button>
          <button
            className={`drawing-btn${drawingMode === "polygon" ? " drawing-btn--active" : ""}`}
            onClick={() => onDrawingModeChange(drawingMode === "polygon" ? null : "polygon")}
            title="Draw a polygon on the map"
          >
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2">
              <polygon points="4,20 12,4 20,20" />
            </svg>
            <span>Polygon</span>
          </button>
          {annotations.length > 0 && (
            <button
              className="drawing-btn drawing-btn--clear"
              onClick={onClearAnnotations}
              title="Clear all annotations"
            >
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
              <span>Clear</span>
            </button>
          )}
        </div>

        {/* Drawing mode hint */}
        {drawingMode && (
          <div className="drawing-hint">
            {drawingMode === "pin"
              ? "Click on the map to drop a pin. Press Esc to cancel."
              : "Click to add vertices. Double-click to finish. Press Esc to cancel."}
          </div>
        )}

        {/* Annotation list */}
        {annotations.length > 0 && (
          <div className="annotation-list">
            {annotations.map((a) => (
              <div key={a.id} className="annotation-item">
                <span className="annotation-icon">{a.type === "pin" ? "\uD83D\uDCCD" : "\u2B1F"}</span>
                <span className="annotation-label">{a.label}</span>
                <span className="annotation-coords">
                  {a.type === "pin" && a.geometry.type === "Point"
                    ? `${a.geometry.coordinates[1].toFixed(4)}, ${a.geometry.coordinates[0].toFixed(4)}`
                    : `${(a.geometry as GeoJSON.Polygon).coordinates[0].length - 1} vertices`}
                </span>
                <button
                  className="annotation-remove"
                  onClick={() => onRemoveAnnotation(a.id)}
                  title="Remove annotation"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}

        <MapContainer
          center={center}
          zoom={zoom}
          style={{ width: "100%", height: "100%" }}
          scrollWheelZoom
        >
          <TileLayer
            key={basemapKey}
            attribution={BASEMAPS[basemapKey].attribution}
            url={BASEMAPS[basemapKey].url}
          />
          <ViewUpdater center={center} zoom={zoom} onViewportChange={onViewportChange} />
          <ScaleBar show={showScaleBar} />
          <CaptureHandler request={captureRequest} onComplete={onCaptureComplete} />
          <PinDropHandler active={drawingMode === "pin"} onDrop={handlePinDrop} />
          <PolygonDrawHandler active={drawingMode === "polygon"} onComplete={handlePolygonComplete} />
          <AnnotationLayers annotations={annotations} />
          {sortedLayers.map((l) =>
            l.type === "raster" ? (
              visibility[l.id] !== false && (
                <RasterTileLayer key={l.id} layer={l} />
              )
            ) : (
              <GeoJSONLayer
                key={`${l.id}-${l.geojson_file ?? ''}`}
                layer={l}
                visible={visibility[l.id] !== false}
              />
            )
          )}
        </MapContainer>
      </div>

      {layers.length === 0 && !annotations.length && (
        <div className="map-overlay-hint">
          <div className="hint-icon">
            <svg viewBox="0 0 24 24" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
              <line x1="3" y1="9" x2="21" y2="9" />
              <line x1="9" y1="21" x2="9" y2="9" />
            </svg>
          </div>
          <span className="hint-text">No layers loaded</span>
        </div>
      )}
    </div>
  );
}
