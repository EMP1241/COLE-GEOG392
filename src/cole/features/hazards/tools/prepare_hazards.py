# Original preparation/download contribution: Bingqian.
"""Prepare COLE's hazards.csv from FEMA's National Risk Index county table.

Offline preparation step, run before packaging (like the EPI and NOAA
preparation). Reads the official NRI_Table_Counties.zip fetched by
tools/fetch_nri.py, matches it to the counties COLE covers, ranks each
county's building (property) and population (life-safety) expected annual
loss rates among those counties, classifies them, and writes:

  hazards.csv          the prepared table cole/hazards.py loads
  hazards_report.json  provenance, coverage, ranking universes, class counts,
                       and the exact manifest entries COLE's release needs

Usage:
  python tools/prepare_hazards.py --nri data/raw/nri/NRI_Table_Counties.zip \
      --counties path/to/households.csv --out data/prepared

--counties is a CSV with county_fips, county_name and state_abbr columns -
COLE's households.csv in the real release, or the demo households.csv.
Preparation stops, rather than writing a partial table, if any matched
county disagrees with NRI on state, name or county type, or if a COLE county
is missing from NRI without being a known, documented gap.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from cole.features.hazards.domain import (CLASS_UPPER_BOUNDS, COMMUNITY_RISK_RATINGS, FIPS_RENAMES, HAZARD_CSV_FIELDS,  # noqa: E402
                          HAZARD_TYPES, MEASURES, NRI_RATE_FIELDS, NRI_STATUS_FIELDS, SMALL_POPULATION,
                          HazardDataError, check_plain_ranks, classify_percentile, county_identity_agrees,
                          load_hazards, percentile_ranks)

NRI_REFERENCE_URL = "https://www.fema.gov/about/openfema/data-sets/national-risk-index-data"
NRI_CSV_NAME = "NRI_Table_Counties.csv"
NRI_LICENSE = "Public domain (U.S. federal government data)"

# COLE counties NRI v1.20 does not cover, for a documented reason. Any other
# missing county stops preparation unless --allow-unmatched is given.
KNOWN_GAPS = {
    **{fips: "Connecticut legacy county; NRI v1.20 uses the 2022 planning regions (09110-09190)"
       for fips in ("09001", "09003", "09005", "09007", "09009", "09011", "09013", "09015")},
    "02261": "Valdez-Cordova Census Area, AK; split in 2019 into Chugach (02063) and Copper River (02066)",
}

# NRI's expected-annual-loss rating -> COLE special class (rated values rank).
STATUS_SPECIALS = {"Not Applicable": "not_applicable", "Insufficient Data": "insufficient_data",
                   "No Expected Annual Losses": "no_rating"}
RATED_STATUSES = {"Very Low", "Relatively Low", "Relatively Moderate", "Relatively High", "Very High"}


def sha256_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def read_nri_rows(source: Path) -> list[dict]:
    try:
        with zipfile.ZipFile(source) as archive:
            data = archive.read(NRI_CSV_NAME)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise SystemExit(f"{source} is not the NRI county table archive ({exc}).") from exc
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline="")))
    if not rows:
        raise SystemExit("The NRI county table is empty.")
    return rows


def read_download_record(source: Path, sha: str, size: int) -> dict:
    sidecar = Path(f"{source}.download.json")
    if not sidecar.is_file():
        raise SystemExit(f"{sidecar} is missing. Download the table with tools/fetch_nri.py so its provenance is recorded.")
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    if record.get("sha256") != sha or record.get("bytes") != size:
        raise SystemExit(f"{source} no longer matches its download record. Fetch it again.")
    try:
        utc = datetime.fromisoformat(record["retrieved_utc"]).utcoffset() == timedelta(0)
    except (KeyError, TypeError, ValueError):
        utc = False
    if not utc:
        raise SystemExit("The download record has no UTC retrieval time.")
    return record


def read_cole_counties(path: Path) -> dict[str, dict]:
    counties: dict[str, dict] = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = {"county_fips", "county_name", "state_abbr"} - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"{path} lacks columns {sorted(missing)}.")
        for row in reader:
            if not re.fullmatch(r"\d{5}", row["county_fips"]):
                raise SystemExit(f"{path} has a malformed county_fips {row['county_fips']!r}.")
            identity = {"county_name": row["county_name"], "state_abbr": row["state_abbr"]}
            prior = counties.setdefault(row["county_fips"], identity)
            if prior != identity:
                raise SystemExit(f"Conflicting county identities for {row['county_fips']}.")
    if not counties:
        raise SystemExit(f"{path} lists no counties.")
    return counties


def rate(row: dict, field: str) -> Decimal | None:
    text = row[field].strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise SystemExit(f"County {row['STCOFIPS']}: {field} is not a number ({text!r}).") from exc
    if not value.is_finite() or value < 0:
        raise SystemExit(f"County {row['STCOFIPS']}: {field} is negative or not finite.")
    return value


def hazard_status(row: dict, name: str) -> dict[str, tuple[str, Decimal | None]]:
    """Return {measure: (special class or "rank", rate)} for one hazard."""
    county, status = row["STCOFIPS"], row[NRI_STATUS_FIELDS[name]].strip()
    rates = {measure: rate(row, NRI_RATE_FIELDS[measure][name]) for measure in MEASURES}
    if status in ("Not Applicable", "Insufficient Data"):
        # Decided by FEMA's rating first, whatever number sits beside it.
        if any(value for value in rates.values()):
            raise SystemExit(f"County {county}: {name} is {status!r} but has a nonzero loss rate.")
        return {measure: (STATUS_SPECIALS[status], None) for measure in MEASURES}
    if status == "No Expected Annual Losses":
        if any(value for value in rates.values()):
            raise SystemExit(f"County {county}: {name} has no expected loss but a nonzero loss rate.")
        return {measure: ("no_rating", None) for measure in MEASURES}
    if status not in RATED_STATUSES:
        raise SystemExit(f"County {county}: unexpected {NRI_STATUS_FIELDS[name]} value {status!r}.")
    result = {}
    for measure, value in rates.items():
        if value is None:
            raise SystemExit(f"County {county}: {NRI_RATE_FIELDS[measure][name]} is blank for a rated hazard.")
        # A rated hazard can still cause no loss to one exposure (e.g. no
        # expected deaths from a minor hurricane exposure).
        result[measure] = ("no_rating", None) if value == 0 else ("rank", value)
    return result


def population(row: dict) -> int:
    value = rate(row, "POPULATION")
    if value is None:
        raise SystemExit(f"County {row['STCOFIPS']}: POPULATION is blank.")
    numerator, denominator = value.as_integer_ratio()
    return (2 * numerator + denominator) // (2 * denominator)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nri", type=Path, required=True, help="NRI_Table_Counties.zip fetched by tools/fetch_nri.py")
    parser.add_argument("--counties", type=Path, required=True, help="CSV with COLE's county_fips, county_name, state_abbr")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--allow-unmatched", action="store_true",
                        help="write the table even if COLE counties outside the known gaps have no NRI row")
    args = parser.parse_args(argv)

    source_sha, source_bytes = sha256_file(args.nri)
    download = read_download_record(args.nri, source_sha, source_bytes)
    nri_rows = read_nri_rows(args.nri)
    required = {"STCOFIPS", "COUNTY", "COUNTYTYPE", "STATEABBRV", "POPULATION", "RISK_RATNG", "NRI_VER",
                *NRI_STATUS_FIELDS.values(), *[field for fields in NRI_RATE_FIELDS.values() for field in fields.values()]}
    missing = required - set(nri_rows[0])
    if missing:
        raise SystemExit(f"The NRI table lacks expected columns: {sorted(missing)}")

    by_fips: dict[str, dict] = {}
    for row in nri_rows:
        if not re.fullmatch(r"\d{5}", row["STCOFIPS"]):
            raise SystemExit(f"NRI county identifier {row['STCOFIPS']!r} is not five digits (was the CSV re-saved?).")
        if row["STCOFIPS"] in by_fips:
            raise SystemExit(f"Duplicate NRI county {row['STCOFIPS']}.")
        by_fips[row["STCOFIPS"]] = row
    if len({row["NRI_VER"] for row in nri_rows}) != 1:
        raise SystemExit("The NRI table mixes releases.")

    # Join COLE counties to NRI rows, directly or through a documented rename.
    cole = read_cole_counties(args.counties)
    joined, renamed = {}, {}
    for county in cole:
        if county in by_fips:
            joined[county] = by_fips[county]
        elif county in FIPS_RENAMES and FIPS_RENAMES[county][0] in by_fips:
            joined[county] = by_fips[FIPS_RENAMES[county][0]]
            renamed[county] = FIPS_RENAMES[county][0]
    # One NRI row must describe one COLE county, or it would rank twice.
    used = Counter(row["STCOFIPS"] for row in joined.values())
    if any(count > 1 for count in used.values()):
        raise SystemExit("COLE lists both an old and a new code for the same county: "
                         + ", ".join(sorted(fips for fips, count in used.items() if count > 1)))

    disagreements = [f"{county}: COLE {cole[county]['county_name']}, {cole[county]['state_abbr']} / "
                     f"NRI {row['COUNTY']} ({row['COUNTYTYPE']}), {row['STATEABBRV']}"
                     for county, row in sorted(joined.items())
                     if row["STATEABBRV"] != cole[county]["state_abbr"]
                     or not (county_identity_agrees(cole[county]["county_name"], row["COUNTY"], row["COUNTYTYPE"])
                             or (county in renamed and (row["COUNTY"], row["COUNTYTYPE"]) == FIPS_RENAMES[county][1:]))]
    if disagreements:
        raise SystemExit("NRI and COLE disagree about these counties:\n  " + "\n  ".join(disagreements))

    unmatched = sorted(cole.keys() - joined.keys())
    unexpected = [county for county in unmatched if county not in KNOWN_GAPS]
    if unexpected and not args.allow_unmatched:
        raise SystemExit("These COLE counties have no NRI row and are not known gaps (check for renumbered "
                         "counties or a damaged county list; --allow-unmatched overrides):\n  "
                         + "\n  ".join(f"{county} {cole[county]['county_name']}, {cole[county]['state_abbr']}"
                                       for county in unexpected))

    # Classify every hazard and measure, then rank each among COLE counties
    # where that hazard causes some expected loss to that exposure.
    statuses = {county: {name: hazard_status(row, name) for name in HAZARD_TYPES} for county, row in joined.items()}
    percentiles, universes = {}, {}
    for name in HAZARD_TYPES:
        for measure in MEASURES:
            ranked = {county: str(statuses[county][name][measure][1]) for county in joined
                      if statuses[county][name][measure][0] == "rank"}
            percentiles[name, measure] = percentile_ranks(ranked)
            try:
                check_plain_ranks(ranked, percentiles[name, measure])
            except HazardDataError as exc:
                raise SystemExit(f"{name} {measure}: {exc}") from exc
            universes[f"{name}_{measure}"] = len(ranked)

    prepared = []
    for county in sorted(joined):
        row = joined[county]
        out = {"county_fips": county, "nri_county_name": row["COUNTY"].strip(), "nri_state_abbr": row["STATEABBRV"].strip(),
               "nri_county_type": row["COUNTYTYPE"].strip(), "population": population(row)}
        for name in HAZARD_TYPES:
            for measure in MEASURES:
                kind = statuses[county][name][measure][0]
                shown = percentiles[name, measure].get(county, "")
                out[f"{name}_{measure}_percentile"] = shown
                out[f"{name}_{measure}_class"] = classify_percentile(shown) if kind == "rank" else kind
        community = row["RISK_RATNG"].strip()
        if community not in COMMUNITY_RISK_RATINGS:
            raise SystemExit(f"County {county}: unexpected community risk rating {community!r}.")
        out["community_risk_rating"] = COMMUNITY_RISK_RATINGS[community]
        out["nri_version"] = row["NRI_VER"].strip()
        prepared.append(out)

    args.out.mkdir(parents=True, exist_ok=True)
    output = args.out / "hazards.csv"
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=HAZARD_CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(prepared)
    # Read it back through the runtime loader: preparation must never produce
    # a table COLE would refuse at startup.
    load_hazards(output, cole)

    output_sha, output_bytes = sha256_file(output)
    nri_version = prepared[0]["nri_version"] if prepared else None
    report = {
        "nri_version": nri_version,
        "method": {
            "property": "Building expected annual loss rate (NRI ALR_VALB; per hazard <HAZ>_ALRB). Drives every limit except heat wave.",
            "life": "Population expected annual loss rate (NRI ALR_VALP; per hazard <HAZ>_ALRP). Drives the heat-wave limit; shown for every hazard; "
                    f"flagged when population < {SMALL_POPULATION}.",
            "ranking": "Percentile among the COLE counties in this table where the hazard causes some expected loss "
                       "to that exposure: lowest 0, highest 100, ties share the midrank, rounded half up to 0.1.",
            "classes": "very_low < 20 <= low < 40 <= moderate < 60 <= high < 80 <= very_high, from the rounded value.",
            "special_values": "From NRI <HAZ>_EALR / EAL_RATNG: Not Applicable -> not_applicable; Insufficient Data -> "
                              "insufficient_data; No Expected Annual Losses, or a zero rate -> no_rating.",
            "not_used": "NRI composite RISK ratings, published combined *_ALR_NPCTL percentiles, and agricultural loss. "
                        "COLE independently ranks the building and population loss rates. Broader weighting-error "
                        "claims in the original submission were not independently verified during integration.",
        },
        "class_upper_bounds": [str(bound) for bound in CLASS_UPPER_BOUNDS],
        "ranking_universe_sizes": universes,
        "coverage": {
            "cole_counties": len(cole),
            "hazard_counties": len(prepared),
            "hazard_unavailable_counties": len(unmatched),
            "hazard_unavailable_county_fips": unmatched,
            "hazard_unavailable_reasons": {county: KNOWN_GAPS.get(county, "UNEXPECTED: not a known gap") for county in unmatched},
            "renamed_fips_joined": renamed,
            "small_population_counties": sum(row["population"] < SMALL_POPULATION for row in prepared),
            "nri_rows_not_covered": len(by_fips.keys() - {row["STCOFIPS"] for row in joined.values()}),
            "nri_rows_not_covered_fips": sorted(by_fips.keys() - {row["STCOFIPS"] for row in joined.values()}),
        },
        "class_counts": {f"{name}_{measure}": dict(Counter(row[f"{name}_{measure}_class"] for row in prepared).most_common())
                         for name in HAZARD_TYPES for measure in MEASURES},
        "field_mapping": {
            **{f"{name}_{measure}_percentile": f"rank of NRI {NRI_RATE_FIELDS[measure][name]}"
               for name in HAZARD_TYPES for measure in MEASURES},
            **{f"{name}_{measure}_class": f"from {name}_{measure}_percentile; special values from NRI {NRI_STATUS_FIELDS[name]}"
               for name in HAZARD_TYPES for measure in MEASURES},
            "population": "NRI POPULATION, rounded to a whole number",
            "community_risk_rating": "NRI RISK_RATNG", "nri_county_name": "NRI COUNTY", "nri_county_type": "NRI COUNTYTYPE",
            "nri_state_abbr": "NRI STATEABBRV", "nri_version": "NRI NRI_VER",
        },
        # Ready to merge into manifest.json by COLE's release preparation.
        "manifest_entries": {
            "schema_version": "1.2",
            "sources.fema_nri": {
                "filename": download["filename"], "url": download["url"], "final_url": download["final_url"],
                "publisher": "Federal Emergency Management Agency (FEMA)",
                "reference_url": NRI_REFERENCE_URL, "kind": "nri_county_table", "license": NRI_LICENSE,
                "sha256": source_sha, "bytes": source_bytes, "retrieved_utc": download["retrieved_utc"],
                "nri_version": nri_version,
            },
            "outputs.hazards.csv": {"bytes": output_bytes, "sha256": output_sha},
            "coverage.hazard_counties": len(prepared),
            "coverage.hazard_unavailable_county_fips": unmatched,
        },
    }
    (args.out / "hazards_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {output} ({len(prepared)} counties, NRI {nri_version}).")
    print(f"COLE counties without an NRI row: {len(unmatched)} {unmatched}")
    if renamed:
        print(f"Joined through renamed FIPS: {renamed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
