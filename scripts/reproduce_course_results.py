"""Reproduce the GEOG 392 county budget comparison from COLE's prepared release."""

import argparse
import csv
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from cole.app import create_app

ROOT = Path(__file__).resolve().parents[1]
ANNUAL_PAY = 85_000
RESERVE_PERCENT = 20
RESERVE_CEILING = ANNUAL_PAY * (100 - RESERVE_PERCENT) // 100
EXAMPLE_COUNTIES = ("48215", "48041", "48453")  # Hidalgo, Brazos and Travis


def read_households():
    with (ROOT / "data/prepared/households.csv").open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    profile = [row for row in rows if row["household_profile"] == "2p1c"]
    result = {}
    for row in profile:
        fips = row["county_fips"]
        cost = int(row["published_annual_total"]) - int(row["published_annual_taxes"])
        if fips in result or cost != int(row["baseline_annual"]):
            raise ValueError(f"Duplicate county or inconsistent baseline at {fips}")
        result[fips] = (row, cost)
    return rows, result


def request_for(state, reserve):
    buffer = {"amount": "0", "period": "annual", "schedule": None}
    return {
        "household": {"adults": 2, "children": 1},
        "income_context": "financial",
        "earners": [{"minimum": None, "maximum": str(ANNUAL_PAY), "period": "annual"}],
        "extras_monthly": "0",
        "buffers": {"lower": buffer, "upper": buffer},
        "reserve_percent": None if reserve is None else str(reserve),
        "query_kind": "discovery",
        "state_filter": state,
        "lookup": None,
        "comparison_county_ids": list(EXAMPLE_COUNTIES),
    }


def verify_query(client, households, state, reserve):
    ceiling = ANNUAL_PAY if reserve is None else RESERVE_CEILING
    expected = {
        fips
        for fips, (row, cost) in households.items()
        if (state is None or row["state_abbr"] == state) and cost <= ceiling
    }
    payload = request_for(state, reserve)
    reply = client.post("/api/query", json=payload)
    preview = client.post("/api/preview", json=payload)
    if reply.status_code != 200 or preview.status_code != 200:
        raise RuntimeError(
            f"COLE could not run the scenario: {reply.status_code}, {preview.status_code}"
        )
    body = reply.get_json()
    if set(body["visible_result_ids"]) != expected or body["total"] != len(expected):
        raise ValueError("The county matches disagree with the published annual totals and taxes.")
    if preview.get_json()["total"] != len(expected):
        raise ValueError("The live preview disagrees with the submitted county search.")
    for county in body["comparison"]:
        county_id = county["county_fips"]
        _, cost = households[county_id]
        if int(county["baseline_annual"]) != cost:
            raise ValueError(f"The comparison panel reports a different cost for {county_id}.")
        if int(county["income_points"][0]["balance"]) != ANNUAL_PAY - cost:
            raise ValueError(f"The comparison panel reports a different balance for {county_id}.")
    return len(expected)


def check_texas_age_filter(client, households):
    """Apply the presentation's age example to the Texas reserve search."""
    database = ROOT / "src/cole/features/age_demographics/data/age.sqlite3"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        age_ids = {
            row[0]
            for row in db.execute(
                """SELECT c.county_fips FROM counties AS c JOIN group_counts AS g USING (county_fips)
                   WHERE c.covered = 1 AND c.population > 0 AND c.median_age_tenths <= 400
                     AND g.group_id = '18_24' AND 100 * g.population >= 15 * c.population"""
            )
        }
    texas_ids = {key for key in age_ids if households[key][0]["state_abbr"] == "TX"}
    expected = {key for key in texas_ids if households[key][1] <= RESERVE_CEILING}
    overview = client.get("/api/features/age").get_json()
    payload = request_for("TX", RESERVE_PERCENT)
    payload["age_feature_contract"] = overview["contract"]
    payload["age_limits"] = {"median_age": {"max": "40"}, "pct_18_24": {"min": "15"}}
    reply = client.post("/api/query", json=payload)
    if reply.status_code != 200 or set(reply.get_json()["visible_result_ids"]) != expected:
        raise ValueError("The submitted age-and-budget search disagrees with the source tables.")
    return {
        "median_age_max": 40,
        "share_18_to_24_min_percent": 15,
        "age_matches_us": len(age_ids),
        "age_matches_tx": len(texas_ids),
        "reserve_and_age_matches_tx": len(expected),
        "reserve_and_age_counties_tx": [
            households[key][0]["county_name"] for key in sorted(expected)
        ],
    }


def reproduce():
    rows, households = read_households()
    profile_counts = Counter(row["household_profile"] for row in rows)
    client = create_app().test_client()
    meta = client.get("/api/meta").get_json()
    summary = {}
    for label, state in (("US", None), ("TX", "TX")):
        base = sum(state is None or row["state_abbr"] == state for row, _ in households.values())
        covered = verify_query(client, households, state, None)
        reserve_met = verify_query(client, households, state, RESERVE_PERCENT)
        summary[label] = {
            "counties": base,
            "costs_covered": covered,
            "reserve_met": reserve_met,
            "costs_covered_below_reserve": covered - reserve_met,
            "costs_above_income": base - covered,
        }
    examples = []
    for fips in EXAMPLE_COUNTIES:
        row, cost = households[fips]
        examples.append(
            {
                "fips": fips,
                "county": row["county_name"],
                "source_sheet": row["source_sheet"],
                "source_row": int(row["source_row"]),
                "annual_total": int(row["published_annual_total"]),
                "annual_taxes": int(row["published_annual_taxes"]),
                "annual_cost": cost,
                "annual_balance": ANNUAL_PAY - cost,
            }
        )
    return {
        "version": meta["app_version"],
        "release": meta["manifest"]["data_release_id"],
        "total_county_profiles": len(rows),
        "profiles": dict(sorted(profile_counts.items())),
        "household": "2 adults, 1 child",
        "annual_net_income": ANNUAL_PAY,
        "reserve_percent": RESERVE_PERCENT,
        "reserve_cost_ceiling": RESERVE_CEILING,
        "geographies": summary,
        "examples": examples,
        "age_example": check_texas_age_filter(client, households),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional destination for the JSON results")
    args = parser.parse_args()
    report = json.dumps(reproduce(), indent=2)
    print(report)
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
