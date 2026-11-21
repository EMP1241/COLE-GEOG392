"""Feature-owned ACS release validation and exact, cached county comparisons."""
from contextlib import closing
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

METRICS = {
    "poverty": {"label": "Poverty rate", "universe": "Population for whom poverty status is determined", "estimate_field": "S1701_C03_001E", "moe_field": "S1701_C03_001M"},
    "education": {"label": "Bachelor's degree or higher (age 25+)", "universe": "Population 25 years and over", "estimate_field": "S1501_C02_015E", "moe_field": "S1501_C02_015M"},
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def percentage(tenths):
    return None if tenths is None else f"{tenths // 10}.{tenths % 10}"


class CountyMeasures:
    def __init__(self, directory, counties):
        directory = Path(directory)
        self.path = directory / "poverty_education.sqlite3"
        self.manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        manifest = self.manifest
        require(manifest["feature_version"] == "1.0.0" and manifest["schema_version"] == 1, "Unsupported ACS feature release.")
        require((manifest["period_start"], manifest["period_end"], manifest["source_vintage"], manifest["storage"]["scale"]) == (2020, 2024, 2024, 10), "Unexpected ACS period or numeric scale.")
        county_hash = hashlib.sha256(("\n".join(sorted(counties)) + "\n").encode()).hexdigest()
        require(county_hash == manifest["county_keys_sha256"], "ACS feature was prepared for different county identities.")
        with self.path.open("rb") as stream:
            expected = manifest["outputs"][self.path.name]
            require(self.path.stat().st_size == expected["bytes"] and hashlib.file_digest(stream, "sha256").hexdigest() == expected["sha256"], "ACS database fingerprint differs.")
        self.contract = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        self.records = {code: {} for code in counties}
        self.values = {code: {} for code in counties}
        expected_metadata = {"schema_version": "1", "feature_version": "1.0.0", "source_vintage": "2024", "period_start": "2020", "period_end": "2024", "stored_scale": "10", "county_keys_sha256": county_hash,
                             **{f"{metric}_source_sha256": manifest["source_files"][metric]["sha256"] for metric in METRICS}}
        with self.connect() as connection:
            require(dict(connection.execute("SELECT key,value FROM metadata")) == expected_metadata, "ACS database metadata differs.")
            require({row[0] for row in connection.execute("SELECT county_fips FROM counties")} == set(counties), "ACS database county membership differs.")
            metric_rows = {row["metric_id"]: row for row in connection.execute("SELECT * FROM metrics")}
            require(set(metric_rows) == set(METRICS), "ACS metric membership differs.")
            for key, spec in METRICS.items():
                row = metric_rows[key]
                require(all(row[field] == spec[field] for field in ("universe", "estimate_field", "moe_field")) and
                        (row["period_start"], row["period_end"], row["units"], row["stored_scale"], row["moe_confidence_percent"]) == (2020, 2024, "percent", 10, 90), "ACS metric meaning differs.")
            sources = {row["metric_id"]: row for row in connection.execute("SELECT * FROM source_files")}
            require(set(sources) == set(METRICS), "ACS source membership differs.")
            for key, row in sources.items():
                require(row["bytes"] == manifest["source_files"][key]["bytes"] and row["sha256"] == manifest["source_files"][key]["sha256"] and row["endpoint"] == manifest["source_endpoint"], "ACS source provenance differs.")
            for row in connection.execute("SELECT * FROM observations"):
                code, key = row["county_fips"], row["metric_id"]
                require(code in self.records and key in METRICS and key not in self.records[code], "Unexpected/duplicate ACS observation.")
                estimate, moe, status = row["estimate_tenths"], row["margin_of_error_tenths"], row["source_status"]
                require(estimate is None or type(estimate) is int and 0 <= estimate <= 1000, "Invalid ACS percentage.")
                require(moe is None or type(moe) is int and moe >= 0, "Invalid ACS margin of error.")
                require(status in ("available", "missing", "not_covered") and (estimate is not None) == (status == "available"), "Contradictory ACS availability.")
                require(status != "not_covered" or moe is None, "MOE on uncovered ACS observation.")
                self.records[code][key] = {"estimate_percent": percentage(estimate), "margin_of_error_points": percentage(moe), "status": status}
                self.values[code][key] = None if estimate is None else Decimal(percentage(estimate))
            require(connection.execute("PRAGMA foreign_key_check").fetchall() == [], "Invalid ACS foreign keys.")
        require(all(set(record) == set(METRICS) for record in self.records.values()), "Incomplete ACS observation membership.")
        coverage = manifest["coverage"]
        require(coverage["cole_counties"] == len(counties) and coverage["observations"] == len(counties) * len(METRICS), "ACS coverage counts differ.")
        for key in METRICS:
            actual = {code for code, rows in self.records.items() if rows[key]["status"] != "not_covered"}
            expected = coverage["metrics"][key]
            require(len(actual) == expected["covered_core_counties"] and sorted(set(counties) - actual) == sorted(row["fips"] for row in expected["missing_core_counties"]), "ACS geographic coverage differs.")

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
        failed, unavailable, checks = [], [], {}
        for key, bounds in limits.items():
            if all(value is None for value in bounds.values()):
                continue
            value = self.values[county][key]
            match = value is not None and all(bounds[side] is None or
                    (value >= Decimal(bounds[side]) if side == "min" else value <= Decimal(bounds[side])) for side in ("min", "max"))
            checks[key] = bool(match)
            if value is None:
                unavailable.append(key)
            elif not match:
                failed.append(key)
        return {"poverty_education_filters_match": all(checks.values()), "poverty_education_metrics": self.records[county],
                "poverty_education_checks": checks, "failed_poverty_education_filters": failed,
                "unavailable_poverty_education_filters": unavailable}
