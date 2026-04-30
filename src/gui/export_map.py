"""Export map as standalone HTML file with full Leaflet styling."""

import json
import logging
import html

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from .map_state import MAPS_DIR, read_map_state

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/export-map")
async def export_map(basemap: str = "street"):
    """Export the current map as a standalone HTML file with full styling."""
    state = read_map_state()
    if not state or not state.get("layers"):
        raise HTTPException(status_code=404, detail="No map to export")

    BASEMAP_TILES = {
        "street": {
            "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
            "attribution": "&copy; <a href='https://www.openstreetmap.org/copyright'>OSM</a>",
        },
        "satellite": {
            "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            "attribution": "&copy; <a href='https://www.esri.com'>Esri</a>",
        },
        "topo": {
            "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
            "attribution": "&copy; <a href='https://opentopomap.org'>OpenTopoMap</a>",
        },
    }
    tile_info = BASEMAP_TILES.get(basemap, BASEMAP_TILES["street"])

    view = state.get("view") or {"center": [39.8, -98.6], "zoom": 5}
    center = view["center"]
    zoom = view["zoom"]
    show_scale = state.get("scale_bar", True)

    layer_scripts = []
    layer_names = []
    legend_items_all = []

    for layer in state["layers"]:
        if layer.get("type") == "raster":
            continue
        geojson_file = layer.get("geojson_file")
        if not geojson_file:
            continue
        geojson_path = MAPS_DIR / geojson_file
        if not geojson_path.is_file():
            continue
        geojson_data = geojson_path.read_text(encoding="utf-8")
        try:
            geojson_data = json.dumps(json.loads(geojson_data)).replace("</", r"<\/")
        except json.JSONDecodeError:
            LOGGER.warning("Skipping layer with invalid GeoJSON: %s", geojson_file)
            continue
        style = layer.get("style", {})
        name = layer.get("name", layer["id"])
        popup_fields = layer.get("popup_fields", [])
        point_style = layer.get("point_style", "")
        visible = layer.get("visible", True)
        dash_array = style.get("dashArray", "")
        label_field = layer.get("label_field")
        label_style = layer.get("label_style", {})

        sbp = layer.get("style_by_property")
        rbp = layer.get("radius_by_property")
        color_field = layer.get("color_field") or (sbp["property"] if sbp and sbp.get("target") == "color" else "")
        fill_color_field = layer.get("fill_color_field") or (
            sbp["property"] if sbp and sbp.get("target") == "fillColor" else ""
        )
        radius_field = layer.get("radius_field") or (rbp["property"] if rbp else "")
        radius_min = rbp.get("min", 0) if rbp else 0
        radius_max = rbp.get("max", 100) if rbp else 100

        classify = layer.get("classify_by")
        classify_js = "null"
        if classify:
            classify_js = json.dumps(classify)

        layer_names.append((name, visible))

        popup_js = ""
        if popup_fields:
            parts = " + ".join(
                json.dumps(f"<b>{f}:</b> ") + f" + (props[{json.dumps(f)}] ?? '') + " + json.dumps("<br/>")
                for f in popup_fields
            )
            popup_js = f"layer.bindPopup({parts});"

        label_js = ""
        if label_field:
            font_size = float(label_style.get("fontSize", 11))
            color = html.escape(str(label_style.get("color", "#222")), quote=True)
            halo_color = html.escape(str(label_style.get("haloColor", "#fff")), quote=True)
            halo_width = float(label_style.get("haloWidth", 2))
            label_field_js = json.dumps(label_field)
            span_open = json.dumps(
                f'<span style="font-size:{font_size}px;color:{color};'
                f"text-shadow:{-halo_width}px 0 0 {halo_color},"
                f"{halo_width}px 0 0 {halo_color},"
                f"0 {-halo_width}px 0 {halo_color},"
                f'0 {halo_width}px 0 {halo_color}">'
            )
            span_close = json.dumps("</span>")
            label_js = f"""
              if (props[{label_field_js}] != null) {{
                layer.bindTooltip(
                  {span_open} + props[{label_field_js}] + {span_close},
                  {{permanent:true, direction:'center', className:'feature-label'}}
                );
              }}
            """

        style_fn = f"""
        function resolveStyle(props) {{
          var baseColor = {json.dumps(str(style.get("color", "#3388ff")))};
          var baseFill = {json.dumps(str(style.get("fillColor", style.get("color", "#3388ff"))))};
          var baseRadius = {style.get("radius", 6)};
          var classify = {classify_js};
          var color = baseColor, fill = baseFill, radius = baseRadius;
          if (classify) {{
            var val = props[classify.field];
            if (classify.method === 'categorical') {{
              var cats = classify.categories || [];
              var idx = cats.indexOf(String(val));
              if (idx >= 0 && idx < classify.colors.length) fill = classify.colors[idx];
            }} else {{
              var breaks = classify.breaks || [];
              var num = parseFloat(val);
              if (!isNaN(num)) {{
                var cls = breaks.length;
                for (var i = 0; i < breaks.length; i++) {{ if (num <= breaks[i]) {{ cls = i; break; }} }}
                if (cls < classify.colors.length) fill = classify.colors[cls];
              }}
            }}
            color = fill;
          }}
          if ({json.dumps(color_field)} && props[{json.dumps(color_field)}]) color = props[{json.dumps(color_field)}];
          if ({json.dumps(fill_color_field)} && props[{json.dumps(fill_color_field)}]) fill = props[{json.dumps(fill_color_field)}];
          if ({json.dumps(radius_field)} && props[{json.dumps(radius_field)}] != null) {{
            var r = parseFloat(props[{json.dumps(radius_field)}]);
            if (!isNaN(r)) radius = Math.max({radius_min}, Math.min({radius_max}, r));
          }}
          return {{color: color, fill: fill, radius: radius}};
        }}
        """

        if point_style == "circle":
            layer_scripts.append(f"""
(function() {{
  var data = {geojson_data};
  {style_fn}
  var lyr = L.geoJSON(data, {{
    pointToLayer: function(feature, latlng) {{
      var s = resolveStyle(feature.properties || {{}});
      return L.circleMarker(latlng, {{
        radius: s.radius, color: s.color, fillColor: s.fill,
        fillOpacity: {style.get("fillOpacity", 0.7)}, weight: {style.get("weight", 1)}, dashArray: {json.dumps(dash_array)}
      }});
    }},
    onEachFeature: function(feature, layer) {{
      var props = feature.properties || {{}};
      {popup_js}
      {label_js}
    }}
  }});
  {"lyr.addTo(map);" if visible else ""}
  overlays[{json.dumps(name)}] = lyr;
}})();
""")
        else:
            layer_scripts.append(f"""
(function() {{
  var data = {geojson_data};
  {style_fn}
  var lyr = L.geoJSON(data, {{
    style: function(feature) {{
      var s = resolveStyle(feature.properties || {{}});
      return {{
        color: s.color, weight: {style.get("weight", 2)}, opacity: {style.get("opacity", 1)},
        fillColor: s.fill, fillOpacity: {style.get("fillOpacity", 0.2)}, dashArray: {json.dumps(dash_array)}
      }};
    }},
    onEachFeature: function(feature, layer) {{
      var props = feature.properties || {{}};
      {popup_js}
      {label_js}
    }}
  }});
  {"lyr.addTo(map);" if visible else ""}
  overlays[{json.dumps(name)}] = lyr;
}})();
""")

        legend_data = layer.get("legend")
        if not legend_data and classify:
            colors = classify.get("colors", [])
            breaks = classify.get("breaks", [])
            cats = classify.get("categories", [])
            method = classify.get("method", "")
            items = []
            for i, c in enumerate(colors):
                if method == "categorical":
                    lbl = cats[i] if i < len(cats) else f"Class {i + 1}"
                elif i == 0 and breaks:
                    lbl = f"\u2264 {breaks[0]}"
                elif i >= len(breaks) and breaks:
                    lbl = f"> {breaks[-1]}"
                elif i > 0 and i <= len(breaks):
                    lbl = f"{breaks[i - 1]} \u2013 {breaks[i]}" if i < len(breaks) else f"> {breaks[-1]}"
                else:
                    lbl = f"Class {i + 1}"
                items.append({"label": lbl, "color": c})
            legend_data = {"title": classify.get("field", name), "items": items}

        if legend_data and legend_data.get("items"):
            legend_items_all.append({"name": name, "legend": legend_data})

    layers_js = "\n".join(layer_scripts)

    legend_html = ""
    if legend_items_all:
        sections = []
        for entry in legend_items_all:
            leg = entry["legend"]
            items_html = ""
            for item in leg["items"]:
                dash = item.get("dashArray", "")
                if dash:
                    color_attr = html.escape(str(item["color"]), quote=True)
                    dash_attr = html.escape(str(dash), quote=True)
                    swatch = f'<svg width="20" height="4" style="vertical-align:middle"><line x1="0" y1="2" x2="20" y2="2" stroke="{color_attr}" stroke-width="2" stroke-dasharray="{dash_attr}"/></svg>'
                else:
                    color_css = html.escape(str(item["color"]), quote=True)
                    swatch = f'<span style="display:inline-block;width:14px;height:10px;background:{color_css};border:1px solid rgba(0,0,0,0.1);border-radius:2px;vertical-align:middle"></span>'
                label_escaped = html.escape(str(item.get("label", "")), quote=True)
                items_html += f'<div style="display:flex;align-items:center;gap:5px;padding:1px 0;font-size:12px;color:#444">{swatch} {label_escaped}</div>'
            title_escaped = html.escape(str(leg.get("title", "")), quote=True)
            sections.append(
                f'<div style="margin-bottom:6px"><div style="font-weight:600;font-size:12px;margin-bottom:3px">{title_escaped}</div>{items_html}</div>'
            )
        legend_html = (
            '<div id="legend" style="position:absolute;bottom:30px;right:10px;z-index:1000;background:#fff;border:1px solid #ccc;border-radius:6px;padding:8px 10px;box-shadow:0 2px 8px rgba(0,0,0,0.15);max-height:50%;overflow-y:auto">'
            + "".join(sections)
            + "</div>"
        )

    layer_control_js = ""
    if layer_names:
        layer_control_js = "L.control.layers(null, overlays, {collapsed: false}).addTo(map);"

    scale_js = "L.control.scale({imperial:true,metric:true}).addTo(map);" if show_scale else ""

    tile_url = tile_info["url"]
    tile_attr = tile_info["attribution"].replace("'", "\\'")

    html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Agora Infrastructure Map</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.css"/>
<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.js"></script>
<style>
html,body{{margin:0;padding:0;width:100%;height:100%;font-family:sans-serif}}
#map{{width:100%;height:100%}}
.feature-label{{background:none!important;border:none!important;box-shadow:none!important;padding:0!important}}
.feature-label::before{{display:none!important}}
</style>
</head>
<body>
<div id="map"></div>
{legend_html}
<script>
var map = L.map('map').setView([{center[0]},{center[1]}],{zoom});
L.tileLayer('{tile_url}',{{
  attribution:'{tile_attr}'
}}).addTo(map);
var overlays = {{}};
{layers_js}
{layer_control_js}
{scale_js}
</script>
</body>
</html>"""

    return HTMLResponse(
        content=html_content,
        headers={"Content-Disposition": "attachment; filename=agora_map.html"},
    )
