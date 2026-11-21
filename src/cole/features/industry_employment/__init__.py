"""Optional industry-employment feature; no financial or hazard internals are imported."""

import logging
from pathlib import Path
import sqlite3

from flask import Blueprint, jsonify, request

from ...calculations import ValidationError
from .domain import EmploymentStore


def invalid(message, code="invalid_industry_sector", field="industry_sector"):
    raise ValidationError([{"field": field, "code": code, "message": message}])


class IndustryEmploymentFeature:
    def __init__(self, app, repository, directory=None):
        self.store = None
        self.error = None
        self.contract = None
        try:
            self.store = EmploymentStore(directory or Path(__file__).parent / "data", repository.counties)
            self.contract = self.store.contract
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            self.store = None
            self.error = "Industry employment data is unavailable. Restore the complete feature release and restart COLE."
            logging.getLogger(__name__).warning("Industry feature disabled: %s", exc)

        blueprint = Blueprint("industry_employment", __name__, static_folder="static", template_folder="templates",
                              static_url_path="/features/industry-employment/static")

        @blueprint.get("/api/features/industry-employment")
        def overview():
            return jsonify(available=self.store is not None, error=self.error, contract=self.contract,
                           feature_version="1.0.0", selection_version=2, match_modes=["all", "any"],
                           year=self.store.manifest["year"] if self.store else None,
                           sectors=self.store.sectors if self.store else [],
                           coverage=self.store.manifest["coverage"] if self.store else None)

        @blueprint.get("/api/features/industry-employment/county/<county>")
        def profile(county):
            if county not in repository.counties:
                invalid("Choose a covered county identifier.")
            if self.store is None:
                invalid(self.error, "industry_feature_unavailable")
            if request.args.get("contract") != self.contract:
                invalid("Industry data changed. Reload COLE before exploring its breakdown.", "industry_feature_unavailable")
            parent = request.args.get("parent") or None
            if parent is not None and parent not in self.store.industries:
                invalid("Choose an industry from this data release.")
            return jsonify(self.store.profile(county, parent))

        app.register_blueprint(blueprint)

    def normalize_request(self, payload, scenario):
        sector = payload.get("industry_sector")
        if "industry_sectors" in payload or "industry_match_mode" in payload:
            mode = payload.get("industry_match_mode", "all")
            if not isinstance(mode, str) or mode not in ("all", "any"):
                invalid("Choose All selected or Any selected for industry matching.", field="industry_match_mode")
            if sector is not None and not isinstance(sector, str):
                invalid("Choose a broad industry sector from the available list.")
            sectors = payload.get("industry_sectors", [sector] if sector else [])
            if not isinstance(sectors, list) or len(sectors) > 20 or any(not isinstance(code, str) or not code for code in sectors):
                invalid("Choose up to 20 broad industry sectors from the available list.", field="industry_sectors")
            sectors = sorted(set(sectors))
            if sector and sectors != [sector]:
                invalid("Use one consistent list of selected industries.", field="industry_sectors")
            if not sectors:
                return scenario
            if self.store is None or payload.get("industry_feature_contract") != self.contract:
                invalid("Industry employment data changed or is unavailable. Reload COLE before using this filter.", "industry_feature_unavailable", "industry_sectors")
            if any(code not in self.store.matches for code in sectors):
                invalid("Choose broad industry sectors from the available list.", field="industry_sectors")
            scenario.update(industry_sectors=sectors, industry_match_mode=mode, industry_feature_contract=self.contract)
            return scenario
        # Preserve scalar requests and their accepted/response shape for older
        # clients. New controls always submit the versioned list form above.
        if sector is None or sector == "":
            return scenario
        if not isinstance(sector, str):
            invalid("Choose one broad industry sector.")
        if self.store is None or payload.get("industry_feature_contract") != self.contract:
            invalid("Industry employment data changed or is unavailable. Reload COLE before using this filter.", "industry_feature_unavailable")
        if sector not in self.store.matches:
            invalid("Choose a broad industry sector from the available list.")
        scenario.update(industry_sector=sector, industry_feature_contract=self.contract)
        return scenario

    def evaluate_county(self, result, county, scenario):
        if scenario.get("industry_sectors"):
            verdict = self.store.evaluate_selection(county, scenario["industry_sectors"], scenario["industry_match_mode"])
            result.update(verdict)
            return verdict["industry_filters_match"]
        sector = scenario.get("industry_sector")
        if sector is None:
            return True
        verdict = self.store.evaluate(county, sector)
        result.update(verdict)
        return verdict["industry_filters_match"]
