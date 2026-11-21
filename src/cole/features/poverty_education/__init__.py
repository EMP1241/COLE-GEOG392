"""Optional county poverty/attainment adapter; all implementation stays in this feature."""
from decimal import Decimal
import logging
from pathlib import Path
import re
import sqlite3

from flask import Blueprint, jsonify

from ...calculations import ValidationError
from .domain import CountyMeasures, METRICS


def normalize_limits(payload):
    supplied = payload.get("poverty_education_limits")
    if supplied is None:
        supplied = {}
    errors = []

    def error(field, message):
        errors.append({"field": "poverty_education_limits" + field, "code": "invalid_percentage", "message": message})

    if not isinstance(supplied, dict) or set(supplied) - set(METRICS):
        raise ValidationError([{"field": "poverty_education_limits", "code": "invalid_percentage", "message": "Choose poverty or educational-attainment percentage limits."}])
    limits = {}
    for key in METRICS:
        bounds = supplied.get(key)
        if bounds is None:
            bounds = {}
        if not isinstance(bounds, dict) or set(bounds) - {"min", "max"}:
            error("." + key, "Use only minimum and maximum percentage fields.")
            continue
        result = {}
        for side in ("min", "max"):
            raw = bounds.get(side)
            if raw is None or isinstance(raw, str) and not raw.strip():
                result[side] = None
                continue
            text = str(raw).strip()
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)) or len(text) > 128 or re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", text) is None or not 0 <= Decimal(text) <= 100:
                error(f".{key}.{side}", "Enter a percentage from 0 to 100, or leave it blank. Use digits without commas.")
                result[side] = None
            else:
                canonical = format(Decimal(text), "f")
                canonical = canonical.rstrip("0").rstrip(".") if "." in canonical else canonical
                if len(canonical) > 32:
                    error(f".{key}.{side}", "Use at most 30 digits after the decimal point.")
                    result[side] = None
                else:
                    result[side] = canonical
        if result["min"] is not None and result["max"] is not None and Decimal(result["min"]) > Decimal(result["max"]):
            error("." + key, "The minimum percentage cannot exceed the maximum.")
        limits[key] = result
    if errors:
        raise ValidationError(errors)
    return limits


class PovertyEducationFeature:
    def __init__(self, app, repository, directory=None):
        self.store = self.contract = self.error = None
        try:
            self.store = CountyMeasures(directory or Path(__file__).parent / "data", repository.counties)
            self.contract = self.store.contract
        except (OSError, ValueError, KeyError, IndexError, TypeError, sqlite3.Error) as exc:
            self.store = None
            self.error = "Poverty and education data is unavailable. Restore the complete feature release and restart COLE."
            logging.getLogger(__name__).warning("Poverty/education feature disabled: %s", exc)
        blueprint = Blueprint("poverty_education", __name__, static_folder="static", template_folder="templates", static_url_path="/features/poverty-education/static")

        @blueprint.get("/api/features/poverty-education")
        def overview():
            return jsonify(available=self.store is not None, error=self.error, contract=self.contract, feature_version="1.0.0",
                           period_start=2020, period_end=2024, moe_confidence_percent=90,
                           metrics=[{"key": key, **spec} for key, spec in METRICS.items()],
                           coverage=self.store.manifest["coverage"] if self.store else None,
                           records=self.store.records if self.store else {})

        app.register_blueprint(blueprint)

    def normalize_request(self, payload, scenario):
        limits = normalize_limits(payload)
        if not any(value is not None for bounds in limits.values() for value in bounds.values()):
            return scenario
        if self.store is None or payload.get("poverty_education_feature_contract") != self.contract:
            raise ValidationError([{"field": "poverty_education_limits", "code": "poverty_education_feature_unavailable", "message": "Poverty/education data changed or is unavailable. Reload COLE before using these limits."}])
        scenario.update(poverty_education_limits=limits, poverty_education_feature_contract=self.contract)
        return scenario

    def evaluate_county(self, result, county, scenario):
        limits = scenario.get("poverty_education_limits")
        if limits is None:
            return True
        verdict = self.store.evaluate(county, limits)
        result.update(verdict)
        return verdict["poverty_education_filters_match"]
