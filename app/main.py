"""Industrial binary message parsing API."""

import re
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from . import database
from .analyzer import CompatibilityError, analyze_compatibility
from .encoder import EncodeError, encode_message
from .parser import ParseError, parse_message
from .regression import capture_snapshot, compare_snapshot
from .schemas import (
    CompatibilityRequest,
    DefinitionCreate,
    DetectRequest,
    EncodeRequest,
    FixtureCreate,
    FixtureRunRequest,
    ParseRequest,
    validate_definition,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="Industrial Binary Message Parser",
    description=(
        "Register versioned binary message definitions (bit widths, endianness, "
        "conditional branches, checksums) and parse hex payloads against them."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Make the service usable even if the lifespan hook is bypassed (e.g. tests).
database.init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/definitions", status_code=201)
def register_definition(defn: DefinitionCreate):
    """Register a new versioned message definition."""
    try:
        validate_definition(defn)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    record = database.create_definition(
        device_type=defn.device_type,
        version=defn.version,
        description=defn.description,
        fields=[f.model_dump(exclude_none=True) for f in defn.fields],
        checksum=defn.checksum.model_dump() if defn.checksum else None,
    )
    if record is None:
        raise HTTPException(
            status_code=409,
            detail=f"definition '{defn.device_type}' version '{defn.version}' already exists",
        )
    return record


@app.get("/api/v1/definitions")
def list_definitions(
    device_type: Optional[str] = Query(default=None),
    include_inactive: bool = Query(default=False),
):
    """List registered definitions (active ones by default)."""
    return {
        "count": len(
            items := database.list_definitions(device_type, include_inactive)
        ),
        "items": items,
    }


@app.get("/api/v1/definitions/{device_type}/latest")
def get_latest_definition(device_type: str):
    """Get the latest active version for a device type."""
    record = database.get_latest(device_type, active_only=True)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"no active definition found for device type '{device_type}'",
        )
    return record


@app.get("/api/v1/definitions/{device_type}/{version}")
def get_definition(device_type: str, version: str):
    record = database.get_definition(device_type, version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{version}' not found",
        )
    return record


@app.post("/api/v1/definitions/{device_type}/{version}/deactivate")
def deactivate_definition(device_type: str, version: str):
    """Deactivate a definition. It stays parseable by explicit version but is
    excluded from listing (by default) and from latest-version lookups."""
    record = database.deactivate(device_type, version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{version}' not found",
        )
    return record


@app.post("/api/v1/definitions/{device_type}/compatibility")
def compatibility(device_type: str, req: CompatibilityRequest):
    """Analyze wire compatibility between two registered versions of the same
    device type. Conditions are partitioned into finite equivalence classes,
    reachable layouts (field bit offsets + total length) are enumerated, and
    each old-version layout must find a new-version layout with identical
    field names/types/bit widths/offsets, total length and checksum algorithm.
    Returns ``breaking`` with per-branch differences otherwise; more than 256
    equivalence-class combinations yields 422."""
    if req.old_version == req.new_version:
        raise HTTPException(
            status_code=422,
            detail="old_version and new_version must differ",
        )
    old_record = database.get_definition(device_type, req.old_version)
    if old_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{req.old_version}' not found",
        )
    new_record = database.get_definition(device_type, req.new_version)
    if new_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{req.new_version}' not found",
        )
    try:
        return analyze_compatibility(old_record, new_record)
    except CompatibilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


_HEX_CLEANER = re.compile(r"(0[xX])|[\s_-]")


def _decode_hex(hex_payload: str) -> bytes:
    """Shared by /parse and /detect: strip whitespace/separators/0x prefix and
    decode. Raises HTTP 422 on an empty or non-hex payload."""
    cleaned = _HEX_CLEANER.sub("", hex_payload)
    if not cleaned:
        raise HTTPException(status_code=422, detail="hex payload is empty")
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="invalid hex payload: non-hex characters or odd number of digits",
        )


def _select_definition(device_type: str, version: Optional[str]) -> dict:
    """Shared by /parse and /encode: an explicit version selects that definition
    (even if inactive); otherwise the latest active version is used."""
    if version:
        record = database.get_definition(device_type, version)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"definition '{device_type}' version '{version}' not found",
            )
    else:
        record = database.get_latest(device_type, active_only=True)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"no active definition found for device type '{device_type}'",
            )
    return record


@app.post("/api/v1/parse")
def parse(req: ParseRequest):
    """Parse a hex payload against a definition (latest active version unless
    an explicit version is given)."""
    record = _select_definition(req.device_type, req.version)

    data = _decode_hex(req.hex)

    try:
        result = parse_message(record["fields"], record["checksum"], data)
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "device_type": record["device_type"],
        "version": record["version"],
        "definition_active": record["active"],
        **result,
    }


@app.post("/api/v1/detect")
def detect(req: DetectRequest):
    """Identify the message version without a version hint: try every registered
    version of the device type (including inactive ones) in version order.

    A version is a candidate only when the definition fully consumes the message
    (no trailing bits) and, when a checksum is configured, its verification
    passes; definitions without a checksum are judged on full consumption alone.
    Exactly one candidate returns the parse result; zero candidates returns 422
    with the per-version failure reason; several candidates return 409 with the
    candidate list — the latest version is never picked silently.
    """
    records = database.list_definitions(req.device_type, include_inactive=True)
    if not records:
        raise HTTPException(
            status_code=404,
            detail=f"no definitions found for device type '{req.device_type}'",
        )
    records.sort(key=lambda r: database.version_key(r["version"]))

    data = _decode_hex(req.hex)

    candidates = []
    failures = []
    for record in records:
        try:
            result = parse_message(record["fields"], record["checksum"], data)
        except ParseError as exc:
            reason = str(exc)
        else:
            reason = None
            if result["trailing_bits"]:
                reason = (
                    f"definition does not fully consume the message: "
                    f"{result['trailing_bits']} trailing bit(s)"
                )
            elif record["checksum"]:
                checksum = result["checksum"]
                if checksum is None:
                    reason = (
                        f"checksum field '{record['checksum']['field']}' was not parsed "
                        f"(skipped by an earlier branch)"
                    )
                elif not checksum["valid"]:
                    reason = (
                        f"checksum mismatch: expected {checksum['expected_hex']}, "
                        f"calculated {checksum['calculated_hex']}"
                    )

        if reason is None:
            candidates.append(
                {
                    "version": record["version"],
                    "active": record["active"],
                    "result": {
                        "device_type": record["device_type"],
                        "version": record["version"],
                        "definition_active": record["active"],
                        **result,
                    },
                }
            )
        else:
            failures.append(
                {
                    "version": record["version"],
                    "active": record["active"],
                    "reason": reason,
                }
            )

    if len(candidates) == 1:
        return candidates[0]["result"]

    if not candidates:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "no registered version matches the message",
                "device_type": req.device_type,
                "failures": failures,
            },
        )

    raise HTTPException(
        status_code=409,
        detail={
            "message": "message matches multiple registered versions; "
            "an explicit version is required",
            "device_type": req.device_type,
            "candidates": [
                {"version": c["version"], "active": c["active"]} for c in candidates
            ],
        },
    )


@app.post("/api/v1/encode")
def encode(req: EncodeRequest):
    """Encode field values into a hex payload (latest active version unless an
    explicit version is given). Conditions are evaluated on raw values; the
    checksum field is computed automatically and must not be provided."""
    record = _select_definition(req.device_type, req.version)

    try:
        result = encode_message(record["fields"], record["checksum"], req.values)
    except EncodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "device_type": record["device_type"],
        "version": record["version"],
        "definition_active": record["active"],
        **result,
    }


def _baseline_snapshot(parse_result: dict, expected_fields) -> dict:
    """Build the stored fixture snapshot, optionally restricted/overridden by
    the caller-supplied ``expected_fields``."""
    snapshot = capture_snapshot(parse_result)
    if expected_fields is None:
        return snapshot

    actual_by_name = {f["name"]: f["raw_value"] for f in snapshot["fields"]}
    selected = []
    for expected in expected_fields:
        if expected.name not in actual_by_name:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"expected field '{expected.name}' was not parsed against this "
                    f"definition (unknown name or skipped by its condition)"
                ),
            )
        raw_value = (
            expected.raw_value
            if "raw_value" in expected.model_fields_set
            else actual_by_name[expected.name]
        )
        selected.append({"name": expected.name, "raw_value": raw_value})
    snapshot["fields"] = selected
    return snapshot


@app.post("/api/v1/fixtures", status_code=201)
def create_fixture(fixture: FixtureCreate):
    """Save a named parse-regression fixture: a hex message for a registered
    definition plus its expected field snapshot. The fixture name must be unique
    within the device type (duplicate -> 409). When ``expected_fields`` is
    omitted, the whole parse result is captured as the baseline."""
    record = _select_definition(fixture.device_type, fixture.version)
    data = _decode_hex(fixture.hex)
    try:
        parse_result = parse_message(record["fields"], record["checksum"], data)
    except ParseError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"fixture baseline message cannot be parsed: {exc}",
        )

    snapshot = _baseline_snapshot(parse_result, fixture.expected_fields)

    saved = database.create_fixture(
        name=fixture.name,
        device_type=record["device_type"],
        version=record["version"],
        hex_payload=fixture.hex,
        snapshot=snapshot,
    )
    if saved is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"fixture '{fixture.name}' already exists for device type "
                f"'{record['device_type']}'"
            ),
        )
    return saved


@app.get("/api/v1/fixtures")
def list_fixtures(
    device_type: Optional[str] = Query(default=None),
):
    """List saved regression fixtures, optionally filtered by device type."""
    items = database.list_fixtures(device_type)
    return {"count": len(items), "items": items}


@app.get("/api/v1/fixtures/{name}")
def get_fixture(
    name: str,
    device_type: str = Query(..., description="device type the fixture belongs to"),
):
    """Fetch a single fixture (names are unique per device type)."""
    fixture = database.get_fixture(device_type, name)
    if fixture is None:
        raise HTTPException(
            status_code=404,
            detail=f"fixture '{name}' not found for device type '{device_type}'",
        )
    return fixture


@app.post("/api/v1/fixtures/{name}/run")
def run_fixture(
    name: str,
    device_type: str = Query(..., description="device type the fixture belongs to"),
    req: Optional[FixtureRunRequest] = None,
):
    """Re-parse a fixture's hex message and compare it with the stored baseline
    snapshot by field name/raw value, branch path and checksum verdict.

    Without ``target_version`` the fixture's own baseline version is re-run (a
    pure regression check); with ``target_version`` the message is parsed with a
    different registered version and added/removed/changed differences plus the
    pass rate are returned. A parse failure against the target version is
    reported per fixture (HTTP 200 with an ``error`` entry), never as a crash.
    """
    fixture = database.get_fixture(device_type, name)
    if fixture is None:
        raise HTTPException(
            status_code=404,
            detail=f"fixture '{name}' not found for device type '{device_type}'",
        )

    target_version = req.target_version if req is not None else None
    run_version = target_version or fixture["version"]
    record = database.get_definition(device_type, run_version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{run_version}' not found",
        )

    data = _decode_hex(fixture["hex"])
    try:
        actual_result = parse_message(record["fields"], record["checksum"], data)
    except ParseError as exc:
        return {
            "name": name,
            "device_type": device_type,
            "baseline_version": fixture["version"],
            "run_version": run_version,
            "version_changed": run_version != fixture["version"],
            "passed": False,
            "error": str(exc),
            "pass_rate": 0.0,
            "passed_checks": 0,
            "total_checks": 0,
        }

    comparison = compare_snapshot(fixture["snapshot"], actual_result)
    return {
        "name": name,
        "device_type": device_type,
        "baseline_version": fixture["version"],
        "run_version": run_version,
        "version_changed": run_version != fixture["version"],
        "passed": comparison["passed"],
        "pass_rate": comparison["pass_rate"],
        "passed_checks": comparison["passed_checks"],
        "total_checks": comparison["total_checks"],
        "differences": {
            "fields_added": comparison["fields"]["added"],
            "fields_removed": comparison["fields"]["removed"],
            "fields_changed": comparison["fields"]["changed"],
            "branches_added": comparison["branch_path"]["added"],
            "branches_removed": comparison["branch_path"]["removed"],
            "branches_changed": comparison["branch_path"]["changed"],
            "checksum": comparison["checksum"],
            "checksum_matched": comparison["checksum_matched"],
        },
        "matched": {
            "fields": comparison["fields"]["matched"],
            "branches": comparison["branch_path"]["matched"],
        },
        "baseline_snapshot": fixture["snapshot"],
        "actual_snapshot": comparison["actual_snapshot"],
        "parse_result": {
            "device_type": device_type,
            "version": run_version,
            "definition_active": record["active"],
            **actual_result,
        },
    }
