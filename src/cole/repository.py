"""Read the prepared, versioned COLE release. No runtime network acquisition."""

# ===========================================================================
# PLAIN-ENGLISH GUIDE TO THIS FILE
# ===========================================================================
#
# WHAT IT DOES
# This file loads COLE's data into memory once, at startup, and then hands it
# to the rest of the app. That is the boring half. The interesting half is
# that roughly 80% of the code here is REFUSING TO LOAD.
#
# WHY SO PARANOID
# COLE compares generalized county renter estimates with submitted criteria.
# If the data files are subtly wrong - a hand-edited row, a half-finished
# download, a file from last month's release mixed with this month's - the app
# will still run happily and give confident, wrong classifications. Nobody
# would notice.
#
# So the rule here is: any disagreement between any two files stops the app at
# startup with a clear message. A tool that refuses to open is annoying. A tool
# that quietly misclassifies the prepared estimates is worse.
#
# WHAT GETS CHECKED, ROUGHLY IN ORDER
#   1. manifest.json is structurally valid          (_validate_manifest)
#   2. Every data file's bytes match its recorded fingerprint
#   3. Every household row's baseline really equals total minus taxes
#   4. Every county has all ten household profiles, no duplicates
#   5. Climate rows point at real counties, in the supported 1991-2020 period
#   6. Map boundaries cover exactly the counties we have costs for
#   7. Place/ZIP lookups only reference counties we can price
#   8. The advertised coverage counts match what was actually loaded
#
# KEY TERMS FOR NEWCOMERS
#   MANIFEST  - manifest.json, a table of contents describing the release:
#               where each source came from, how big each file should be, how
#               many counties it covers. It is the thing everything else is
#               checked against.
#   SHA-256   - a "fingerprint" for a file: run the bytes through a formula and
#               get a 64-character code. Change one byte anywhere and the code
#               changes completely. So comparing fingerprints proves a file is
#               byte-for-byte what we expect.
#   FIPS      - the US government's county ID. Five digits: the first two are
#               the state, the last three the county. "48041" is Brazos
#               County, Texas.
#   ZCTA      - "ZIP Code Tabulation Area", the Census Bureau's mappable
#               approximation of a ZIP code. Real ZIP codes are mail routes,
#               not areas, so they cannot be drawn on a map directly.
#   GEOJSON   - a JSON format for map shapes (county outlines, state outlines).
#
# "No runtime network acquisition" in the docstring means COLE never downloads
# anything while the application is running. Everything ships with the app.
# Development data-acquisition and preparation happen before packaging and may
# use the internet; they are not part of the end-user runtime.
# ===========================================================================

from __future__ import annotations

import csv  # read the .csv data tables
import hashlib  # compute SHA-256 fingerprints
import json  # read manifest.json, lookups.json, the .geojson files
import re  # text pattern matching, for ID format checks
import sys  # used to detect whether we are running inside a packaged build
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException
from pathlib import Path  # a tidy, cross-platform way to handle file paths

# Borrowed from the calculator so the two files agree by construction rather
# than by someone remembering to update both.
from .calculations import CLIMATE_MONTHS, COST_COMPONENTS, derive_baseline


def default_data_directory() -> Path:
    # Find the prepared data folder, in development or in a packaged build.
    # PyInstaller (the tool that turns this into a double-clickable Windows app)
    # sets sys.frozen=True and exposes bundled files under sys._MEIPASS. COLE's
    # one-folder build points there into its packaged _internal directory; a
    # one-file build could instead use an extraction directory. The lookup is
    # the same either way:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "data" / "prepared"
    # Development: walk up from this file to the project root, then down.
    # `.parents[2]` means "two folders above the folder this file lives in".
    return Path(__file__).resolve().parents[2] / "data" / "prepared"


class DataError(RuntimeError):
    """The installed release is missing, corrupt or internally inconsistent."""

    # A dedicated error type so the launcher can tell "your data is broken"
    # apart from "the network port was busy" and show the right message.


# The ten household shapes EPI publishes: 1 or 2 adults x 0-4 children.
# A "set" (curly braces) is an unordered collection with no duplicates.
PROFILES = {f"{adults}p{children}c" for adults in (1, 2) for children in range(5)}

# Every file that must be present, fingerprint-checked, and loadable.
REQUIRED_OUTPUTS = (
    "households.csv",
    "climate.csv",
    "counties.geojson",
    "states.geojson",
    "lookups.json",
    "validation.json",
)

# The original sources the release was built from, and what kind each must be.
# Checking the "kind" catches a manifest that points the county-shapefile entry
# at, say, the EPI spreadsheet.
SOURCE_KINDS = {
    "epi": "xlsx",
    "counties": "shapefile",
    "states": "shapefile",
    "places": "places",
    "zctas": "zctas",
    "noaa_temperature": "noaa_county_normals",
    "noaa_county_readme": "noaa_county_readme",
    "noaa_normals_readme": "noaa_normals_readme",
}

# PROVENANCE: which spreadsheet column each cost category came from.
# `zip(...)` pairs the six categories plus taxes and total with the letters
# "NOPQRSTU", producing {"published_annual_housing": "County!N", ...}.
# This is stored so any number in the app can be traced back to a specific
# cell in EPI's published workbook.
SOURCE_FIELD_MAPPING = {
    f"published_annual_{name}": f"County!{column}"
    for name, column in zip(
        (
            "housing",
            "food",
            "transportation",
            "healthcare",
            "other_necessities",
            "childcare",
            "taxes",
            "total",
        ),
        "NOPQRSTU",
    )
}

# The same idea for NOAA's climate file, which is fixed-width text rather than
# columns: these strings document which character positions mean what.
CLIMATE_FIELD_MAPPING = {
    "identifier": "positions 1-5 (NOAA sequential state code plus three-digit county FIPS)",
    "element_code": "positions 6-7; 02 means average temperature",
    "normal_period_code": "positions 8-11; 0010 means 1991-2020",
    "monthly_values": "positions 12-95; twelve right-justified seven-character Fahrenheit values",
    "state_crosswalk": "NOAA sequential state code to USPS abbreviation to Census state FIPS derived from EPI county identities",
}


def manifest_contract_sha256(manifest: dict) -> str:
    """Fingerprint the validated manifest's complete semantic contract."""
    # This produces one 64-character code summarizing the ENTIRE manifest.
    # The app puts it in response headers; the browser page holds the copy it
    # started with. If they ever differ, the data release changed underneath a
    # running session and COLE forces a reload.
    #
    # The json.dumps settings exist to make the result reproducible - the same
    # manifest must always produce the same code:
    #   sort_keys=True    - key order in the file must not change the result
    #   separators        - no cosmetic spaces between items
    #   ensure_ascii=False- consistent handling of non-English characters
    #   allow_nan=False   - reject NaN/Infinity, which are not valid JSON anyway
    encoded = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# SMALL VALIDATION HELPERS
# Each takes a value and a field name; each raises DataError if unhappy. They
# exist so the long validator below reads as a checklist instead of a wall of
# if-statements.
# ---------------------------------------------------------------------------


def _require_manifest(condition: bool, field: str) -> None:
    # Raise DataError unless `condition` is true. The one place errors are raised.
    if not condition:
        raise DataError(f"Invalid data manifest: {field}")


def _object(value, field: str) -> dict:
    # Require a nested section, and return it so callers can chain.
    _require_manifest(isinstance(value, dict), f"{field} must be an object.")
    return value


def _text(value, field: str) -> None:
    # Require a non-empty string. A field of spaces counts as empty.
    _require_manifest(
        isinstance(value, str) and bool(value.strip()), f"{field} must be a nonempty string."
    )


def _integer(value, field: str, minimum: int = 1) -> None:
    # Require a whole number at or above `minimum`.
    # The comment below is the original author's:
    # bool is an int subclass, but never a valid year, byte size or count.
    #
    # In Python, True == 1 and isinstance(True, int) is True. So `type(value)
    # is int` is used deliberately to reject True/False, which would otherwise
    # sail through as the number 1.
    _require_manifest(
        type(value) is int and value >= minimum, f"{field} must be an integer >= {minimum}."
    )


def _fingerprint(value, field: str) -> None:
    # Require exactly 64 lowercase hex characters - the shape of a SHA-256.
    _require_manifest(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
        f"{field} must be a SHA-256 fingerprint.",
    )


def _utc_timestamp(value, field: str) -> None:
    # Require a timestamp that is explicitly in UTC.
    _text(value, field)
    try:
        # `.utcoffset() == timedelta(0)` means "zero hours from UTC". This
        # rejects a local time like "2026-01-15T10:00:00-06:00" and also a
        # naive time with no zone at all (whose utcoffset is None).
        # Why insist? Because release timestamps get compared across machines,
        # and a timezone-less timestamp is ambiguous by several hours.
        valid = datetime.fromisoformat(value).utcoffset() == timedelta(0)
    except ValueError:
        valid = False
    _require_manifest(valid, f"{field} must be an ISO timestamp with UTC offset.")


def _calendar_date(value, field: str) -> None:
    # Require a plain YYYY-MM-DD date.
    _text(value, field)
    try:
        # The round-trip check is the clever bit: parse the text into a date,
        # write it back out, and require it to be character-identical. That
        # rejects sloppy-but-parseable forms like "2026-1-5", ensuring every
        # date in the manifest is written exactly one way.
        valid = date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        valid = False
    _require_manifest(valid, f"{field} must be an ISO calendar date (YYYY-MM-DD).")


def _validate_manifest(value) -> None:
    """Validate the offline producer contract, not a hardcoded release or date.

    Source files and preparation scripts are intentionally absent from the
    portable runtime. Their fingerprints describe provenance; only bundled
    output fingerprints can be verified against bytes here.
    """
    # The docstring is making an honest distinction worth understanding:
    #
    #   OUTPUT files (households.csv etc.) ship with the app, so we can hash
    #   the actual bytes and PROVE they are unmodified.
    #
    #   SOURCE files (the EPI spreadsheet, NOAA's raw data) do NOT ship - they
    #   would bloat the download. Their fingerprints are recorded so a human
    #   can verify provenance by hand, but the app cannot check them at
    #   runtime. The manifest is a *claim* about where data came from, not a
    #   proof. This code checks the claim is well-formed and internally
    #   consistent; it cannot check it is true.
    #
    # "Not a hardcoded release or date" means we validate the SHAPE of a
    # release, so next year's data works without a code change - but we do pin
    # the few things the calculator genuinely depends on, like the 1991-2020
    # climate period.

    manifest = _object(value, "root")
    # Pinned: a manifest written to a different schema is refused outright
    # rather than half-understood.
    _require_manifest(manifest.get("schema_version") == "1.1", "unsupported schema_version.")
    for field in ("data_release_id", "preparation_version"):
        _text(manifest.get(field), field)
    # Four years that must be plausible calendar years, not 1 or 5,000,000.
    for field in ("cost_year", "edition_year", "geography_vintage", "relationship_vintage"):
        _integer(manifest.get(field), field)
        _require_manifest(manifest[field] <= 9999, f"{field} must be a calendar year.")
    # The two climate years must also be whole numbers before being pinned below.
    for field in ("climate_normal_period_start", "climate_normal_period_end"):
        _integer(manifest.get(field), field)
        _require_manifest(manifest[field] <= 9999, f"{field} must be a calendar year.")
    # Pinned deliberately: calculations.py hardcodes 1991/2020 when checking
    # each climate row, so a release using a different normals period would be
    # rejected row by row anyway. Failing here gives a clearer message.
    _require_manifest(
        manifest["climate_normal_period_start"] == 1991
        and manifest["climate_normal_period_end"] == 2020,
        "unsupported climate normal period.",
    )
    # The app's temperature filters are labelled in Fahrenheit throughout, so
    # silently accepting Celsius data would be a serious, invisible error.
    _require_manifest(
        manifest.get("climate_units") == "degrees Fahrenheit", "unsupported climate units."
    )
    _utc_timestamp(manifest.get("prepared_utc"), "prepared_utc")
    for field in ("preparation_script_sha256", "source_manifest_sha256"):
        _fingerprint(manifest.get(field), field)

    # --- Each original source must be fully described ---------------------
    sources = _object(manifest.get("sources"), "sources")
    for name, kind in SOURCE_KINDS.items():
        source = _object(sources.get(name), f"sources.{name}")
        # Where it came from and who published it - the provenance trail.
        for field in ("filename", "url", "final_url", "publisher", "reference_url"):
            _text(source.get(field), f"sources.{name}.{field}")
        _require_manifest(source.get("kind") == kind, f"sources.{name}.kind is unsupported.")
        _fingerprint(source.get("sha256"), f"sources.{name}.sha256")
        _integer(source.get("bytes"), f"sources.{name}.bytes")
        _utc_timestamp(source.get("retrieved_utc"), f"sources.{name}.retrieved_utc")
        # Optional: only checked if present, since not every source is re-verified.
        if "last_remote_verified_utc" in source:
            _utc_timestamp(
                source["last_remote_verified_utc"], f"sources.{name}.last_remote_verified_utc"
            )

        # CROSS-CHECK: a source's own year fields must agree with the
        # manifest's top-level years. This catches a release assembled from
        # mismatched vintages - 2026 costs stapled to 2019 county boundaries.
        if name == "epi":
            year_fields = (("cost_year", "cost_year"), ("edition_year", "edition_year"))
        elif name in ("counties", "states", "places", "zctas"):
            # Boundary files use geography_vintage; place/ZIP relationship
            # files use relationship_vintage. Same check, different target.
            year_fields = (
                (
                    "geography_vintage",
                    (
                        "geography_vintage"
                        if name in ("counties", "states")
                        else "relationship_vintage"
                    ),
                ),
            )
        else:
            year_fields = ()  # NOAA files carry no year field of this kind
        for source_field, manifest_field in year_fields:
            _integer(source.get(source_field), f"sources.{name}.{source_field}")
            _require_manifest(
                source[source_field] == manifest[manifest_field],
                f"sources.{name}.{source_field} disagrees with {manifest_field}.",
            )

    # A legal requirement, not a technical one: EPI's data is licensed
    # CC BY-NC-SA 4.0 (attribution, non-commercial, share-alike). Pinning it
    # here means the app refuses to run on data whose licensing is unrecorded.
    _require_manifest(
        sources["epi"].get("license") == "CC BY-NC-SA 4.0", "unsupported or missing EPI license."
    )

    # NOAA's entry must agree with the top-level climate settings.
    noaa = sources["noaa_temperature"]
    for field, manifest_field in (
        ("normal_period_start", "climate_normal_period_start"),
        ("normal_period_end", "climate_normal_period_end"),
    ):
        _integer(noaa.get(field), f"sources.noaa_temperature.{field}")
        _require_manifest(
            noaa[field] == manifest[manifest_field],
            f"sources.noaa_temperature.{field} disagrees with {manifest_field}.",
        )
    _require_manifest(
        noaa.get("units") == manifest["climate_units"],
        "sources.noaa_temperature.units disagrees with climate_units.",
    )
    _calendar_date(noaa.get("release_date"), "sources.noaa_temperature.release_date")

    # --- Map geometry settings --------------------------------------------
    geometry = _object(manifest.get("prepared_geometry"), "prepared_geometry")
    # A "CRS" (coordinate reference system) says what the numbers in a map file
    # mean. EPSG:4326 is ordinary GPS latitude/longitude, which is what Leaflet
    # expects. EPSG:4269 is the North American system Census ships. Pinning
    # both records that a conversion happened during preparation.
    #
    # "coordinate_order: longitude, latitude" looks backwards to most people -
    # and that is exactly why it is pinned. GeoJSON puts longitude first;
    # humans say "latitude and longitude". Getting this backwards silently
    # relocates every county to the wrong hemisphere.
    for field, expected in (
        ("crs", "EPSG:4326"),
        ("coordinate_order", "longitude, latitude"),
        ("source_crs", "EPSG:4269"),
    ):
        _require_manifest(
            geometry.get(field) == expected, f"prepared_geometry.{field} is unsupported."
        )
    _text(geometry.get("scale"), "prepared_geometry.scale")
    _integer(geometry.get("source_vintage"), "prepared_geometry.source_vintage")
    _require_manifest(
        geometry["source_vintage"] == manifest["geography_vintage"],
        "prepared_geometry.source_vintage disagrees with geography_vintage.",
    )

    # The manifest must reproduce our provenance tables EXACTLY. `==` on
    # dictionaries compares every key and value. If the app's understanding of
    # which spreadsheet column is "housing" ever drifts from the manifest's,
    # the release is refused rather than silently mislabelling costs.
    _require_manifest(
        manifest.get("source_field_mapping") == SOURCE_FIELD_MAPPING,
        "unsupported source_field_mapping.",
    )
    _require_manifest(
        manifest.get("climate_field_mapping") == CLIMATE_FIELD_MAPPING,
        "unsupported climate_field_mapping.",
    )

    # --- Advertised coverage ----------------------------------------------
    # These are the counts the release CLAIMS. Later, _validate_coverage
    # compares them against what was actually loaded.
    coverage = _object(manifest.get("coverage"), "coverage")
    _require_manifest(
        coverage.get("kind") == "national EPI-covered counties", "unsupported coverage.kind."
    )
    for field in (
        "counties",
        "household_records",
        "climate_counties",
        "states_and_dc",
        "places",
        "zctas",
    ):
        _integer(coverage.get(field), f"coverage.{field}")
    # minimum=0 here: zero counties missing climate data is a perfectly good
    # outcome, whereas zero counties overall would mean an empty release.
    _integer(
        coverage.get("climate_unavailable_counties"),
        "coverage.climate_unavailable_counties",
        minimum=0,
    )

    # The list of counties with no climate data must be internally consistent:
    # right length, no duplicates, every entry a real five-digit FIPS code.
    unavailable = coverage.get("climate_unavailable_county_fips")
    _require_manifest(
        isinstance(unavailable, list)
        and len(unavailable) == coverage["climate_unavailable_counties"]
        # Comparing a set against the list catches duplicates: sets drop them,
        # so a shorter set means the list repeated something.
        and len(set(unavailable)) == len(unavailable)
        and all(
            isinstance(county, str) and re.fullmatch(r"\d{5}", county) for county in unavailable
        ),
        "coverage.climate_unavailable_county_fips is malformed.",
    )
    # Exactly the ten profiles, each once - checked by comparing sets.
    profiles = coverage.get("household_profiles")
    _require_manifest(
        isinstance(profiles, list)
        and len(profiles) == len(PROFILES)
        and all(isinstance(profile, str) for profile in profiles)
        and set(profiles) == PROFILES,
        "coverage.household_profiles must contain each supported profile once.",
    )

    # --- The preparation run's own report ---------------------------------
    validation = _object(manifest.get("validation"), "validation")
    # A long, exact status string. It reads oddly but is doing real work: it
    # pins the ONE outcome we support. A release prepared with a different
    # status - including "passed with warnings we did not anticipate" - is
    # refused rather than assumed fine.
    _require_manifest(
        validation.get("status")
        == "passed with documented component differences and geography exclusions",
        "validation.status does not identify a supported successful preparation.",
    )
    _require_manifest(
        validation.get("report") == "validation.json", "unsupported validation.report."
    )
    for field in (
        "missing_cost_geometry",
        "missing_climate_counties",
        "duplicate_county_profiles",
        "component_total_discrepancies",
        "multi_county_places",
        "multi_county_zctas",
    ):
        _integer(validation.get(field), f"validation.{field}", minimum=0)
    # Two of those counts MUST be zero - they are unacceptable in any release:
    #   missing_cost_geometry     - a county we can price but cannot draw
    #   duplicate_county_profiles - the same county/household row twice
    # The others may legitimately be non-zero and are merely recorded, e.g.
    # component_total_discrepancies (EPI's six categories not summing exactly
    # to their published total, a known rounding artifact in the source).
    for field in ("missing_cost_geometry", "duplicate_county_profiles"):
        _require_manifest(validation[field] == 0, f"validation.{field} must be zero.")
    _require_manifest(
        validation["missing_climate_counties"] == coverage["climate_unavailable_counties"],
        "validation.missing_climate_counties disagrees with coverage.",
    )

    # --- Expected size and fingerprint of every shipped file --------------
    # These are the entries the loader below checks the actual bytes against.
    outputs = _object(manifest.get("outputs"), "outputs")
    for filename in REQUIRED_OUTPUTS:
        output = _object(outputs.get(filename), f"outputs.{filename}")
        _integer(output.get("bytes"), f"outputs.{filename}.bytes")
        _fingerprint(output.get("sha256"), f"outputs.{filename}.sha256")


class Repository:
    # =======================================================================
    # THE LOADED DATA RELEASE
    # =======================================================================
    # One of these is created at startup and then used read-only for the life
    # of the process. Everything is held in memory - there is no database, no
    # queries, no disk access after startup. The whole national dataset is
    # small enough to fit comfortably, which is why COLE can be instant and
    # fully offline.
    #
    # After construction, these are the useful attributes:
    #   .manifest      the validated table of contents
    #   .release_id    the release's name, stamped onto every response
    #   .households    {(county_fips, profile): row}  - the cost data
    #   .counties      {county_fips: {name, state}}   - county identities
    #   .climate       {county_fips: {...}}           - temperatures
    #   .locations     {"county"/"place"/"zcta": {id: {...}}} - the search index
    #   .ordered_counties  every county, sorted for stable display order
    #   .state_abbreviations / .state_names
    # =======================================================================

    def __init__(self, directory: Path | str | None = None):
        self.directory = Path(directory) if directory is not None else default_data_directory()
        # One big try block wraps the whole load. The `except` clauses at the
        # bottom turn any low-level failure (missing file, malformed JSON, bad
        # number) into a single clear DataError.
        try:
            # --- Step 1: the manifest, validated before anything trusts it ---
            self.manifest = self._read_json("manifest.json")
            _validate_manifest(self.manifest)
            self.manifest_contract_sha256 = manifest_contract_sha256(self.manifest)
            self.release_id = self.manifest["data_release_id"]

            # --- Step 2: prove every file is byte-for-byte as expected ------
            # Size is checked first because it is instant and catches truncated
            # downloads without hashing a large file.
            for filename in REQUIRED_OUTPUTS:
                expected = self.manifest["outputs"][filename]
                raw = (self.directory / filename).read_bytes()
                if (
                    len(raw) != expected["bytes"]
                    or hashlib.sha256(raw).hexdigest() != expected["sha256"]
                ):
                    raise DataError(f"Prepared data integrity check failed: {filename}")

            # --- Step 3: household costs ------------------------------------
            self.households = {}
            self.counties = {}
            # "utf-8-sig" strips the invisible marker character Excel adds to the
            # start of CSV files, which would otherwise corrupt the first column
            # name. newline="" is what Python's csv module requires.
            with (self.directory / "households.csv").open(
                encoding="utf-8-sig", newline=""
            ) as stream:
                for row in csv.DictReader(stream):
                    county, profile = row["county_fips"], row["household_profile"]
                    # Shape check on the two identifiers.
                    if not re.fullmatch(r"\d{5}", county) or not re.fullmatch(
                        r"[12]p[0-4]c", profile
                    ):
                        raise DataError("Invalid county or household identifier in prepared data.")
                    # The same county+profile twice means one silently wins.
                    if (county, profile) in self.households:
                        raise DataError("Duplicate county/household record in prepared data.")
                    # Every row must belong to the release the manifest names -
                    # catches files mixed between two releases.
                    if row["data_release_id"] != self.release_id:
                        raise DataError(
                            "The household table and manifest identify different releases."
                        )

                    # THE MOST IMPORTANT CHECK IN THE FILE.
                    # Original comment: Compare canonical numeric values,
                    # preserving published strings in the row.
                    #
                    # Re-run the after-tax subtraction from calculations.py and
                    # require the stored answer to match. This proves the number
                    # every financial comparison rests on was not hand-edited
                    # or mis-prepared. Comparing as Decimal, not as text, so
                    # "1200" and "1200.00" are treated as equal.
                    if Decimal(row["baseline_annual"]) != Decimal(
                        derive_baseline(
                            row["published_annual_total"], row["published_annual_taxes"]
                        )
                    ):
                        raise DataError(
                            "A prepared baseline differs from the original total minus taxes."
                        )

                    # All six categories present, numeric, and non-negative.
                    # (The pattern has no minus sign, so a negative value fails.)
                    for component in COST_COMPONENTS:
                        value = row[f"published_annual_{component}"]
                        if len(value) > 128 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None:
                            raise DataError(
                                f"A prepared {component} component is missing, malformed or negative."
                            )

                    # The profile label must agree with its own columns:
                    # "2p1c" has "2" at position 0 and "1" at position 2.
                    if row["adults"] != profile[0] or row["children"] != profile[2]:
                        raise DataError(
                            "A household profile disagrees with its adult/child counts."
                        )

                    # Provenance: which sheet and row of EPI's workbook this came
                    # from, so any figure on screen can be traced to its origin.
                    if (
                        not row["source_sheet"].strip()
                        or not row["source_row"].isdigit()
                        or int(row["source_row"]) < 1
                    ):
                        raise DataError("A household record has no valid source location.")

                    # Store the whole row, keyed by county AND profile.
                    self.households[county, profile] = row

                    # Build the county identity table as a side effect.
                    # `setdefault` stores the identity the first time and returns
                    # the existing one after that - so comparing the result to
                    # `identity` detects a county whose name or state differs
                    # between its ten rows.
                    identity = {
                        key: row[key] for key in ("county_fips", "county_name", "state_abbr")
                    }
                    if self.counties.setdefault(county, identity) != identity:
                        raise DataError("Inconsistent county labels across household records.")

            # Completeness: ten profiles per county, no gaps. A county missing
            # its "2p3c" row would silently vanish for families of that shape.
            if not self.counties or len(self.households) != len(self.counties) * 10:
                raise DataError(
                    "The release must contain all ten profiles for each covered county."
                )

            # --- Step 4: climate --------------------------------------------
            self.climate = {}
            # The exact column list, in the exact order, that climate.csv must have.
            climate_fields = [
                "county_fips",
                *[f"{month}_tenths_f" for month in CLIMATE_MONTHS],
                "normal_period_start",
                "normal_period_end",
                "source_state_code",
                "source_county_code",
                "source_element_code",
                "source_period_code",
                "data_release_id",
            ]
            with (self.directory / "climate.csv").open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                # Exact header match, including ORDER. Stricter than necessary
                # for reading (DictReader works by name), but it means a
                # reordered or renamed column is caught immediately rather than
                # producing subtly shifted data.
                if getattr(reader, "fieldnames", climate_fields) != climate_fields:
                    raise DataError(
                        "The climate table columns differ from the supported release contract."
                    )
                for row in reader:
                    county = row["county_fips"]
                    # Must reference a county we actually have costs for, and
                    # must not appear twice.
                    if county not in self.counties or county in self.climate:
                        raise DataError(
                            "The climate table has an unknown or duplicate county identifier."
                        )
                    if row["data_release_id"] != self.release_id:
                        raise DataError(
                            "The climate table and manifest identify different releases."
                        )
                    climate = {"county_fips": county}
                    for month in CLIMATE_MONTHS:
                        text = row[f"{month}_tenths_f"]
                        # Stored as whole numbers of tenths of a degree: 723 = 72.3F.
                        if re.fullmatch(r"-?\d{1,4}", text) is None:
                            raise DataError("A prepared monthly temperature is malformed.")
                        value = int(text)
                        # -50.0F to 140.0F, NOAA's documented range. Checked here
                        # AND again in calculations.py - deliberate redundancy on
                        # the numbers users actually see.
                        if not -500 <= value <= 1400:
                            raise DataError(
                                "A prepared monthly temperature is outside NOAA's documented range."
                            )
                        climate[f"{month}_tenths_f"] = value
                    # Every row must use the same normals period as the manifest.
                    if row["normal_period_start"] != str(
                        self.manifest["climate_normal_period_start"]
                    ) or row["normal_period_end"] != str(
                        self.manifest["climate_normal_period_end"]
                    ):
                        raise DataError("A climate row disagrees with the manifest normal period.")
                    # NOAA's own codes: element "02" is average temperature,
                    # period "0010" is the 1991-2020 normals. Pinning both means
                    # a row describing, say, precipitation cannot be read as heat.
                    if (
                        re.fullmatch(r"\d{2}", row["source_state_code"]) is None
                        or re.fullmatch(r"\d{3}", row["source_county_code"]) is None
                        or row["source_element_code"] != "02"
                        or row["source_period_code"] != "0010"
                    ):
                        raise DataError("A climate row has malformed NOAA source identifiers.")
                    climate.update(
                        normal_period_start=int(row["normal_period_start"]),
                        normal_period_end=int(row["normal_period_end"]),
                        source_state_code=row["source_state_code"],
                        source_county_code=row["source_county_code"],
                        source_element_code=row["source_element_code"],
                        source_period_code=row["source_period_code"],
                    )
                    self.climate[county] = climate

            # Which counties lack climate data must be EXACTLY the list the
            # manifest advertises - not a subset, not a superset. Set
            # subtraction gives "counties with costs but no climate".
            unavailable = set(self.manifest["coverage"]["climate_unavailable_county_fips"])
            if set(self.counties) - set(self.climate) != unavailable:
                raise DataError(
                    "The climate table's unavailable counties disagree with the manifest."
                )

            # --- Step 5: display order and the search index -----------------
            # Sorted by state, then county name (case-insensitively), then FIPS
            # as a tiebreaker. Precomputed once so every result list comes back
            # in the same stable order without re-sorting per request.
            self.ordered_counties = sorted(
                self.counties,
                key=lambda key: (
                    self.counties[key]["state_abbr"],
                    self.counties[key]["county_name"].casefold(),
                    key,
                ),
            )

            # The searchable location index. Counties are built directly; each
            # county's "county_fips" is a one-item list so that counties, places
            # and ZCTAs all share one shape and the API can treat them alike.
            self.locations = {
                "county": {
                    key: {
                        "id": key,
                        "name": row["county_name"],
                        "state_abbr": row["state_abbr"],
                        "county_fips": [key],
                    }
                    for key, row in self.counties.items()
                }
            }

            lookups = self._read_json("lookups.json")
            for kind, key in (("place", "places"), ("zcta", "zctas")):
                self.locations[kind] = {}
                for item in lookups[key]:
                    item = dict(item)  # copy, so we never mutate the loaded JSON
                    # Must be unique, must map to at least one county, and must
                    # not list the same county twice.
                    if (
                        item["id"] in self.locations[kind]
                        or not item["county_fips"]
                        or len(set(item["county_fips"])) != len(item["county_fips"])
                    ):
                        raise DataError("Duplicate or empty location relationship.")
                    # Every referenced county must be one we can price -
                    # otherwise a search could return a location we cannot cost.
                    if set(item["county_fips"]) - self.counties.keys():
                        raise DataError("A lookup references unsupported county costs.")
                    # ZCTAs have no names of their own, so give them one.
                    item.setdefault("name", f"ZCTA {item['id']}")
                    # A place or ZIP area can straddle a state line, so the label
                    # joins every state it touches: "KS, MO".
                    item.setdefault(
                        "state_abbr",
                        ", ".join(
                            sorted({self.counties[c]["state_abbr"] for c in item["county_fips"]})
                        ),
                    )
                    self.locations[kind][item["id"]] = item

            # --- Step 6: states and the final cross-checks ------------------
            self.state_abbreviations = sorted({row["state_abbr"] for row in self.counties.values()})
            states = self._read_json("states.geojson")
            self.state_names = {
                feature["properties"]["state_abbr"]: feature["properties"]["state_name"]
                for feature in states["features"]
            }
            self._validate_coverage(self._read_json("counties.geojson"), states)

        except DataError:
            # Already a clear, specific message - let it through untouched.
            raise
        except (OSError, ValueError, KeyError, TypeError, DecimalException) as exc:
            # Anything else (missing file, bad JSON, absent column, bad number)
            # becomes one DataError. `from exc` keeps the original attached for
            # the log while the user sees the readable version.
            raise DataError(f"Cannot load the installed COLE data: {exc}") from exc

    def _read_json(self, filename):
        # Read one JSON file from the release directory.
        return json.loads((self.directory / filename).read_text(encoding="utf-8"))

    def _validate_coverage(self, counties: dict, states: dict) -> None:
        """Cross-check advertised coverage against loaded records and boundaries."""
        # The last line of defence. Everything above checked files individually;
        # this checks that the files AGREE WITH EACH OTHER and with the counts
        # the manifest advertises.

        # What we actually ended up with.
        actual = {
            "counties": len(self.counties),
            "household_records": len(self.households),
            "climate_counties": len(self.climate),
            "climate_unavailable_counties": len(self.counties) - len(self.climate),
            "states_and_dc": len(self.state_abbreviations),
            "places": len(self.locations["place"]),
            "zctas": len(self.locations["zcta"]),
        }
        # Every advertised count must match reality exactly.
        for field, count in actual.items():
            _require_manifest(
                self.manifest["coverage"][field] == count,
                f"coverage.{field} disagrees with loaded data.",
            )

        # Map shapes must cover exactly the geography we have data for - no
        # county drawn that we cannot price, none priced that we cannot draw.
        for kind, geometry, property_name, expected in (
            ("county", counties, "county_fips", set(self.counties)),
            # A state's FIPS is the first two digits of any of its counties'.
            ("state", states, "state_fips", {county[:2] for county in self.counties}),
        ):
            if geometry["type"] != "FeatureCollection" or not isinstance(
                geometry["features"], list
            ):
                raise DataError(f"Invalid bundled {kind} geography.")
            features = geometry["features"]
            ids = [feature["properties"][property_name] for feature in features]
            # Comparing both length and set content catches duplicates as well
            # as mismatches: equal sets with unequal lengths means a repeat.
            if len(ids) != len(expected) or set(ids) != expected:
                raise DataError(f"Bundled {kind} boundaries disagree with covered geography.")
            for feature in features:
                properties = feature["properties"]
                # Each shape's own id must match the id inside it, and its
                # vintage must match the manifest - so 2021 boundaries cannot be
                # quietly mixed with 2019 ones.
                if (
                    feature["id"] != properties[property_name]
                    or type(properties["geography_vintage"]) is not int
                    or properties["geography_vintage"] != self.manifest["geography_vintage"]
                ):
                    raise DataError(
                        f"Bundled {kind} boundary identity or vintage disagrees with the manifest."
                    )
                # County shapes also carry name and state, which must match the
                # cost table - otherwise the map could label a shape with the
                # wrong county's name while showing the right county's costs.
                if kind == "county" and any(
                    properties[key] != value for key, value in self.counties[feature["id"]].items()
                ):
                    raise DataError("Bundled county labels disagree with household records.")

        if len(self.state_names) != len(states["features"]) or set(self.state_names) != set(
            self.state_abbreviations
        ):
            raise DataError("Bundled state labels disagree with household records.")
        # Each county's state abbreviation must match the state whose FIPS
        # prefix it carries - catching a county filed under the wrong state.
        state_properties = {
            feature["properties"]["state_fips"]: feature["properties"]
            for feature in states["features"]
        }
        if any(
            row["state_abbr"] != state_properties[county[:2]]["state_abbr"]
            for county, row in self.counties.items()
        ):
            raise DataError("Bundled state identifiers disagree with household records.")
        if any(not isinstance(name, str) or not name.strip() for name in self.state_names.values()):
            raise DataError("A bundled state has no valid name.")

        # Count places/ZCTAs that cross county lines and check that against the
        # manifest. `sum()` over True/False works because True counts as 1.
        for kind in ("place", "zcta"):
            count = sum(len(item["county_fips"]) > 1 for item in self.locations[kind].values())
            _require_manifest(
                self.manifest["validation"][f"multi_county_{kind}s"] == count,
                f"validation.multi_county_{kind}s disagrees with loaded relationships.",
            )

        # Finally, the preparation script's own report must agree with what we
        # loaded. This is a third independent witness: the manifest says one
        # thing, the data files say another, and validation.json says a third.
        # All three must agree.
        report = self._read_json("validation.json")
        for group, field, count in (
            ("households", "source_rows", actual["household_records"]),
            ("households", "counties", actual["counties"]),
            ("climate", "selected_counties", actual["climate_counties"]),
            ("climate", "matched_epi_counties", actual["climate_counties"]),
            ("county_geometry", "included_features", actual["counties"]),
            ("state_geometry", "included_features", actual["states_and_dc"]),
            ("lookups", "prepared_place_ids", actual["places"]),
            ("lookups", "prepared_zcta_ids", actual["zctas"]),
            (
                "households",
                "component_total_discrepancy_count",
                self.manifest["validation"]["component_total_discrepancies"],
            ),
        ):
            if type(report[group][field]) is not int or report[group][field] != count:
                raise DataError(f"Validation report {group}.{field} disagrees with the release.")
        if (
            report["climate"]["missing_epi_county_fips"]
            != self.manifest["coverage"]["climate_unavailable_county_fips"]
        ):
            raise DataError("Validation report climate gaps disagree with the release.")
        # Every profile must appear once per county.
        if report["households"]["profile_counts"] != {
            profile: actual["counties"] for profile in PROFILES
        }:
            raise DataError("Validation report profile counts disagree with household coverage.")

    def search_locations(self, kind: str, query: str, limit: int = 60) -> dict:
        # Find counties, places, or ZIP areas matching a typed search.
        # Deliberately simple: no fuzzy matching, no ranking by popularity.
        # Every search term must appear somewhere in the item's text. Typing
        # "travis tx" finds Travis County, Texas because both words appear.
        #
        # This scans every location on every keystroke-triggered search. With a
        # dataset this size that is fast enough, and it avoids the complexity of
        # a search index - a reasonable trade for a local single-user app.
        terms = query.casefold().split()
        candidates = []
        for item in self.locations[kind].values():
            # Build one searchable string per item: ID, name, and state.
            # `.casefold()` is a more thorough lowercase that handles accented
            # and non-English characters correctly.
            searchable = f"{item['id']} {item['name']} {item['state_abbr']}".casefold()
            if all(term in searchable for term in terms):
                candidates.append(item)
        # Sorting: `item["id"] != query` is False (which sorts as 0, i.e. first)
        # for an exact ID match - so typing a ZIP code puts that exact area at
        # the top. Then state, then name, then ID for stability.
        candidates.sort(
            key=lambda item: (
                item["id"] != query,
                item["state_abbr"],
                item["name"].casefold(),
                item["id"],
            )
        )
        # Return a capped list plus honest totals, so the page can say
        # "showing 60 of 340" rather than silently hiding results.
        return {
            "items": candidates[:limit],
            "total": len(candidates),
            "truncated": len(candidates) > limit,
        }
