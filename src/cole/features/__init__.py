"""Optional features connect through this small, explicit application boundary."""

from .hazards import HazardFeature
from .industry_employment import IndustryEmploymentFeature
from .poverty_education import PovertyEducationFeature
from .age_demographics import AgeFeature
from .greenspace import GreenspaceFeature


class FeatureRegistry:
    def __init__(self, app, repository):
        self.hazards = HazardFeature(app, repository)
        self.industry_employment = IndustryEmploymentFeature(app, repository)
        self.poverty_education = PovertyEducationFeature(app, repository)
        self.age_demographics = AgeFeature(app, repository)
        self.greenspace = GreenspaceFeature(app, repository)
        self._features = (self.hazards, self.industry_employment, self.poverty_education, self.age_demographics, self.greenspace)

    def normalize_request(self, payload, scenario):
        for feature in self._features:
            scenario = feature.normalize_request(payload, scenario)
        return scenario

    def evaluate_county(self, result, county, scenario):
        verdicts = [feature.evaluate_county(result, county, scenario) for feature in self._features]
        return all(verdicts)


def register_features(app, repository):
    registry = FeatureRegistry(app, repository)
    app.extensions["cole_features"] = registry
    return registry
