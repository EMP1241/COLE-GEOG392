"""Feature-owned age release loading and exact county age/share comparisons."""
from contextlib import closing
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sqlite3

GROUPS = {
    "under_18": ("Under 18", 3, 6),
    "18_24": ("18–24", 7, 10),
    "25_34": ("25–34", 11, 12),
    "35_44": ("35–44", 13, 14),
    "45_54": ("45–54", 15, 16),
    "55_64": ("55–64", 17, 19),
    "65_plus": ("65 and over", 20, 25),
}
METRICS = {"median_age": {"label": "Median age", "unit": "years"}, **{
    "pct_" + key: {"label": "Residents aged " + label.lower(), "unit": "percent"}
    for key, (label, _, _) in GROUPS.items()
}}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def tenths(value):
    return None if value is None else f"{value // 10}.{value % 10}"


def share_text(count, total, places):
    if count is None or total is None or total <= 0:
        return None
    # Integer round-half-up is independent of the process Decimal context.
    scale = 10 ** places
    rounded = (2 * count * 100 * scale + total) // (2 * total)
    return f"{rounded // scale}.{rounded % scale:0{places}d}"


class AgeStore:
    def __init__(self, directory, counties):
        directory = Path(directory)
        self.path = directory / "age.sqlite3"
        self.manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        manifest = self.manifest
        require((manifest["schema_version"], manifest["feature_version"], manifest["period_start"], manifest["period_end"])
                == (1, "1.0.0", 2020, 2024), "Unsupported Age release or period.")
        county_hash = hashlib.sha256(("\n".join(sorted(counties)) + "\n").encode()).hexdigest()
        require(manifest["county_keys_sha256"] == county_hash, "Age county identities differ.")
        expected = manifest["outputs"][self.path.name]
        with self.path.open("rb") as stream:
            require(self.path.stat().st_size == expected["bytes"] and hashlib.file_digest(stream, "sha256").hexdigest() == expected["sha256"], "Age database fingerprint differs.")
        self.contract = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        self.records, self.values = {}, {}
        with self.connect() as connection:
            require(dict(connection.execute("SELECT key,value FROM metadata")) == {
                "schema_version": "1", "period_start": "2020", "period_end": "2024", "county_keys_sha256": county_hash}, "Age metadata differs.")
            groups = {row["group_id"]: row for row in connection.execute("SELECT * FROM age_groups")}
            require(set(groups) == set(GROUPS), "Age groups differ.")
            for key, (label, lo, hi) in GROUPS.items():
                cells = [f"B01001_E{cell:03}" for cell in (*range(lo, hi + 1), *range(lo + 24, hi + 25))]
                require(groups[key]["label"] == label and json.loads(groups[key]["estimate_columns_json"]) == cells, "Age group definition differs.")
            source_files = {row["filename"]: row for row in connection.execute("SELECT * FROM source_files")}
            require(set(source_files) == set(manifest["sources"]), "Age source membership differs.")
            require(all(row["sha256"] == manifest["sources"][name]["sha256"] and row["bytes"] == manifest["sources"][name]["bytes"]
                        for name, row in source_files.items()), "Age source provenance differs.")
            group_counts = {}
            for row in connection.execute("SELECT county_fips,group_id,population FROM group_counts"):
                code, key, count = row
                require(code in counties and key in GROUPS and key not in group_counts.setdefault(code, {}), "Unexpected Age group membership.")
                require(count is None or type(count) is int and count >= 0, "Invalid age-group count.")
                group_counts[code][key] = count
            for row in connection.execute("SELECT * FROM counties"):
                code = row["county_fips"]
                require(code in counties and code not in self.records, "Unexpected Age county membership.")
                require(row["covered"] in (0, 1), "Invalid Age coverage flag.")
                for field in ("population", "population_moe", "median_age_tenths", "median_age_moe_tenths"):
                    require(row[field] is None or type(row[field]) is int and row[field] >= 0, "Invalid Age observation.")
                counts = group_counts.get(code, {})
                require(set(counts) == set(GROUPS), "Incomplete Age groups.")
                total = row["population"]
                covered = bool(row["covered"])
                if not covered:
                    require(all(row[field] is None for field in ("population", "population_moe", "median_age_tenths", "median_age_moe_tenths", "age_source_row", "median_source_row"))
                            and all(count is None for count in counts.values()), "Values on uncovered Age county.")
                if all(count is not None for count in counts.values()):
                    require(total is not None and sum(counts.values()) == total, "Age groups do not partition population.")
                require(total is None or all(count is None or count <= total for count in counts.values()), "Age group exceeds population.")
                values = {"median_age": None if row["median_age_tenths"] is None else Fraction(row["median_age_tenths"], 10)}
                records = {"covered": covered, "population": total, "population_moe": row["population_moe"],
                           "median_age_years": tenths(row["median_age_tenths"]), "median_age_moe_years": tenths(row["median_age_moe_tenths"]),
                           "groups": {}}
                for key, count in counts.items():
                    values["pct_" + key] = None if count is None or not total else Fraction(count * 100, total)
                    records["groups"][key] = {"population": count, "share_percent": share_text(count, total, 12),
                                               "display_percent": share_text(count, total, 1)}
                self.values[code], self.records[code] = values, records
            require(connection.execute("PRAGMA foreign_key_check").fetchall() == [], "Age foreign keys differ.")
        require(set(self.records) == set(counties), "Incomplete Age county membership.")
        coverage = manifest["coverage"]
        actual = sorted(code for code, row in self.records.items() if not row["covered"])
        require(coverage["core_counties"] == len(counties) and coverage["missing_core"] == actual and
                coverage["covered_core_counties"] == len(counties) - len(actual), "Age coverage differs.")

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
            match = value is not None and all(bound is None or
                (value >= Fraction(bound) if side == "min" else value <= Fraction(bound)) for side, bound in bounds.items())
            checks[key] = bool(match)
            if value is None:
                unavailable.append(key)
            elif not match:
                failed.append(key)
        return {"age_filters_match": all(checks.values()), "age_metrics": self.records[county], "age_checks": checks,
                "failed_age_filters": failed, "unavailable_age_filters": unavailable}
