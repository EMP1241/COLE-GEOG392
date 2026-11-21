"""Read-only, exact-decimal profile of the preserved county ACS handoff."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

HOME = Path(__file__).resolve().parents[1]
APP = HOME.parents[1]
METRICS = {
    "poverty": {
        "relative_path": "County_Poverty/county_poverty_2024.csv",
        "column": "poverty_rate_percent",
        "estimate_field": "S1701_C03_001E", "moe_field": "S1701_C03_001M",
        "label": "Population below the poverty level",
        "universe": "Population for whom poverty status is determined",
    },
    "education": {
        "relative_path": "County_Education/county_education_2024.csv",
        "column": "bachelors_or_higher_percent",
        "estimate_field": "S1501_C02_015E", "moe_field": "S1501_C02_015M",
        "label": "Adults age 25+ with a bachelor's degree or higher",
        "universe": "Population 25 years and over",
    },
}


def fingerprint(path):
    with path.open("rb") as stream:
        return {"bytes": path.stat().st_size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value, label, *, estimate=False):
    if value == "":
        return None
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid number: {label}") from exc
    require(result.is_finite() and result >= 0, f"Nonfinite or negative number: {label}")
    require(not estimate or result <= 100, f"Percentage above 100: {label}")
    require(result * 10 == (result * 10).to_integral_value(), f"Unexpected precision: {label}")
    return result


def read_counties(path):
    counties = {}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            code = row["county_fips"]
            require(re.fullmatch(r"[0-9]{5}", code) is not None, f"Invalid core FIPS: {code}")
            identity = (row["county_name"], row["state_abbr"])
            require(all(identity), f"Blank core county identity: {code}")
            require(code not in counties or counties[code] == identity, f"Conflicting core county: {code}")
            counties[code] = identity
    require(bool(counties), "No core county identities.")
    return counties


def read_table(path, metric):
    expected = ["county_fips", "county_name_and_state", metric["column"],
                "margin_of_error_percentage_points", "estimate_status", "acs_year"]
    rows = {}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        require(reader.fieldnames == expected, f"Unexpected header: {path.name}")
        for line, row in enumerate(reader, 2):
            require(None not in row and all(value is not None for value in row.values()), f"Malformed row: {path.name}:{line}")
            code = row["county_fips"]
            require(re.fullmatch(r"[0-9]{5}", code) is not None, f"Invalid FIPS: {path.name}:{line}")
            require(code not in rows, f"Duplicate FIPS: {path.name}:{line}")
            require(bool(row["county_name_and_state"].strip()), f"Blank name: {path.name}:{line}")
            require(row["acs_year"] == "2024", f"Mixed/unexpected vintage: {path.name}:{line}")
            estimate = number(row[metric["column"]], f"{path.name}:{line}:estimate", estimate=True)
            moe = number(row["margin_of_error_percentage_points"], f"{path.name}:{line}:MOE")
            require(row["estimate_status"] == ("missing" if estimate is None else "available"), f"Contradictory status: {path.name}:{line}")
            rows[code] = {**row, "source_row": line, "estimate": estimate, "moe": moe}
    require(len(rows) > 3000 and any(row["estimate"] is not None for row in rows.values()), f"Empty/all-missing/incomplete source: {path.name}")
    return rows


def inspect(source, counties_path):
    counties = read_counties(counties_path)
    tables = {name: read_table(source / metric["relative_path"], metric) for name, metric in METRICS.items()}
    overlap = tables["poverty"].keys() & tables["education"].keys()
    require(all(tables["poverty"][code]["county_name_and_state"] == tables["education"][code]["county_name_and_state"] for code in overlap), "Poverty/education county names conflict.")
    report = {"core_counties": len(counties), "county_keys_sha256": hashlib.sha256(("\n".join(sorted(counties)) + "\n").encode()).hexdigest(),
              "core_input": {"file": str(counties_path), **fingerprint(counties_path)},
              "period": "2020-2024 ACS 5-year", "source_vintage": 2024,
              "cross_metric_key_sets_equal": tables["poverty"].keys() == tables["education"].keys(), "metrics": {}}
    for name, rows in tables.items():
        known = [row for row in rows.values() if row["estimate"] is not None]
        with_moe = [row for row in known if row["moe"] is not None]
        missing = sorted(counties.keys() - rows.keys())
        excluded = sorted(rows.keys() - counties.keys())
        largest = sorted(with_moe, key=lambda row: (-row["moe"], row["county_fips"]))[:5]
        report["metrics"][name] = {
            "input": {"file": METRICS[name]["relative_path"], **fingerprint(source / METRICS[name]["relative_path"])},
            "rows": len(rows), "unique_county_ids": len(rows), "available_estimates": len(known),
            "missing_estimates": len(rows) - len(known), "missing_margins_of_error": sum(row["moe"] is None for row in rows.values()),
            "estimate_range_percent": [str(min(row["estimate"] for row in known)), str(max(row["estimate"] for row in known))],
            "margin_of_error_range_points": [str(min(row["moe"] for row in with_moe)), str(max(row["moe"] for row in with_moe))] if with_moe else None,
            "zero_estimate_count": sum(row["estimate"] == 0 for row in known),
            "unbounded_interval_extends_below_zero_or_above_100": sum(row["estimate"] - row["moe"] < 0 or row["estimate"] + row["moe"] > 100 for row in with_moe),
            "covered_core_counties": len(rows.keys() & counties.keys()),
            "missing_core_counties": [{"fips": code, "county": counties[code][0], "state": counties[code][1]} for code in missing],
            "source_only_counties": [{"fips": code, "name": rows[code]["county_name_and_state"]} for code in excluded],
            "source_only_counts_by_state_fips": dict(sorted(Counter(code[:2] for code in excluded).items())),
            "largest_margins_of_error": [{"fips": row["county_fips"], "name": row["county_name_and_state"], "estimate": str(row["estimate"]), "moe": str(row["moe"])} for row in largest],
            "brazos": {key: value for key, value in rows["48041"].items() if key not in ("estimate", "moe")},
        }
    return report, counties, tables


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=HOME / "original")
    parser.add_argument("--counties", type=Path, default=APP / "data/prepared/households.csv")
    parser.add_argument("--out", type=Path, default=HOME / "verification/preintegration-profile.json")
    args = parser.parse_args()
    report, _, _ = inspect(args.source, args.counties)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps({name: {key: row[key] for key in ("rows", "covered_core_counties", "missing_estimates", "missing_margins_of_error", "source_only_counts_by_state_fips", "largest_margins_of_error")}
                      for name, row in report["metrics"].items()}, indent=2))


if __name__ == "__main__":
    main()
