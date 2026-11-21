"""Curate county NAICS observations from a BLS annual ZIP into an offline SQLite release."""

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import zipfile


SECTORS = ("11", "21", "22", "23", "31-33", "42", "44-45", "48-49", "51", "52",
           "53", "54", "55", "56", "61", "62", "71", "72", "81", "92")
OWNERSHIPS = {"1": "Federal government", "2": "State government", "3": "Local government", "5": "Private"}
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE counties (code TEXT PRIMARY KEY CHECK(length(code)=5), name TEXT NOT NULL, state TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE industries (code TEXT PRIMARY KEY, title TEXT NOT NULL, parent TEXT REFERENCES industries(code),
                        sector TEXT NOT NULL, level INTEGER NOT NULL CHECK(level BETWEEN 2 AND 6)) WITHOUT ROWID;
CREATE TABLE observations (
    county TEXT REFERENCES counties(code), industry TEXT REFERENCES industries(code),
    ownership TEXT CHECK(ownership IN ('1','2','3','5')),
    employment INTEGER CHECK(employment IS NULL OR employment >= 0),
    disclosure TEXT NOT NULL CHECK(disclosure IN ('','N')),
    CHECK((disclosure='N' AND employment IS NULL) OR (disclosure='' AND employment IS NOT NULL)),
    PRIMARY KEY(county,industry,ownership)) WITHOUT ROWID;
CREATE TABLE aggregates (
    county TEXT REFERENCES counties(code), industry TEXT REFERENCES industries(code),
    disclosed_employment INTEGER, withheld_ownerships INTEGER NOT NULL,
    disclosed_ownerships INTEGER NOT NULL, PRIMARY KEY(county,industry)) WITHOUT ROWID;
CREATE TABLE sector_ranks (
    county TEXT REFERENCES counties(code), industry TEXT REFERENCES industries(code),
    rank INTEGER NOT NULL CHECK(rank>0), PRIMARY KEY(county,industry)) WITHOUT ROWID;
CREATE INDEX observation_industry ON observations(industry,county);
CREATE INDEX industry_parent ON industries(parent,code);
CREATE INDEX ranking_sector ON sector_ranks(industry,rank,county);
"""


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sector_for(code):
    if code in SECTORS:
        return code
    prefix = code[:2]
    return next((sector for sector in SECTORS if prefix in sector.split("-")), None) or (
        "31-33" if prefix == "32" else None
    )


def read_counties(path):
    result = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            code = row["county_fips"]
            identity = (row["county_name"], row["state_abbr"])
            if not re.fullmatch(r"[0-9]{5}", code):
                raise ValueError("Invalid county identifier.")
            if code in result and result[code] != identity:
                raise ValueError(f"Conflicting county identity: {code}")
            result[code] = identity
    if not result:
        raise ValueError("No covered counties supplied.")
    return result


def build(source, county_file, output, year=2025):
    source, output = Path(source), Path(output)
    counties = read_counties(county_file)
    output.mkdir(parents=True, exist_ok=True)
    targets = [output / "employment.sqlite3", output / "manifest.json"]
    if any(path.exists() for path in targets):
        raise ValueError("Prepare into an empty release directory; existing output is preserved.")
    source_hash = sha256(source)
    county_hash = hashlib.sha256(("\n".join(sorted(counties)) + "\n").encode()).hexdigest()
    files = {}
    observed_rows = 0
    excluded_areas = set()
    with tempfile.TemporaryDirectory(prefix="employment-prepare-", dir=output) as staging:
        database = Path(staging) / "employment.sqlite3"
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                match = re.fullmatch(rf"[^/]+/{year}\.annual ([0-9-]+) NAICS \1 (.+)\.csv", info.filename)
                if not match:
                    continue
                code, title = match.groups()
                sector = sector_for(code)
                if sector is None or (code not in SECTORS and not re.fullmatch(r"[0-9]{3,6}", code)):
                    continue
                if code in files:
                    raise ValueError(f"Duplicate industry source file: {code}")
                parent = None if code in SECTORS else sector if len(code) == 3 else code[:-1]
                files[code] = (info.filename, title, parent, sector, 2 if code in SECTORS else len(code))
            if set(SECTORS) - files.keys():
                raise ValueError("The source archive does not contain every broad NAICS sector.")
            if any(item[2] is not None and item[2] not in files for item in files.values()):
                raise ValueError("The source industry hierarchy is incomplete.")
            connection = sqlite3.connect(database)
            try:
                connection.executescript(SCHEMA)
                connection.executemany("INSERT INTO counties VALUES (?,?,?)", [(code, *identity) for code, identity in sorted(counties.items())])
                for code in sorted(files, key=lambda item: (files[item][4], item)):
                    _, title, parent, sector, level = files[code]
                    connection.execute("INSERT INTO industries VALUES (?,?,?,?,?)", (code, title, parent, sector, level))
                for code, (filename, _, _, _, level) in sorted(files.items()):
                    expected_level = str(72 + level)
                    batch = []
                    with archive.open(filename) as raw:
                        for row in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")):
                            if row["industry_code"] != code or row["year"] != str(year) or row["qtr"] != "A" or row["size_code"] != "0":
                                raise ValueError(f"Incompatible observation in {filename}")
                            if row["agglvl_code"] != expected_level:
                                continue
                            county = row["area_fips"].zfill(5)
                            if county not in counties:
                                excluded_areas.add(county)
                                continue
                            ownership, disclosure = row["own_code"], row["disclosure_code"].strip()
                            if ownership not in OWNERSHIPS or disclosure not in ("", "N"):
                                raise ValueError("Unsupported ownership or disclosure code.")
                            value = row["annual_avg_emplvl"]
                            if not re.fullmatch(r"[0-9]+", value):
                                raise ValueError("Invalid annual employment count.")
                            employment = None if disclosure == "N" else int(value)
                            batch.append((county, code, ownership, employment, disclosure))
                            observed_rows += 1
                            if len(batch) >= 5000:
                                connection.executemany("INSERT INTO observations VALUES (?,?,?,?,?)", batch)
                                batch.clear()
                    connection.executemany("INSERT INTO observations VALUES (?,?,?,?,?)", batch)
                connection.execute("""INSERT INTO aggregates SELECT county,industry,SUM(employment),
                    SUM(disclosure='N'),SUM(disclosure='') FROM observations GROUP BY county,industry"""
                )
                connection.execute("""INSERT INTO sector_ranks
                    SELECT county,industry,RANK() OVER (PARTITION BY county ORDER BY disclosed_employment DESC)
                    FROM aggregates JOIN industries ON industries.code=aggregates.industry
                    WHERE industries.level=2 AND disclosed_employment IS NOT NULL""")
                metadata = {"schema_version": "1", "feature_version": "1.0.0", "year": str(year),
                            "county_keys_sha256": county_hash, "source_sha256": source_hash}
                connection.executemany("INSERT INTO metadata VALUES (?,?)", sorted(metadata.items()))
                connection.commit()
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Prepared database failed integrity validation.")
                covered = {row[0] for row in connection.execute("SELECT DISTINCT county FROM aggregates JOIN industries ON industries.code=industry WHERE level=2")}
                withheld = connection.execute("SELECT COUNT(*) FROM observations WHERE disclosure='N'").fetchone()[0]
                rank_rows = connection.execute("SELECT COUNT(*) FROM sector_ranks WHERE rank<=5").fetchone()[0]
            finally:
                connection.close()
        manifest = {
            "feature_version": "1.0.0", "schema_version": 1, "year": year,
            "county_keys_sha256": county_hash, "source": {"filename": source.name, "sha256": source_hash,
                "publisher": "U.S. Bureau of Labor Statistics", "dataset": "QCEW annual averages",
                "provided_by": "Brandon Santor", "contributor": None,
                "documentation_url": "https://www.bls.gov/cew/questions-and-answers.htm"},
            "coverage": {"cole_counties": len(counties), "employment_counties": len(covered),
                "unavailable_county_fips": sorted(counties.keys() - covered),
                "excluded_source_county_codes": sorted(excluded_areas)},
            "counts": {"industries": len(files), "broad_sectors": len(SECTORS),
                "ownership_observations": observed_rows, "withheld_observations": withheld,
                "top_five_including_ties_rows": rank_rows},
            "ranking": {"measure": "sum of disclosed annual average employment across ownership categories",
                "scope": "broad NAICS sectors only", "fifth_place_ties": "included",
                "withheld_values": "unknown; never zero-filled or imputed", "zero_disclosed_counts": "retained as published",
                "limits": "Ranks describe disclosed employment; withheld values can change actual totals and ranks."},
            "outputs": {"employment.sqlite3": {"bytes": database.stat().st_size, "sha256": sha256(database)}},
        }
        staged_manifest = Path(staging) / "manifest.json"
        staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        database.replace(targets[0])
        staged_manifest.replace(targets[1])
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--counties", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--year", type=int, default=2025)
    args = parser.parse_args(argv)
    result = build(args.source, args.counties, args.out, args.year)
    print(json.dumps({"coverage": result["coverage"], "counts": result["counts"], "outputs": result["outputs"]}, indent=2))


if __name__ == "__main__":
    main()
