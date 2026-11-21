"""Independently replay every curated ACS value and check read-only SQLite behavior."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

HOME = Path(__file__).resolve().parents[1]
APP = HOME.parents[1]


def fingerprint(path):
    with path.open("rb") as stream:
        return {"bytes": path.stat().st_size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    database = HOME / "prepared/poverty_education.sqlite3"
    manifest = json.loads((HOME / "prepared/manifest.json").read_text(encoding="utf-8"))
    before = fingerprint(database)
    require(before == manifest["outputs"][database.name], "Database differs from its manifest.")
    with (APP / "data/prepared/households.csv").open(newline="", encoding="utf-8") as stream:
        core = {row["county_fips"] for row in csv.DictReader(stream)}
    inputs = {"poverty": ("County_Poverty/county_poverty_2024.csv", "poverty_rate_percent"),
              "education": ("County_Education/county_education_2024.csv", "bachelors_or_higher_percent")}
    raw = {}
    for metric, (relative, _) in inputs.items():
        with (HOME / "original" / relative).open(newline="", encoding="utf-8-sig") as stream:
            raw[metric] = {row["county_fips"]: (line, row) for line, row in enumerate(csv.DictReader(stream), 2)}
    uri = database.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "SQLite integrity check failed.")
        require(connection.execute("PRAGMA foreign_key_check").fetchall() == [], "SQLite foreign-key check failed.")
        require({row[0] for row in connection.execute("SELECT county_fips FROM counties")} == core, "Curated county identities differ.")
        records = connection.execute("SELECT county_fips,metric_id,estimate_tenths,margin_of_error_tenths,source_status,source_name,source_row FROM observations").fetchall()
        require(len(records) == len(core) * 2 == 6286, "Curated observation membership differs.")
        require({(row[0], row[1]) for row in records} == {(code, metric) for code in core for metric in inputs}, "Missing county/metric keys.")
        for code, metric, estimate, moe, status, name, line in records:
            source = raw[metric].get(code)
            if source is None:
                require((estimate, moe, status, name, line) == (None, None, "not_covered", None, None), f"Unavailable county changed: {code}/{metric}")
                continue
            source_line, row = source
            require((name, line, status) == (row["county_name_and_state"], source_line, row["estimate_status"]), f"Source identity/line differs: {code}/{metric}")
            for stored, key in ((estimate, inputs[metric][1]), (moe, "margin_of_error_percentage_points")):
                require((stored is None and row[key] == "") or (stored is not None and Decimal(stored) / 10 == Decimal(row[key])), f"Estimate or MOE differs: {code}/{metric}/{key}")
        statuses = dict(connection.execute("SELECT source_status,count(*) FROM observations GROUP BY source_status"))
        require(statuses == {"available": 6270, "not_covered": 16}, "Availability differs.")
        require(connection.execute("SELECT estimate_tenths,margin_of_error_tenths FROM observations WHERE county_fips='48301' AND metric_id='education'").fetchone() == (0, 661), "True zero or large MOE changed.")
        query_plan = connection.execute("EXPLAIN QUERY PLAN SELECT county_fips FROM observations WHERE metric_id='poverty' AND estimate_tenths BETWEEN 100 AND 150").fetchall()
        require(any("observations_by_metric_estimate" in row[3] for row in query_plan), "Range query is not indexed.")
        try:
            connection.execute("UPDATE observations SET estimate_tenths=1 WHERE county_fips='48041'")
        except sqlite3.OperationalError as exc:
            require("readonly" in str(exc).lower(), "Unexpected read-only write failure.")
        else:
            raise RuntimeError("Read-only connection allowed a write.")

    def profile(_):
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            return connection.execute("SELECT metric_id,estimate_tenths,margin_of_error_tenths FROM observations WHERE county_fips='48041' ORDER BY metric_id").fetchall()

    with ThreadPoolExecutor(max_workers=8) as executor:
        profiles = list(executor.map(profile, range(24)))
    require(all(value == [("education", 430, 12), ("poverty", 243, 13)] for value in profiles), "Parallel profiles differ.")
    verification = HOME / "verification"
    verification.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".rebuild-", dir=verification) as temporary:
        output = Path(temporary) / "prepared"
        result = subprocess.run([sys.executable, str(HOME / "tools/prepare_database.py"), "--out", str(output)], capture_output=True, text=True)
        require(result.returncode == 0, f"Independent rebuild failed: {result.stderr}")
        require(fingerprint(output / database.name) == before, "The SQLite rebuild is not byte deterministic.")
        refused = subprocess.run([sys.executable, str(HOME / "tools/prepare_database.py"), "--out", str(output)], capture_output=True, text=True)
        require(refused.returncode != 0 and "Refusing to overwrite" in refused.stderr, "Preparation overwrote an existing release.")
        require(fingerprint(output / database.name) == before, "Refused preparation changed the prior release.")
    intake = json.loads((HOME / "intake.json").read_text(encoding="utf-8"))
    for entry in intake["files"]:
        require(fingerprint(HOME / entry["path"]) == {key: entry[key] for key in ("bytes", "sha256")}, "An original payload changed.")
    require(fingerprint(Path(intake["source_path"])) == fingerprint(HOME / intake["preserved_archive"]) == {"bytes": intake["source_bytes"], "sha256": intake["source_sha256"]}, "The source/preserved ZIP changed.")
    require(fingerprint(database) == before, "Verification changed the release.")
    record = {"status": "passed", "checked_utc": datetime.now(timezone.utc).isoformat(), "database": before,
              "core_counties": len(core), "observations_independently_compared": len(records), "statuses": statuses,
              "true_zero_and_large_moe_preserved": True, "readonly_write_rejected": True, "integrity_and_foreign_keys": "passed",
              "concurrent_profiles": 24, "indexed_range_query": query_plan, "deterministic_rebuild": True,
              "existing_release_overwrite_refused": True, "all_originals_and_download_preserved": True,
              "scope": "Offline curation verification only; no runtime/UI integration or independent full Census data download."}
    (verification / "database-receipt.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
