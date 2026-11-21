"""Release invariants and independent checks against preserved publisher bytes."""
from collections import Counter, defaultdict
import csv
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "prepared"
SOURCE = ROOT / "data" / "source"


@pytest.fixture(scope="module")
def households():
    with (DATA / "households.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="module")
def climate():
    with (DATA / "climate.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_release_hashes_and_source_provenance():
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    assert (manifest["edition_year"], manifest["cost_year"], manifest["geography_vintage"], manifest["relationship_vintage"]) == (2026, 2025, 2021, 2020)
    assert (manifest["climate_normal_period_start"], manifest["climate_normal_period_end"], manifest["climate_units"]) == (1991, 2020, "degrees Fahrenheit")
    assert hashlib.sha256((ROOT / "scripts" / "prepare_data.py").read_bytes()).hexdigest() == manifest["preparation_script_sha256"]
    for name, record in manifest["outputs"].items():
        contents = (DATA / name).read_bytes()
        assert len(contents) == record["bytes"]
        assert hashlib.sha256(contents).hexdigest() == record["sha256"]


def test_noaa_temperature_source_and_prepared_join_are_exact(climate, households):
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    source = SOURCE / manifest["sources"]["noaa_temperature"]["filename"]
    assert source.stat().st_size == 3712401
    assert hashlib.sha256(source.read_bytes()).hexdigest() == "19f5769e118925d8371db37167bf1785ba9ecc25972227ddf804b2295bc88851"
    assert len(climate) == 3142
    assert len({row["county_fips"] for row in climate}) == len(climate)
    household_counties = {row["county_fips"] for row in households}
    climate_counties = {row["county_fips"] for row in climate}
    assert household_counties - climate_counties == {"51678"}
    assert climate_counties - household_counties == set()
    assert manifest["coverage"]["climate_unavailable_county_fips"] == ["51678"]

    prepared = {row["county_fips"]: row for row in climate}
    assert (prepared["11001"]["source_state_code"], prepared["11001"]["source_county_code"]) == ("18", "511")
    assert (prepared["24510"]["source_state_code"], prepared["24510"]["source_county_code"]) == ("18", "510")
    assert (prepared["02290"]["source_state_code"], prepared["02290"]["source_county_code"]) == ("50", "290")
    for row in climate:
        assert row["source_element_code"] == "02" and row["source_period_code"] == "0010"
        values = [int(row[f"{month}_tenths_f"]) for month in (
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        )]
        assert all(-500 <= value <= 1400 for value in values)

    target_lines = []
    with source.open(encoding="ascii") as handle:
        for line in handle:
            line = line.rstrip("\r\n")
            assert len(line) >= 95 and not line[95:].strip()
            if line[5:7] == "02" and line[7:11] == "0010":
                target_lines.append(line)
    assert len(target_lines) == 3142
    assert len({line[:5] for line in target_lines}) == 3142
    assert all(value not in {"-99.90", "-99.99"} for line in target_lines for value in (
        line[11 + index * 7:18 + index * 7].strip() for index in range(12)
    ))
    for record in manifest["sources"].values():
        contents = (SOURCE / record["filename"]).read_bytes()
        assert len(contents) == record["bytes"]
        assert hashlib.sha256(contents).hexdigest() == record["sha256"]


def test_national_household_coverage_and_exact_baseline(households):
    assert len(households) == 31430
    ids = {r["county_fips"] for r in households}
    assert len(ids) == 3143
    assert "01001" in ids and "09001" in ids and "48041" in ids
    assert all(len(c) == 5 and c.isdigit() for c in ids)
    assert len({(r["county_fips"], r["household_profile"]) for r in households}) == len(households)
    expected_profiles = {f"{a}p{c}c" for a in (1, 2) for c in range(5)}
    by_county = defaultdict(set)
    for row in households:
        by_county[row["county_fips"]].add(row["household_profile"])
        assert row["household_profile"] == f"{row['adults']}p{row['children']}c"
        assert Decimal(row["published_annual_total"]) - Decimal(row["published_annual_taxes"]) == Decimal(row["baseline_annual"])
    assert all(profiles == expected_profiles for profiles in by_county.values())


def test_sample_values_against_raw_workbook_xml(households):
    # Independent from openpyxl and the preparation function: read literal XML
    # numeric cells in the original preserved workbook for every sample row.
    sample = [r for r in households if r["county_fips"] in {"01001", "48041", "48395"}]
    assert len(sample) == 30
    wanted = {r["source_row"] for r in sample}
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    raw_rows = {}
    with zipfile.ZipFile(SOURCE / "fbc_data_2026.xlsx") as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        assert [s.attrib["name"] for s in workbook.findall("s:sheets/s:sheet", namespace)][1] == "County"
        with archive.open("xl/worksheets/sheet2.xml") as handle:
            for _, element in ET.iterparse(handle, events=("end",)):
                if element.tag.endswith("}row"):
                    index = element.attrib["r"]
                    if index in wanted:
                        raw_rows[index] = {cell.attrib["r"]: cell.find("s:v", namespace).text for cell in element.findall("s:c", namespace) if cell.find("s:v", namespace) is not None}
                    element.clear()
    for row in sample:
        number = row["source_row"]
        raw = raw_rows[number]
        assert str(int(raw[f"C{number}"])).zfill(5) == row["county_fips"]
        total, taxes = Decimal(raw[f"U{number}"]), Decimal(raw[f"T{number}"])
        assert total == Decimal(row["published_annual_total"])
        assert taxes == Decimal(row["published_annual_taxes"])
        assert total - taxes == Decimal(row["baseline_annual"])
    reference = {(r["county_fips"], r["household_profile"]): r for r in sample}
    component_columns = dict(zip(
        ("housing", "food", "transportation", "healthcare", "other_necessities", "childcare"),
        "NOPQRS",
    ))
    for row in sample:
        number = row["source_row"]
        for component, column in component_columns.items():
            assert Decimal(raw_rows[number][f"{column}{number}"]) == Decimal(row[f"published_annual_{component}"])
    assert reference["01001", "1p0c"]["baseline_annual"] == "39733"  # County!U3 49533 - T3 9800
    assert reference["48041", "1p0c"]["baseline_annual"] == "39138"  # 46364 - 7226


def test_geometry_covers_every_cost_county_with_valid_rings(households):
    counties = json.loads((DATA / "counties.geojson").read_text(encoding="utf-8"))
    states = json.loads((DATA / "states.geojson").read_text(encoding="utf-8"))
    assert {f["id"] for f in counties["features"]} == {r["county_fips"] for r in households}
    assert len(states["features"]) == 51
    assert {f["properties"]["state_abbr"] for f in states["features"]} == {r["state_abbr"] for r in households}
    for feature in counties["features"] + states["features"]:
        geometry = feature["geometry"]
        polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        assert polygons
        for polygon in polygons:
            for ring in polygon:
                assert len(ring) >= 4 and ring[0] == ring[-1]
                assert all(-180 <= lon <= 180 and -90 <= lat <= 90 for lon, lat in ring)


def test_lookup_arrays_preserve_source_multiplicity(households):
    supported = {r["county_fips"] for r in households}
    lookups = json.loads((DATA / "lookups.json").read_text(encoding="utf-8"))
    expected_places, expected_zctas = defaultdict(set), defaultdict(set)
    with (SOURCE / "national_place_by_county2020.txt").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="|"):
            county = row["STATEFP"] + row["COUNTYFP"]
            if county in supported:
                expected_places[row["STATEFP"] + row["PLACEFP"]].add(county)
    with (SOURCE / "tab20_zcta520_county20_natl.txt").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="|"):
            if row["GEOID_ZCTA5_20"] and row["GEOID_COUNTY_20"] in supported:
                expected_zctas[row["GEOID_ZCTA5_20"]].add(row["GEOID_COUNTY_20"])
    for kind, expected in (("places", expected_places), ("zctas", expected_zctas)):
        actual = {p["id"]: set(p["county_fips"]) for p in lookups[kind]}
        assert actual == expected
        assert all(p["county_fips"] == sorted(set(p["county_fips"])) for p in lookups[kind])
    assert expected_zctas["77807"] == {"48041", "48395"}
    assert expected_places["4850472"] == {"48041", "48185"}  # Navasota city
    assert len(expected_places) == 31617 and len(expected_zctas) == 33642


def test_source_exclusions_and_component_differences_are_explicit(households):
    validation = json.loads((DATA / "validation.json").read_text(encoding="utf-8"))
    report = validation["lookups"]
    assert report["blank_zcta_rows"] == 903
    assert len(report["excluded_place_ids"]) == 571
    assert len(report["excluded_zcta_ids"]) == 149
    assert "72001" in report["unsupported_county_fips"]
    assert len(validation["county_geometry"]["excluded_features"]) == 91
    # Do not replace the published total by a component sum.
    differences = Counter()
    components = ("housing", "food", "transportation", "healthcare", "other_necessities", "childcare", "taxes")
    for row in households:
        delta = Decimal(row["published_annual_total"]) - sum(Decimal(row[f"published_annual_{c}"]) for c in components)
        if delta:
            differences[str(delta)] += 1
    assert differences == {"1": 7350, "-1": 7058, "2": 537, "-2": 533, "3": 2}
    assert sum(differences.values()) == validation["households"]["component_total_discrepancy_count"]
    assert validation["climate"]["source_records"] == 37499
    assert validation["climate"]["selected_counties"] == 3142
    assert validation["climate"]["missing_epi_county_fips"] == ["51678"]
    assert validation["climate"]["special_source_geoid_mapping"] == {"18511": "11001"}


def test_sample_coverage_never_silently_loses_other_counties():
    lookups = json.loads((DATA / "sample_lookups.json").read_text(encoding="utf-8"))
    places = {p["id"]: p for p in lookups["places"]}
    assert places["4850472"]["county_fips"] == ["48041"]
    assert places["4850472"]["outside_sample_county_fips"] == ["48185"]
    sample = json.loads((DATA / "sample_manifest.json").read_text(encoding="utf-8"))
    assert sample["household_records"] == 30
    assert sample["climate_records"] == 3
    assert sample["coverage_kind"] == "three-county integration sample"


def test_acquisition_rejects_html_and_unsafe_archives(tmp_path):
    spec = importlib.util.spec_from_file_location("cole_fetch_test", ROOT / "scripts" / "fetch_sources.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    html = tmp_path / "fake.txt"
    html.write_text("<html>temporary service failure</html>", encoding="utf-8")
    with pytest.raises(ValueError, match="Unexpected"):
        module.validate_source(html, "places")
    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../outside.txt", "do not extract")
    with pytest.raises(ValueError, match="Unsafe"):
        module.validate_archive(unsafe, "shapefile")
    assert not (tmp_path.parent / "outside.txt").exists()
