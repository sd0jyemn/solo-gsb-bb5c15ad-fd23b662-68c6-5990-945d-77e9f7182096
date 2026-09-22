import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="parser-test-"), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.checksum import crc16_ccitt  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

SENSOR_DEF = {
    "device_type": "temp_sensor",
    "version": "1.0.0",
    "description": "温湿度传感器报文",
    "fields": [
        {"name": "header", "type": "uint", "bit_length": 8},
        {"name": "msg_type", "type": "uint", "bit_length": 4},
        {"name": "flags", "type": "uint", "bit_length": 4},
        {
            "name": "temperature",
            "type": "int",
            "bit_length": 16,
            "endianness": "little",
            "scale": 0.1,
            "condition": {"field": "msg_type", "op": "eq", "value": 1},
        },
        {
            "name": "humidity",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "msg_type", "op": "eq", "value": 2},
        },
        {"name": "crc", "type": "uint", "bit_length": 16},
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
}


def make_message(body: bytes) -> str:
    crc = crc16_ccitt(body)
    return (body + crc.to_bytes(2, "big")).hex()


def test_register_definition():
    resp = client.post("/api/v1/definitions", json=SENSOR_DEF)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["device_type"] == "temp_sensor"
    assert data["version"] == "1.0.0"
    assert data["active"] is True
    assert len(data["fields"]) == 6


def test_register_duplicate_conflict():
    resp = client.post("/api/v1/definitions", json=SENSOR_DEF)
    assert resp.status_code == 409


def test_register_invalid_condition_reference():
    bad = {
        "device_type": "bad_dev",
        "version": "1.0.0",
        "fields": [
            {"name": "a", "type": "uint", "bit_length": 8,
             "condition": {"field": "ghost", "op": "eq", "value": 1}},
        ],
    }
    resp = client.post("/api/v1/definitions", json=bad)
    assert resp.status_code == 422


def test_register_checksum_width_mismatch():
    bad = {
        "device_type": "bad_crc",
        "version": "1.0.0",
        "fields": [{"name": "crc", "type": "uint", "bit_length": 8}],
        "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
    }
    resp = client.post("/api/v1/definitions", json=bad)
    assert resp.status_code == 422


def test_parse_temperature_branch():
    # header=0xAA, msg_type=1, flags=0, temperature raw=235 (LE) -> 23.5 °C
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "version": "1.0.0", "hex": make_message(body)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["checksum"]["valid"] is True
    assert data["checksum"]["algorithm"] == "crc16-ccitt"
    assert data["consumed_bits"] == 48

    fields = {f["name"]: f for f in data["fields"]}
    assert fields["header"]["value"] == 0xAA
    assert fields["header"]["bit_offset"] == 0
    assert fields["msg_type"]["value"] == 1
    assert fields["msg_type"]["bit_offset"] == 8
    assert fields["temperature"]["value"] == 23.5
    assert fields["temperature"]["raw_value"] == 235
    assert fields["temperature"]["bit_offset"] == 16
    assert "humidity" not in fields  # skipped by branch

    branch = {b["field"]: b for b in data["branch_path"]}
    assert branch["temperature"]["matched"] is True
    assert branch["temperature"]["action"] == "parsed"
    assert branch["humidity"]["matched"] is False
    assert branch["humidity"]["action"] == "skipped"


def test_parse_humidity_branch():
    # msg_type=2 -> humidity branch, temperature skipped
    body = bytes([0xAA, 0x20, 0x55])
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": make_message(body)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    fields = {f["name"]: f for f in data["fields"]}
    assert fields["humidity"]["value"] == 0x55
    assert fields["humidity"]["bit_offset"] == 16
    assert "temperature" not in fields
    assert data["checksum"]["valid"] is True


def test_parse_checksum_mismatch():
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    good = make_message(body)
    bad = good[:-2] + ("00" if good[-2:] != "00" else "ff")
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": bad},
    )
    assert resp.status_code == 200
    assert resp.json()["checksum"]["valid"] is False


def test_parse_truncated_message():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": "AA10"},
    )
    assert resp.status_code == 422


def test_parse_invalid_hex():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": "zzzz"},
    )
    assert resp.status_code == 422


def test_parse_unknown_device():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "nope", "hex": "AABB"},
    )
    assert resp.status_code == 404


def test_trailing_bits_warning():
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    msg = make_message(body) + "FF"  # extra byte beyond definition
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": msg},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["trailing_bits"] == 8
    assert data["warnings"]


def _register(device_type, version):
    payload = {
        "device_type": device_type,
        "version": version,
        "fields": [{"name": "val", "type": "uint", "bit_length": 8}],
    }
    resp = client.post("/api/v1/definitions", json=payload)
    assert resp.status_code == 201, resp.text


def test_latest_version_and_deactivate():
    _register("meter", "1.9.0")
    _register("meter", "1.10.0")  # semver-ish: 1.10.0 > 1.9.0

    resp = client.get("/api/v1/definitions/meter/latest")
    assert resp.status_code == 200
    assert resp.json()["version"] == "1.10.0"

    resp = client.post("/api/v1/definitions/meter/1.10.0/deactivate")
    assert resp.status_code == 200
    assert resp.json()["active"] is False

    # latest now falls back to the still-active older version
    resp = client.get("/api/v1/definitions/meter/latest")
    assert resp.json()["version"] == "1.9.0"

    # explicit-version parse of an inactive definition still works
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "meter", "version": "1.10.0", "hex": "2A"},
    )
    assert resp.status_code == 200
    assert resp.json()["definition_active"] is False
    assert resp.json()["fields"][0]["value"] == 0x2A


def test_list_definitions():
    resp = client.get("/api/v1/definitions")
    assert resp.status_code == 200
    data = resp.json()
    types = {item["device_type"] for item in data["items"]}
    assert {"temp_sensor", "meter"} <= types
    # inactive 1.10.0 hidden by default
    versions = {(i["device_type"], i["version"]) for i in data["items"]}
    assert ("meter", "1.10.0") not in versions

    resp = client.get("/api/v1/definitions", params={"include_inactive": True})
    versions = {(i["device_type"], i["version"]) for i in resp.json()["items"]}
    assert ("meter", "1.10.0") in versions

    resp = client.get("/api/v1/definitions", params={"device_type": "meter"})
    assert all(i["device_type"] == "meter" for i in resp.json()["items"])


def test_get_definition_and_404s():
    resp = client.get("/api/v1/definitions/temp_sensor/1.0.0")
    assert resp.status_code == 200
    assert resp.json()["checksum"]["algorithm"] == "crc16-ccitt"

    assert client.get("/api/v1/definitions/temp_sensor/9.9.9").status_code == 404
    assert client.get("/api/v1/definitions/ghost/latest").status_code == 404
    assert client.post("/api/v1/definitions/ghost/1.0.0/deactivate").status_code == 404


# ------------------------------------------------------------------ encode --


def test_encode_temperature_branch_roundtrip():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "version": "1.0.0",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.5},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # crc computed by the service, humidity not required on this branch
    assert data["hex"] == make_message(bytes([0xAA, 0x10, 0xEB, 0x00]))
    assert data["message_bits"] == 48
    assert data["padding_bits"] == 0

    fields = {f["name"]: f for f in data["fields"]}
    assert fields["temperature"]["value"] == 23.5
    assert fields["temperature"]["raw_value"] == 235
    assert fields["crc"]["value"] == crc16_ccitt(bytes([0xAA, 0x10, 0xEB, 0x00]))
    assert "humidity" not in fields

    branch = {b["field"]: b for b in data["branch_path"]}
    assert branch["temperature"]["matched"] is True
    assert branch["temperature"]["action"] == "encoded"
    assert branch["humidity"]["matched"] is False
    assert branch["humidity"]["action"] == "skipped"
    assert data["checksum"]["calculated_hex"] == "0x" + data["hex"][-4:]

    # the encoded payload parses back to the same values
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "version": "1.0.0", "hex": data["hex"]},
    )
    assert resp.status_code == 200
    parsed = {f["name"]: f for f in resp.json()["fields"]}
    assert parsed["temperature"]["value"] == 23.5
    assert resp.json()["checksum"]["valid"] is True


def test_encode_humidity_branch():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 2, "flags": 0, "humidity": 0x55},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["hex"] == make_message(bytes([0xAA, 0x20, 0x55]))
    names = [f["name"] for f in data["fields"]]
    assert "temperature" not in names  # skipped branch not required, not encoded


def test_encode_negative_temperature_two_complement():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": -5.0},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # raw = -50 -> two's complement 0xFFCE, little-endian -> CE FF
    assert data["hex"] == make_message(bytes([0xAA, 0x10, 0xCE, 0xFF]))
    resp = client.post(
        "/api/v1/parse", json={"device_type": "temp_sensor", "hex": data["hex"]}
    )
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["temperature"]["value"] == -5.0
    assert fields["temperature"]["raw_value"] == -50


def test_encode_missing_matched_field_422():
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "temp_sensor", "values": {"header": 0xAA, "msg_type": 2, "flags": 0}},
    )
    assert resp.status_code == 422
    assert "humidity" in resp.json()["detail"]


def test_encode_unknown_field_422():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.5, "ghost": 1},
        },
    )
    assert resp.status_code == 422
    assert "ghost" in resp.json()["detail"]


def test_encode_value_for_skipped_field_422():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.5, "humidity": 5},
        },
    )
    assert resp.status_code == 422
    assert "humidity" in resp.json()["detail"]


def test_encode_checksum_field_not_settable():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.5, "crc": 1},
        },
    )
    assert resp.status_code == 422
    assert "crc" in resp.json()["detail"]


def test_encode_out_of_range_422():
    # header is uint8, 0x1AA = 426 does not fit
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0x1AA, "msg_type": 1, "flags": 0, "temperature": 23.5},
        },
    )
    assert resp.status_code == 422


def test_encode_scaled_value_not_representable_422():
    # scale 0.1 -> 23.55 has no exact raw
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.55},
        },
    )
    assert resp.status_code == 422


def test_encode_scaled_raw_out_of_range_422():
    # 4000.0 -> raw 40000 exceeds int16 range
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 4000.0},
        },
    )
    assert resp.status_code == 422


def test_encode_wrong_value_type_422():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": "0xAA", "msg_type": 1, "flags": 0, "temperature": 23.5},
        },
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": True, "msg_type": 1, "flags": 0, "temperature": 23.5},
        },
    )
    assert resp.status_code == 422


def test_encode_unknown_device_404():
    resp = client.post("/api/v1/encode", json={"device_type": "ghost", "values": {}})
    assert resp.status_code == 404


def test_encode_unaligned_checksum_422():
    resp = client.post(
        "/api/v1/definitions",
        json={
            "device_type": "nibble_dev",
            "version": "1.0.0",
            "fields": [
                {"name": "tag", "type": "uint", "bit_length": 4},
                {"name": "crc", "type": "uint", "bit_length": 8},
            ],
            "checksum": {"field": "crc", "algorithm": "sum8"},
        },
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        "/api/v1/encode", json={"device_type": "nibble_dev", "values": {"tag": 5}}
    )
    assert resp.status_code == 422
    assert "byte boundary" in resp.json()["detail"]


def test_encode_bool_hex_ascii_non_byte_aligned():
    resp = client.post(
        "/api/v1/definitions",
        json={
            "device_type": "mixed_dev",
            "version": "1.0.0",
            "fields": [
                {"name": "enabled", "type": "bool", "bit_length": 1},
                {"name": "code", "type": "uint", "bit_length": 7},
                {"name": "magic", "type": "hex", "bit_length": 16},
                {"name": "label", "type": "ascii", "bit_length": 16},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "mixed_dev",
            "values": {"enabled": True, "code": 0x55, "magic": "0xABCD", "label": "OK"},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 1|1010101 -> 0xD5, then magic and label bytes
    assert data["hex"] == "d5abcd4f4b"
    assert data["checksum"] is None

    resp = client.post("/api/v1/parse", json={"device_type": "mixed_dev", "hex": data["hex"]})
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["enabled"]["value"] is True
    assert fields["code"]["value"] == 0x55
    assert fields["magic"]["value"] == "0xabcd"
    assert fields["label"]["value"] == "OK"


def test_encode_condition_uses_raw_values():
    resp = client.post(
        "/api/v1/definitions",
        json={
            "device_type": "raw_cond",
            "version": "1.0.0",
            "fields": [
                {"name": "temp", "type": "int", "bit_length": 16, "scale": 0.1},
                {
                    "name": "alarm",
                    "type": "uint",
                    "bit_length": 8,
                    "condition": {"field": "temp", "op": "ge", "value": 1000},
                },
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    # 99.9 -> raw 999 < 1000 -> alarm skipped, must not be provided
    resp = client.post(
        "/api/v1/encode", json={"device_type": "raw_cond", "values": {"temp": 99.9}}
    )
    assert resp.status_code == 200, resp.text
    assert [f["name"] for f in resp.json()["fields"]] == ["temp"]

    # 100.0 -> raw 1000 >= 1000 -> alarm required
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "raw_cond", "values": {"temp": 100.0, "alarm": 1}},
    )
    assert resp.status_code == 200, resp.text
    assert [f["name"] for f in resp.json()["fields"]] == ["temp", "alarm"]

    resp = client.post(
        "/api/v1/encode", json={"device_type": "raw_cond", "values": {"temp": 100.0}}
    )
    assert resp.status_code == 422


def test_encode_signed_int_range():
    resp = client.post(
        "/api/v1/definitions",
        json={
            "device_type": "signed_dev",
            "version": "1.0.0",
            "fields": [{"name": "delta", "type": "int", "bit_length": 8}],
        },
    )
    assert resp.status_code == 201, resp.text

    def enc(value):
        return client.post(
            "/api/v1/encode", json={"device_type": "signed_dev", "values": {"delta": value}}
        )

    resp = enc(-1)
    assert resp.status_code == 200, resp.text
    assert resp.json()["hex"] == "ff"  # two's complement

    assert enc(127).json()["hex"] == "7f"
    assert enc(-128).json()["hex"] == "80"
    assert enc(128).status_code == 422
    assert enc(-129).status_code == 422


def test_encode_version_rules_match_parse():
    for version, bits in (("1.0.0", 8), ("2.0.0", 16)):
        resp = client.post(
            "/api/v1/definitions",
            json={
                "device_type": "enc_dev",
                "version": version,
                "fields": [{"name": "val", "type": "uint", "bit_length": bits}],
            },
        )
        assert resp.status_code == 201, resp.text

    # latest active (2.0.0) used by default
    resp = client.post(
        "/api/v1/encode", json={"device_type": "enc_dev", "values": {"val": 0x1234}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == "2.0.0"
    assert resp.json()["hex"] == "1234"

    resp = client.post("/api/v1/definitions/enc_dev/2.0.0/deactivate")
    assert resp.status_code == 200

    # falls back to the still-active 1.0.0
    resp = client.post(
        "/api/v1/encode", json={"device_type": "enc_dev", "values": {"val": 0x12}}
    )
    assert resp.json()["version"] == "1.0.0"
    assert resp.json()["hex"] == "12"

    # explicit inactive version still encodable
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "enc_dev", "version": "2.0.0", "values": {"val": 0x1234}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["definition_active"] is False
    assert resp.json()["hex"] == "1234"


# ------------------------------------------------------------- version detect --


def _register_detect_version(device_type, version, fields, checksum=None):
    payload = {"device_type": device_type, "version": version, "fields": fields}
    if checksum is not None:
        payload["checksum"] = checksum
    resp = client.post("/api/v1/definitions", json=payload)
    assert resp.status_code == 201, resp.text


def test_detect_unique_candidate():
    # 1.0.0: [a, sum8] -> 2 bytes; 2.0.0: [a, b, crc16-ccitt] -> 4 bytes
    _register_detect_version(
        "detect_sensor",
        "1.0.0",
        [
            {"name": "a", "type": "uint", "bit_length": 8},
            {"name": "cs", "type": "uint", "bit_length": 8},
        ],
        {"field": "cs", "algorithm": "sum8"},
    )
    _register_detect_version(
        "detect_sensor",
        "2.0.0",
        [
            {"name": "a", "type": "uint", "bit_length": 8},
            {"name": "b", "type": "uint", "bit_length": 8},
            {"name": "crc", "type": "uint", "bit_length": 16},
        ],
        {"field": "crc", "algorithm": "crc16-ccitt"},
    )

    body = bytes([0xAA, 0x2A])
    msg = (body + crc16_ccitt(body).to_bytes(2, "big")).hex()
    resp = client.post("/api/v1/detect", json={"device_type": "detect_sensor", "hex": msg})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["version"] == "2.0.0"
    assert data["device_type"] == "detect_sensor"
    assert data["definition_active"] is True
    assert data["consumed_bits"] == 32
    assert data["trailing_bits"] == 0
    assert data["checksum"]["valid"] is True
    assert {f["name"] for f in data["fields"]} == {"a", "b", "crc"}


def test_detect_inactive_version_still_detected():
    # the 2-byte sum8 message only fits 1.0.0, which is now deactivated
    resp = client.post("/api/v1/definitions/detect_sensor/1.0.0/deactivate")
    assert resp.status_code == 200

    msg = bytes([0xAB, 0xAB]).hex()  # sum8([0xAB]) == 0xAB
    resp = client.post("/api/v1/detect", json={"device_type": "detect_sensor", "hex": msg})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["version"] == "1.0.0"
    assert data["definition_active"] is False
    assert data["checksum"]["valid"] is True


def test_detect_no_candidate_422_lists_reasons():
    # 1-byte message is too short for every version
    resp = client.post(
        "/api/v1/detect", json={"device_type": "detect_sensor", "hex": "AA"}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["device_type"] == "detect_sensor"
    failures = {f["version"]: f for f in detail["failures"]}
    assert set(failures) == {"1.0.0", "2.0.0"}
    assert "too short" in failures["1.0.0"]["reason"]
    assert "too short" in failures["2.0.0"]["reason"]
    # inactive flag is reported per version as well
    assert failures["1.0.0"]["active"] is False
    assert failures["2.0.0"]["active"] is True

    # well-sized for 1.0.0 but wrong checksum; still too short for 2.0.0
    resp = client.post(
        "/api/v1/detect", json={"device_type": "detect_sensor", "hex": "AA00"}
    )
    assert resp.status_code == 422
    reasons = {f["version"]: f["reason"] for f in resp.json()["detail"]["failures"]}
    assert "checksum mismatch" in reasons["1.0.0"]
    assert "too short" in reasons["2.0.0"]


def test_detect_multiple_candidates_409():
    # two registered versions with the exact same wire format and checksum
    for version in ("1.0.0", "2.0.0"):
        _register_detect_version(
            "detect_dup",
            version,
            [
                {"name": "a", "type": "uint", "bit_length": 8},
                {"name": "cs", "type": "uint", "bit_length": 8},
            ],
            {"field": "cs", "algorithm": "sum8"},
        )

    resp = client.post(
        "/api/v1/detect", json={"device_type": "detect_dup", "hex": "3c3c"}
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    # candidates are listed in version order; latest is never picked silently
    assert [c["version"] for c in detail["candidates"]] == ["1.0.0", "2.0.0"]
    assert all(c["active"] is True for c in detail["candidates"])
    assert "fields" not in detail

    # ambiguity persists even after one version is deactivated
    client.post("/api/v1/definitions/detect_dup/2.0.0/deactivate")
    resp = client.post(
        "/api/v1/detect", json={"device_type": "detect_dup", "hex": "3c3c"}
    )
    assert resp.status_code == 409
    candidates = {c["version"]: c["active"] for c in resp.json()["detail"]["candidates"]}
    assert candidates == {"1.0.0": True, "2.0.0": False}


def test_detect_without_checksum_uses_full_consumption_only():
    _register_detect_version(
        "detect_nocs",
        "1.0.0",
        [
            {"name": "a", "type": "uint", "bit_length": 8},
            {"name": "b", "type": "uint", "bit_length": 8},
        ],
    )
    _register_detect_version(
        "detect_nocs",
        "2.0.0",
        [
            {"name": "a", "type": "uint", "bit_length": 8},
            {"name": "b", "type": "uint", "bit_length": 8},
            {"name": "c", "type": "uint", "bit_length": 8},
        ],
    )

    resp = client.post(
        "/api/v1/detect", json={"device_type": "detect_nocs", "hex": "0x01-02"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == "1.0.0"  # cleaned hex, no checksum needed

    resp = client.post(
        "/api/v1/detect", json={"device_type": "detect_nocs", "hex": "010203"}
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == "2.0.0"

    # trailing bits for both versions -> no candidate
    resp = client.post(
        "/api/v1/detect", json={"device_type": "detect_nocs", "hex": "01020304"}
    )
    assert resp.status_code == 422
    reasons = {f["version"]: f["reason"] for f in resp.json()["detail"]["failures"]}
    assert set(reasons) == {"1.0.0", "2.0.0"}
    assert all("trailing" in r for r in reasons.values())


def test_detect_unknown_device_invalid_hex_and_version_hint():
    assert (
        client.post("/api/v1/detect", json={"device_type": "ghost", "hex": "AABB"}).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/detect", json={"device_type": "detect_nocs", "hex": "zzzz"}
        ).status_code
        == 422
    )
    # the request must contain only device_type and hex: no version hint allowed
    resp = client.post(
        "/api/v1/detect",
        json={"device_type": "detect_nocs", "hex": "0102", "version": "1.0.0"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------- version compatibility --


def _compat(device_type, old, new):
    return client.post(
        f"/api/v1/definitions/{device_type}/compatibility",
        json={"old_version": old, "new_version": new},
    )


def test_compatibility_identical_structure_compatible():
    # temp_sensor 1.0.0 vs a re-registered copy under a new version
    payload = dict(SENSOR_DEF)
    payload["version"] = "1.1.0"
    payload["description"] = "same wire format, new docs"
    resp = client.post("/api/v1/definitions", json=payload)
    assert resp.status_code == 201, resp.text

    resp = _compat("temp_sensor", "1.0.0", "1.1.0")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["compatible"] is True
    assert data["status"] == "compatible"
    assert data["breaking_branches"] == 0
    # msg_type is a 4-bit control field cut at 1,2 -> 4 equivalence classes
    assert data["combination_count"] == 4
    assert data["reachable_branches"] == 4
    assert data["equivalence_classes"][0]["field"] == "msg_type"
    assert data["equivalence_classes"][0]["representatives"] == [0, 1, 2, 3]

    # every enumerated branch lays out fields with offsets and a total length
    for branch in data["branches"]:
        assert branch["compatible"] is True
        layout = branch["old_layout"]
        assert layout["total_bits"] == branch["new_layout"]["total_bits"]
        assert layout["checksum_algorithm"] == "crc16-ccitt"
        offsets = [f["bit_offset"] for f in layout["fields"]]
        assert offsets == sorted(offsets)
        assert all(f["bit_length"] > 0 for f in layout["fields"])

    # the temperature branch (msg_type == 1) layout
    temp_branch = next(
        b for b in data["branches"]
        if b["representative"]["msg_type"]["value"] == 1
    )
    names = [f["name"] for f in temp_branch["old_layout"]["fields"]]
    assert names == ["header", "msg_type", "flags", "temperature", "crc"]
    temp_field = next(f for f in temp_branch["old_layout"]["fields"] if f["name"] == "temperature")
    assert (temp_field["type"], temp_field["bit_length"], temp_field["bit_offset"]) == (
        "int", 16, 16,
    )
    assert temp_branch["old_layout"]["total_bits"] == 48


def test_compatibility_field_width_change_is_breaking():
    payload = {
        "device_type": "compat_w",
        "version": "1.0.0",
        "fields": [
            {"name": "t", "type": "uint", "bit_length": 4},
            {"name": "v", "type": "uint", "bit_length": 8,
             "condition": {"field": "t", "op": "eq", "value": 1}},
        ],
    }
    assert client.post("/api/v1/definitions", json=payload).status_code == 201

    new = {
        "device_type": "compat_w",
        "version": "2.0.0",
        "fields": [
            {"name": "t", "type": "uint", "bit_length": 4},
            {"name": "v", "type": "uint", "bit_length": 16,
             "condition": {"field": "t", "op": "eq", "value": 1}},
        ],
    }
    assert client.post("/api/v1/definitions", json=new).status_code == 201

    resp = _compat("compat_w", "1.0.0", "2.0.0")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["compatible"] is False
    assert data["status"] == "breaking"
    # t == 1 isolates a point, leaving one class above it -> 3 classes total;
    # only the t == 1 branch contains v.
    assert data["combination_count"] == 3
    assert data["breaking_branches"] == 1

    bad = next(
        b for b in data["branches"]
        if b["representative"]["t"]["value"] == 1
    )
    assert bad["compatible"] is False
    kinds = {d["kind"]: d for d in bad["differences"]}
    assert kinds["field_changed"]["field"] == "v"
    assert kinds["field_changed"]["bit_length"] == {"old": 8, "new": 16}
    assert kinds["total_length_changed"]["old"] == 12
    assert kinds["total_length_changed"]["new"] == 20

    # the other branches (t != 1) are unaffected and remain compatible
    assert all(b["compatible"] for b in data["branches"]
               if b["representative"]["t"]["value"] != 1)


def test_compatibility_checksum_algorithm_change_is_breaking():
    payload = {
        "device_type": "compat_crc",
        "version": "1.0.0",
        "fields": [{"name": "crc", "type": "uint", "bit_length": 16}],
        "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
    }
    assert client.post("/api/v1/definitions", json=payload).status_code == 201
    new = dict(payload)
    new["version"] = "2.0.0"
    new["checksum"] = {"field": "crc", "algorithm": "crc16-modbus"}
    assert client.post("/api/v1/definitions", json=new).status_code == 201

    resp = _compat("compat_crc", "1.0.0", "2.0.0")
    data = resp.json()
    assert data["compatible"] is False
    assert data["combination_count"] == 1  # no control fields -> one layout
    diff = data["branches"][0]["differences"][0]
    assert diff == {
        "kind": "checksum_algorithm_changed",
        "old": "crc16-ccitt",
        "new": "crc16-modbus",
    }
    assert data["branches"][0]["old_layout"]["checksum_algorithm"] == "crc16-ccitt"


def test_compatibility_new_version_adds_finer_branch_is_breaking_only_there():
    base = {
        "device_type": "compat_fine",
        "version": "1.0.0",
        "fields": [
            {"name": "t", "type": "uint", "bit_length": 4},
            {"name": "v", "type": "uint", "bit_length": 8,
             "condition": {"field": "t", "op": "eq", "value": 1}},
        ],
    }
    assert client.post("/api/v1/definitions", json=base).status_code == 201
    new = {
        "device_type": "compat_fine",
        "version": "2.0.0",
        "fields": [
            {"name": "t", "type": "uint", "bit_length": 4},
            {"name": "v", "type": "uint", "bit_length": 8,
             "condition": {"field": "t", "op": "eq", "value": 1}},
            {"name": "w", "type": "uint", "bit_length": 8,
             "condition": {"field": "t", "op": "eq", "value": 2}},
        ],
    }
    assert client.post("/api/v1/definitions", json=new).status_code == 201

    resp = _compat("compat_fine", "1.0.0", "2.0.0")
    data = resp.json()
    # eq 1 isolates one point and eq 2 another -> joint partition:
    # [0], [1], [2], [3..15]
    assert data["combination_count"] == 4
    assert data["breaking_branches"] == 1
    bad = next(
        b for b in data["branches"]
        if b["representative"]["t"]["value"] == 2
    )
    assert bad["compatible"] is False
    assert any(d["kind"] == "field_added" and d["field"] == "w" for d in bad["differences"])
    assert {
        b["representative"]["t"]["value"]
        for b in data["branches"] if b["compatible"]
    } == {0, 1, 3}


def test_compatibility_versions_missing_404_and_same_version_422():
    resp = _compat("compat_missing", "1.0.0", "2.0.0")
    assert resp.status_code == 404

    resp = _compat("temp_sensor", "1.0.0", "9.9.9")
    assert resp.status_code == 404

    resp = _compat("temp_sensor", "1.0.0", "1.0.0")
    assert resp.status_code == 422


def test_compatibility_too_many_combinations_422():
    # two independent uint9 control fields, each split into 17 classes -> 289
    def payload(version):
        fields = [{"name": "a", "type": "uint", "bit_length": 9}]
        for i in range(16):
            fields.append(
                {"name": f"x{i}", "type": "uint", "bit_length": 8,
                 "condition": {"field": "a", "op": "lt", "value": i + 1}}
            )
        fields.append({"name": "b", "type": "uint", "bit_length": 9})
        for i in range(16):
            fields.append(
                {"name": f"y{i}", "type": "uint", "bit_length": 8,
                 "condition": {"field": "b", "op": "lt", "value": i + 1}}
            )
        return {"device_type": "compat_big", "version": version, "fields": fields}

    assert client.post("/api/v1/definitions", json=payload("1.0.0")).status_code == 201
    assert client.post("/api/v1/definitions", json=payload("2.0.0")).status_code == 201

    resp = _compat("compat_big", "1.0.0", "2.0.0")
    assert resp.status_code == 422
    assert "256" in resp.json()["detail"]


def test_compatibility_nested_conditional_unreachable_combinations_skipped():
    def payload(version):
        return {
            "device_type": "compat_nested",
            "version": version,
            "fields": [
                {"name": "t", "type": "uint", "bit_length": 4},
                {"name": "c1", "type": "uint", "bit_length": 8,
                 "condition": {"field": "t", "op": "eq", "value": 1}},
                # c2's control field c1 is itself conditional: when t != 1 the
                # branch is unreachable and must not be compared.
                {"name": "c2", "type": "uint", "bit_length": 8,
                 "condition": {"field": "c1", "op": "gt", "value": 5}},
            ],
        }

    assert client.post("/api/v1/definitions", json=payload("1.0.0")).status_code == 201
    assert client.post("/api/v1/definitions", json=payload("2.0.0")).status_code == 201

    resp = _compat("compat_nested", "1.0.0", "2.0.0")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # t partition [0],[1],[2..15] crossed with c1 classes {..5},{6..} = 6
    # combos; only the t==1 ones are reachable (c1 is absent otherwise).
    assert data["combination_count"] == 6
    assert data["reachable_branches"] == 2
    assert data["unreachable_combinations"] == 4
    assert data["compatible"] is True
    # only reachable combos (t == 1, c1 present) appear in the branch list
    reps = {
        (b["representative"]["t"]["value"], b["representative"]["c1"]["value"])
        for b in data["branches"]
    }
    assert reps == {(1, 0), (1, 6)}
    c2_branch = next(
        b for b in data["branches"]
        if b["representative"]["t"]["value"] == 1
        and b["representative"]["c1"]["value"] == 6
    )
    assert [f["name"] for f in c2_branch["old_layout"]["fields"]] == ["t", "c1", "c2"]
    assert c2_branch["old_layout"]["total_bits"] == 20


# ------------------------------------------------------------------ repeats --


REPEAT_DEF = {
    "device_type": "array_dev",
    "version": "1.0.0",
    "description": "传感器采样数组报文",
    "fields": [
        {"name": "header", "type": "uint", "bit_length": 8},
        {"name": "count", "type": "uint", "bit_length": 8},
        {
            "name": "samples",
            "type": "uint",
            "bit_length": 16,
            "endianness": "little",
            "scale": 0.1,
            "repeat": {"field": "count", "max": 8},
        },
        {"name": "crc", "type": "uint", "bit_length": 16},
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
}


def _register_repeat():
    resp = client.post("/api/v1/definitions", json=REPEAT_DEF)
    assert resp.status_code == 201, resp.text


def _repeat_hex(samples):
    body = bytes([0xAA, len(samples)]) + b"".join(
        int(round(s * 10)).to_bytes(2, "little") for s in samples
    )
    return (body + crc16_ccitt(body).to_bytes(2, "big")).hex()


def test_register_repeat_validation():
    _register_repeat()

    def bad(fields, version):
        return client.post(
            "/api/v1/definitions",
            json={"device_type": "rep_bad", "version": version, "fields": fields},
        )

    # counter must be an earlier scalar uint field
    assert bad(
        [{"name": "a", "type": "int", "bit_length": 8},
         {"name": "r", "type": "uint", "bit_length": 8, "repeat": {"field": "a", "max": 4}}],
        "1.0.0",
    ).status_code == 422
    # forward / unknown reference
    assert bad(
        [{"name": "r", "type": "uint", "bit_length": 8, "repeat": {"field": "ghost", "max": 4}}],
        "2.0.0",
    ).status_code == 422
    # self reference
    assert bad(
        [{"name": "r", "type": "uint", "bit_length": 8, "repeat": {"field": "r", "max": 4}}],
        "3.0.0",
    ).status_code == 422
    # max out of [1, 64]
    assert bad(
        [{"name": "a", "type": "uint", "bit_length": 8},
         {"name": "r", "type": "uint", "bit_length": 8, "repeat": {"field": "a", "max": 0}}],
        "4.0.0",
    ).status_code == 422
    assert bad(
        [{"name": "a", "type": "uint", "bit_length": 8},
         {"name": "r", "type": "uint", "bit_length": 8, "repeat": {"field": "a", "max": 65}}],
        "5.0.0",
    ).status_code == 422
    # a repeated field cannot drive later conditions
    assert bad(
        [
            {"name": "a", "type": "uint", "bit_length": 8},
            {"name": "r", "type": "uint", "bit_length": 8, "repeat": {"field": "a", "max": 4}},
            {"name": "x", "type": "uint", "bit_length": 8,
             "condition": {"field": "r", "op": "eq", "value": 1}},
        ],
        "6.0.0",
    ).status_code == 422
    # ... nor serve as another counter
    assert bad(
        [
            {"name": "a", "type": "uint", "bit_length": 8},
            {"name": "r", "type": "uint", "bit_length": 8, "repeat": {"field": "a", "max": 4}},
            {"name": "r2", "type": "uint", "bit_length": 8, "repeat": {"field": "r", "max": 4}},
        ],
        "7.0.0",
    ).status_code == 422
    # the checksum field itself must not be repeated
    resp = client.post(
        "/api/v1/definitions",
        json={
            "device_type": "rep_bad",
            "version": "8.0.0",
            "fields": [
                {"name": "a", "type": "uint", "bit_length": 8},
                {"name": "cs", "type": "uint", "bit_length": 8,
                 "repeat": {"field": "a", "max": 4}},
            ],
            "checksum": {"field": "cs", "algorithm": "sum8"},
        },
    )
    assert resp.status_code == 422


def test_parse_repeated_field_arrays_and_offsets():
    samples = [23.2, 200.0, 1000.0]  # raws 232, 2000, 10000
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "array_dev", "version": "1.0.0", "hex": _repeat_hex(samples)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    fields = {f["name"]: f for f in data["fields"]}

    rep = fields["samples"]
    assert rep["bit_offset"] == 16
    assert rep["bit_length"] == 16  # per-element width
    assert rep["count"] == 3
    assert rep["value"] == samples
    assert rep["raw_value"] == [232, 2000, 10000]

    # repeats push later fields and the checksum position forward
    assert fields["crc"]["bit_offset"] == 16 + 3 * 16
    assert data["consumed_bits"] == 16 + 3 * 16 + 16
    assert data["trailing_bits"] == 0
    assert data["checksum"]["valid"] is True
    assert data["checksum"]["range"] == f"bytes [0, {fields['crc']['bit_offset'] // 8})"


def test_parse_repeated_zero_count():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "array_dev", "version": "1.0.0", "hex": _repeat_hex([])},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    fields = {f["name"]: f for f in data["fields"]}
    rep = fields["samples"]
    assert rep["count"] == 0
    assert rep["value"] == []
    assert rep["raw_value"] == []
    assert rep["bit_offset"] == 16
    assert fields["crc"]["bit_offset"] == 16  # nothing expanded
    assert data["consumed_bits"] == 32
    assert data["checksum"]["valid"] is True


def test_parse_repeat_count_over_max_422():
    # count byte = 9 > max 8; payload deliberately short as well
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "array_dev", "version": "1.0.0", "hex": "aa09"},
    )
    assert resp.status_code == 422
    assert "exceeds the declared maximum" in resp.json()["detail"]


def test_parse_repeat_message_too_short_422():
    # count = 2 but no sample bytes follow
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "array_dev", "version": "1.0.0", "hex": "aa023412"},
    )
    assert resp.status_code == 422
    assert "message too short" in resp.json()["detail"]


def test_parse_repeat_counter_skipped_by_branch_422():
    payload = {
        "device_type": "rep_skip",
        "version": "1.0.0",
        "fields": [
            {"name": "kind", "type": "uint", "bit_length": 8},
            {"name": "n", "type": "uint", "bit_length": 8,
             "condition": {"field": "kind", "op": "eq", "value": 1}},
            {"name": "tags", "type": "uint", "bit_length": 8,
             "repeat": {"field": "n", "max": 4}},
        ],
    }
    assert client.post("/api/v1/definitions", json=payload).status_code == 201
    # kind != 1 -> counter n absent -> repeat cannot be resolved
    resp = client.post(
        "/api/v1/parse", json={"device_type": "rep_skip", "hex": "00"}
    )
    assert resp.status_code == 422
    assert "repeat counter 'n' was not parsed" in resp.json()["detail"]


def test_parse_repeat_behind_condition_not_expanded_when_missed():
    payload = {
        "device_type": "rep_cond",
        "version": "1.0.0",
        "fields": [
            {"name": "kind", "type": "uint", "bit_length": 8},
            {"name": "n", "type": "uint", "bit_length": 8},
            {"name": "tags", "type": "ascii", "bit_length": 8,
             "repeat": {"field": "n", "max": 4},
             "condition": {"field": "kind", "op": "eq", "value": 1}},
        ],
    }
    assert client.post("/api/v1/definitions", json=payload).status_code == 201

    # condition missed: n's value must not expand tags
    resp = client.post("/api/v1/parse", json={"device_type": "rep_cond", "hex": "0002"})
    assert resp.status_code == 200
    data = resp.json()
    assert [f["name"] for f in data["fields"]] == ["kind", "n"]
    assert data["consumed_bits"] == 16

    # condition hit: tags = ["A", "B"]
    resp = client.post("/api/v1/parse", json={"device_type": "rep_cond", "hex": "01024142"})
    assert resp.status_code == 200
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["tags"]["value"] == ["A", "B"]
    assert fields["tags"]["count"] == 2


def test_encode_repeated_field_roundtrip():
    samples = [23.2, 200.0, 1000.0]
    expected_hex = _repeat_hex(samples)
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "array_dev",
            "version": "1.0.0",
            "values": {"header": 0xAA, "count": 3, "samples": samples},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["hex"] == expected_hex
    fields = {f["name"]: f for f in data["fields"]}
    assert fields["samples"]["value"] == samples
    assert fields["samples"]["raw_value"] == [232, 2000, 10000]
    assert fields["samples"]["count"] == 3
    assert fields["crc"]["bit_offset"] == 64

    # parses back identically
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "array_dev", "version": "1.0.0", "hex": data["hex"]},
    )
    assert resp.status_code == 200
    rep = {f["name"]: f for f in resp.json()["fields"]}["samples"]
    assert rep["value"] == samples
    assert resp.json()["checksum"]["valid"] is True


def test_encode_repeated_zero_count_requires_empty_list():
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "array_dev", "values": {"header": 0xAA, "count": 0, "samples": []}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["hex"] == _repeat_hex([])

    # count == 0 but a non-empty list is supplied
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "array_dev",
              "values": {"header": 0xAA, "count": 0, "samples": [1.0]}},
    )
    assert resp.status_code == 422
    assert "exactly 0" in resp.json()["detail"]


def test_encode_repeated_array_length_mismatch_422():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "array_dev",
            "values": {"header": 0xAA, "count": 3, "samples": [1.0, 2.0]},
        },
    )
    assert resp.status_code == 422
    assert "exactly 3" in resp.json()["detail"]


def test_encode_repeated_value_must_be_list_422():
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "array_dev",
              "values": {"header": 0xAA, "count": 3, "samples": 1.0}},
    )
    assert resp.status_code == 422
    assert "expected a list" in resp.json()["detail"]


def test_encode_repeated_element_type_and_range_422():
    # one element is a string where an integer/float is required
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "array_dev",
            "values": {"header": 0xAA, "count": 2, "samples": [1.0, "nope"]},
        },
    )
    assert resp.status_code == 422
    assert "samples[1]" in resp.json()["detail"]

    # one element's raw value does not fit in uint16
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "array_dev",
            "values": {"header": 0xAA, "count": 1, "samples": [70000.0]},
        },
    )
    assert resp.status_code == 422
    assert "samples[0]" in resp.json()["detail"]


def test_encode_repeat_count_over_max_422():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "array_dev",
            "values": {"header": 0xAA, "count": 9, "samples": [0.0] * 9},
        },
    )
    assert resp.status_code == 422
    assert "exceeds the declared maximum" in resp.json()["detail"]


def test_compatibility_rejects_repeat_definitions_422():
    # array_dev 1.0.0 carries a repeat declaration
    payload = dict(REPEAT_DEF)
    payload["version"] = "2.0.0"
    assert client.post("/api/v1/definitions", json=payload).status_code == 201

    resp = _compat("array_dev", "1.0.0", "2.0.0")
    assert resp.status_code == 422
    assert "repeated" in resp.json()["detail"]

    # even comparing against a definition without repeats is refused when
    # either side contains one
    plain = {
        "device_type": "array_dev",
        "version": "3.0.0",
        "fields": [
            {"name": "header", "type": "uint", "bit_length": 8},
            {"name": "count", "type": "uint", "bit_length": 8},
            {"name": "crc", "type": "uint", "bit_length": 16},
        ],
        "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
    }
    assert client.post("/api/v1/definitions", json=plain).status_code == 201
    resp = _compat("array_dev", "3.0.0", "1.0.0")
    assert resp.status_code == 422
    assert "repeated" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Parse regression fixtures (/api/v1/fixtures)
# ---------------------------------------------------------------------------

FX_DEVICE = "fixture_sensor"

FX_DEF_V1 = {
    "device_type": FX_DEVICE,
    "version": "1.0.0",
    "fields": [
        {"name": "header", "type": "uint", "bit_length": 8},
        {"name": "msg_type", "type": "uint", "bit_length": 4},
        {"name": "flags", "type": "uint", "bit_length": 4},
        {
            "name": "temperature",
            "type": "int",
            "bit_length": 16,
            "endianness": "little",
            "scale": 0.1,
            "condition": {"field": "msg_type", "op": "eq", "value": 1},
        },
        {
            "name": "humidity",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "msg_type", "op": "eq", "value": 2},
        },
        {"name": "crc", "type": "uint", "bit_length": 16},
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
}

# v2: same layout/widths but temperature becomes big-endian -> raw value changes
FX_DEF_V2_BE = {
    "device_type": FX_DEVICE,
    "version": "2.0.0",
    "fields": [
        {"name": "header", "type": "uint", "bit_length": 8},
        {"name": "msg_type", "type": "uint", "bit_length": 4},
        {"name": "flags", "type": "uint", "bit_length": 4},
        {
            "name": "temperature",
            "type": "int",
            "bit_length": 16,
            "endianness": "big",
            "scale": 0.1,
            "condition": {"field": "msg_type", "op": "eq", "value": 1},
        },
        {
            "name": "humidity",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "msg_type", "op": "eq", "value": 2},
        },
        {"name": "crc", "type": "uint", "bit_length": 16},
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
}

# v3: same as v1 but adds a byte-sized pressure field on the temperature
# branch -> the 48-bit temperature message no longer parses, while the
# humidity branch layout is unchanged (one extra branch-path entry).
FX_DEF_V3_PRESSURE = {
    "device_type": FX_DEVICE,
    "version": "3.0.0",
    "fields": [
        {"name": "header", "type": "uint", "bit_length": 8},
        {"name": "msg_type", "type": "uint", "bit_length": 4},
        {"name": "flags", "type": "uint", "bit_length": 4},
        {
            "name": "temperature",
            "type": "int",
            "bit_length": 16,
            "endianness": "little",
            "scale": 0.1,
            "condition": {"field": "msg_type", "op": "eq", "value": 1},
        },
        {
            "name": "pressure",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "msg_type", "op": "eq", "value": 1},
        },
        {
            "name": "humidity",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "msg_type", "op": "eq", "value": 2},
        },
        {"name": "crc", "type": "uint", "bit_length": 16},
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
}

# v4: requires far more bits than the v1 6-byte message provides -> parse error
FX_DEF_V4_TOOLONG = {
    "device_type": FX_DEVICE,
    "version": "4.0.0",
    "fields": [
        {"name": "header", "type": "uint", "bit_length": 8},
        {"name": "payload", "type": "uint", "bit_length": 64},
        {"name": "crc", "type": "uint", "bit_length": 16},
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
}

# v5: identical to v1 but without a checksum -> only the checksum verdict differs
FX_DEF_V5_NOCRC = {
    "device_type": FX_DEVICE,
    "version": "5.0.0",
    "fields": [
        {"name": "header", "type": "uint", "bit_length": 8},
        {"name": "msg_type", "type": "uint", "bit_length": 4},
        {"name": "flags", "type": "uint", "bit_length": 4},
        {
            "name": "temperature",
            "type": "int",
            "bit_length": 16,
            "endianness": "little",
            "scale": 0.1,
            "condition": {"field": "msg_type", "op": "eq", "value": 1},
        },
        {
            "name": "humidity",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "msg_type", "op": "eq", "value": 2},
        },
        {"name": "crc", "type": "uint", "bit_length": 16},
    ],
}


def _fx_hex(body: bytes) -> str:
    return (body + crc16_ccitt(body).to_bytes(2, "big")).hex()


FX_TEMP_HEX = _fx_hex(bytes([0xAA, 0x10, 0xEB, 0x00]))
FX_HUM_HEX = _fx_hex(bytes([0xAA, 0x20, 0x55]))


def _register_fixture_defs():
    assert client.post("/api/v1/definitions", json=FX_DEF_V1).status_code == 201
    assert client.post("/api/v1/definitions", json=FX_DEF_V2_BE).status_code == 201
    assert client.post("/api/v1/definitions", json=FX_DEF_V3_PRESSURE).status_code == 201
    assert client.post("/api/v1/definitions", json=FX_DEF_V4_TOOLONG).status_code == 201
    assert client.post("/api/v1/definitions", json=FX_DEF_V5_NOCRC).status_code == 201


def _create_fixture(name, **overrides):
    payload = {
        "name": name,
        "device_type": FX_DEVICE,
        "version": "1.0.0",
        "hex": FX_TEMP_HEX,
    }
    payload.update(overrides)
    return client.post("/api/v1/fixtures", json=payload)


def test_fixture_create_captures_snapshot():
    _register_fixture_defs()
    resp = _create_fixture("temp-branch")
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "temp-branch"
    assert data["device_type"] == FX_DEVICE
    assert data["version"] == "1.0.0"
    assert data["hex"] == FX_TEMP_HEX

    snap = data["snapshot"]
    fields = {f["name"]: f["raw_value"] for f in snap["fields"]}
    assert fields == {
        "header": 0xAA,
        "msg_type": 1,
        "flags": 0,
        "temperature": 235,
        "crc": crc16_ccitt(bytes([0xAA, 0x10, 0xEB, 0x00])),
    }
    # skipped branch field (humidity) is absent from the parsed-field snapshot
    assert "humidity" not in fields
    branches = {b["field"]: b for b in snap["branch_path"]}
    assert branches["temperature"]["matched"] is True
    assert branches["humidity"]["matched"] is False
    assert snap["checksum_valid"] is True
    assert snap["checksum_algorithm"] == "crc16-ccitt"
    assert "created_at" in data


def test_fixture_name_unique_per_device_type_only():
    # same (device_type, name) -> 409
    resp = _create_fixture("temp-branch")
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]

    # same name under a different device type is fine once that type exists
    other = dict(FX_DEF_V1)
    other["device_type"] = "fixture_sensor_other"
    assert client.post("/api/v1/definitions", json=other).status_code == 201
    resp = _create_fixture(
        "temp-branch", device_type="fixture_sensor_other"
    )
    assert resp.status_code == 201, resp.text


def test_fixture_create_validation_errors():
    # unknown device/version
    resp = client.post(
        "/api/v1/fixtures",
        json={"name": "x", "device_type": "ghost", "hex": FX_TEMP_HEX},
    )
    assert resp.status_code == 404

    # baseline message that does not parse -> 422 (not stored)
    resp = _create_fixture("truncated", hex="AABB")
    assert resp.status_code == 422
    assert "baseline" in resp.json()["detail"]
    assert (
        client.get("/api/v1/fixtures/truncated", params={"device_type": FX_DEVICE}).status_code
        == 404
    )

    # invalid hex -> 422
    assert _create_fixture("badhex", hex="zzzz").status_code == 422

    # illegal name -> 422
    assert _create_fixture("bad name!").status_code == 422

    # extra/unknown body fields are rejected
    assert _create_fixture("extra", bogus=1).status_code == 422


def test_fixture_create_with_expected_fields():
    # subset: omitted raw_value is captured from the actual parse
    resp = _create_fixture(
        "subset",
        expected_fields=[
            {"name": "header"},
            {"name": "temperature"},
        ],
    )
    assert resp.status_code == 201, resp.text
    names = [f["name"] for f in resp.json()["snapshot"]["fields"]]
    assert names == ["header", "temperature"]
    assert resp.json()["snapshot"]["fields"][1]["raw_value"] == 235

    # explicit raw_value overrides the captured baseline (intentional mismatch)
    resp = _create_fixture(
        "pinned",
        expected_fields=[{"name": "header", "raw_value": 1}],
    )
    assert resp.status_code == 201
    assert resp.json()["snapshot"]["fields"][0]["raw_value"] == 1

    # explicit raw_value null is respected (distinct from "omitted")
    resp = _create_fixture(
        "nulled",
        expected_fields=[{"name": "header", "raw_value": None}],
    )
    assert resp.status_code == 201
    assert resp.json()["snapshot"]["fields"][0]["raw_value"] is None

    # expected field not present in the parse -> 422
    resp = _create_fixture(
        "unknown-expected",
        expected_fields=[{"name": "humidity"}],  # skipped on this branch
    )
    assert resp.status_code == 422
    resp = _create_fixture(
        "ghost-expected",
        expected_fields=[{"name": "nope"}],
    )
    assert resp.status_code == 422


def test_fixture_run_same_version_passes():
    resp = client.post(
        f"/api/v1/fixtures/temp-branch/run?device_type={FX_DEVICE}", json={}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["passed"] is True
    assert data["version_changed"] is False
    assert data["baseline_version"] == data["run_version"] == "1.0.0"
    assert data["pass_rate"] == 1.0
    assert data["passed_checks"] == data["total_checks"]
    assert data["differences"]["fields_added"] == []
    assert data["differences"]["fields_removed"] == []
    assert data["differences"]["fields_changed"] == []
    assert data["differences"]["branches_changed"] == []
    assert data["differences"]["checksum_matched"] is True
    assert set(data["matched"]["fields"]) == {
        "header",
        "msg_type",
        "flags",
        "temperature",
        "crc",
    }
    # full parse result is embedded
    assert data["parse_result"]["checksum"]["valid"] is True


def test_fixture_run_accepts_no_body():
    resp = client.post(f"/api/v1/fixtures/temp-branch/run?device_type={FX_DEVICE}")
    assert resp.status_code == 200
    assert resp.json()["passed"] is True


def test_fixture_run_pinned_mismatch_lowers_pass_rate():
    resp = client.post(
        f"/api/v1/fixtures/pinned/run?device_type={FX_DEVICE}", json={}
    )
    data = resp.json()
    assert data["passed"] is False
    changed = data["differences"]["fields_changed"]
    assert changed == [{"name": "header", "baseline": 1, "actual": 0xAA}]
    # the subset baseline names only header; every other parsed field is an
    # unexpected addition: 1 changed + 4 added fields, 2 branches + checksum ok
    assert {f["name"] for f in data["differences"]["fields_added"]} == {
        "msg_type",
        "flags",
        "temperature",
        "crc",
    }
    # checks = baseline fields (1) + added fields (4) + branches (2) + crc (1) = 8
    assert data["total_checks"] == 8
    assert data["passed_checks"] == 3  # both branches + checksum
    assert abs(data["pass_rate"] - (3 / 8)) < 1e-6


def test_fixture_run_against_new_version_reports_diff():
    # v2 only flips temperature endianness: the message still parses fully and
    # the diff reports exactly the changed raw value.
    resp = client.post(
        f"/api/v1/fixtures/temp-branch/run?device_type={FX_DEVICE}",
        json={"target_version": "2.0.0"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["version_changed"] is True
    assert data["run_version"] == "2.0.0"
    assert data["passed"] is False

    diffs = data["differences"]
    # bytes EB 00: little-endian int16 = 0x00EB = 235; big-endian int16 =
    # 0xEB00, sign-extended to -5376
    assert diffs["fields_changed"] == [
        {"name": "temperature", "baseline": 235, "actual": -5376}
    ]
    assert diffs["fields_added"] == []
    assert diffs["fields_removed"] == []
    assert diffs["branches_changed"] == []
    assert diffs["branches_added"] == []
    assert diffs["branches_removed"] == []
    # fields 5 (1 fails) + branches 2 + checksum 1 = 8 checks, 7 pass
    assert data["total_checks"] == 8
    assert data["passed_checks"] == 7
    assert data["pass_rate"] == 0.875


def test_fixture_run_parse_failure_is_retained_per_fixture():
    # v3 adds pressure (8 bits) on this branch -> message too short; the run
    # stays HTTP 200 and carries the parser error instead of crashing.
    resp = client.post(
        f"/api/v1/fixtures/temp-branch/run?device_type={FX_DEVICE}",
        json={"target_version": "3.0.0"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["passed"] is False
    assert data["error"]
    assert "too short" in data["error"] or "need" in data["error"]
    assert data["pass_rate"] == 0.0
    assert data["total_checks"] == 0
    assert data["version_changed"] is True

    # v4 needs far more bytes than available: error retained, not a 5xx/422
    resp = client.post(
        f"/api/v1/fixtures/temp-branch/run?device_type={FX_DEVICE}",
        json={"target_version": "4.0.0"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["passed"] is False
    assert data["error"]
    assert "too short" in data["error"]


def test_fixture_run_version_change_unaffected_branch_still_passes():
    # humidity branch: v1/v3 layouts are byte-identical there (pressure is only
    # on the temperature branch), so fields/raw/checksum all match.
    assert _create_fixture("hum-branch", hex=FX_HUM_HEX).status_code == 201
    resp = client.post(
        f"/api/v1/fixtures/hum-branch/run?device_type={FX_DEVICE}",
        json={"target_version": "3.0.0"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["version_changed"] is True
    assert data["differences"]["fields_added"] == []
    assert data["differences"]["fields_removed"] == []
    assert data["differences"]["fields_changed"] == []
    # pressure conditional exists only in v3 -> an added branch path entry, but
    # its field is skipped on this message, so no added field
    assert [b["field"] for b in data["differences"]["branches_added"]] == [
        "pressure"
    ]
    assert data["differences"]["checksum_matched"] is True


def test_fixture_run_added_branch_counts_against_pass_rate():
    # same humidity run as above: v3 has one extra conditional -> 1 extra check
    data = client.post(
        f"/api/v1/fixtures/hum-branch/run?device_type={FX_DEVICE}",
        json={"target_version": "3.0.0"},
    ).json()
    # humidity field + header + msg_type + flags + crc = 5 fields;
    # branches: temperature, humidity baseline (2) + pressure added (1);
    # checksum 1 -> total 9, the added branch fails -> 8/9
    assert data["total_checks"] == 9
    assert data["passed_checks"] == 8
    assert abs(data["pass_rate"] - (8 / 9)) < 1e-6
    assert data["passed"] is False


def test_fixture_run_404s():
    # unknown fixture
    resp = client.post(
        f"/api/v1/fixtures/ghost/run?device_type={FX_DEVICE}", json={}
    )
    assert resp.status_code == 404

    # unknown target version
    resp = client.post(
        f"/api/v1/fixtures/temp-branch/run?device_type={FX_DEVICE}",
        json={"target_version": "9.9.9"},
    )
    assert resp.status_code == 404

    # missing device_type query parameter -> 422
    resp = client.post("/api/v1/fixtures/temp-branch/run")
    assert resp.status_code == 422


def test_fixture_run_checksum_verdict_diff_is_not_an_error():
    # Baseline fixture on v1 (checksum valid); run against v5 whose layout is
    # identical but configures no checksum. Parsing still succeeds and only the
    # checksum verdict differs.
    assert _create_fixture("crc-vs-none").status_code == 201
    resp = client.post(
        f"/api/v1/fixtures/crc-vs-none/run?device_type={FX_DEVICE}",
        json={"target_version": "5.0.0"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "error" not in data
    assert data["passed"] is False
    checksum = data["differences"]["checksum"]
    assert checksum["baseline_valid"] is True
    assert checksum["actual_valid"] is None
    assert data["differences"]["checksum_matched"] is False
    # everything else matches: 5 fields + 2 branches pass, checksum fails -> 7/8
    assert data["total_checks"] == 8
    assert data["passed_checks"] == 7
    assert data["pass_rate"] == 0.875


def test_fixture_run_checksum_invalid_baseline_is_reproduced():
    # A baseline captured with a corrupt CRC stores valid=false; re-running the
    # same version reproduces it (both verdicts false -> check check passes).
    bad = FX_TEMP_HEX[:-2] + ("00" if FX_TEMP_HEX[-2:] != "00" else "ff")
    assert _create_fixture("bad-crc", hex=bad).status_code == 201
    resp = client.post(
        f"/api/v1/fixtures/bad-crc/run?device_type={FX_DEVICE}", json={}
    )
    data = resp.json()
    assert data["passed"] is True
    assert data["differences"]["checksum"]["baseline_valid"] is False
    assert data["differences"]["checksum"]["actual_valid"] is False
    assert data["differences"]["checksum_matched"] is True


def test_fixture_list_and_get():
    # self-contained: create fixtures under a fresh device type
    list_device = "fixture_list_dev"
    list_def = dict(FX_DEF_V1)
    list_def["device_type"] = list_device
    assert client.post("/api/v1/definitions", json=list_def).status_code == 201
    list_hex = _fx_hex(bytes([0xAA, 0x10, 0xEB, 0x00]))
    for fx_name in ("list-a", "list-b"):
        assert (
            client.post(
                "/api/v1/fixtures",
                json={
                    "name": fx_name,
                    "device_type": list_device,
                    "version": "1.0.0",
                    "hex": list_hex,
                },
            ).status_code
            == 201
        )

    resp = client.get("/api/v1/fixtures")
    assert resp.status_code == 200
    all_items = resp.json()["items"]
    names = {f["name"] for f in all_items}
    assert {"list-a", "list-b"} <= names
    assert resp.json()["count"] == len(all_items)

    resp = client.get("/api/v1/fixtures", params={"device_type": list_device})
    list_items = resp.json()["items"]
    assert resp.json()["count"] == 2
    assert {f["name"] for f in list_items} == {"list-a", "list-b"}
    assert all(f["device_type"] == list_device for f in list_items)

    resp = client.get(
        "/api/v1/fixtures/list-a", params={"device_type": list_device}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "list-a"

    # the same name can live under a different device type (uniqueness is per
    # device type), so the d2 copy resolves to d2 rather than 404
    other_device = "fixture_list_dev_other"
    other_def = dict(list_def)
    other_def["device_type"] = other_device
    assert client.post("/api/v1/definitions", json=other_def).status_code == 201
    assert (
        client.post(
            "/api/v1/fixtures",
            json={
                "name": "list-a",
                "device_type": other_device,
                "version": "1.0.0",
                "hex": list_hex,
            },
        ).status_code
        == 201
    )
    other = client.get(
        "/api/v1/fixtures/list-a", params={"device_type": other_device}
    )
    assert other.status_code == 200
    assert other.json()["device_type"] == other_device

    # genuinely unknown names -> 404
    assert (
        client.get("/api/v1/fixtures/ghost", params={"device_type": list_device}).status_code
        == 404
    )
    assert (
        client.get(
            "/api/v1/fixtures/list-a", params={"device_type": "no-such-device"}
        ).status_code
        == 404
    )


def test_fixture_persists_in_same_database():
    # a fresh client against the same DB_PATH still sees the fixture
    from fastapi.testclient import TestClient

    client2 = TestClient(app)
    resp = client2.get(
        "/api/v1/fixtures/temp-branch", params={"device_type": FX_DEVICE}
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == "1.0.0"
