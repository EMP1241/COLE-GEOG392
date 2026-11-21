"""Inspect publisher statistics for planning; does not prepare runtime data."""

import csv
import hashlib
import io
import json
from collections import Counter, defaultdict
from decimal import Decimal, localcontext
from pathlib import Path
from zipfile import ZipFile


def main():
    home = Path(__file__).resolve().parents[1]
    archive = home / "source/padus-4.1/PADUS4_1SummaryStatistics_TabularData_CSV.zip.zip"
    with ZipFile(archive) as source:
        member = next(n for n in source.namelist() if n.endswith("_Counties_Clip_CENSUS2022.csv"))
        body = source.read(member)
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"), newline="")))
    core = json.loads((home.parents[1] / "data/prepared/counties.geojson").read_text(encoding="utf-8"))
    counties = {f["properties"]["county_fips"]: f["properties"] for f in core["features"]}
    # Diagnostic exact labels only. This is NOT an authoritative FIPS crosswalk.
    labels = defaultdict(list)
    for fips, county in counties.items():
        labels[f"{county['county_name']}, {county['state_abbr']}"].append(fips)
    assert all(len(keys) == 1 for keys in labels.values())
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["BndryName"]].append(row)
    matched = {labels[name][0] for name in grouped if name in labels}
    examples = {}
    with localcontext() as context:
        context.prec = 80
        for name in ("Brazos County, TX", "Keweenaw County, MI", "San Juan County, WA"):
            amounts = {access: sum((Decimal(r["GIS_AcrsDb"]) for r in grouped[name]
                                   if r["Pub_Access"] == access), Decimal(0))
                       for access in ("OA", "RA", "XA", "UK")}
            total = sum(amounts.values(), Decimal(0))
            examples[name] = {
                "publisher_area_acres_by_access": {key: str(value) for key, value in amounts.items()},
                "all_access_rows_acres": str(total),
                "open_fraction_of_all_publisher_rows_percent": str(amounts["OA"] * 100 / total),
                "warning": "Planning arithmetic only; geographic extent/denominator not independently validated.",
            }
    report = {
        "purpose": "Source inspection for planning, not a prepared dataset or runtime verification",
        "retrieved_date": "2026-10-21",
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "county_csv_member": member,
        "county_csv_sha256": hashlib.sha256(body).hexdigest(),
        "rows": len(rows),
        "distinct_boundary_names": len(grouped),
        "distinct_state_and_boundary_pairs": len({(r["ST_Name"], r["BndryName"]) for r in rows}),
        "access_row_counts": dict(Counter(r["Pub_Access"] for r in rows)),
        "manager_row_counts": dict(Counter(r["MngTp_Desc"] for r in rows)),
        "boundary_id_length_counts": dict(Counter(len(r["BndryID"]) for r in rows)),
        "boundary_id_warning": "BndryID contains state codes, not county FIPS. Do not join on it.",
        "exceptional_identity_rows": [r for r in rows if len(r["BndryID"]) != 2],
        "diagnostic_exact_label_matching": {
            "authoritative_crosswalk": False,
            "core_counties": len(counties),
            "core_counties_with_matching_label": len(matched),
            "core_without_matching_label": [counties[key] for key in sorted(counties.keys() - matched)],
            "publisher_labels_without_core_match": sorted(set(grouped) - labels.keys()),
        },
        "examples": examples,
        "unresolved": [
            "Resolve county identifiers against an authoritative 2022 Census reference.",
            "Validate complete county denominator, water treatment and boundary extent.",
            "Publisher flattening prioritizes conservation status before public access; it is not an any-open overlap union.",
            "PAD-US is an estimate of documented areas, not a complete inventory of every outdoor opportunity.",
        ],
    }
    target = home / "verification/padus-source-profile.json"
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(target), "rows": len(rows), "boundary_names": len(grouped),
                      "diagnostic_core_label_matches": len(matched), "core_counties": len(counties)}))


if __name__ == "__main__":
    main()
