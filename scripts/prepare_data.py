"""Prepare validated national and explicitly limited sample COLE data.

Original publisher bytes remain under data/source. Files are first built in a
temporary sibling directory, validated, then individually atomically replaced;
manifest.json is committed last and contains hashes of every release file.
Run preparation with the application stopped (multi-file replacement is not a
transaction for a concurrent reader). Requires openpyxl, pyshp and pyproj.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import math
from pathlib import Path
import re
import tempfile
import zipfile

import openpyxl
from pyproj import CRS, Transformer, network
from pyproj.transformer import TransformerGroup
import shapefile

from fetch_sources import ROOT, SOURCES, SOURCE_DIR, sha256, validate_source

RELEASE_ID = "epi-2026-usd2025-cb2021-rel2020-noaa-nclimdiv-1991-2020-v1"
PREPARED = ROOT / "data" / "prepared"
PROFILES = {f"{adults}p{children}c" for adults in (1, 2) for children in range(5)}
SAMPLE_IDS = {"01001", "48041", "48395"}
COMPONENTS = ("housing", "food", "transportation", "healthcare", "other_necessities", "childcare")
FIELDS = ["county_fips", "county_name", "state_abbr", "household_profile", "adults", "children", "published_annual_total", "published_annual_taxes", "baseline_annual", *[f"published_annual_{c}" for c in COMPONENTS], "source_case_id", "source_sheet", "source_row", "data_release_id"]
CLIMATE_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
CLIMATE_FIELDS = [
    "county_fips", *[f"{month}_tenths_f" for month in CLIMATE_MONTHS],
    "normal_period_start", "normal_period_end", "source_state_code",
    "source_county_code", "source_element_code", "source_period_code",
    "data_release_id",
]
NOAA_PERIOD_CODES = {
    "0001", "0002", "0003", "0004", "0005", "0006",
    "0007", "0008", "0009", "0010", "0031", "0032",
}
NOAA_TARGET_PERIOD_CODE = "0010"
# NOAA's sequential climate state codes are not Census state FIPS codes.
# This explicit crosswalk follows NOAA's county and normals readmes.
NOAA_STATE_CODE_TO_ABBR = {
    "01": "AL", "02": "AZ", "03": "AR", "04": "CA", "05": "CO",
    "06": "CT", "07": "DE", "08": "FL", "09": "GA", "10": "ID",
    "11": "IL", "12": "IN", "13": "IA", "14": "KS", "15": "KY",
    "16": "LA", "17": "ME", "18": "MD", "19": "MA", "20": "MI",
    "21": "MN", "22": "MS", "23": "MO", "24": "MT", "25": "NE",
    "26": "NV", "27": "NH", "28": "NJ", "29": "NM", "30": "NY",
    "31": "NC", "32": "ND", "33": "OH", "34": "OK", "35": "OR",
    "36": "PA", "37": "RI", "38": "SC", "39": "SD", "40": "TN",
    "41": "TX", "42": "UT", "43": "VT", "44": "VA", "45": "WA",
    "46": "WV", "47": "WI", "48": "WY", "49": "HI", "50": "AK",
}
# NOAA's bulk county file stores Washington, DC under its Maryland sequence.
# Match all five source identifier characters so Baltimore city (18510) and
# other county code 511 records continue through the ordinary crosswalk.
NOAA_SPECIAL_SOURCE_GEOIDS = {"18511": "11001"}
network.set_network_enabled(False)


def json_write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def numeric(value: object, address: str) -> Decimal:
    # The pinned edition stores all annual amounts as integer numeric cells.
    # Blocking a changed representation avoids lossy binary-float conversion.
    require(type(value) is int, f"Expected original integer annual amount at {address}, got {value!r}; review source precision before changing parser")
    return Decimal(value)


def prepare_households(path: Path) -> tuple[list[dict], dict, dict]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        require(workbook.sheetnames == ["Summary", "County", "Metro"], "Unexpected workbook sheet structure")
        summary = str(workbook["Summary"]["A2"].value)
        require("2026 edition" in summary and "2025 dollars" in summary, "Workbook edition/cost year differs from selected release")
        sheet = workbook["County"]
        header = tuple(v.strip() if isinstance(v, str) else v for v in next(sheet.iter_rows(min_row=2, max_row=2, values_only=True)))
        expected = ("case_id", "State abv.", "county_fips", "County", "Family", "Housing", "Food", "Transportation", "Healthcare", "Other Necessities", "Childcare", "Taxes", "Total", "Housing", "Food", "Transportation", "Healthcare", "Other Necessities", "Childcare", "Taxes", "Total")
        require(header[:21] == expected, "EPI County header mapping changed")
        require(sheet["N1"].value == "Annual", "Expected annual columns N:U")
        rows, county_map, seen = [], {}, set()
        profile_counts = Counter()
        component_differences = []
        for index, source in enumerate(sheet.iter_rows(min_row=3, values_only=True), start=3):
            require(type(source[2]) is int and 0 <= source[2] <= 99999, f"Invalid county ID at County!C{index}")
            county = f"{source[2]:05d}"
            profile = source[4]
            require(profile in PROFILES, f"Unsupported household at County!E{index}: {profile}")
            require((county, profile) not in seen, f"Duplicate county/profile at source row {index}")
            require(isinstance(source[1], str) and re.fullmatch(r"[A-Z]{2}", source[1]) is not None, f"Invalid state at row {index}")
            require(isinstance(source[3], str) and source[3].strip(), f"Missing county name at row {index}")
            seen.add((county, profile))
            labels = {"county_fips": county, "county_name": source[3], "state_abbr": source[1]}
            require(county_map.get(county, labels) == labels, f"Inconsistent county labels for {county}")
            county_map[county] = labels
            annual = [numeric(source[c], f"County!{openpyxl.utils.get_column_letter(c + 1)}{index}") for c in range(13, 21)]
            total, taxes = annual[7], annual[6]
            baseline = total - taxes
            require(baseline >= 0, f"Negative derived baseline at row {index}; review before release")
            diff = total - sum(annual[:7])
            if diff:
                component_differences.append({"county_fips": county, "household_profile": profile, "source_row": index, "total_minus_sum_components": str(diff)})
            rows.append({**labels, "household_profile": profile, "adults": int(profile[0]), "children": int(profile[2]), "published_annual_total": str(total), "published_annual_taxes": str(taxes), "baseline_annual": str(baseline), **{f"published_annual_{name}": str(annual[i]) for i, name in enumerate(COMPONENTS)}, "source_case_id": source[0], "source_sheet": "County", "source_row": index, "data_release_id": RELEASE_ID})
            profile_counts[profile] += 1
        require(len(county_map) == 3143 and len(rows) == 31430, "County/profile coverage differs from selected release")
        require(dict(profile_counts) == {p: 3143 for p in PROFILES}, "Incomplete household profile coverage")
        require(all((c, p) in seen for c in county_map for p in PROFILES), "Some counties lack a supported profile")
        report = {"source_sheet": "County", "source_rows": len(rows), "counties": len(county_map), "profile_counts": dict(sorted(profile_counts.items())), "duplicate_pairs": 0, "missing_required_values": 0, "negative_baselines": 0, "component_total_discrepancy_count": len(component_differences), "component_total_discrepancies": component_differences, "component_discrepancy_interpretation": "Published whole-dollar components and totals differ by up to three dollars. This is consistent with independent rounding, but the cause is not established by the workbook.", "annual_source_cells": "N:U", "all_annual_values_are_published_integers": True, "baseline_rule": "published_annual_total - published_annual_taxes; source total retained even where published components do not sum exactly"}
        return sorted(rows, key=lambda r: (r["state_abbr"], r["county_name"], r["household_profile"])), county_map, report
    finally:
        workbook.close()


def prepare_temperature(path: Path, county_map: dict) -> tuple[list[dict], dict]:
    """Select NOAA's 1991–2020 county mean-temperature records.

    NOAA uses sequential two-digit state codes followed by a three-digit
    county FIPS code. State FIPS prefixes are derived from the already
    validated EPI county identities, then joined through the documented NOAA
    state-code-to-USPS crosswalk above.
    """
    census_state_fips = {}
    for county_fips, labels in county_map.items():
        state_abbr, state_fips = labels["state_abbr"], county_fips[:2]
        require(
            census_state_fips.setdefault(state_abbr, state_fips) == state_fips,
            f"Inconsistent Census state prefix for {state_abbr}",
        )
    require(set(NOAA_STATE_CODE_TO_ABBR.values()) == set(census_state_fips) - {"DC"},
            "NOAA state crosswalk differs from the EPI state coverage")

    target_rows = {}
    periods_by_source_county = defaultdict(set)
    period_counts = Counter()
    seen_records = set()
    source_records = 0
    with path.open("r", encoding="ascii", newline="") as handle:
        for source_line, line in enumerate(handle, start=1):
            record = line.rstrip("\r\n")
            require(record.strip(), f"Blank NOAA record at line {source_line}")
            require(len(record) >= 95 and not record[95:].strip(),
                    f"Unexpected NOAA record width at line {source_line}")
            identity = record[:11]
            require(identity.isdigit(), f"Malformed NOAA identifier at line {source_line}")
            state_code, county_code = identity[:2], identity[2:5]
            element_code, period_code = identity[5:7], identity[7:11]
            require(state_code in NOAA_STATE_CODE_TO_ABBR,
                    f"Unknown NOAA state code {state_code} at line {source_line}")
            require(county_code != "000", f"Invalid NOAA county code at line {source_line}")
            require(element_code == "02", f"Unexpected NOAA element at line {source_line}")
            require(period_code in NOAA_PERIOD_CODES,
                    f"Unexpected NOAA normal period {period_code} at line {source_line}")
            source_key = (state_code, county_code)
            record_key = (*source_key, period_code)
            require(record_key not in seen_records, f"Duplicate NOAA record at line {source_line}")
            seen_records.add(record_key)
            periods_by_source_county[source_key].add(period_code)
            period_counts[period_code] += 1
            source_records += 1

            values_tenths = []
            for index, month in enumerate(CLIMATE_MONTHS):
                value_text = record[11 + index * 7:18 + index * 7].strip()
                require(re.fullmatch(r"-?\d{1,3}\.\d{2}", value_text) is not None,
                        f"Malformed NOAA {month} value at line {source_line}")
                value = Decimal(value_text)
                require(Decimal("-50") <= value <= Decimal("140"),
                        f"Missing or out-of-range NOAA {month} value at line {source_line}")
                tenths = value * Decimal("10")
                require(tenths == tenths.to_integral_value(),
                        f"NOAA {month} value exceeds documented tenth-degree precision at line {source_line}")
                values_tenths.append(int(tenths))

            if period_code == NOAA_TARGET_PERIOD_CODE:
                state_abbr = NOAA_STATE_CODE_TO_ABBR[state_code]
                source_geoid = state_code + county_code
                county_fips = NOAA_SPECIAL_SOURCE_GEOIDS.get(
                    source_geoid, census_state_fips[state_abbr] + county_code,
                )
                require(county_fips not in target_rows,
                        f"Duplicate NOAA 1991–2020 county after crosswalk: {county_fips}")
                target_rows[county_fips] = {
                    "county_fips": county_fips,
                    **{f"{month}_tenths_f": value for month, value in zip(CLIMATE_MONTHS, values_tenths)},
                    "normal_period_start": 1991,
                    "normal_period_end": 2020,
                    "source_state_code": state_code,
                    "source_county_code": county_code,
                    "source_element_code": element_code,
                    "source_period_code": period_code,
                    "data_release_id": RELEASE_ID,
                }

    require(set(period_counts) == NOAA_PERIOD_CODES,
            f"NOAA normal periods differ from the documented set: {sorted(period_counts)}")
    require(all(NOAA_TARGET_PERIOD_CODE in periods for periods in periods_by_source_county.values()),
            "A NOAA county lacks the selected 1991-2020 normal period")
    require(len(target_rows) == 3142 and period_counts[NOAA_TARGET_PERIOD_CODE] == 3142,
            "NOAA 1991-2020 county coverage differs from the pinned release")
    source_only = sorted(set(target_rows) - set(county_map))
    missing = sorted(set(county_map) - set(target_rows))
    require(not source_only, f"NOAA climate rows do not join to EPI counties: {source_only}")
    require(missing == ["51678"], f"Unexpected EPI counties without NOAA climate data: {missing}")
    report = {
        "source_records": source_records,
        "source_counties": len(periods_by_source_county),
        "period_record_counts": dict(sorted(period_counts.items())),
        "selected_period_code": NOAA_TARGET_PERIOD_CODE,
        "selected_period": "1991-2020",
        "selected_counties": len(target_rows),
        "matched_epi_counties": len(set(target_rows) & set(county_map)),
        "source_only_county_fips": source_only,
        "missing_epi_county_fips": missing,
        "special_source_geoid_mapping": NOAA_SPECIAL_SOURCE_GEOIDS,
        "missing_interpretation": "Lexington city, Virginia has no county record in this NOAA nClimDiv file; COLE does not impute a neighboring, climate-division or station value.",
        "state_crosswalk": [
            {"noaa_state_code": code, "state_abbr": abbr, "census_state_fips": census_state_fips[abbr]}
            for code, abbr in NOAA_STATE_CODE_TO_ABBR.items()
        ],
        "stored_precision": "signed integer tenths of a degree Fahrenheit",
        "derived_summary_rule": "Coldest and warmest are the extrema of the 12 monthly means; annual context is the arithmetic mean of those 12 values.",
    }
    return [target_rows[county] for county in sorted(target_rows)], report


def transform_coordinates(coordinates, transform):
    if isinstance(coordinates[0], (int, float)):
        lon, lat = transform(*coordinates[:2])
        require(math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90, "Out-of-range transformed coordinate")
        return [lon, lat]
    return [transform_coordinates(part, transform) for part in coordinates]


def prepare_geometry(path: Path, county_map: dict, layer: str) -> tuple[dict, dict]:
    with zipfile.ZipFile(path) as archive:
        stem = next(n[:-4] for n in archive.namelist() if n.endswith(".shp"))
        source_crs = CRS.from_wkt(archive.read(stem + ".prj").decode("utf-8"))
        require(source_crs.to_epsg() == 4269, "Expected Census NAD83 geographic coordinates (EPSG:4269)")
        transformer = Transformer.from_crs(source_crs, CRS.from_epsg(4326), always_xy=True, allow_ballpark=False)
        reader = shapefile.Reader(shp=io.BytesIO(archive.read(stem + ".shp")), shx=io.BytesIO(archive.read(stem + ".shx")), dbf=io.BytesIO(archive.read(stem + ".dbf")), encoding="utf-8")
        features, excluded, seen = [], [], set()
        states = {c[:2] for c in county_map}
        for record in reader.iterShapeRecords():
            properties = record.record.as_dict()
            geoid = properties["GEOID"]
            require(geoid not in seen, f"Duplicate {layer} geometry: {geoid}")
            seen.add(geoid)
            if (layer == "county" and geoid not in county_map) or (layer == "state" and geoid not in states):
                excluded.append({"geoid": geoid, "name": properties.get("NAMELSAD", properties["NAME"]), "reason": "no EPI-covered county in selected geography" if layer == "state" else "no EPI county cost record"})
                continue
            raw = record.shape.__geo_interface__
            require(raw["type"] in {"Polygon", "MultiPolygon"} and raw["coordinates"], f"Unsupported or empty {layer} geometry: {geoid}")
            geometry = {"type": raw["type"], "coordinates": transform_coordinates(raw["coordinates"], transformer.transform)}
            labels = county_map[geoid] if layer == "county" else {"state_fips": geoid, "state_abbr": properties["STUSPS"], "state_name": properties["NAME"]}
            features.append({"type": "Feature", "id": geoid, "properties": {**labels, "geography_vintage": 2021}, "geometry": geometry})
        reader.close()
        supported = set(county_map) if layer == "county" else states
        require({f["id"] for f in features} == supported, f"Missing covered {layer} geometry: {sorted(supported - seen)}")
        available = TransformerGroup(source_crs, CRS.from_epsg(4326), always_xy=True, allow_ballpark=False)
        report = {"source_features": len(seen), "included_features": len(features), "excluded_features": excluded, "missing_covered_geometry_ids": [], "source_crs": source_crs.to_string(), "prepared_crs": "EPSG:4326; GeoJSON longitude, latitude", "transform_description": "PROJ chooses an available non-ballpark NAD83 to WGS84 operation per coordinate; network grids disabled", "available_offline_operations": [{"description": operation.description, "stated_accuracy_meters": operation.accuracy} for operation in available.transformers], "coordinates_rounded_or_simplified": False, "scale": "1:5,000,000 cartographic boundaries; not parcel or navigation geometry"}
        return {"type": "FeatureCollection", "features": sorted(features, key=lambda f: f["id"])}, report


def prepare_lookups(counties: dict) -> tuple[dict, dict]:
    places, zctas = {}, defaultdict(set)
    exclusions = {"places": [], "zctas": []}
    statistics = Counter()
    with (SOURCE_DIR / SOURCES["places"]["filename"]).open(encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle, delimiter="|"), start=2):
            statistics["place_source_rows"] += 1
            county = row["STATEFP"] + row["COUNTYFP"]
            place_id = row["STATEFP"] + row["PLACEFP"]
            require(re.fullmatch(r"\d{5}", county) is not None and re.fullmatch(r"\d{7}", place_id) is not None, f"Malformed place relationship at source row {index}")
            labels = (row["PLACENAME"], row["STATE"])
            place = places.setdefault(place_id, {"id": place_id, "name": labels[0], "state_abbr": labels[1], "county_fips": set(), "unsupported_county_fips": set()})
            require((place["name"], place["state_abbr"]) == labels, f"Place labels vary for {place_id}")
            if county in place["county_fips"] or county in place["unsupported_county_fips"]:
                statistics["duplicate_place_county_pairs"] += 1
            place["county_fips" if county in counties else "unsupported_county_fips"].add(county)
            if county not in counties:
                exclusions["places"].append({"source_row": index, "id": place_id, "name": labels[0], "county_fips": county, "reason": "county outside EPI coverage"})
    with (SOURCE_DIR / SOURCES["zctas"]["filename"]).open(encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle, delimiter="|"), start=2):
            statistics["zcta_source_rows"] += 1
            zcta, county = row["GEOID_ZCTA5_20"], row["GEOID_COUNTY_20"]
            if not zcta or not county:
                reason = "blank ZCTA" if not zcta else "blank county"
                statistics["blank_zcta_rows" if not zcta else "blank_county_rows"] += 1
                exclusions["zctas"].append({"source_row": index, "id": zcta, "county_fips": county, "reason": reason})
                continue
            require(re.fullmatch(r"\d{5}", zcta) is not None and re.fullmatch(r"\d{5}", county) is not None, f"Malformed ZCTA relationship at source row {index}")
            if county in zctas[zcta]:
                statistics["duplicate_zcta_county_pairs"] += 1
            zctas[zcta].add(county)
            if county not in counties:
                exclusions["zctas"].append({"source_row": index, "id": zcta, "county_fips": county, "reason": "county outside EPI coverage"})
    prepared_places = [{**p, "county_fips": sorted(p["county_fips"]), "unsupported_county_fips": sorted(p["unsupported_county_fips"])} for p in places.values() if p["county_fips"]]
    prepared_zctas = [{"id": z, "county_fips": sorted(ids.intersection(counties)), "unsupported_county_fips": sorted(ids.difference(counties))} for z, ids in sorted(zctas.items()) if ids.intersection(counties)]
    report = {**{key: statistics[key] for key in ("place_source_rows", "zcta_source_rows", "duplicate_place_county_pairs", "duplicate_zcta_county_pairs", "blank_zcta_rows", "blank_county_rows")}, "source_place_ids": len(places), "prepared_place_ids": len(prepared_places), "excluded_place_ids": sorted(p["id"] for p in places.values() if not p["county_fips"]), "source_nonblank_zcta_ids": len(zctas), "prepared_zcta_ids": len(prepared_zctas), "excluded_zcta_ids": sorted(z for z, ids in zctas.items() if not ids.intersection(counties)), "multi_county_places": sum(len(p["county_fips"]) > 1 for p in prepared_places), "multi_county_zctas": sum(len(z["county_fips"]) > 1 for z in prepared_zctas), "unsupported_county_fips": sorted({e["county_fips"] for group in exclusions.values() for e in group if e["reason"] == "county outside EPI coverage"}), "excluded_source_rows": exclusions}
    return {"places": sorted(prepared_places, key=lambda p: (p["state_abbr"], p["name"], p["id"])), "zctas": prepared_zctas}, report


def write_households(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_climate(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CLIMATE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def prepare() -> dict:
    source_manifest = json.loads((SOURCE_DIR / "sources.json").read_text(encoding="utf-8"))
    for key, spec in SOURCES.items():
        record = source_manifest["sources"][key]
        path = SOURCE_DIR / spec["filename"]
        require(record["url"] == spec["url"] and record["sha256"] == sha256(path), f"Source provenance mismatch: {key}")
        validate_source(path, spec["kind"])
    rows, county_map, cost_report = prepare_households(SOURCE_DIR / SOURCES["epi"]["filename"])
    climate, climate_report = prepare_temperature(
        SOURCE_DIR / SOURCES["noaa_temperature"]["filename"], county_map,
    )
    counties, county_report = prepare_geometry(SOURCE_DIR / SOURCES["counties"]["filename"], county_map, "county")
    states, state_report = prepare_geometry(SOURCE_DIR / SOURCES["states"]["filename"], county_map, "state")
    lookups, lookup_report = prepare_lookups(county_map)
    require(SAMPLE_IDS.issubset(county_map), "Sample county missing from prepared data")
    validation = {"households": cost_report, "climate": climate_report, "county_geometry": county_report, "state_geometry": state_report, "lookups": lookup_report}
    PREPARED.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=PREPARED) as temporary:
        stage = Path(temporary)
        # Verify the library-managed cleanup target is inside prepared data.
        stage.resolve().relative_to(PREPARED.resolve())
        write_households(stage / "households.csv", rows)
        write_climate(stage / "climate.csv", climate)
        json_write(stage / "counties.geojson", counties)
        json_write(stage / "states.geojson", states)
        json_write(stage / "lookups.json", lookups)
        json_write(stage / "validation.json", validation)
        sample_rows = [r for r in rows if r["county_fips"] in SAMPLE_IDS]
        sample_climate = [r for r in climate if r["county_fips"] in SAMPLE_IDS]
        write_households(stage / "sample_households.csv", sample_rows)
        write_climate(stage / "sample_climate.csv", sample_climate)
        json_write(stage / "sample_counties.geojson", {"type": "FeatureCollection", "features": [f for f in counties["features"] if f["id"] in SAMPLE_IDS]})
        sample_lookups = {kind: [{**entry, "county_fips": [c for c in entry["county_fips"] if c in SAMPLE_IDS], "outside_sample_county_fips": [c for c in entry["county_fips"] if c not in SAMPLE_IDS]} for entry in entries if SAMPLE_IDS.intersection(entry["county_fips"])] for kind, entries in lookups.items()}
        json_write(stage / "sample_lookups.json", sample_lookups)
        sample = {"coverage_kind": "three-county integration sample", "county_fips": sorted(SAMPLE_IDS), "counties": [county_map[c] for c in sorted(SAMPLE_IDS)], "household_records": len(sample_rows), "climate_records": len(sample_climate), "selection_reason": "Autauga preserves a leading-zero ID; Brazos provides local Texas context; Brazos and Robertson jointly cover multi-county ZCTAs 77807, 77808 and 77859.", "limitations": "Sample lookup arrays contain only these three counties. outside_sample_county_fips preserves excluded supported associations; never present the sample as national coverage.", "multi_county_zcta_examples": [z for z in sample_lookups["zctas"] if len(z["county_fips"]) > 1]}
        json_write(stage / "sample_manifest.json", sample)
        outputs = {p.name: {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in sorted(stage.iterdir())}
        manifest = {
            "schema_version": "1.1", "data_release_id": RELEASE_ID,
            "cost_year": 2025, "edition_year": 2026, "geography_vintage": 2021,
            "relationship_vintage": 2020,
            "climate_normal_period_start": 1991,
            "climate_normal_period_end": 2020,
            "climate_units": "degrees Fahrenheit",
            "prepared_utc": datetime.now(timezone.utc).isoformat(),
            "preparation_version": "1.1",
            "preparation_script_sha256": sha256(Path(__file__)),
            "source_manifest_sha256": sha256(SOURCE_DIR / "sources.json"),
            "sources": source_manifest["sources"],
            "coverage": {
                "kind": "national EPI-covered counties", "counties": len(county_map),
                "household_records": len(rows), "household_profiles": sorted(PROFILES),
                "climate_counties": len(climate),
                "climate_unavailable_counties": len(county_map) - len(climate),
                "climate_unavailable_county_fips": climate_report["missing_epi_county_fips"],
                "states_and_dc": len(states["features"]),
                "places": len(lookups["places"]), "zctas": len(lookups["zctas"]),
            },
            "validation": {
                "status": "passed with documented component differences and geography exclusions",
                "report": "validation.json", "missing_cost_geometry": 0,
                "missing_climate_counties": len(climate_report["missing_epi_county_fips"]),
                "duplicate_county_profiles": 0,
                "component_total_discrepancies": cost_report["component_total_discrepancy_count"],
                "multi_county_places": lookup_report["multi_county_places"],
                "multi_county_zctas": lookup_report["multi_county_zctas"],
            },
            "source_field_mapping": {
                "published_annual_housing": "County!N", "published_annual_food": "County!O",
                "published_annual_transportation": "County!P", "published_annual_healthcare": "County!Q",
                "published_annual_other_necessities": "County!R", "published_annual_childcare": "County!S",
                "published_annual_taxes": "County!T", "published_annual_total": "County!U",
            },
            "climate_field_mapping": {
                "identifier": "positions 1-5 (NOAA sequential state code plus three-digit county FIPS)",
                "element_code": "positions 6-7; 02 means average temperature",
                "normal_period_code": "positions 8-11; 0010 means 1991-2020",
                "monthly_values": "positions 12-95; twelve right-justified seven-character Fahrenheit values",
                "state_crosswalk": "NOAA sequential state code to USPS abbreviation to Census state FIPS derived from EPI county identities",
            },
            "interpretation": [
                "Generalized county-level EPI household estimates; not predictions of individual spending.",
                "Prepared baseline equals published annual total minus published annual taxes.",
                "Place and ZCTA relationships locate county estimates; no city or ZIP-level cost estimate is created.",
                "2020 ZCTAs approximate ZIP areas and do not represent every postal ZIP code.",
                "EPI 2026 cost data are in 2025 dollars and retain historical county definitions including Connecticut counties.",
                "Published whole-dollar EPI components can differ slightly from their published total; no source values are rewritten.",
                "NOAA nClimDiv temperature values are 1991-2020 county-area long-term average estimates, stored as signed integer tenths of a degree Fahrenheit.",
                "Lexington city, Virginia lacks a county temperature record in this NOAA file; the value remains unavailable and is never imputed.",
                "NOAA calls these values normals for convenience but states that they are not official NCEI Normals Program normals.",
                "EPI data: Economic Policy Institute. 2026. Family Budget Calculator. CC BY-NC-SA 4.0; see source reference for terms.",
            ],
            "prepared_geometry": {
                "crs": "EPSG:4326", "coordinate_order": "longitude, latitude",
                "source_crs": "EPSG:4269", "scale": "1:5,000,000", "source_vintage": 2021,
            },
            "outputs": outputs, "sample": "sample_manifest.json",
        }
        json_write(stage / "manifest.json", manifest)
        # Manifest committed last: a partial interruption is detectable by hashes.
        for name in sorted(outputs):
            (stage / name).replace(PREPARED / name)
        (stage / "manifest.json").replace(PREPARED / "manifest.json")
    print(json.dumps({"coverage": manifest["coverage"], "validation": manifest["validation"], "sample": sample}, indent=2))
    return manifest


if __name__ == "__main__":
    prepare()
