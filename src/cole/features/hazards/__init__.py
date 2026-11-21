"""Bingqian's natural-hazard contribution, isolated from COLE's core contracts."""

import hashlib
import json
import logging
from pathlib import Path

from flask import Blueprint, jsonify

from ...calculations import ValidationError
from .domain import (
    CLASS_LABELS,
    FILTER_MEASURES,
    HAZARD_LABELS,
    HAZARD_TYPES,
    LIMIT_CHOICES,
    evaluate_hazards,
    load_hazards,
    normalize_hazard_limits,
)


class HazardFeature:
    def __init__(self, app, repository, directory=None):
        self.records = {}
        self.error = None
        self.contract = None
        self.manifest = None
        directory = Path(directory) if directory is not None else Path(__file__).parent / "data"
        try:
            self.manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            if self.manifest["feature_version"] != "1.0.0":
                raise ValueError("Unsupported hazard feature version.")
            county_keys = "\n".join(sorted(repository.counties)) + "\n"
            if hashlib.sha256(county_keys.encode()).hexdigest() != self.manifest["county_keys_sha256"]:
                raise ValueError("The hazard data was prepared for a different county set.")
            for name in ("hazards.csv", "hazards_report.json"):
                content = (directory / name).read_bytes()
                expected = self.manifest["outputs"][name]
                if len(content) != expected["bytes"] or hashlib.sha256(content).hexdigest() != expected["sha256"]:
                    raise ValueError(f"The hazard data fingerprint differs: {name}.")
            self.records = load_hazards(directory / "hazards.csv", repository.counties)
            missing = sorted(set(repository.counties) - self.records.keys())
            if (len(self.records) != self.manifest["coverage"]["hazard_counties"]
                    or missing != self.manifest["coverage"]["hazard_unavailable_county_fips"]
                    or {record["nri_version"] for record in self.records.values()} != {self.manifest["nri_version"]}):
                raise ValueError("The hazard coverage or source release differs from its manifest.")
            self.contract = hashlib.sha256(json.dumps(self.manifest, sort_keys=True).encode()).hexdigest()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.records = {}
            self.error = "Natural-hazard data is unavailable. Restore the complete feature folder and restart COLE to enable its limits."
            logging.getLogger(__name__).warning("Hazard feature disabled: %s", exc)

        blueprint = Blueprint("hazards", __name__, static_folder="static", template_folder="templates",
                              static_url_path="/features/hazards/static")

        @blueprint.get("/api/features/hazards")
        def overview():
            return jsonify(
                available=self.error is None, error=self.error, contract=self.contract,
                contributor="Bingqian", feature_version="1.0.0", manifest=self.manifest if not self.error else None,
                hazard_types=[{"key": name, "label": HAZARD_LABELS[name], "measure": FILTER_MEASURES[name]}
                              for name in HAZARD_TYPES],
                limit_choices=[{"key": key, "label": CLASS_LABELS[key]} for key in LIMIT_CHOICES],
                records=self.records,
            )

        app.register_blueprint(blueprint)

    def normalize_request(self, payload, scenario):
        if "hazard_limits" not in payload and "hazard_feature_contract" not in payload:
            return scenario
        errors = []
        limits = normalize_hazard_limits(payload, errors)
        if errors:
            raise ValidationError(errors)
        if not any(value is not None for value in limits.values()):
            return scenario
        if self.error or payload.get("hazard_feature_contract") != self.contract:
            raise ValidationError([{"field": "hazard_limits", "code": "hazard_feature_unavailable",
                                    "message": "The natural-hazard feature or data changed. Reload COLE before using hazard limits."}])
        scenario.update(hazard_limits=limits, hazard_feature_contract=self.contract)
        return scenario

    def evaluate_county(self, result, county, scenario):
        if "hazard_limits" not in scenario:
            return True
        verdict = evaluate_hazards(self.records.get(county), scenario)
        result.update(verdict)
        return verdict["hazard_filters_match"]
