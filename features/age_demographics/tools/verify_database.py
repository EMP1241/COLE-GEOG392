"""Independently compare the age candidate with the preserved prepared CSV."""
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from prepare_database import APP, HOME, SOURCE, build


def verify():
    release = HOME / "prepared"
    manifest = json.loads((release / "manifest.json").read_text())
    database = release / "age.sqlite3"
    expected_hash = manifest["outputs"][database.name]["sha256"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == expected_hash
    with (SOURCE / "county_age_2024.csv").open(newline="", encoding="utf-8") as stream:
        oracle = {row["county_fips"]: row for row in csv.DictReader(stream)}
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    checked = 0
    try:
        assert [tuple(row) for row in connection.execute("PRAGMA integrity_check")] == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for row in connection.execute("SELECT * FROM counties"):
            code = row["county_fips"]
            supplied = oracle.get(code)
            assert bool(row["covered"]) == (supplied is not None)
            groups = {r["group_id"]: r["population"] for r in connection.execute("SELECT * FROM group_counts WHERE county_fips=?", (code,))}
            assert len(groups) == 7
            if supplied is None:
                assert all(value is None for value in groups.values())
            else:
                assert row["population"] == int(float(supplied["population"]))
                # String parsing gives an independent fixed-point conversion.
                for column, source in (("median_age_tenths", "median_age"), ("median_age_moe_tenths", "median_age_moe")):
                    raw = supplied[source]
                    whole, _, fraction = raw.partition(".")
                    assert row[column] == (int(whole) * 10 + int(fraction.ljust(1, "0"))) if raw else row[column] is None
                assert all(groups[key] == int(supplied["population_" + key]) for key in groups)
                assert sum(groups.values()) == row["population"]
                checked += 1
        try:
            connection.execute("DELETE FROM counties")
        except sqlite3.OperationalError as error:
            assert "readonly" in str(error).lower()
        else:
            raise AssertionError("Read-only connection accepted a write")
    finally:
        connection.close()
    with tempfile.TemporaryDirectory(prefix="verify-age-", dir=APP / "output") as scratch:
        rebuilt = build(Path(scratch) / "release")
        assert rebuilt["outputs"] == manifest["outputs"]
        try:
            build(Path(scratch) / "release")
        except ValueError as error:
            assert "overwrite" in str(error)
        else:
            raise AssertionError("Existing output was overwritten")
    return {"status": "passed", "covered_counties_compared": checked, "core_counties": 3143,
            "unavailable_counties": manifest["coverage"]["missing_core"],
            "database_sha256": expected_hash, "checks": ["original prepared CSV values and all seven groups",
            "integrity and foreign keys", "read-only write rejection", "byte-identical rebuild", "existing-output refusal"],
            "limits": "Offline data candidate only; no runtime, GUI or portable integration is claimed."}


if __name__ == "__main__":
    report = verify()
    output = HOME / "verification"
    output.mkdir(exist_ok=True)
    (output / "database-receipt.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
