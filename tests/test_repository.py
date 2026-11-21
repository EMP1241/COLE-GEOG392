"""The application must refuse damaged data rather than serve plausible values."""
from copy import deepcopy
import io
import json
from pathlib import Path

import pytest

import cole.repository as repository_module
from cole.repository import (DataError, Repository, _validate_manifest,
                             default_data_directory, manifest_contract_sha256)


@pytest.fixture(scope="module")
def manifest():
    return json.loads((default_data_directory() / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_contract_fingerprint_is_canonical_and_complete(manifest):
    reordered = {key: manifest[key] for key in reversed(manifest)}
    changed = deepcopy(manifest)
    changed["coverage"] = dict(changed["coverage"])
    changed["coverage"]["counties"] += 1
    assert manifest_contract_sha256(reordered) == manifest_contract_sha256(manifest)
    assert manifest_contract_sha256(changed) != manifest_contract_sha256(manifest)


def replace_manifest(monkeypatch, manifest, path, value):
    changed = deepcopy(manifest)
    target = changed
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    original = Repository._read_json

    def read(repository, filename):
        return changed if filename == "manifest.json" else original(repository, filename)

    monkeypatch.setattr(Repository, "_read_json", read)


def test_missing_release_is_explicit(tmp_path):
    with pytest.raises(DataError, match="Cannot load"):
        Repository(tmp_path)


def test_changed_data_bytes_fail_before_calculation(monkeypatch):
    original = Path.read_bytes

    def read(path):
        raw = original(path)
        return raw + b"\n" if path.name == "households.csv" else raw

    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(DataError, match="integrity check failed: households.csv"):
        Repository()


def test_missing_manifest_integrity_metadata_is_an_error(monkeypatch):
    original = Path.read_text

    def read(path, *args, **kwargs):
        text = original(path, *args, **kwargs)
        if path.name == "manifest.json":
            manifest = json.loads(text)
            del manifest["outputs"]["households.csv"]
            return json.dumps(manifest)
        return text

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(DataError):
        Repository()


@pytest.mark.parametrize(("path", "value"), [
    (("schema_version",), "999.0"),
    (("schema_version",), 1.0),
    (("data_release_id",), {}),
    (("data_release_id",), "  "),
    (("preparation_version",), None),
    *((((field,), value) for field in ("cost_year", "edition_year", "geography_vintage", "relationship_vintage", "climate_normal_period_start", "climate_normal_period_end")
       for value in (None, {}, True, 2025.0, "2025", -1, 10000))),
    (("cost_year",), 2024),
    (("edition_year",), 2025),
    (("geography_vintage",), 2020),
    (("relationship_vintage",), 2021),
    (("sources",), None),
    *(((("sources", name), None) for name in ("epi", "counties", "states", "places", "zctas", "noaa_temperature", "noaa_county_readme", "noaa_normals_readme"))),
    (("sources", "epi", "cost_year"), True),
    (("sources", "states", "geography_vintage"), 2021.0),
    (("sources", "zctas", "geography_vintage"), 2010),
    (("sources", "epi", "filename"), ""),
    (("sources", "epi", "publisher"), []),
    (("sources", "epi", "url"), {}),
    (("sources", "epi", "final_url"), None),
    (("sources", "epi", "reference_url"), "  "),
    (("sources", "counties", "kind"), "xlsx"),
    (("sources", "states", "sha256"), "x" * 64),
    (("sources", "places", "sha256"), "a" * 63),
    (("sources", "zctas", "bytes"), 0),
    (("sources", "noaa_temperature", "normal_period_start"), 1981),
    (("sources", "noaa_temperature", "units"), "Celsius"),
    (("sources", "noaa_temperature", "kind"), "noaa_station_normals"),
    (("sources", "noaa_temperature", "release_date"), None),
    (("sources", "noaa_temperature", "release_date"), 20260904),
    (("sources", "noaa_temperature", "release_date"), "2026-9-4"),
    (("sources", "noaa_temperature", "release_date"), "2026-02-30"),
    (("sources", "noaa_temperature", "release_date"), "2026-09-04T00:00:00Z"),
    (("sources", "epi", "bytes"), True),
    (("sources", "epi", "license"), None),
    (("sources", "epi", "license"), "public domain"),
    (("prepared_utc",), "yesterday"),
    (("prepared_utc",), "2026-09-13T05:45:59"),
    (("prepared_utc",), "2026-09-13T05:45:59-05:00"),
    (("sources", "epi", "retrieved_utc"), None),
    (("sources", "counties", "retrieved_utc"), "2026-09-13"),
    (("sources", "places", "last_remote_verified_utc"), "invalid"),
    (("preparation_script_sha256",), ""),
    (("source_manifest_sha256",), {}),
    (("prepared_geometry",), []),
    (("prepared_geometry", "crs"), "EPSG:3857"),
    (("prepared_geometry", "coordinate_order"), "latitude, longitude"),
    (("prepared_geometry", "source_crs"), "EPSG:4326"),
    (("prepared_geometry", "source_vintage"), 2020),
    (("prepared_geometry", "source_vintage"), 2021.0),
    (("prepared_geometry", "scale"), ""),
    (("source_field_mapping",), None),
    (("source_field_mapping", "published_annual_taxes"), "County!U"),
    (("climate_field_mapping",), None),
    (("climate_field_mapping", "normal_period_code"), "0010"),
    (("climate_units",), "degrees Celsius"),
    (("coverage",), None),
    (("coverage", "kind"), "three-county integration sample"),
    (("coverage", "counties"), "3143"),
    (("coverage", "household_records"), 31430.0),
    (("coverage", "climate_counties"), "3142"),
    (("coverage", "climate_unavailable_counties"), -1),
    (("coverage", "climate_unavailable_county_fips"), ["51678", "51678"]),
    (("coverage", "states_and_dc"), True),
    (("coverage", "places"), -1),
    (("coverage", "zctas"), 0),
    (("coverage", "household_profiles"), ["1p0c"] * 10),
    (("coverage", "household_profiles"), [None] * 10),
    (("validation", "status"), "failed"),
    (("validation", "report"), "other-report.json"),
    (("validation", "missing_cost_geometry"), 1),
    (("validation", "missing_climate_counties"), False),
    (("validation", "duplicate_county_profiles"), False),
    (("validation", "component_total_discrepancies"), -1),
    (("validation", "multi_county_places"), "1294"),
    (("outputs", "households.csv", "bytes"), True),
    (("outputs", "climate.csv", "bytes"), -1),
    (("outputs", "states.geojson", "bytes"), -1),
    (("outputs", "lookups.json", "sha256"), "not a fingerprint"),
])
def test_malformed_manifest_is_rejected_before_data_loading(monkeypatch, manifest, path, value):
    replace_manifest(monkeypatch, manifest, path, value)

    def unexpected_read(*args, **kwargs):
        pytest.fail("Invalid metadata must fail before reading any prepared output bytes.")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    with pytest.raises(DataError, match="Invalid data manifest"):
        Repository()


@pytest.mark.parametrize("field", ["cost_year", "sources", "preparation_version", "source_field_mapping", "climate_field_mapping", "climate_normal_period_start"])
def test_missing_required_manifest_fields_are_rejected(monkeypatch, manifest, field):
    changed = deepcopy(manifest)
    del changed[field]
    monkeypatch.setattr(Repository, "_read_json", lambda self, filename: changed)
    with pytest.raises(DataError, match="Invalid data manifest"):
        Repository()


def test_manifest_contract_does_not_pin_the_release_id_or_years(manifest):
    changed = deepcopy(manifest)
    changed.update(data_release_id="another-reviewed-release", preparation_version="2.1",
                   cost_year=2029, edition_year=2030, geography_vintage=2028, relationship_vintage=2027)
    changed["sources"]["epi"].update(cost_year=2029, edition_year=2030)
    for name in ("counties", "states"):
        changed["sources"][name]["geography_vintage"] = 2028
    for name in ("places", "zctas"):
        changed["sources"][name]["geography_vintage"] = 2027
    changed["prepared_geometry"]["source_vintage"] = 2028
    _validate_manifest(changed)


def test_complete_installed_release_passes_runtime_contract():
    repository = Repository()
    assert repository.search_locations("zcta", "77807")["items"][0]["county_fips"] == ["48041", "48395"]
    assert repository.state_names["TX"] == "Texas"
    assert len(repository.climate) == 3142
    assert set(repository.counties) - set(repository.climate) == {"51678"}
    assert repository.climate["11001"]["source_state_code"] == "18"
    assert repository.climate["11001"]["source_county_code"] == "511"


@pytest.mark.parametrize("value", ["", "-1", "NaN", "1e3"])
def test_runtime_rejects_missing_malformed_or_negative_components(monkeypatch, value):
    original = repository_module.csv.DictReader

    def changed_reader(*args, **kwargs):
        for index, row in enumerate(original(*args, **kwargs)):
            if index == 0:
                row["published_annual_food"] = value
            yield row

    monkeypatch.setattr(repository_module.csv, "DictReader", changed_reader)
    with pytest.raises(DataError, match="food component is missing, malformed or negative"):
        Repository()


@pytest.mark.parametrize("replacement,error", [
    ("not-an-integer", "monthly temperature is malformed"),
    ("1401", "outside NOAA's documented range"),
])
def test_runtime_rejects_malformed_prepared_climate(monkeypatch, replacement, error):
    original = Path.open

    def changed_open(path, mode="r", *args, **kwargs):
        if path.name == "climate.csv" and "b" not in mode:
            with original(path, mode, *args, **kwargs) as handle:
                lines = handle.readlines()
            fields = lines[0].rstrip("\r\n").split(",")
            values = lines[1].rstrip("\r\n").split(",")
            values[fields.index("january_tenths_f")] = replacement
            lines[1] = ",".join(values) + "\n"
            return io.StringIO("".join(lines))
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", changed_open)
    with pytest.raises(DataError, match=error):
        Repository()


@pytest.mark.parametrize("field", ["counties", "household_records", "climate_counties", "states_and_dc", "places", "zctas"])
def test_advertised_coverage_must_match_loaded_data(monkeypatch, manifest, field):
    replace_manifest(monkeypatch, manifest, ("coverage", field), manifest["coverage"][field] + 1)
    with pytest.raises(DataError, match=rf"coverage\.{field} disagrees with loaded data"):
        Repository()


@pytest.mark.parametrize("field", ["multi_county_places", "multi_county_zctas"])
def test_advertised_multiplicity_must_match_relationships(monkeypatch, manifest, field):
    replace_manifest(monkeypatch, manifest, ("validation", field), manifest["validation"][field] + 1)
    with pytest.raises(DataError, match=rf"validation\.{field} disagrees with loaded relationships"):
        Repository()


def test_advertised_component_discrepancies_must_match_report(monkeypatch, manifest):
    replace_manifest(monkeypatch, manifest, ("validation", "component_total_discrepancies"), 0)
    with pytest.raises(DataError, match="Validation report households.component_total_discrepancy_count"):
        Repository()


@pytest.mark.parametrize(("filename", "defect", "error"), [
    ("counties.geojson", "missing", "county boundaries disagree"),
    ("counties.geojson", "duplicate", "county boundaries disagree"),
    ("states.geojson", "missing", "state boundaries disagree"),
    ("states.geojson", "duplicate", "state boundaries disagree"),
    ("counties.geojson", "vintage", "boundary identity or vintage"),
    ("states.geojson", "identity", "boundary identity or vintage"),
    ("states.geojson", "swap_state_abbr", "state identifiers disagree"),
])
def test_geometry_coverage_and_identity_must_agree_with_manifest(monkeypatch, filename, defect, error):
    original = Repository._read_json

    def read(repository, name):
        value = original(repository, name)
        if name == filename:
            features = value["features"]
            if defect == "missing":
                features.pop()
            elif defect == "duplicate":
                features.append(deepcopy(features[0]))
            elif defect == "vintage":
                features[0]["properties"]["geography_vintage"] = 2020
            elif defect == "identity":
                features[0]["id"] = "different"
            else:
                first, second = features[0]["properties"], features[1]["properties"]
                first["state_abbr"], second["state_abbr"] = second["state_abbr"], first["state_abbr"]
        return value

    monkeypatch.setattr(Repository, "_read_json", read)
    with pytest.raises(DataError, match=error):
        Repository()
