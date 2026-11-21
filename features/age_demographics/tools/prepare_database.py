"""Prepare an offline age-data candidate without executing contributor code.

This curates data only. It does not register a feature or change COLE behavior.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

APP = Path(__file__).resolve().parents[3]
HOME = Path(__file__).resolve().parents[1]
SOURCE = HOME / "original/Age-Addon/data"
GROUPS = {
    "under_18": ("Under 18", 3, 6),
    "18_24": ("18–24", 7, 10),
    "25_34": ("25–34", 11, 12),
    "35_44": ("35–44", 13, 14),
    "45_54": ("45–54", 15, 16),
    "55_64": ("55–64", 17, 19),
    "65_plus": ("65 and over", 20, 25),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fingerprint(path):
    return {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def rows(path, key):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        result = {}
        for line, row in enumerate(reader, 2):
            require(None not in row and None not in row.values(), f"Malformed {path.name}:{line}")
            code = row[key]
            require(code not in result, f"Duplicate {key}: {code}")
            result[code] = (line, row)
        return result


def numeric(raw, integer=False):
    # Keep original source tokens separately. Negative Census sentinel codes
    # and nonnumeric annotations never become numeric zero or a numeric MOE.
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0:
        return None
    if integer:
        require(number == number.to_integral_value(), f"Noninteger Census count: {raw}")
        return int(number)
    with localcontext() as context:
        context.prec = 40
        scaled = number * 10
    require(scaled == scaled.to_integral_value(), f"Unexpected age precision: {raw}")
    return int(scaled)


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE source_files(filename TEXT PRIMARY KEY, bytes INTEGER NOT NULL,
 sha256 TEXT NOT NULL, publisher_url TEXT) WITHOUT ROWID;
CREATE TABLE counties(county_fips TEXT PRIMARY KEY CHECK(length(county_fips)=5),
 covered INTEGER NOT NULL CHECK(covered IN (0,1)), population INTEGER CHECK(population>=0),
 population_moe INTEGER CHECK(population_moe>=0), median_age_tenths INTEGER CHECK(median_age_tenths>=0),
 median_age_moe_tenths INTEGER CHECK(median_age_moe_tenths>=0),
 age_source_row INTEGER, median_source_row INTEGER,
 CHECK(covered=1 OR (population IS NULL AND population_moe IS NULL AND median_age_tenths IS NULL
 AND median_age_moe_tenths IS NULL AND age_source_row IS NULL AND median_source_row IS NULL))) WITHOUT ROWID;
CREATE TABLE age_groups(group_id TEXT PRIMARY KEY, label TEXT NOT NULL,
 estimate_columns_json TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE group_counts(county_fips TEXT REFERENCES counties(county_fips),
 group_id TEXT REFERENCES age_groups(group_id), population INTEGER CHECK(population>=0),
 PRIMARY KEY(county_fips,group_id)) WITHOUT ROWID;
CREATE TABLE source_cells(table_id TEXT NOT NULL, geo_id TEXT NOT NULL,
 variable TEXT NOT NULL, raw_value TEXT NOT NULL, source_row INTEGER NOT NULL,
 PRIMARY KEY(table_id,geo_id,variable)) WITHOUT ROWID;
CREATE INDEX group_counts_by_group ON group_counts(group_id,population,county_fips);
"""


def build(output):
    require(not output.exists(), f"Refusing to overwrite {output}")
    age = rows(SOURCE / "b01001_2024_counties.csv", "GEO_ID")
    median = rows(SOURCE / "b01002_2024_counties.csv", "GEO_ID")
    prepared = rows(SOURCE / "county_age_2024.csv", "county_fips")
    geography = rows(SOURCE / "geography_2024_counties.csv", "GEO_ID")
    require(set(age) == set(median) == set(geography) and len(age) == 3222, "Source geography mismatch")
    with (APP / "data/prepared/households.csv").open(encoding="utf-8", newline="") as stream:
        core = {r["county_fips"] for r in csv.DictReader(stream)}
    require(all(len(code) == 5 and code.isascii() and code.isdigit() for code in core), "Invalid core FIPS")
    provenance = json.loads((SOURCE / "sources_2024.json").read_text())
    urls = {s["csv"]: s["url"] for s in provenance["sources"]}
    for source in provenance["sources"]:
        require(fingerprint(SOURCE / source["csv"])["sha256"] == source["csv_sha256"], "Source hash mismatch")
    columns = {key: [f"B01001_E{cell:03}" for cell in (*range(lo, hi + 1), *range(lo + 24, hi + 25))]
               for key, (label, lo, hi) in GROUPS.items()}
    prepared_values = {}
    for code, (line, supplied) in prepared.items():
        geo = "0500000US" + code
        require(geo in age and supplied["state_fips"] == code[:2] and
                (supplied["acs_start_year"], supplied["acs_end_year"]) == ("2020", "2024"), "Prepared identity/period mismatch")
        ar, mr = age[geo][1], median[geo][1]
        total = numeric(ar["B01001_E001"], integer=True)
        pop_moe = numeric(ar["B01001_M001"], integer=True)
        estimate = numeric(mr["B01002_E001"])
        moe = numeric(mr["B01002_M001"])
        require((total, pop_moe, estimate, moe) ==
                (numeric(supplied["population"], True), numeric(supplied["population_moe"], True),
                 numeric(supplied["median_age"]), numeric(supplied["median_age_moe"])), f"Prepared totals differ: {code}")
        counts = {}
        for group, cells in columns.items():
            values = [numeric(ar[c], integer=True) for c in cells]
            count = None if None in values else sum(values)
            require(count == numeric(supplied["population_" + group], True), f"Prepared group differs: {code}/{group}")
            if count is not None and total:
                with localcontext() as context:
                    context.prec = 50
                    exact = Decimal(count) * 100 / Decimal(total)
                    require(abs(exact - Decimal(supplied["pct_" + group])) < Decimal("0.000000000001"), "Prepared share differs")
            else:
                require(numeric(supplied["pct_" + group]) is None, "Share supplied without denominator")
            counts[group] = count
        require(None not in counts.values() and sum(counts.values()) == total, f"Population partition differs: {code}")
        prepared_values[code] = (total, pop_moe, estimate, moe, counts)
    require(len(prepared_values) == 3144, "Unexpected supplied prepared coverage")
    county_hash = hashlib.sha256(("\n".join(sorted(core)) + "\n").encode()).hexdigest()
    manifest = {"status": "Prepared data candidate; no runtime integration", "schema_version": 1,
                "feature_version": "1.0.0", "publisher": "U.S. Census Bureau",
                "add_on_contributor": "Mitch Zhang", "attribution_basis": "Brandon's user instruction",
                "dataset": "ACS 5-Year Detailed Tables", "period_start": 2020, "period_end": 2024,
                "county_keys_sha256": county_hash, "sources": {p.name: fingerprint(p) for p in sorted(SOURCE.iterdir()) if p.is_file()},
                "coverage": {"core_counties": len(core), "covered_core_counties": len(core & prepared_values.keys()),
                             "missing_core": sorted(core - prepared_values.keys()), "source_only": sorted(prepared_values.keys() - core)},
                "storage": "Integer people; median age and its published MOE in integer tenths of years; NULL unavailable; no floating percentages stored",
                "derived_share_method": "100 * group population / total B01001 population; seven mutually exclusive groups partition the total",
                "uncertainty": "Published source MOEs preserved. Derived age-group count/share MOEs are not calculated or claimed.",
                "provenance_limit": "Full publisher download bodies were not supplied or independently replayed; the extract hashes match supplied provenance.",
                "filter_policy": "Runtime/UI choices not implemented in this intake checkpoint"}
    metadata = {"schema_version": "1", "period_start": "2020", "period_end": "2024", "county_keys_sha256": county_hash}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".age-candidate-", dir=output.parent) as temporary:
        staging = Path(temporary) / "release"
        staging.mkdir()
        db = staging / "age.sqlite3"
        with closing(sqlite3.connect(db)) as connection:
            connection.executescript(SCHEMA)
            with connection:
                connection.executemany("INSERT INTO metadata VALUES (?,?)", sorted(metadata.items()))
                connection.executemany("INSERT INTO source_files VALUES (?,?,?,?)", [(name, s["bytes"], s["sha256"], urls.get(name)) for name, s in manifest["sources"].items()])
                connection.executemany("INSERT INTO age_groups VALUES (?,?,?)", [(key, GROUPS[key][0], json.dumps(cells)) for key, cells in columns.items()])
                for code in sorted(core):
                    values = prepared_values.get(code)
                    geo = "0500000US" + code
                    connection.execute("INSERT INTO counties VALUES (?,?,?,?,?,?,?,?)", (code, int(values is not None),
                        *(values[:4] if values else (None,) * 4), age[geo][0] if values else None, median[geo][0] if values else None))
                    connection.executemany("INSERT INTO group_counts VALUES (?,?,?)", [(code, key, values[4][key] if values else None) for key in GROUPS])
                for table_id, table in (("B01001", age), ("B01002", median)):
                    for geo, (line, row) in sorted(table.items()):
                        connection.executemany("INSERT INTO source_cells VALUES (?,?,?,?,?)", [(table_id, geo, key, value, line) for key, value in sorted(row.items()) if key.startswith(table_id + "_")])
            require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "Database integrity failure")
            require(connection.execute("PRAGMA foreign_key_check").fetchall() == [], "Foreign key failure")
        manifest["outputs"] = {db.name: fingerprint(db)}
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        staging.rename(output)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.out.resolve())
    print(json.dumps({"status": result["status"], "coverage": result["coverage"], "outputs": result["outputs"]}, indent=2))
