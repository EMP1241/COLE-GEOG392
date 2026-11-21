# Original natural-hazard logic: Bingqian, submitted 2026-10-12.
# Integration changes are documented in features/hazards/README.md.
# The explanatory comments below preserve the contributor's original rationale;
# weighting/correlation claims are not verified project findings. The feature
# adapter now calls normalization; the core calculator is unchanged.
"""Natural-hazard add-on: FEMA National Risk Index expected-annual-loss rates.

Validates the optional hazard filters a user submits, loads the prepared
hazards.csv table, and evaluates one county against the submitted filters.
Like calculations.py, nothing here touches the network or remembers anything
between calls.
"""

# ===========================================================================
# PLAIN-ENGLISH GUIDE TO THIS FILE
# ===========================================================================
#
# WHAT IT DOES
# Adds a "natural hazard" filter to COLE, built the same way as the existing
# temperature filter:
#
#   normalize_hazard_limits(payload, errors)   called by normalize_scenario
#   evaluate_hazards(source, normalized)       called by app.py per county
#   load_hazards(path, counties)               called once at startup
#
# WHICH NUMBERS, AND WHY
# FEMA's National Risk Index (NRI) publishes, per county and hazard, the
# EXPECTED ANNUAL LOSS RATE: the share of an exposure expected to be lost in
# an average year. COLE uses two of its exposures, each as its own measure:
#
#   property   building loss rate   (NRI *_ALRB, all hazards: ALR_VALB)
#   life       population loss rate (NRI *_ALRP, all hazards: ALR_VALP):
#              expected deaths per resident per year, with each injury
#              counted as a tenth of a death (FEMA's population equivalence)
#
# The FILTER uses property for every hazard except heat wave, whose harm is
# almost entirely to people (buildings are a median 0.01% of its expected
# loss); the heat-wave limit uses life safety. Life safety is shown beside
# property for every hazard, flagged when the county has under 5,000
# residents, because a rate built on a fraction of a death per year in a
# tiny county swings widely.
#
# Three things COLE deliberately does NOT use:
#   - NRI's headline Risk rating: it starts from dollar losses, so it tracks
#     population (rank correlation 0.86) and mostly flags big cities.
#   - NRI's published loss-rate percentiles (*_ALR_NPCTL): FEMA's own data
#     weight the population term by a count of deaths rather than its dollar
#     value, so loss of life barely moves them (St. Louis city, among the
#     highest heat-death rates in the country, scores 4.7 for heat). They
#     also mix in agriculture and rank across territories.
#   - Agricultural loss: it matters to farms, not to a household's home.
#
# HOW A CLASS IS MADE
# tools/prepare_hazards.py ranks each rate among COLE's own counties where
# that hazard causes some expected loss, as a percentile from 0 to 100
# (ties share a midrank), rounded half up to one decimal. The class follows
# the rounded value COLE displays:
#
#   very_low  < 20  <= low < 40 <= moderate < 60 <= high < 80 <= very_high
#
# A user filter reads "Low or lower". Classes are ordered categories, so no
# floating-point value takes part in a match.
#
# THREE SPECIAL VALUES (from NRI's expected-annual-loss rating)
#   not_applicable     the hazard does not occur there      -> passes a limit
#   no_rating          no expected loss to this exposure    -> passes a limit
#   insufficient_data  FEMA could not estimate it           -> "unavailable"
#
# A county with no NRI row at all (Connecticut's legacy counties, because
# NRI v1.20 uses Connecticut's newer planning regions) is unavailable as a
# whole, exactly like a county with no NOAA climate record. COLE never
# substitutes a neighbour's value.
# ===========================================================================

from __future__ import annotations

import csv
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

# The hazards COLE offers, in display order. "overall" is NRI's composite
# across all 18 hazards; the rest are the hazards most relevant to a household
# choosing where to live. Adding one means one entry here and one in each
# mapping below - every other layer reads these tuples.
HAZARD_TYPES = (
    "overall",
    "hurricane",
    "wildfire",
    "inland_flooding",
    "coastal_flooding",
    "tornado",
    "earthquake",
    "winter_weather",
    "heat_wave",
)

HAZARD_LABELS = {
    "overall": "All hazards combined",
    "hurricane": "Hurricane",
    "wildfire": "Wildfire",
    "inland_flooding": "Inland flooding",
    "coastal_flooding": "Coastal flooding",
    "tornado": "Tornado",
    "earthquake": "Earthquake",
    "winter_weather": "Winter weather",
    "heat_wave": "Heat wave",
}

# The two measures, and which one each hazard's limit filters on.
MEASURES = ("property", "life")
MEASURE_LABELS = {"property": "Property", "life": "Life safety"}
FILTER_MEASURES = {name: "property" for name in HAZARD_TYPES} | {"heat_wave": "life"}

# NRI v1.20 provenance for each hazard: (expected-annual-loss rating that
# carries the special values, building loss rate, population loss rate).
_NRI_PREFIXES = {
    "hurricane": "HRCN", "wildfire": "WFIR", "inland_flooding": "IFLD", "coastal_flooding": "CFLD",
    "tornado": "TRND", "earthquake": "ERQK", "winter_weather": "WNTW", "heat_wave": "HWAV",
}
NRI_STATUS_FIELDS = {"overall": "EAL_RATNG", **{name: f"{prefix}_EALR" for name, prefix in _NRI_PREFIXES.items()}}
NRI_RATE_FIELDS = {
    "property": {"overall": "ALR_VALB", **{name: f"{prefix}_ALRB" for name, prefix in _NRI_PREFIXES.items()}},
    "life": {"overall": "ALR_VALP", **{name: f"{prefix}_ALRP" for name, prefix in _NRI_PREFIXES.items()}},
}

# The five classes, lowest first. Position in this tuple IS the order;
# CLASS_UPPER_BOUNDS gives each class's exclusive upper percentile.
HAZARD_CLASSES = ("very_low", "low", "moderate", "high", "very_high")
CLASS_UPPER_BOUNDS = (Decimal("20"), Decimal("40"), Decimal("60"), Decimal("80"))
NO_EXPOSURE_CLASSES = ("not_applicable", "no_rating")
UNAVAILABLE_CLASSES = ("insufficient_data",)
SOURCE_CLASSES = HAZARD_CLASSES + NO_EXPOSURE_CLASSES + UNAVAILABLE_CLASSES

CLASS_LABELS = {
    "very_low": "Very Low",
    "low": "Low",
    "moderate": "Moderate",
    "high": "High",
    "very_high": "Very High",
    "not_applicable": "Not applicable",
    "no_rating": "No expected loss",
    "insufficient_data": "Insufficient data",
}

# A user may choose "at most" any class except Very High. "At most Very High"
# would admit every rated county and differ from "no limit" only by excluding
# unavailable counties - a distinction nobody would guess from the label.
LIMIT_CHOICES = HAZARD_CLASSES[:-1]

# Counties that were renamed AND renumbered, same territory: an older county
# list (COLE's EPI release may predate the change) uses the old code, NRI the
# new one. prepare_hazards.py joins them through this table and the loader
# accepts the new name for the old code. Splits and mergers are not here.
FIPS_RENAMES = {
    "02270": ("02158", "Kusilvak", "Census Area"),        # Wade Hampton -> Kusilvak, AK (2015)
    "46113": ("46102", "Oglala Lakota", "County"),        # Shannon -> Oglala Lakota, SD (2015)
    "12025": ("12086", "Miami-Dade", "County"),           # Dade -> Miami-Dade, FL (1997)
}

# Below this population the life-safety rate is flagged as unstable.
SMALL_POPULATION = 5000

# FEMA's composite community risk rating, shown for context.
COMMUNITY_RISK_RATINGS = {
    "Very Low": "very_low", "Relatively Low": "relatively_low",
    "Relatively Moderate": "relatively_moderate", "Relatively High": "relatively_high",
    "Very High": "very_high", "Insufficient Data": "insufficient_data",
}
COMMUNITY_RISK_LABELS = {key: label for label, key in COMMUNITY_RISK_RATINGS.items()}

# The exact header of the prepared table, in order.
HAZARD_CSV_FIELDS = [
    "county_fips", "nri_county_name", "nri_state_abbr", "nri_county_type", "population",
    *[f"{name}_{measure}_{column}" for name in HAZARD_TYPES for measure in MEASURES
      for column in ("percentile", "class")],
    "community_risk_rating", "nri_version",
]

# A one-decimal percentile from 0.0 to 100.0, kept as text.
_PERCENTILE = re.compile(r"(?:100|[1-9]?[0-9])\.[0-9]\Z")
_TENTH = Decimal("0.1")

# COLE label suffix -> the NRI COUNTYTYPE it must carry. Checking the type
# separates same-named pairs such as Baltimore County (24005) and Baltimore
# city (24510), which NRI both calls "Baltimore". Longest suffixes first.
_SUFFIX_TYPES = (
    ("city and borough", "City and Borough"), ("planning region", "Planning Region"),
    ("census area", "Census Area"), ("municipality", "Municipality"), ("borough", "Borough"),
    ("parish", "Parish"), ("county", "County"), ("city", "City"),
)


class HazardDataError(ValueError):
    """The prepared hazard table is missing, malformed or inconsistent."""
    # A ValueError subclass so repository.py's existing loader converts it
    # into its own DataError with the message intact.


def percentile_ranks(rates: Mapping[str, str]) -> dict[str, str]:
    """Rank decimal-string rates as one-decimal percentiles from 0 to 100.

    The lowest rate is 0.0, the highest 100.0, and tied rates share their
    midrank. Computed with exact fractions, then rounded half up.
    """
    values = {key: Decimal(text) for key, text in rates.items()}
    ordered = sorted(values.values())
    count = len(ordered)
    result = {}
    for key, value in values.items():
        if count == 1:
            fraction = Fraction(1, 2)
        else:
            below = _count_below(ordered, value)
            ties = _count_below(ordered, value, inclusive=True) - below
            fraction = Fraction(2 * below + ties - 1, 2 * (count - 1))
        tenths = (2000 * fraction.numerator + fraction.denominator) // (2 * fraction.denominator)
        result[key] = f"{tenths // 10}.{tenths % 10}"
    return result


def _count_below(ordered: list, value: Decimal, inclusive: bool = False) -> int:
    # Binary search on the sorted Decimals (bisect without float keys).
    low, high = 0, len(ordered)
    while low < high:
        middle = (low + high) // 2
        if ordered[middle] < value or (inclusive and ordered[middle] == value):
            low = middle + 1
        else:
            high = middle
    return low


def plain_rank_claim(percentile: str) -> tuple[str, int]:
    """The claim app.js's plainRank makes for a percentile string.

    ("more", k) reads "more than k% of other counties"; ("less", k) reads
    "less than k% ...". Must stay identical to plainRank (a test compares
    the two on every possible percentile).
    """
    whole, tenth = percentile.split(".")
    floor = int(whole)
    if floor >= 50:
        return "more", floor - 1 if tenth == "0" else floor
    return "less", 99 - floor


def check_plain_ranks(rates: Mapping[str, str], percentiles: Mapping[str, str]) -> None:
    """Fail if any plain-language sentence for these ranks would be false.

    A midrank shared by a large tie can sit above the share of counties
    strictly below it, so the sentence is checked against exact counts
    rather than trusted from the percentile alone.
    """
    values = {key: Decimal(text) for key, text in rates.items()}
    ordered = sorted(values.values())
    others = len(ordered) - 1
    for key, value in values.items():
        direction, share = plain_rank_claim(percentiles[key])
        below = _count_below(ordered, value)
        count = below if direction == "more" else len(ordered) - _count_below(ordered, value, inclusive=True)
        if others > 0 and not count * 100 > share * others:
            raise HazardDataError(
                f"County {key}: percentile {percentiles[key]} would read '{direction} than {share}% of other "
                f"counties', but only {count} of {others} are {'below' if direction == 'more' else 'above'} it.")


def classify_percentile(percentile: str) -> str:
    """Return the class of a one-decimal percentile string."""
    value = Decimal(percentile)
    for name, bound in zip(HAZARD_CLASSES, CLASS_UPPER_BOUNDS):
        if value < bound:
            return name
    return HAZARD_CLASSES[-1]


def _name_key(text: str) -> str:
    # Accent-, case- and punctuation-insensitive comparison key.
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold().strip()
    text = re.sub(r"\bsaint\b", "st", text)
    return re.sub(r"[^a-z0-9]", "", text)


def _split_suffix(cole_name: str) -> tuple[str, str | None]:
    lowered = cole_name.casefold().strip()
    for suffix, county_type in _SUFFIX_TYPES:
        if lowered.endswith(" " + suffix):
            return lowered[: -len(suffix) - 1], county_type
    return lowered, None


def county_identity_agrees(cole_name: str, nri_name: str, nri_type: str) -> bool:
    """True when NRI's name and county type describe COLE's county label.

    "Charles City County" matches ("Charles City", County); "Baltimore city"
    matches ("Baltimore", City) but not ("Baltimore", County); "Carson City"
    matches ("Carson City", City).
    """
    stripped, expected_type = _split_suffix(cole_name)
    if _name_key(nri_name) not in (_name_key(cole_name), _name_key(stripped)):
        return False
    return expected_type is None or nri_type == expected_type


def _validation_error(field: str, code: str, message: str):
    # Imported here, not at the top, because calculations.py imports this
    # module; a top-level import back would be circular.
    from ...calculations import ValidationError
    return ValidationError([{"field": field, "code": code, "message": message}])


def normalize_hazard_limits(payload: dict, errors: list) -> dict[str, str | None]:
    """Validate optional "at most this class" hazard limits.

    Blank or null means no limit. An omitted section behaves like an empty
    one, so a scenario written before this add-on existed is still valid.
    """
    raw = payload.get("hazard_limits", {})
    if not isinstance(raw, dict):
        errors.append({"field": "hazard_limits", "code": "invalid_hazard_limits",
                       "message": "Provide hazard limits as an object; leave a hazard blank for no limit."})
        return {name: None for name in HAZARD_TYPES}
    if any(name not in HAZARD_TYPES for name in raw):
        errors.append({"field": "hazard_limits", "code": "invalid_hazard_limits",
                       "message": "Use only the supported FEMA National Risk Index hazards."})

    limits = {}
    for name in HAZARD_TYPES:
        value = raw.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            limits[name] = None
        elif isinstance(value, str) and value in LIMIT_CHOICES:
            limits[name] = value
        else:
            errors.append({"field": f"hazard_limits.{name}", "code": "invalid_hazard_limit",
                           "message": "Choose no limit or a maximum class from Very Low through High."})
            limits[name] = None
    return limits


def evaluate_hazards(source_hazards: Any, normalized: dict) -> dict:
    """Evaluate one county's classes against the submitted limits (FILTER_MEASURES)."""
    limits = normalized.get("hazard_limits")
    if not isinstance(limits, dict) or any(name not in limits for name in HAZARD_TYPES):
        raise _validation_error("hazard_limits", "invalid_hazard_limits",
                                "The normalized scenario has no complete hazard-limit definition.")
    active = [name for name in HAZARD_TYPES if limits[name] is not None]
    for name in active:
        # Re-check, as evaluate_temperature does, in case a scenario was
        # assembled by hand rather than by normalize_scenario.
        if limits[name] not in LIMIT_CHOICES:
            raise _validation_error(f"hazard_limits.{name}", "invalid_hazard_limits",
                                    "A normalized hazard limit is malformed.")

    # No NRI row for this county: every active filter is unavailable. With no
    # active filter the county still matches, mirroring missing climate data.
    if source_hazards is None:
        return {
            "hazard_data_availability": "unavailable",
            "hazards": None,
            "hazard_filter_matches": {name: None for name in active},
            "hazard_filters_match": not active,
            "failed_hazard_filters": [],
            "unavailable_hazard_filters": active,
        }

    measures = source_hazards.get("measures") if isinstance(source_hazards, dict) else None
    if not isinstance(measures, dict) or any(
            not isinstance(measures.get(measure), dict)
            or any(not isinstance(measures[measure].get(name), dict)
                   or measures[measure][name].get("class") not in SOURCE_CLASSES for name in HAZARD_TYPES)
            for measure in MEASURES):
        raise _validation_error("hazards", "invalid_source_hazards",
                                "The prepared county hazard record is malformed.")

    # None = this filter could not be applied (insufficient data).
    matches = {}
    for name in active:
        value = measures[FILTER_MEASURES[name]][name]["class"]
        if value in UNAVAILABLE_CLASSES:
            matches[name] = None
        elif value in NO_EXPOSURE_CLASSES:
            matches[name] = True
        else:
            matches[name] = HAZARD_CLASSES.index(value) <= HAZARD_CLASSES.index(limits[name])

    return {
        "hazard_data_availability": "available",
        "hazards": {
            "property": {name: dict(measures["property"][name]) for name in HAZARD_TYPES},
            "life": {name: dict(measures["life"][name]) for name in HAZARD_TYPES},
            "population": source_hazards.get("population"),
            "small_population": source_hazards.get("small_population"),
            "community_risk_rating": source_hazards.get("community_risk_rating"),
            "nri_county_name": source_hazards.get("nri_county_name"),
            "nri_version": source_hazards.get("nri_version"),
        },
        "hazard_filter_matches": matches,
        # Only an explicit pass counts: an unavailable filter cannot be met.
        "hazard_filters_match": all(match is True for match in matches.values()),
        "failed_hazard_filters": [name for name in HAZARD_TYPES if matches.get(name) is False],
        "unavailable_hazard_filters": [name for name in HAZARD_TYPES if name in matches and matches[name] is None],
    }


def load_hazards(path: Path | str, counties: Mapping[str, Mapping[str, str]]) -> dict[str, dict]:
    """Load and validate hazards.csv against the counties COLE can price.

    `counties` maps county FIPS to COLE's identity ({"county_name",
    "state_abbr", ...}). Every row must name a covered county exactly once and
    agree with COLE on its state, name and county type, so a recoded FIPS
    cannot silently attach one county's hazards to another. Covered counties
    without a row are reported by the caller as hazard-unavailable.
    """
    hazards: dict[str, dict] = {}
    versions = set()
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != HAZARD_CSV_FIELDS:
            raise HazardDataError("The hazard table columns differ from the supported add-on contract.")
        for row in reader:
            # A short row leaves fields as None; a long one keeps extras under None.
            if None in row or None in row.values():
                raise HazardDataError(f"Hazard table line {reader.line_num} has the wrong number of fields.")
            county = row["county_fips"]
            if not re.fullmatch(r"\d{5}", county) or county not in counties or county in hazards:
                raise HazardDataError("The hazard table has an unknown or duplicate county identifier.")
            identity = counties[county]
            renamed = FIPS_RENAMES.get(county)
            if (row["nri_state_abbr"] != identity.get("state_abbr")
                    or not (county_identity_agrees(identity.get("county_name", ""), row["nri_county_name"],
                                                   row["nri_county_type"])
                            or (renamed and (row["nri_county_name"], row["nri_county_type"]) == renamed[1:]))):
                raise HazardDataError(
                    f"Hazard row {county} ({row['nri_county_name']} {row['nri_county_type']}, "
                    f"{row['nri_state_abbr']}) does not match COLE's {identity.get('county_name')}, "
                    f"{identity.get('state_abbr')}.")
            if not re.fullmatch(r"[1-9][0-9]{0,8}|0", row["population"]):
                raise HazardDataError(f"County {county} has a malformed population.")

            measures = {measure: {} for measure in MEASURES}
            for name in HAZARD_TYPES:
                for measure in MEASURES:
                    value = row[f"{name}_{measure}_class"]
                    percentile = row[f"{name}_{measure}_percentile"]
                    if value in HAZARD_CLASSES:
                        # The stored class must be exactly what the stored
                        # percentile implies - the hazard equivalent of
                        # re-deriving every baseline from total minus taxes.
                        if (not _PERCENTILE.fullmatch(percentile) or Decimal(percentile) > 100
                                or classify_percentile(percentile) != value):
                            raise HazardDataError(
                                f"County {county} has a {name} {measure} class that disagrees with its percentile.")
                    elif value in NO_EXPOSURE_CLASSES + UNAVAILABLE_CLASSES:
                        if percentile:
                            raise HazardDataError(
                                f"County {county} has a {name} {measure} percentile without a ranked class.")
                        percentile = None
                    else:
                        raise HazardDataError(f"County {county} has an unsupported {name} {measure} class.")
                    measures[measure][name] = {"class": value, "percentile": percentile}
                # A hazard that does not occur, or cannot be estimated, is so
                # for both measures.
                for special in ("not_applicable", "insufficient_data"):
                    if (measures["property"][name]["class"] == special) != (measures["life"][name]["class"] == special):
                        raise HazardDataError(f"County {county} has inconsistent {name} measures.")

            if row["community_risk_rating"] not in COMMUNITY_RISK_LABELS:
                raise HazardDataError(f"County {county} has an unsupported community risk rating.")
            if not row["nri_version"].strip():
                raise HazardDataError(f"County {county} has no NRI provenance.")
            versions.add(row["nri_version"])
            population = int(row["population"])
            hazards[county] = {
                "county_fips": county,
                "measures": measures,
                "population": population,
                "small_population": population < SMALL_POPULATION,
                "community_risk_rating": row["community_risk_rating"],
                "nri_county_name": row["nri_county_name"],
                "nri_state_abbr": row["nri_state_abbr"],
                "nri_version": row["nri_version"],
            }
    # One release per table, just as households.csv carries one release id.
    if len(versions) > 1:
        raise HazardDataError("The hazard table mixes NRI releases.")
    return hazards
