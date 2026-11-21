"""Optional Public outdoor areas adapter, isolated from household cost calculations."""
from decimal import Decimal
import logging
from pathlib import Path
import re
import sqlite3

from flask import Blueprint, jsonify

from ...calculations import ValidationError
from .domain import GreenspaceStore, OWNERSHIP

# Full name confirmed after data preparation; retain the original audit metadata.
ADDON_CONTRIBUTOR = "Margaret Worsham"


def normalize_limits(payload):
    supplied = payload.get("greenspace_limits")
    if supplied is None:
        supplied = {}
    errors, limits = [], {}

    def error(side, message):
        errors.append({"field": "greenspace_limits" + side, "code": "invalid_greenspace_limit", "message": message})

    if not isinstance(supplied, dict) or set(supplied) - {"min", "max"}:
        error("", "Use only minimum and maximum percentage fields for Public outdoor areas.")
        raise ValidationError(errors)
    for side in ("min", "max"):
        raw = supplied.get(side)
        if raw is None or isinstance(raw, str) and not raw.strip():
            limits[side] = None
            continue
        text = str(raw).strip()
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)) or len(text) > 128 or re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", text) is None:
            error("." + side, "Enter a percentage from 0 to 100 without commas, or leave this field blank.")
            limits[side] = None
            continue
        number = Decimal(text)
        canonical = format(number, "f")
        if "." in canonical:
            canonical = canonical.rstrip("0").rstrip(".")
        if number > 100 or len(canonical) > 64 or len(canonical.partition(".")[2]) > 30:
            error("." + side, "Use a percentage from 0 to 100 with at most 30 decimal places.")
            limits[side] = None
        else:
            limits[side] = canonical
    if limits["min"] is not None and limits["max"] is not None and Decimal(limits["min"]) > Decimal(limits["max"]):
        error(".max", "The minimum cannot exceed the maximum.")
    if errors:
        raise ValidationError(errors)
    return limits


class GreenspaceFeature:
    def __init__(self, app, repository, directory=None):
        self.store = self.contract = self.error = None
        try:
            core_directory = getattr(repository, "directory", None)
            if core_directory is None:
                raise ValueError("Core geography directory is unavailable.")
            self.store = GreenspaceStore(directory or Path(__file__).parent / "data", repository.counties,
                                        Path(core_directory) / "counties.geojson")
            self.contract = self.store.contract
        except (OSError, ValueError, KeyError, IndexError, TypeError, OverflowError, sqlite3.Error) as exc:
            self.store = None
            self.error = "Public outdoor areas data is unavailable. Restore the complete feature release and restart COLE."
            logging.getLogger(__name__).warning("Public outdoor areas feature disabled: %s", exc)
        blueprint = Blueprint("greenspace", __name__, static_folder="static", template_folder="templates",
                              static_url_path="/features/greenspace/static")

        @blueprint.get("/api/features/greenspace")
        def overview():
            return jsonify(available=self.store is not None, error=self.error, contract=self.contract,
                           feature_version="1.0.0", dataset="PAD-US 4.1", county_vintage=2021,
                           denominator="whole_county_including_water",
                           credits={**self.store.manifest["credits"], "feature_addon_contributor": ADDON_CONTRIBUTOR} if self.store else None,
                           ownership=[{"key": key, "label": label} for key, (label, _) in OWNERSHIP.items()],
                           coverage=self.store.coverage if self.store else None,
                           records=self.store.records if self.store else {})

        app.register_blueprint(blueprint)

    def normalize_request(self, payload, scenario):
        limits = normalize_limits(payload)
        if all(value is None for value in limits.values()):
            return scenario
        if self.store is None or payload.get("greenspace_feature_contract") != self.contract:
            raise ValidationError([{"field": "greenspace_limits", "code": "greenspace_feature_unavailable",
                                    "message": "Public outdoor areas data changed or is unavailable. Reload COLE before using these limits."}])
        scenario.update(greenspace_limits=limits, greenspace_feature_contract=self.contract)
        return scenario

    def evaluate_county(self, result, county, scenario):
        limits = scenario.get("greenspace_limits")
        if limits is None:
            return True
        verdict = self.store.evaluate(county, limits)
        result.update(verdict)
        return verdict["greenspace_filters_match"]
