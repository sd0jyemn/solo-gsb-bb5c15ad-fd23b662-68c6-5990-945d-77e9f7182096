"""Parsing regression fixtures: baseline snapshot capture and field-level diffing.

A fixture stores a hex message plus the expected parse snapshot of a registered
definition version. Running the fixture re-parses the same hex (optionally
against a different ``target_version``) and compares the new parse result with
the baseline by field name/raw value, branch path and checksum verdict.
"""

from typing import Any, Dict, List


def _field_key(field: Dict[str, Any]) -> Any:
    """The value a field is compared on: raw_value for numeric fields (it exists
    for every parsed uint/int, including repeated ones), display value otherwise
    (bool / hex / ascii)."""
    if "raw_value" in field:
        return field["raw_value"]
    return field.get("value")


def capture_snapshot(parse_result: Dict[str, Any]) -> Dict[str, Any]:
    """Capture the comparable parts of a parse result: per-field name+raw value,
    the branch path and the checksum verdict."""
    return {
        "fields": [
            {"name": f["name"], "raw_value": _field_key(f)}
            for f in parse_result["fields"]
        ],
        "branch_path": [dict(b) for b in parse_result["branch_path"]],
        "checksum_valid": (
            parse_result["checksum"]["valid"]
            if parse_result.get("checksum") is not None
            else None
        ),
        "checksum_algorithm": (
            parse_result["checksum"]["algorithm"]
            if parse_result.get("checksum") is not None
            else None
        ),
    }


def _diff_fields(
    baseline: List[Dict[str, Any]],
    actual_fields: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Compare parsed fields by name and raw/value key."""
    base_by_name = {f["name"]: f for f in baseline}
    actual_by_name = {f["name"]: f for f in actual_fields}

    added, removed, changed, matched = [], [], [], []

    for name in base_by_name:
        base_value = base_by_name[name]["raw_value"]
        if name not in actual_by_name:
            removed.append({"name": name, "baseline": base_value, "actual": None})
            continue
        actual_value = _field_key(actual_by_name[name])
        if actual_value != base_value:
            changed.append(
                {"name": name, "baseline": base_value, "actual": actual_value}
            )
        else:
            matched.append(name)
    for name, f in actual_by_name.items():
        if name not in base_by_name:
            added.append({"name": name, "baseline": None, "actual": _field_key(f)})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "matched": matched,
    }


def _diff_branch_path(
    baseline: List[Dict[str, Any]],
    actual: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Compare the branch path per conditional field: its matched verdict and
    action must agree."""
    base_by_field = {b["field"]: b for b in baseline}
    actual_by_field = {b["field"]: b for b in actual}

    added, removed, changed, matched = [], [], [], []

    for field, base_entry in base_by_field.items():
        if field not in actual_by_field:
            removed.append({"field": field, "baseline": base_entry, "actual": None})
            continue
        actual_entry = actual_by_field[field]
        differences = []
        for key in ("matched", "action", "actual_value"):
            if actual_entry.get(key) != base_entry.get(key):
                differences.append(
                    {
                        "aspect": key,
                        "baseline": base_entry.get(key),
                        "actual": actual_entry.get(key),
                    }
                )
        if differences:
            changed.append({"field": field, "differences": differences})
        else:
            matched.append(field)
    for field, entry in actual_by_field.items():
        if field not in base_by_field:
            added.append({"field": field, "baseline": None, "actual": entry})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "matched": matched,
    }


def compare_snapshot(
    baseline_snapshot: Dict[str, Any],
    actual_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Diff an actual parse result against a stored baseline snapshot and
    compute the per-check pass rate (fields + branch entries + checksum verdict,
    one point each)."""
    actual_snapshot = capture_snapshot(actual_result)

    fields = _diff_fields(baseline_snapshot["fields"], actual_snapshot["fields"])
    branches = _diff_branch_path(
        baseline_snapshot["branch_path"], actual_snapshot["branch_path"]
    )

    base_checksum = baseline_snapshot.get("checksum_valid")
    actual_checksum = actual_snapshot["checksum_valid"]
    checksum = {
        "baseline_valid": base_checksum,
        "actual_valid": actual_checksum,
        "actual": actual_result.get("checksum"),
    }
    checksum_matched = base_checksum == actual_checksum

    total_fields = len(set(f["name"] for f in baseline_snapshot["fields"])) + len(
        fields["added"]
    )
    total_branches = len(
        {b["field"] for b in baseline_snapshot["branch_path"]}
    ) + len(branches["added"])
    total_checks = total_fields + total_branches + 1
    passed_checks = (
        len(fields["matched"]) + len(branches["matched"]) + int(checksum_matched)
    )
    pass_rate = passed_checks / total_checks if total_checks else 1.0

    return {
        "actual_snapshot": actual_snapshot,
        "fields": fields,
        "branch_path": branches,
        "checksum": checksum,
        "checksum_matched": checksum_matched,
        "passed_checks": passed_checks,
        "total_checks": total_checks,
        "pass_rate": round(pass_rate, 6),
        "passed": (
            not fields["added"]
            and not fields["removed"]
            and not fields["changed"]
            and not branches["added"]
            and not branches["removed"]
            and not branches["changed"]
            and checksum_matched
        ),
    }
