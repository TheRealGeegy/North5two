"""
OGC API Processes — Automated Integration Test Suite
=====================================================
Tests the full lifecycle of the geospatial-buffer process against the live
pygeoapi instance at http://localhost:5000.

Run:
    python tests/test_ogc_lifecycle.py
"""

import json
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Optional

BASE_URL = "http://localhost:5000"
TIMEOUT = 10  # seconds per request
POLL_INTERVAL = 0.5
MAX_POLL_ATTEMPTS = 20

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

PASS = f"{GREEN}PASS{RESET}"
FAIL = f"{RED}FAIL{RESET}"
INFO = f"{CYAN}INFO{RESET}"


# ── Result tracking ───────────────────────────────────────────────────────────

@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""
    extra: dict = field(default_factory=dict)


results: list[TestResult] = []


def record(name: str, passed: bool, detail: str = "", **extra) -> TestResult:
    r = TestResult(name, passed, detail, extra)
    results.append(r)
    status = PASS if passed else FAIL
    print(f"  [{status}] {name}")
    if detail:
        prefix = f"       {DIM}"
        print(f"{prefix}{detail}{RESET}")
    if extra:
        for k, v in extra.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, indent=None, separators=(", ", ": "))
                if len(v) > 120:
                    v = v[:117] + "..."
            print(f"       {DIM}{k}: {v}{RESET}")
    return r


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def get(path: str, params: str = "") -> tuple[int, dict, dict]:
    url = f"{BASE_URL}{path}{'?f=json' if '?' not in path else '&f=json'}"
    if params:
        url += f"&{params}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read().decode())
        except Exception:
            pass
        return e.code, body, {}


def post(path: str, payload: dict, prefer: str = "respond-async") -> tuple[int, dict, dict]:
    url = f"{BASE_URL}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": prefer,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read().decode())
        except Exception:
            pass
        return e.code, body, {}


def poll_job(job_id: str) -> Optional[dict]:
    for _ in range(MAX_POLL_ATTEMPTS):
        _, body, _ = get(f"/jobs/{job_id}")
        status = body.get("status")
        if status in ("successful", "failed", "dismissed"):
            return body
        time.sleep(POLL_INTERVAL)
    return None


# ── Test cases ────────────────────────────────────────────────────────────────

def section(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'─' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'─' * 60}{RESET}")


def test_server_reachable() -> None:
    section("1. Server Health")
    try:
        status, body, _ = get("/")
        desc = body.get("description", "")
        title = desc.get("en", "") if isinstance(desc, dict) else str(desc)[:80]
        record(
            "Server responds on port 5000",
            status == 200,
            f"HTTP {status}",
            title=title or body.get("title", "—"),
        )
    except Exception as e:
        record("Server responds on port 5000", False, str(e))
        print(f"\n  {RED}Cannot reach server. Is Docker running?{RESET}")
        print(f"  {DIM}Run: docker compose up -d{RESET}\n")
        sys.exit(1)


def test_processes_endpoint() -> None:
    section("2. GET /processes")
    status, body, _ = get("/processes")

    record("Returns HTTP 200", status == 200, f"HTTP {status}")

    processes = body.get("processes", [])
    record(
        "Response contains 'processes' array",
        isinstance(processes, list),
        f"type={type(processes).__name__}",
    )

    ids = [p.get("id") for p in processes]
    record(
        "geospatial-buffer is registered",
        "geospatial-buffer" in ids,
        f"found: {ids}",
    )

    if processes:
        p = next((x for x in processes if x.get("id") == "geospatial-buffer"), processes[0])
        has_async = "async-execute" in p.get("jobControlOptions", [])
        has_sync  = "sync-execute"  in p.get("jobControlOptions", [])
        record(
            "Process declares async-execute support",
            has_async,
            f"jobControlOptions={p.get('jobControlOptions')}",
        )
        record(
            "Process declares sync-execute support",
            has_sync,
        )


def test_process_description() -> None:
    section("3. GET /processes/geospatial-buffer")
    status, body, _ = get("/processes/geospatial-buffer")

    record("Returns HTTP 200", status == 200, f"HTTP {status}")

    inputs = body.get("inputs", {})
    record(
        "Input 'geometry' is described",
        "geometry" in inputs,
    )
    record(
        "Input 'distance' is described",
        "distance" in inputs,
    )
    record(
        "Input 'segments' is described (optional)",
        "segments" in inputs,
    )

    outputs = body.get("outputs", {})
    record(
        "Output 'buffered_geometry' is described",
        "buffered_geometry" in outputs,
    )
    record(
        "Output 'metadata' is described",
        "metadata" in outputs,
    )


def test_async_job_lifecycle() -> tuple[Optional[str], Optional[dict]]:
    section("4. Async Job Lifecycle  (Prefer: respond-async)")

    payload = {
        "inputs": {
            "geometry": {
                "type": "Point",
                "coordinates": [3.8792, 6.9271],   # Lagos, Nigeria
            },
            "distance": 5000,
        }
    }

    status, body, headers = post(
        "/processes/geospatial-buffer/execution", payload, prefer="respond-async"
    )

    record("Submission returns HTTP 201 Created", status == 201, f"HTTP {status}")

    pref_applied = headers.get("Preference-Applied", headers.get("preference-applied", ""))
    record(
        "Server returns Preference-Applied: respond-async",
        "respond-async" in pref_applied.lower(),
        f"Preference-Applied: {pref_applied or '(missing)'}",
    )

    job_id = body.get("jobID") or body.get("id")
    location = headers.get("Location", headers.get("location", ""))
    record(
        "Response body contains jobID",
        bool(job_id),
        f"jobID={job_id}",
    )
    record(
        "Location header present",
        bool(location),
        f"Location: {location}",
    )

    initial_status = body.get("status")
    record(
        "Initial status is 'accepted'",
        initial_status == "accepted",
        f"status={initial_status}",
    )

    if not job_id:
        return None, None

    # Poll until terminal state
    print(f"\n       {DIM}Polling job {job_id} …{RESET}")
    final = poll_job(job_id)

    if final is None:
        record("Job reaches terminal state within timeout", False, "Timed out after polling")
        return job_id, None

    record(
        "Job reaches status 'successful'",
        final.get("status") == "successful",
        f"final status={final.get('status')}",
    )
    record(
        "Job record includes timestamps",
        bool(final.get("created") and final.get("finished")),
        f"created={final.get('created')}  finished={final.get('finished')}",
    )

    # Results contain hypermedia link
    links = final.get("links", [])
    result_links = [l for l in links if "results" in l.get("href", "")]
    record(
        "Job status response links to results endpoint",
        bool(result_links),
        f"results link: {result_links[0].get('href') if result_links else '(none)'}",
    )

    return job_id, final


def test_job_results(job_id: str) -> None:
    section("5. GET /jobs/{jobId}/results")

    status, body, _ = get(f"/jobs/{job_id}/results")
    record("Returns HTTP 200", status == 200, f"HTTP {status}")

    record(
        "Response contains 'buffered_geometry'",
        "buffered_geometry" in body,
    )
    record(
        "Response contains 'metadata'",
        "metadata" in body,
    )

    bg = body.get("buffered_geometry", {})
    geom = bg.get("geometry", {})

    record(
        "buffered_geometry is a GeoJSON Feature",
        bg.get("type") == "Feature",
        f"type={bg.get('type')}",
    )
    record(
        "Geometry type is Polygon",
        geom.get("type") == "Polygon",
        f"type={geom.get('type')}",
    )

    coords = geom.get("coordinates", [[]])[0]
    record(
        "Polygon has sufficient vertices (≥ 8)",
        len(coords) >= 8,
        f"vertex count={len(coords)}",
    )
    record(
        "Polygon is closed (first == last coordinate)",
        len(coords) >= 2 and coords[0] == coords[-1],
    )

    props = bg.get("properties", {})
    record(
        "Properties include utm_crs_epsg",
        "utm_crs_epsg" in props,
        f"utm_crs_epsg={props.get('utm_crs_epsg')}",
    )

    meta = body.get("metadata", {})
    record(
        "Metadata reports correct buffer distance",
        meta.get("buffer_distance_metres") == 5000.0,
        f"buffer_distance_metres={meta.get('buffer_distance_metres')}",
    )
    record(
        "Metadata confirms output CRS is WGS84",
        "4326" in str(meta.get("output_crs", "")),
        f"output_crs={meta.get('output_crs')}",
    )

    # Rough area check: π × 5² ≈ 78.54 km²
    # Coordinate span check: ~0.09° ≈ 10 km east-west for 5 km radius at equator
    if coords:
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        lon_span = max(lons) - min(lons)
        lat_span = max(lats) - min(lats)
        # At ~7°N, 1° lon ≈ 110.5 km × cos(7°) ≈ 109.7 km
        # 10 km span → ~0.091°
        reasonable = 0.07 < lon_span < 0.12 and 0.07 < lat_span < 0.12
        record(
            "Buffer polygon spans ~10 km (geometric sanity check)",
            reasonable,
            f"lon_span={lon_span:.4f}°  lat_span={lat_span:.4f}°  (expect ~0.09°)",
        )


def test_sync_execution() -> None:
    section("6. Synchronous Execution  (Prefer: respond-sync)")

    payload = {
        "inputs": {
            "geometry": {"type": "Point", "coordinates": [-0.1276, 51.5074]},  # London
            "distance": 2000,
            "segments": 16,
        }
    }

    status, body, headers = post(
        "/processes/geospatial-buffer/execution", payload, prefer="respond-sync"
    )

    record("Submission returns HTTP 200", status == 200, f"HTTP {status}")

    pref_applied = headers.get("Preference-Applied", headers.get("preference-applied", ""))
    record(
        "Server returns Preference-Applied: wait",
        "wait" in pref_applied.lower(),
        f"Preference-Applied: {pref_applied or '(missing)'}",
    )
    record(
        "Result body contains buffered_geometry inline",
        "buffered_geometry" in body,
    )

    meta = body.get("metadata", {})
    # London is in northern hemisphere, longitude −0.13° → UTM zone 30 → EPSG 32630
    record(
        "UTM zone auto-selected correctly for London (EPSG:32630)",
        meta.get("utm_epsg_used") == 32630,
        f"utm_epsg_used={meta.get('utm_epsg_used')}  (expected 32630)",
    )
    record(
        "Segments parameter honoured (16 segs → 65 vertices)",
        meta.get("output_vertex_count") == 65,
        f"output_vertex_count={meta.get('output_vertex_count')}  (expected 65 = 4×16+1)",
    )


def test_error_handling() -> None:
    section("7. Error Handling")

    # Missing required input
    status, body, _ = post(
        "/processes/geospatial-buffer/execution",
        {"inputs": {"distance": 1000}},
        prefer="respond-sync",
    )
    record(
        "Missing 'geometry' returns 4xx error",
        400 <= status < 500,
        f"HTTP {status}",
    )

    # Zero distance
    status, body, _ = post(
        "/processes/geospatial-buffer/execution",
        {"inputs": {
            "geometry": {"type": "Point", "coordinates": [0, 0]},
            "distance": 0,
        }},
        prefer="respond-sync",
    )
    record(
        "Distance = 0 returns 4xx error",
        400 <= status < 500,
        f"HTTP {status}",
    )

    # Invalid geometry type
    status, body, _ = post(
        "/processes/geospatial-buffer/execution",
        {"inputs": {
            "geometry": {"type": "FeatureCollection", "features": []},
            "distance": 100,
        }},
        prefer="respond-sync",
    )
    record(
        "Unsupported geometry type returns 4xx error",
        400 <= status < 500,
        f"HTTP {status}",
    )

    # Non-existent process
    status, _, _ = get("/processes/does-not-exist")
    record(
        "GET /processes/does-not-exist returns 404",
        status == 404,
        f"HTTP {status}",
    )

    # Non-existent job
    status, _, _ = get("/jobs/00000000-0000-0000-0000-000000000000")
    record(
        "GET /jobs/<invalid-id> returns 404",
        status == 404,
        f"HTTP {status}",
    )


def test_jobs_list() -> None:
    section("8. GET /jobs")
    status, body, _ = get("/jobs")
    record("Returns HTTP 200", status == 200, f"HTTP {status}")

    jobs = body.get("jobs", [])
    record(
        "Response contains 'jobs' array",
        isinstance(jobs, list),
    )
    record(
        "Jobs list includes previously submitted jobs",
        len(jobs) >= 1,
        f"job count={len(jobs)}",
    )


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary() -> int:
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total  = len(results)

    print(f"\n{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD}  Results: {GREEN}{passed} passed{RESET}  "
          f"{RED}{failed} failed{RESET}  "
          f"{DIM}({total} total){RESET}")
    print(f"{BOLD}{'═' * 60}{RESET}")

    if failed:
        print(f"\n{BOLD}{RED}  Failed tests:{RESET}")
        for r in results:
            if not r.passed:
                print(f"  {RED}✗{RESET} {r.name}")
                if r.detail:
                    print(f"    {DIM}{r.detail}{RESET}")
        print()

    return failed


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\n{BOLD}OGC API Processes — Integration Test Suite{RESET}")
    print(f"{DIM}Target: {BASE_URL}{RESET}")

    test_server_reachable()
    test_processes_endpoint()
    test_process_description()

    job_id, _ = test_async_job_lifecycle()

    if job_id:
        test_job_results(job_id)
    else:
        section("5. GET /jobs/{jobId}/results")
        record("Skipped — no job ID from previous step", False, "async job submission failed")

    test_sync_execution()
    test_error_handling()
    test_jobs_list()

    failed = print_summary()
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
