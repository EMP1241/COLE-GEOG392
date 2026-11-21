"""Inspect the preserved handoff without executing its notebook or changing runtime data."""

import collections
import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path


def fingerprint(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def main():
    feature = Path(__file__).resolve().parents[1]
    app = feature.parents[1]
    source = feature / "original" / "2025 industry avg by county master BLS.csv"
    archive = feature / "original" / "2025_annual_by_industry.zip"
    households = app / "data" / "prepared" / "households.csv"
    receipt = json.loads((feature / "intake.json").read_text(encoding="utf-8"))
    for path in (source, archive):
        original = next(item for item in receipt["files"] if item["path"] == path.relative_to(feature).as_posix())
        if fingerprint(path) != original["sha256"]:
            raise ValueError(f"Preserved input differs from intake: {path.name}")

    rows = read_rows(source)
    counties = {row["county_fips"]: (row["county_name"], row["state_abbr"]) for row in read_rows(households)}
    county_rows = [row for row in rows if row["agglvl_code"] in ("74", "75", "76", "78")]
    supplied = {row["area_fips"].zfill(5) for row in county_rows}
    examples = {}
    for code in ("48041", "48113"):
        selected = sorted(
            (row for row in rows if row["area_fips"].zfill(5) == code),
            key=lambda row: int(row["annual_avg_emplvl"]),
            reverse=True,
        )
        top_five = []
        seen = set()
        for row in selected:
            if row["industry_title"] in seen:
                continue
            seen.add(row["industry_title"])
            top_five.append({key: row[key] for key in (
                "industry_code", "industry_title", "own_title", "annual_avg_emplvl", "disclosure_code"
            )})
            if len(top_five) == 5:
                break
        examples[code] = {
            "county_name": counties[code][0],
            "notebook_ranking_method_replay": top_five,
            "education_ownership_rows": [{key: row[key] for key in (
                "own_code", "own_title", "annual_avg_emplvl", "disclosure_code"
            )} for row in selected if row["industry_code"] == "61"],
        }

    with zipfile.ZipFile(archive) as zipped:
        entry = next(name for name in zipped.namelist() if "annual 61 " in name)
        with zipped.open(entry) as stream:
            education = list(csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")))

    result = {
        "date": "2026-10-17",
        "evidence_type": "Full prepared CSV profile, narrow source-archive check and notebook-method replay; notebook not executed.",
        "source_csv_sha256": fingerprint(source),
        "source_archive_sha256": fingerprint(archive),
        "cole_households_sha256": fingerprint(households),
        "rows": len(rows),
        "years": dict(collections.Counter(row["year"] for row in rows)),
        "quarters": dict(collections.Counter(row["qtr"] for row in rows)),
        "industry_codes": sorted({row["industry_code"] for row in rows}),
        "ownership_rows": dict(collections.Counter(row["own_title"] for row in rows)),
        "aggregation_level_rows": dict(collections.Counter(row["agglvl_code"] for row in rows)),
        "disclosure_codes": dict(collections.Counter(row["disclosure_code"] for row in rows)),
        "duplicate_area_ownership_industry_keys": sum(
            count - 1 for count in collections.Counter(
                (row["area_fips"], row["own_code"], row["industry_code"]) for row in rows
            ).values() if count > 1
        ),
        "county_join": {
            "cole_counties": len(counties),
            "matched_counties_with_any_county_row": len(counties.keys() & supplied),
            "missing_counties": [{"county_fips": code, "county_name": counties[code][0], "state_abbr": counties[code][1]}
                                 for code in sorted(counties.keys() - supplied)],
            "source_only_county_codes": sorted(supplied - counties.keys()),
        },
        "examples": examples,
        "source_archive_education_check": {
            "entry": entry,
            "brazos_rows": [{key: row[key] for key in (
                "area_fips", "own_code", "annual_avg_emplvl", "disclosure_code"
            )} for row in education if row["area_fips"] == "48041"],
        },
        "limits": [
            "Any-row county coverage does not establish complete industry or ownership coverage.",
            "Notebook execution, pandas tie order, full nested-archive validation and runtime integration were not tested.",
            "Official BLS disclosure documentation verifies interpretation; supplied file origin and preparation lineage are not independently authenticated.",
        ],
    }
    output = feature / "verification" / "preintegration-profile.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output), "rows": result["rows"], "industry_codes": len(result["industry_codes"]),
                      "withheld_rows": result["disclosure_codes"].get("N", 0), "county_join": {
                          key: value for key, value in result["county_join"].items() if key != "source_only_county_codes"
                      }}, indent=2))


if __name__ == "__main__":
    main()
