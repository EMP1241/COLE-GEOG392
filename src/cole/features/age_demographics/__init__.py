"""Optional county-age adapter; the host composes its independent verdict."""
from decimal import Decimal
import logging
from pathlib import Path
import re
import sqlite3

from flask import Blueprint, jsonify

from ...calculations import ValidationError
from .domain import AgeStore, GROUPS, METRICS


def normalize_limits(payload):
    supplied = payload.get("age_limits")
    if supplied is None:
        supplied = {}
    errors = []

    def error(field, message):
        errors.append({"field": "age_limits" + field, "code": "invalid_age_limit", "message": message})

    if not isinstance(supplied, dict) or set(supplied) - set(METRICS):
        error("", "Choose median-age or supported age-group percentage limits.")
        raise ValidationError(errors)
    limits = {}
    for key, spec in METRICS.items():
        bounds = supplied.get(key)
        if bounds is None:
            bounds = {}
        if not isinstance(bounds, dict) or set(bounds) - {"min", "max"}:
            error("." + key, "Use only minimum and maximum fields for this age measure.")
            continue
        result = {}
        for side in ("min", "max"):
            raw = bounds.get(side)
            if raw is None or isinstance(raw, str) and not raw.strip():
                result[side] = None
                continue
            text = str(raw).strip()
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)) or len(text) > 128 or re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", text) is None:
                error(f".{key}.{side}", "Enter 0 or a positive number without commas, or leave this field blank.")
                result[side] = None
                continue
            number = Decimal(text)
            canonical = format(number, "f")
            if "." in canonical:
                canonical = canonical.rstrip("0").rstrip(".")
            if len(canonical) > 64 or len(canonical.partition(".")[2]) > 30 or spec["unit"] == "percent" and number > 100:
                error(f".{key}.{side}", "Use a percentage from 0 to 100 with at most 30 decimal places." if spec["unit"] == "percent" else "Use at most 64 digits total and 30 decimal places.")
                result[side] = None
            else:
                result[side] = canonical
        if result["min"] is not None and result["max"] is not None and Decimal(result["min"]) > Decimal(result["max"]):
            error(f".{key}.max", "The minimum cannot exceed the maximum.")
        limits[key] = result
    if errors:
        raise ValidationError(errors)
    return limits


class AgeFeature:
    def __init__(self, app, repository, directory=None):
        self.store = self.contract = self.error = None
        try:
            self.store = AgeStore(directory or Path(__file__).parent / "data", repository.counties)
            self.contract = self.store.contract
        except (OSError, ValueError, KeyError, IndexError, TypeError, sqlite3.Error) as exc:
            self.store = None
            self.error = "Age data is unavailable. Restore the complete feature release and restart COLE."
            logging.getLogger(__name__).warning("Age feature disabled: %s", exc)
        blueprint = Blueprint("age_demographics", __name__, static_folder="static", template_folder="templates", static_url_path="/features/age/static")

        @blueprint.get("/api/features/age")
        def overview():
            return jsonify(available=self.store is not None, error=self.error, contract=self.contract,
                           feature_version="1.0.0", period_start=2020, period_end=2024, contributor="Mitch Zhang",
                           median_age_moe_confidence_percent=90, derived_share_moe_available=False,
                           metrics=[{"key": key, **spec} for key, spec in METRICS.items()],
                           groups=[{"key": key, "label": spec[0]} for key, spec in GROUPS.items()],
                           coverage=self.store.manifest["coverage"] if self.store else None,
                           records=self.store.records if self.store else {})

        app.register_blueprint(blueprint)

    def normalize_request(self, payload, scenario):
        limits = normalize_limits(payload)
        if not any(value is not None for bounds in limits.values() for value in bounds.values()):
            return scenario
        if self.store is None or payload.get("age_feature_contract") != self.contract:
            raise ValidationError([{"field": "age_limits", "code": "age_feature_unavailable", "message": "Age data changed or is unavailable. Reload COLE before using these limits."}])
        scenario.update(age_limits=limits, age_feature_contract=self.contract)
        return scenario

    def evaluate_county(self, result, county, scenario):
        limits = scenario.get("age_limits")
        if limits is None:
            return True
        verdict = self.store.evaluate(county, limits)
        result.update(verdict)
        return verdict["age_filters_match"]
