"""
Geospatial Buffer Process — OGC API Processes implementation.

Buffers any GeoJSON geometry (Point, LineString, Polygon) by a given distance
in metres. The critical implementation detail: shapely operates in the
coordinate space of the input geometry, so buffering WGS84 coordinates
directly would produce a ~5000-degree polygon instead of a 5000-metre one.
The fix is to project to a metric CRS (UTM), buffer, then reproject to WGS84.

UTM zone is auto-selected from the geometry's centroid so the process works
correctly anywhere on Earth without the caller specifying a CRS.
"""

import logging
from typing import Any, Optional, Tuple

from pyproj import CRS, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform as shapely_transform

from pygeoapi.process.base import BaseProcessor, ProcessorExecuteError

LOGGER = logging.getLogger(__name__)

PROCESS_METADATA = {
    "version": "1.0.0",
    "id": "geospatial-buffer",
    "title": {
        "en": "Geospatial Buffer"
    },
    "description": {
        "en": (
            "Creates a buffer polygon around an input GeoJSON geometry at a "
            "specified distance in metres. The process automatically selects "
            "an appropriate UTM coordinate reference system based on the "
            "geometry's centroid, ensuring metric accuracy regardless of "
            "geographic location. Supports Point, LineString, and Polygon "
            "inputs. Output is a GeoJSON Feature in WGS84 (EPSG:4326)."
        )
    },
    "jobControlOptions": ["sync-execute", "async-execute"],
    "outputTransmission": ["value"],
    "keywords": ["buffer", "geometry", "GIS", "OGC", "geospatial"],
    "links": [
        {
            "type": "text/html",
            "rel": "about",
            "title": "OGC API Processes specification",
            "href": "https://docs.ogc.org/is/18-062r2/18-062r2.html",
            "hreflang": "en-US",
        }
    ],
    "inputs": {
        "geometry": {
            "title": "Input Geometry",
            "description": (
                "A GeoJSON geometry object (Point, LineString, Polygon, or "
                "their Multi equivalents) in WGS84 (EPSG:4326). "
                "Coordinates must be [longitude, latitude] per RFC 7946."
            ),
            "schema": {
                "type": "object",
                "contentMediaType": "application/geo+json",
            },
            "minOccurs": 1,
            "maxOccurs": 1,
        },
        "distance": {
            "title": "Buffer Distance (metres)",
            "description": (
                "The buffer radius or width in metres. Must be a positive "
                "number. For example, 1000 creates a 1 km buffer zone."
            ),
            "schema": {
                "type": "number",
                "exclusiveMinimum": 0,
            },
            "minOccurs": 1,
            "maxOccurs": 1,
        },
        "segments": {
            "title": "Circle Approximation Segments",
            "description": (
                "Number of line segments used to approximate a quarter circle "
                "when buffering point geometries. Higher values produce smoother "
                "polygons at the cost of more vertices. Default: 32."
            ),
            "schema": {
                "type": "integer",
                "minimum": 4,
                "maximum": 128,
                "default": 32,
            },
            "minOccurs": 0,
            "maxOccurs": 1,
        },
    },
    "outputs": {
        "buffered_geometry": {
            "title": "Buffered Geometry",
            "description": (
                "A GeoJSON Feature whose geometry is the buffer polygon around "
                "the input geometry. Coordinates are in WGS84 (EPSG:4326). "
                "Feature properties include the input geometry type, buffer "
                "distance, and the UTM CRS used internally."
            ),
            "schema": {
                "type": "object",
                "contentMediaType": "application/geo+json",
            },
        },
        "metadata": {
            "title": "Process Metadata",
            "description": (
                "Diagnostic information about the buffer operation: UTM EPSG "
                "code selected, output vertex count, and confirmed CRS."
            ),
            "schema": {
                "type": "object",
                "contentMediaType": "application/json",
            },
        },
    },
    "example": {
        "inputs": {
            "geometry": {
                "type": "Point",
                "coordinates": [3.8792, 6.9271],  # Lagos, Nigeria
            },
            "distance": 5000,
            "segments": 32,
        }
    },
}


class GeospatialBufferProcessor(BaseProcessor):
    """
    Buffers a GeoJSON geometry by a metric distance using UTM projection.

    Execution flow:
      1. Validate inputs.
      2. Parse GeoJSON geometry to a shapely object.
      3. Auto-select the best-fit UTM zone from the geometry's centroid.
      4. Project WGS84 → UTM (metric), buffer in metres, reproject → WGS84.
      5. Return a GeoJSON Feature + metadata dict.
    """

    def __init__(self, processor_def: dict) -> None:
        super().__init__(processor_def, PROCESS_METADATA)
        self.supports_outputs = True

    def execute(
        self, data: dict, outputs: Optional[dict] = None
    ) -> Tuple[str, Any]:

        geometry_input = data.get("geometry")
        distance = data.get("distance")
        segments = int(data.get("segments", 32))

        # --- Input validation ---
        if geometry_input is None:
            raise ProcessorExecuteError("'geometry' input is required.")
        if distance is None:
            raise ProcessorExecuteError("'distance' input is required.")
        try:
            distance = float(distance)
        except (TypeError, ValueError):
            raise ProcessorExecuteError("'distance' must be a numeric value.")
        if distance <= 0:
            raise ProcessorExecuteError("'distance' must be greater than zero.")
        if not (4 <= segments <= 128):
            raise ProcessorExecuteError("'segments' must be between 4 and 128.")

        allowed_types = {
            "Point", "LineString", "Polygon",
            "MultiPoint", "MultiLineString", "MultiPolygon",
        }
        geom_type = geometry_input.get("type", "")
        if geom_type not in allowed_types:
            raise ProcessorExecuteError(
                f"Unsupported geometry type '{geom_type}'. "
                f"Supported: {sorted(allowed_types)}"
            )

        # --- Execute buffer ---
        try:
            buffered, utm_epsg = self._buffer_geometry(
                geometry_input, distance, segments
            )
        except ProcessorExecuteError:
            raise
        except Exception as exc:
            LOGGER.exception("Buffer operation failed")
            raise ProcessorExecuteError(f"Buffer failed: {exc}") from exc

        # Vertex count only makes sense for simple polygons
        vertex_count = (
            len(list(buffered.exterior.coords))
            if hasattr(buffered, "exterior")
            else None
        )

        result = {
            "buffered_geometry": {
                "type": "Feature",
                "geometry": mapping(buffered),
                "properties": {
                    "input_geometry_type": geom_type,
                    "buffer_distance_m": distance,
                    "utm_crs_epsg": utm_epsg,
                },
            },
            "metadata": {
                "input_geometry_type": geom_type,
                "buffer_distance_metres": distance,
                "segments_per_quarter_circle": segments,
                "utm_epsg_used": utm_epsg,
                "output_crs": "EPSG:4326 (WGS84)",
                "output_vertex_count": vertex_count,
            },
        }

        return "application/json", result

    def _buffer_geometry(
        self, geojson_geom: dict, distance_m: float, segments: int
    ) -> Tuple[Any, int]:
        """
        Project the geometry to UTM, apply the buffer, reproject to WGS84.

        Using UTM ensures the buffer distance is in metres and the result is
        geometrically accurate. The UTM zone is chosen from the geometry's
        centroid to minimise projection distortion.

        Args:
            geojson_geom: A GeoJSON geometry dict in WGS84.
            distance_m:   Buffer distance in metres (must be > 0).
            segments:     Segments per quarter circle for curved approximation.

        Returns:
            (buffered_shapely_geometry, utm_epsg_code)
        """
        geom = shape(geojson_geom)
        centroid = geom.centroid
        utm_epsg = self._utm_epsg_for_point(centroid.x, centroid.y)

        LOGGER.info(
            "Buffering %s by %.1f m using EPSG:%d",
            geom.geom_type, distance_m, utm_epsg,
        )

        wgs84 = CRS.from_epsg(4326)
        utm = CRS.from_epsg(utm_epsg)

        # always_xy=True forces (longitude, latitude) coordinate order,
        # overriding pyproj's default behaviour of honouring the CRS axis
        # order (which for EPSG:4326 is latitude-first).
        to_utm = Transformer.from_crs(wgs84, utm, always_xy=True)
        to_wgs84 = Transformer.from_crs(utm, wgs84, always_xy=True)

        projected = shapely_transform(to_utm.transform, geom)
        buffered_utm = projected.buffer(distance_m, quad_segs=segments)
        buffered_wgs84 = shapely_transform(to_wgs84.transform, buffered_utm)

        return buffered_wgs84, utm_epsg

    @staticmethod
    def _utm_epsg_for_point(lon: float, lat: float) -> int:
        """
        Return the EPSG code of the UTM zone covering the given WGS84 point.

        UTM divides the world into 60 zones, each 6° of longitude wide,
        numbered 1–60 eastward from 180°W. The zone number is:
            zone = floor((lon + 180) / 6) + 1

        Northern hemisphere → EPSG 32601–32660
        Southern hemisphere → EPSG 32701–32760

        Args:
            lon: Longitude in decimal degrees (−180 to 180).
            lat: Latitude in decimal degrees (−90 to 90).

        Returns:
            EPSG integer code.
        """
        zone = int((lon + 180.0) / 6.0) + 1
        zone = min(zone, 60)  # handle lon == 180 edge case
        return 32600 + zone if lat >= 0.0 else 32700 + zone

    def __repr__(self) -> str:
        return f"<GeospatialBufferProcessor> {self.name}"
