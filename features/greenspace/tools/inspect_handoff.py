"""Profile contributor greenspace values without guessing their source or units."""
import csv
from decimal import Decimal
import hashlib
import json
from pathlib import Path

APP = Path(__file__).resolve().parents[3]
HOME = Path(__file__).resolve().parents[1]
FIELDS = ("% Federal", "% Local", "% State", "% Total Open Greenspace")


def inspect():
    path = HOME / "original/master_calculated_greenspace.csv"
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == ["FIPS", "County_Name", *FIELDS]
        rows = list(reader)
    with (APP / "data/prepared/households.csv").open(newline="", encoding="utf-8") as stream:
        core = {row["county_fips"] for row in csv.DictReader(stream)}
    keys = {row["FIPS"] for row in rows}
    assert len(keys) == len(rows)
    assert all(len(key) == 5 and key.isascii() and key.isdigit() for key in keys)
    invalid = []
    for line, row in enumerate(rows, 2):
        assert None not in row and None not in row.values()
        values = [Decimal(row[key]) for key in FIELDS]
        assert all(value.is_finite() for value in values)
        if any(value < 0 or value > 100 for value in values):
            invalid.append({"source_row": line, **row})
    return {"status": "method and data correction required before runtime integration",
            "source_file": path.name, "source_bytes": path.stat().st_size,
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_rows": len(rows), "core_counties": len(core), "matched_counties": len(keys & core),
            "missing_core_counties": sorted(core - keys), "source_only_counties": sorted(keys - core),
            "out_of_range_rows": invalid,
            "contributor": "Margaret (Brandon-confirmed; surname not supplied)",
            "unknowns": ["publisher/dataset", "source year", "county geography vintage",
                         "open-access definition", "percentage denominator", "overlap/dissolve/clipping/area method",
                         "meaning of omitted counties"],
            "interpretation": "These values cannot currently establish a percent of county area. Values and missing records are preserved without clamping or zero-filling."}


if __name__ == "__main__":
    report = inspect()
    output = HOME / "verification"
    output.mkdir(exist_ok=True)
    (output / "preintegration-profile.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"source_rows": report["source_rows"], "matched_counties": report["matched_counties"],
                      "missing_core_counties": len(report["missing_core_counties"]), "out_of_range_rows": report["out_of_range_rows"]}, indent=2))
