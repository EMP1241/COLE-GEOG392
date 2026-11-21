"""Read the audited scalar release; no GIS processing occurs in the application."""
from collections import Counter
from contextlib import closing
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import sqlite3

ACRE_M2 = 4046.8564224
OWNERSHIP = {
    "federal": ("Federal", "federal_exclusive_m2"),
    "state": ("State", "state_exclusive_m2"),
    "local_regional": ("Local / regional", "local_regional_exclusive_m2"),
    "territorial": ("Territorial", "territorial_exclusive_m2"),
    "shared_public": ("Shared public ownership", "shared_public_m2"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


class GreenspaceStore:
    def __init__(self, directory, counties, county_geometry):
        directory = Path(directory)
        self.path = directory / "greenspace.sqlite3"
        self.manifest = manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        prepared = (directory / "preparation-manifest.json").read_bytes()
        require(manifest["feature_version"] == "1.0.0" and hashlib.sha256(prepared).hexdigest() == manifest["preparation_manifest_sha256"],
                "Greenspace runtime provenance differs.")
        require({key: value for key, value in manifest.items() if key not in ("feature_version", "preparation_manifest_sha256")}
                == json.loads(prepared), "Greenspace prepared release differs.")
        require((manifest["schema_version"], manifest["dataset"], manifest["county_vintage"], manifest["area_crs"])
                == (1, "PAD-US 4.1", 2021, "ESRI:102039"), "Unsupported Greenspace release.")
        require(manifest["acre_m2"] == ACRE_M2, "Greenspace area units differ.")
        with Path(county_geometry).open("rb") as stream:
            require(hashlib.file_digest(stream, "sha256").hexdigest() == manifest["core_geography_sha256"],
                    "Greenspace county geography differs from the active COLE release.")
        expected = manifest["database"]
        require(expected["file"] == self.path.name, "Greenspace database name differs.")
        with self.path.open("rb") as stream:
            require(self.path.stat().st_size == expected["bytes"] and
                    hashlib.file_digest(stream, "sha256").hexdigest() == expected["sha256"],
                    "Greenspace database fingerprint differs.")
        audit = manifest["publication_audit"]["full_source_primary_precision_replays"]
        require(audit["status"] == "passed" and audit["compared_counties"] == 3234 and
                audit["flagged_counties"] == [], "Greenspace preparation is incomplete.")
        self.contract = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        self.records, self.values = {}, {}
        with self.connect() as connection:
            require(connection.execute("PRAGMA user_version").fetchone()[0] == 1 and
                    connection.execute("PRAGMA application_id").fetchone()[0] == 1129270343,
                    "Greenspace database schema differs.")
            metadata = {r[0]: json.loads(r[1]) for r in connection.execute("SELECT key,value_json FROM metadata")}
            require(all(key in metadata for key in ("schema_version", "dataset", "county_vintage", "credits", "policy", "publication_audit"))
                    and all(value == manifest.get(key) for key, value in metadata.items()),
                    "Greenspace metadata differs from its release.")
            credits = dict(connection.execute("SELECT role,credit FROM credits"))
            require(credits == {key: value if isinstance(value, str) else "; ".join(value)
                                for key, value in manifest["credits"].items()}, "Greenspace credits differ.")
            rows = connection.execute("SELECT * FROM county_metrics ORDER BY county_fips").fetchall()
            accounting = {r["county_fips"]: r for r in connection.execute("SELECT * FROM county_accounting")}
            require(len(rows) == manifest["county_rows"] == 3234 and
                    {r["county_fips"] for r in rows} == set(accounting), "Greenspace county accounting is incomplete.")
            require({r["county_fips"] for r in rows if r["core_county"]} == set(counties) and
                    manifest["core_county_rows"] == len(counties), "Greenspace core county identities differ.")
            require(dict(Counter(r["coverage_status"] for r in rows)) == manifest["coverage_status_counts"],
                    "Greenspace coverage differs.")
            for row in rows:
                code = row["county_fips"]
                require(isinstance(code, str) and len(code) == 5 and code.isascii() and code.isdigit()
                        and row["core_county"] in (0, 1), "Invalid Greenspace county identity.")
                for field in row.keys():
                    if field.endswith(("_m2", "_acres", "_percent", "_pp")):
                        require(type(row[field]) in (int, float) and math.isfinite(row[field]) and row[field] >= 0,
                                "Invalid Greenspace observation.")
                area, eligible, percent = row["county_area_m2"], row["eligible_m2"], row["eligible_percent"]
                require(area > 0 and 0 <= percent <= 100 and
                        math.isclose(percent, eligible / area * 100, rel_tol=1e-12, abs_tol=1e-12) and
                        math.isclose(row["eligible_acres"], eligible / ACRE_M2, rel_tol=1e-12, abs_tol=1e-10),
                        "Greenspace percentage or acreage differs.")
                check = accounting[code]
                require(all(type(check[key]) in (int, float) and math.isfinite(check[key]) and check[key] >= 0
                            for key in check.keys() if key != "county_fips") and
                        check["combined_review_sensitivity_pp"] <= .01,
                        "Greenspace numerical accounting is invalid.")
                partition = sum(row[field] for key, (_, field) in OWNERSHIP.items() if key != "shared_public") + row["shared_public_m2"]
                # Python versions differ in float-sum compensation. This only
                # permits machine-roundoff when reading the audited scalars.
                rounding = max(1e-7, 8 * math.ulp(max(partition, eligible)))
                require(math.isclose(abs(partition - eligible), check["partition_residual_m2"], rel_tol=1e-10, abs_tol=rounding)
                        and check["partition_residual_m2"] / area * 100 <= .01,
                        "Greenspace ownership accounting differs.")
                require(row["coverage_status"] == ("mapped_estimate" if eligible > 0 else "no_qualifying_mapped_area"),
                        "Greenspace coverage status differs from its area.")
                require(type(row["source_record_count"]) is int and row["source_record_count"] >= 0,
                        "Invalid Greenspace source count.")
                if not row["core_county"]:
                    continue
                # Compare the stored estimate's round-trip decimal representation.
                # Presentation rounding never feeds back into the filter.
                self.values[code] = Fraction(str(percent))
                self.records[code] = {
                    "percent": str(percent), "acres": row["eligible_acres"],
                    "county_area_acres": area / ACRE_M2, "coverage_status": row["coverage_status"],
                    "trace_area": 0 < eligible < 1,
                    "ownership": {key: {"acres": row[field] / ACRE_M2, "percent": row[field] / area * 100}
                                  for key, (_, field) in OWNERSHIP.items()},
                }
        require(set(self.records) == set(counties), "Incomplete Greenspace county membership.")
        self.coverage = {"core_counties": len(counties), "reference_counties": len(rows),
                         "no_qualifying_mapped_area": sum(r["coverage_status"] == "no_qualifying_mapped_area"
                                                         for r in self.records.values())}

    def connect(self):
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
        except BaseException:
            connection.close()
            raise
        return closing(connection)

    def evaluate(self, county, limits):
        value = self.values[county]
        match = all(bound is None or (value >= Fraction(bound) if side == "min" else value <= Fraction(bound))
                    for side, bound in limits.items())
        return {"greenspace_filters_match": match, "greenspace_metrics": self.records[county]}
