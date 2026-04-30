/** Style properties for a GeoJSON layer. */
export interface LayerStyle {
  color?: string;
  weight?: number;
  opacity?: number;
  fillColor?: string;
  fillOpacity?: number;
  radius?: number;
  /** Stroke dash pattern. e.g. "5 5" (dashed), "1 5" (dotted), "10 5 1 5" (dash-dot). */
  dashArray?: string;
}

/** Map a feature property value to a style attribute. */
export interface StyleByProperty {
  property: string;
  target: "color" | "fillColor";
}

/** Read circle marker radius from a feature property. */
export interface RadiusByProperty {
  property: string;
  min?: number;
  max?: number;
}

/** Choropleth classification. */
export interface ClassifyBy {
  field: string;
  method: "equal_interval" | "quantile" | "manual" | "categorical";
  num_classes?: number;
  breaks?: number[];
  categories?: string[];
  colors: string[];
}

/** Text label styling. */
export interface LabelStyle {
  fontSize?: number;
  color?: string;
  haloColor?: string;
  haloWidth?: number;
  minZoom?: number;
}

/** Single legend entry. */
export interface LegendItem {
  label: string;
  color: string;
  radius?: number;
  dashArray?: string;
}

/** Legend displayed on the map for a layer. */
export interface Legend {
  title: string;
  items: LegendItem[];
}

/** A single layer in the map state. */
export interface MapLayer {
  id: string;
  name: string;
  type?: "vector" | "raster";
  geojson_file?: string;
  tif_file?: string;
  description?: string;
  source?: string;
  feature_count?: number;
  visible?: boolean;
  z_index?: number;

  style?: LayerStyle;
  point_style?: "marker" | "circle";
  popup_fields?: string[];

  /* Per-feature styling (flat shorthand) */
  color_field?: string;
  fill_color_field?: string;
  radius_field?: string;

  /* Per-feature styling (structured) */
  style_by_property?: StyleByProperty;
  radius_by_property?: RadiusByProperty;

  /* Choropleth classification */
  classify_by?: ClassifyBy;

  /* Labels */
  label_field?: string;
  label_style?: LabelStyle;

  /* Legend */
  legend?: Legend;

  /* Raster-specific */
  colormap?: string;
  value_range?: [number, number];
  opacity?: number;
}

/** The full map state written by the agent. */
export interface MapState {
  view: {
    center: [number, number];
    zoom: number;
  } | null;
  layers: MapLayer[];
  scale_bar?: boolean;
}

/** A user-placed annotation (pin or polygon) on the map. */
export interface MapAnnotation {
  id: string;
  label: string;
  type: "pin" | "polygon";
  geometry: GeoJSON.Point | GeoJSON.Polygon;
}

export type DrawingMode = "pin" | "polygon" | null;

/** A single step in a story map walkthrough. */
export interface StoryMapStep {
  title: string;
  narrative: string;
  latitude: number;
  longitude: number;
  zoom: number;
  highlight_layers?: string[];
}

/** A story map emitted by the agent's present_story_map tool. */
export interface StoryMap {
  story_id: string;
  title: string;
  steps: StoryMapStep[];
}
