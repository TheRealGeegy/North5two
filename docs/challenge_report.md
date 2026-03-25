# OGC API Processes — Code Challenge Report

**Applicant:** Nsiadu Uchenna
**Project:** Developing Multi Context Protocols for the suite of OGC APIs (52°North, GSoC 2026)
**Date:** March 25, 2026

---

## Setup

I ran everything in GitHub Codespaces. Docker is available there out of the box, which made this straightforward to reproduce without any local setup.

**Versions:**

| Component | Version |
|-----------|---------|
| pygeoapi | 0.24.dev0 |
| Python | 3.12 (container venv) |
| shapely | 2.0.3 |
| pyproj | 3.6.1 |
| Docker | 28.5.1 |

The `docker-compose.yml` maps host port 5000 to container port 80 — not 5000 to 5000. That tripped me up initially. The pygeoapi image runs gunicorn on port 80 internally; the config's `server.bind.port: 80` refers to that internal binding, not what you curl from the host.

```yaml
services:
  pygeoapi:
    image: geopython/pygeoapi:latest
    ports:
      - "5000:80"
    volumes:
      - ./pygeoapi.config.yml:/pygeoapi/local.config.yml:ro
      - ./processes:/pygeoapi/processes:ro
    environment:
      - PYGEOAPI_CONFIG=/pygeoapi/local.config.yml
      - PYGEOAPI_OPENAPI=/pygeoapi/local.openapi.yml
      - PYTHONPATH=/pygeoapi
```

The `PYTHONPATH=/pygeoapi` line was the other thing I had to figure out. The image uses a virtualenv at `/venv`, and that venv's Python path doesn't include `/pygeoapi` by default. Without it the container would restart-loop on startup because it couldn't import the process class when generating the OpenAPI spec. Setting `PYTHONPATH` is cleaner than installing the module into the venv — no image rebuild needed.

I also needed the `manager` block in the config:

```yaml
server:
  manager:
    name: TinyDB
    connection: /tmp/pygeoapi-process-manager.db
    output_dir: /tmp/
```

Without a manager, `GET /jobs/{id}` returns 404 for everything. TinyDB is fine for a demo; a production setup would use PostgreSQL or something that survives container restarts.

---

## The Buffer Process

I implemented a geospatial buffer — given a GeoJSON geometry and a distance in metres, return a polygon covering everything within that distance of the input.

It's the right process to demonstrate for this challenge because:
- It has non-trivial inputs and outputs, so it actually exercises the OGC schema design
- It requires understanding CRS, which is the interesting part of geospatial work
- The result is easy to verify visually — paste the output into geojson.io and check it looks like a circle

### The CRS problem

This is the thing most people get wrong when they first write a buffer operation.

`shapely.buffer(5000)` buffers by 5000 *in whatever units the coordinates are in*. GeoJSON uses WGS84 (EPSG:4326), where coordinates are in degrees. So `point.buffer(5000)` produces a polygon with a radius of 5000 degrees — something like a third of the Earth's surface. Obviously wrong.

The fix is to project to a metric CRS first, buffer in metres, then reproject back:

```
WGS84 (degrees) → UTM (metres) → buffer → WGS84 (degrees)
```

I auto-select the UTM zone from the geometry's centroid using:

```python
zone = int((lon + 180.0) / 6.0) + 1
epsg = 32600 + zone if lat >= 0 else 32700 + zone
```

UTM divides the world into 60 zones of 6° each, numbered eastward from 180°W. For Lagos at 3.88°E: zone 31, northern hemisphere → EPSG:32631. The process logs which EPSG it picked, so you can verify.

One more thing: pyproj 3.x respects the axis order defined by each CRS. For EPSG:4326 that's (latitude, longitude), not the (longitude, latitude) order GeoJSON uses. If you don't pass `always_xy=True` to `Transformer.from_crs()`, coordinates get silently swapped and your buffer ends up somewhere in the ocean. I spent about 20 minutes on that.

```python
to_utm = Transformer.from_crs(wgs84, utm, always_xy=True)
```

### Process schema

The process implements the pygeoapi `BaseProcessor` interface. `PROCESS_METADATA` at the top of the module defines the OGC-compliant description — title, inputs, outputs, and `jobControlOptions`. I declared both `sync-execute` and `async-execute` since there's no reason to restrict it.

Inputs: `geometry` (GeoJSON, required), `distance` (metres, required), `segments` (int 4–128, optional, controls polygon smoothness).

Outputs: `buffered_geometry` (GeoJSON Feature) and `metadata` (UTM zone used, vertex count, etc.). I kept them separate so a caller can take just the geometry without parsing a compound response.

---

## Job Lifecycle

### GET /processes

```bash
curl -s http://localhost:5000/processes | python3 -m json.tool
```

```json
{
    "processes": [
        {
            "version": "1.0.0",
            "id": "geospatial-buffer",
            "title": "Geospatial Buffer",
            "jobControlOptions": ["sync-execute", "async-execute"],
            "links": [
                {
                    "rel": "http://www.opengis.net/def/rel/ogc/1.0/execute",
                    "href": "http://localhost:5000/processes/geospatial-buffer/execution?f=json"
                }
            ]
        }
    ]
}
```

### POST /execution (async)

```bash
curl -s -i \
  -X POST http://localhost:5000/processes/geospatial-buffer/execution \
  -H "Content-Type: application/json" \
  -H "Prefer: respond-async" \
  -d '{
    "inputs": {
      "geometry": {"type": "Point", "coordinates": [3.8792, 6.9271]},
      "distance": 5000
    }
  }'
```

```
HTTP/1.1 201 CREATED
Preference-Applied: respond-async
Location: http://localhost:5000/jobs/dfb6d510-27e3-11f1-af5d-5b544c6a3117

{"jobID":"dfb6d510-27e3-11f1-af5d-5b544c6a3117","type":"process","status":"accepted"}
```

The spec (OGC 18-062r2 §7.11.2) says async job creation returns 201, not 200. The `Location` header is important — you follow it to check status. `Preference-Applied: respond-async` confirms the server accepted the async preference (RFC 7240 §2).

### GET /jobs/{jobId}

```bash
curl -s http://localhost:5000/jobs/dfb6d510-27e3-11f1-af5d-5b544c6a3117
```

```json
{
    "jobID": "dfb6d510-27e3-11f1-af5d-5b544c6a3117",
    "processID": "geospatial-buffer",
    "status": "successful",
    "progress": 100,
    "created": "2026-03-25T00:45:08.958653Z",
    "started": "2026-03-25T00:45:08.958672Z",
    "finished": "2026-03-25T00:45:08.970551Z",
    "links": [
        {
            "rel": "http://www.opengis.net/def/rel/ogc/1.0/results",
            "href": "http://localhost:5000/jobs/dfb6d510-27e3-11f1-af5d-5b544c6a3117/results?f=json"
        }
    ]
}
```

The spec defines five terminal states: `accepted`, `running`, `successful`, `failed`, `dismissed`. This one finished in ~12ms (in-process execution, no queue). For a real long-running computation the polling loop matters.

### GET /jobs/{jobId}/results

```bash
curl -s "http://localhost:5000/jobs/dfb6d510-27e3-11f1-af5d-5b544c6a3117/results?f=json"
```

```json
{
    "buffered_geometry": {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [3.924456295137893, 6.927014142661214],
                    [3.9243974617178496, 6.924795210282714],
                    "... 126 more coordinate pairs ...",
                    [3.924456295137893, 6.927014142661214]
                ]
            ]
        },
        "properties": {
            "input_geometry_type": "Point",
            "buffer_distance_m": 5000.0,
            "utm_crs_epsg": 32631
        }
    },
    "metadata": {
        "buffer_distance_metres": 5000.0,
        "utm_epsg_used": 32631,
        "output_crs": "EPSG:4326 (WGS84)",
        "output_vertex_count": 129
    }
}
```

EPSG:32631 is UTM Zone 31N, which is correct for Lagos. The polygon has 129 vertices (32 segments per quarter circle × 4 + closing vertex). Geodesic area works out to ~78.51 km², versus the expected π×5² = 78.54 km² — 0.04% off.

### Sync execution for comparison

```bash
curl -s -i \
  -X POST http://localhost:5000/processes/geospatial-buffer/execution \
  -H "Content-Type: application/json" \
  -H "Prefer: respond-sync" \
  -d '{"inputs": {"geometry": {"type": "Point", "coordinates": [3.8792, 6.9271]}, "distance": 5000}}'
```

```
HTTP/1.1 200 OK
Preference-Applied: wait
Location: http://localhost:5000/jobs/083ef4ad-27e4-11f1-a72e-dbca65260b8b
```

Same result body, returned inline. Server still records the job (note the `Location` header) so you can retrieve it later. The `Prefer: respond-sync` maps to `Prefer: wait` in the server's response.

---

## Connection to the GSoC Project

The interesting thing about OGC API Processes from an MCP standpoint is how directly the two models correspond:

| OGC API Processes | MCP |
|---|---|
| `GET /processes` | `list_tools` |
| `GET /processes/{id}` (input/output schema) | Tool parameter schema |
| `POST /processes/{id}/execution` | `call_tool` |
| `GET /jobs/{id}` | Async result polling |
| `GET /jobs/{id}/results` | Tool return value |

Both are: named operation, typed inputs, structured output. The OGC side adds the async job layer on top.

This means an MCP server wrapping OGC API Processes is mostly a protocol translation problem, not a domain modelling problem. The process I/O schema maps directly to MCP tool parameters. The async job lifecycle gets hidden inside the tool call — the LLM submits inputs and gets back a result; it doesn't need to know about job IDs or polling.

The auto-discovery angle is what makes it useful beyond a one-process demo: at startup, call `GET /processes`, read each process description, and register it as an MCP tool. The same server works against any OGC API Processes endpoint without code changes.

That's what Phase 2 builds. This challenge is the foundation it runs on.
