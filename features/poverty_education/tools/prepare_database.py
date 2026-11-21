"""Curate the supplied ACS CSVs into one new, feature-owned SQLite release offline."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile

from inspect_handoff import APP, HOME, METRICS, fingerprint, inspect, require

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE counties(
    county_fips TEXT PRIMARY KEY CHECK(length(county_fips)=5),
    county_name TEXT NOT NULL, state_abbr TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE metrics(
    metric_id TEXT PRIMARY KEY, label TEXT NOT NULL, universe TEXT NOT NULL,
    estimate_field TEXT NOT NULL, moe_field TEXT NOT NULL,
    period_start INTEGER NOT NULL, period_end INTEGER NOT NULL,
    units TEXT NOT NULL, stored_scale INTEGER NOT NULL, moe_confidence_percent INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE source_files(
    metric_id TEXT PRIMARY KEY REFERENCES metrics(metric_id), filename TEXT NOT NULL,
    bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, endpoint TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE observations(
    county_fips TEXT NOT NULL REFERENCES counties(county_fips),
    metric_id TEXT NOT NULL REFERENCES source_files(metric_id),
    estimate_tenths INTEGER CHECK(estimate_tenths BETWEEN 0 AND 1000),
    margin_of_error_tenths INTEGER CHECK(margin_of_error_tenths>=0),
    source_status TEXT NOT NULL CHECK(source_status IN ('available','missing','not_covered')),
    source_name TEXT, source_row INTEGER CHECK(source_row>=2),
    PRIMARY KEY(county_fips,metric_id),
    CHECK((source_status='available' AND estimate_tenths IS NOT NULL) OR
          (source_status IN ('missing','not_covered') AND estimate_tenths IS NULL)),
    CHECK((source_status='not_covered' AND source_name IS NULL AND source_row IS NULL AND margin_of_error_tenths IS NULL) OR
          (source_status IN ('available','missing') AND source_name IS NOT NULL AND source_row IS NOT NULL))
) WITHOUT ROWID;
CREATE INDEX observations_by_metric_estimate ON observations(metric_id,estimate_tenths,county_fips);
"""


def build(source, counties_path, output):
    require(not output.exists(), f"Refusing to overwrite an existing release: {output}")
    profile, counties, tables = inspect(source, counties_path)
    input_fingerprints = {name: fingerprint(source / metric["relative_path"]) for name, metric in METRICS.items()}
    intake = json.loads((HOME / "intake.json").read_text(encoding="utf-8"))
    supplied = {row["path"]: {key: row[key] for key in ("bytes", "sha256")} for row in intake["files"]}
    for name, metric in METRICS.items():
        require(input_fingerprints[name] == supplied["original/" + metric["relative_path"]], f"Source differs from the preserved intake: {name}")
    endpoint = "https://api.census.gov/data/2024/acs/acs5/subject"
    metadata = {"schema_version": "1", "feature_version": "1.0.0", "source_vintage": "2024",
                "period_start": "2020", "period_end": "2024", "stored_scale": "10",
                "county_keys_sha256": profile["county_keys_sha256"],
                "poverty_source_sha256": input_fingerprints["poverty"]["sha256"],
                "education_source_sha256": input_fingerprints["education"]["sha256"]}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".poverty-education-", dir=output.parent) as temporary:
        staging = Path(temporary) / "release"
        staging.mkdir()
        database = staging / "poverty_education.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(SCHEMA)
            with connection:
                connection.executemany("INSERT INTO metadata VALUES (?,?)", sorted(metadata.items()))
                connection.executemany("INSERT INTO counties VALUES (?,?,?)", [(code, *counties[code]) for code in sorted(counties)])
                for name, metric in METRICS.items():
                    connection.execute("INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?,?,?)", (name, metric["label"], metric["universe"], metric["estimate_field"], metric["moe_field"], 2020, 2024, "percent", 10, 90))
                    inputs = input_fingerprints[name]
                    connection.execute("INSERT INTO source_files VALUES (?,?,?,?,?)", (name, Path(metric["relative_path"]).name, inputs["bytes"], inputs["sha256"], endpoint))
                    observations = []
                    for code in sorted(counties):
                        row = tables[name].get(code)
                        observations.append((code, name, None if row is None or row["estimate"] is None else int(row["estimate"] * 10),
                                             None if row is None or row["moe"] is None else int(row["moe"] * 10),
                                             "not_covered" if row is None else row["estimate_status"],
                                             None if row is None else row["county_name_and_state"], None if row is None else row["source_row"]))
                    connection.executemany("INSERT INTO observations VALUES (?,?,?,?,?,?,?)", observations)
            require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "SQLite integrity failed.")
            require(connection.execute("PRAGMA foreign_key_check").fetchall() == [], "SQLite foreign keys failed.")
            connection.execute("VACUUM")
        require(input_fingerprints == {name: fingerprint(source / metric["relative_path"]) for name, metric in METRICS.items()}, "Source changed during preparation.")
        require(fingerprint(counties_path) == {key: profile["core_input"][key] for key in ("bytes", "sha256")}, "Core identities changed during preparation.")
        manifest = {
            "feature_version": "1.0.0", "schema_version": 1, "prepared_utc": datetime.now(timezone.utc).isoformat(),
            "status": "Prepared data only; not registered in the COLE runtime.",
            "publisher": "U.S. Census Bureau", "dataset": "ACS 5-Year Subject Tables", "source_vintage": 2024,
            "period_start": 2020, "period_end": 2024, "county_keys_sha256": profile["county_keys_sha256"],
            "source_endpoint": endpoint, "source_files": input_fingerprints,
            "supplied_archive_sha256": intake["source_sha256"], "original_contributor": None, "original_download_timestamp": None,
            "storage": {"scale": 10, "explanation": "Percent and percentage-point MOE stored as integer tenths; NULL remains unavailable; a true zero remains 0."},
            "filter_policy": {"user_approved": "Optional independent minimum and maximum percentages for both metrics; filter published point estimates and display margins of error.",
                              "defaults": "All limits blank/inactive; supplied 15/30 thresholds are examples only.", "runtime_implemented": False},
            "coverage": {"cole_counties": len(counties), "observations": len(counties) * len(METRICS),
                         "metrics": {name: {key: profile["metrics"][name][key] for key in ("covered_core_counties", "missing_core_counties", "source_only_counts_by_state_fips")}
                                     for name in METRICS}},
            "provenance_limits": "Supplied CSV bytes are fingerprinted; original download responses and annotation fields were not supplied or independently replayed.",
            "outputs": {database.name: fingerprint(database)},
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        require(not output.exists(), f"Release destination appeared during preparation: {output}")
        staging.rename(output)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=HOME / "original")
    parser.add_argument("--counties", type=Path, default=APP / "data/prepared/households.csv")
    parser.add_argument("--out", type=Path, default=HOME / "prepared")
    args = parser.parse_args()
    manifest = build(args.source, args.counties, args.out)
    print(json.dumps({"output": str(args.out), "database": manifest["outputs"], "counties": manifest["coverage"]["cole_counties"],
                      "observations": manifest["coverage"]["observations"]}, indent=2))


if __name__ == "__main__":
    main()
