"""Read-only employment release validation, indexed profiles and cached sector verdicts."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class EmploymentStore:
    def __init__(self, directory, counties):
        directory = Path(directory)
        self.path = directory / "employment.sqlite3"
        self.manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        manifest = self.manifest
        if manifest["feature_version"] != "1.0.0" or manifest["schema_version"] != 1:
            raise ValueError("Unsupported industry release.")
        county_hash = hashlib.sha256(("\n".join(sorted(counties)) + "\n").encode()).hexdigest()
        if manifest["county_keys_sha256"] != county_hash:
            raise ValueError("Industry release covers a different COLE county set.")
        expected = manifest["outputs"]["employment.sqlite3"]
        if self.path.stat().st_size != expected["bytes"] or sha256(self.path) != expected["sha256"]:
            raise ValueError("Industry database fingerprint differs from its manifest.")
        self.contract = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        with self.connect() as connection:
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
            if metadata != {"schema_version": "1", "feature_version": "1.0.0", "year": str(manifest["year"]),
                            "county_keys_sha256": county_hash, "source_sha256": manifest["source"]["sha256"]}:
                raise ValueError("Industry database metadata differs from its manifest.")
            self.industries = {row["code"]: dict(row) for row in connection.execute("SELECT * FROM industries")}
            self.sectors = [{"code": code, "title": row["title"]} for code, row in self.industries.items() if row["level"] == 2]
            self.parents = frozenset(row["parent"] for row in self.industries.values() if row["parent"] is not None)
            self.covered = frozenset(row[0] for row in connection.execute("SELECT DISTINCT county FROM aggregates JOIN industries ON code=industry WHERE level=2"))
            self.incomplete = frozenset(row[0] for row in connection.execute("SELECT DISTINCT county FROM aggregates JOIN industries ON code=industry WHERE level=2 AND withheld_ownerships>0"))
            self.matches = {item["code"]: set() for item in self.sectors}
            for county, code in connection.execute("SELECT county,industry FROM sector_ranks WHERE rank<=5"):
                self.matches[code].add(county)
            self.matches = {code: frozenset(values) for code, values in self.matches.items()}
            self.sector_status = {code: {} for code in self.matches}
            for county, code, employment in connection.execute("""SELECT county,industry,disclosed_employment
                    FROM aggregates JOIN industries ON code=industry WHERE level=2"""):
                self.sector_status[code][county] = "withheld" if employment is None else "disclosed"
        coverage = manifest["coverage"]
        if (len(self.covered) != coverage["employment_counties"] or len(counties) != coverage["cole_counties"]
                or sorted(set(counties) - self.covered) != coverage["unavailable_county_fips"]
                or not self.covered <= set(counties) or len(self.sectors) != 20
                or len(self.industries) != manifest["counts"]["industries"]):
            raise ValueError("Industry coverage or hierarchy differs from its manifest.")

    def connect(self):
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return closing(connection)

    def evaluate(self, county, sector):
        return {
            "industry_filters_match": county in self.matches[sector],
            "industry_data_availability": "available" if county in self.covered else "unavailable",
            "industry_ranking_incomplete": county in self.incomplete,
        }

    def evaluate_selection(self, county, sectors, mode):
        items = []
        for code in sectors:
            matches = county in self.matches[code]
            status = ("county_unavailable" if county not in self.covered else
                      "top_five" if matches else self.sector_status[code].get(county, "no_source_row"))
            if status == "disclosed":
                status = "outside_top_five"
            items.append({"code": code, "matches": matches, "status": status})
        matches = [item["matches"] for item in items]
        return {"industry_filters_match": (all(matches) if mode == "all" else any(matches)),
                "industry_match_mode": mode, "industry_sector_results": items,
                "industry_data_availability": "available" if county in self.covered else "unavailable",
                "industry_ranking_incomplete": county in self.incomplete}

    def profile(self, county, parent=None):
        with self.connect() as connection:
            rows = connection.execute("""SELECT i.code,i.title,i.parent,i.level,
                a.disclosed_employment,a.withheld_ownerships,a.disclosed_ownerships,r.rank
                FROM industries i LEFT JOIN aggregates a ON a.industry=i.code AND a.county=?
                LEFT JOIN sector_ranks r ON r.industry=i.code AND r.county=?
                WHERE i.parent IS ? ORDER BY a.disclosed_employment IS NULL,a.disclosed_employment DESC,i.code""",
                (county, county, parent)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item.update(has_children=item["code"] in self.parents,
                        incomplete=bool(item["withheld_ownerships"]),
                        source_status="no_source_row" if item["withheld_ownerships"] is None else
                                      "withheld" if item["disclosed_employment"] is None else "disclosed",
                        top_five=item["rank"] is not None and item["rank"] <= 5)
            items.append(item)
        ancestry = []
        cursor = parent
        while cursor is not None:
            industry = self.industries[cursor]
            ancestry.append({"code": cursor, "title": industry["title"], "parent": industry["parent"]})
            cursor = industry["parent"]
        return {"county_fips": county, "year": self.manifest["year"], "contract": self.contract,
                "available": county in self.covered, "ranking_incomplete": county in self.incomplete,
                "parent": parent, "ancestry": list(reversed(ancestry)), "items": items}
