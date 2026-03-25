# OGC API Processes + MCP

GSoC 2026 application work for 52°North — project: *Developing Multi Context Protocols for the suite of OGC APIs*.

This repository contains the mandatory code challenge submission and the foundation for the GSoC project itself.

## What's here

| Path | What it is |
|------|-----------|
| `docker-compose.yml` | Runs pygeoapi on port 5000 |
| `pygeoapi.config.yml` | Server config — registers the buffer process, enables async job manager |
| `processes/buffer_process.py` | Custom OGC process: buffers any GeoJSON geometry by a distance in metres |
| `tests/test_ogc_lifecycle.py` | Integration tests covering the full job lifecycle |
| `docs/challenge_report.md` | Code challenge report (goes in the GSoC proposal) |

## Running it

Requires Docker.

```bash
docker compose up -d

# Verify it's up
curl http://localhost:5000/processes
```

## Running the tests

No dependencies to install — the test script uses only the Python standard library.

```bash
python tests/test_ogc_lifecycle.py
```

Expected output: 44 tests, all passing.

## The buffer process

Takes a GeoJSON geometry (Point, LineString, or Polygon) and a distance in metres, returns a buffer polygon in WGS84.

```bash
# Submit async job — Lagos, 5 km buffer
curl -X POST http://localhost:5000/processes/geospatial-buffer/execution \
  -H "Content-Type: application/json" \
  -H "Prefer: respond-async" \
  -d '{
    "inputs": {
      "geometry": {"type": "Point", "coordinates": [3.8792, 6.9271]},
      "distance": 5000
    }
  }'

# Check status
curl http://localhost:5000/jobs/{jobID}

# Get result
curl "http://localhost:5000/jobs/{jobID}/results?f=json"
```

The process auto-selects a UTM projection from the geometry's centroid so the buffer distance is accurate in metres regardless of where on Earth the input is.

## Project background

The GSoC project (if accepted) builds an MCP layer on top of any OGC API Processes endpoint — letting LLM agents discover and call geospatial operations via natural language, without the user needing to know about REST APIs or coordinate systems.

The core idea: `GET /processes` maps to MCP `list_tools`, `POST /execution` maps to `call_tool`, and the async job lifecycle is hidden inside the tool call. See `docs/challenge_report.md` for a fuller write-up.

## License

Apache 2.0
