from decimal import Decimal

import pytest

from cole.calculations import ValidationError, evaluate_county, normalize_scenario


def draft(percent="20", lower=None, upper="90000"):
    return {
        "household": {"adults": 1, "children": 0},
        "income_context": "financial",
        "earners": [{"minimum": lower, "maximum": upper, "period": "annual", "schedule": None}],
        "extras_monthly": "0",
        "buffers": {
            side: {"amount": "0", "period": "annual", "schedule": None}
            for side in ("lower", "upper")
        },
        "reserve_percent": percent,
    }


def test_twenty_percent_preserves_pay_and_changes_the_cost_ceiling():
    accepted = normalize_scenario(draft())
    assert accepted["normalized_annual"]["income_upper"] == "90000"
    assert accepted["reserve_goal"] == {
        "percent": "20",
        "income_basis": "upper",
        "income_annual": "90000",
        "target_annual": "18000",
        "cost_ceiling_annual": "72000",
    }
    edge = evaluate_county("72000", accepted)
    assert edge["financial_match"] is True
    assert edge["income_points"][0]["balance"] == "18000"
    assert edge["income_points"][0]["balance_after_reserve_annual"] == "0"
    assert evaluate_county("72000.0001", accepted)["financial_match"] is False
    screenshot = evaluate_county("89594", accepted)
    assert screenshot["income_points"][0]["balance"] == "406"
    assert screenshot["income_points"][0]["balance_after_reserve_annual"] == "-17594"
    assert screenshot["income_points"][0]["reserve_goal_met"] is False


def test_bounded_goal_uses_lower_income_and_does_not_exclude_cheaper_counties():
    accepted = normalize_scenario(draft(lower="60000"))
    assert accepted["reserve_goal"]["income_basis"] == "lower"
    assert accepted["normalized_annual"]["effective_combined_min"] is None
    assert accepted["normalized_annual"]["effective_combined_max"] == "48000"
    assert evaluate_county("10000", accepted)["financial_match"] is True
    rejected = evaluate_county("50000", accepted)
    assert rejected["financial_match"] is False
    assert [point["reserve_goal_met"] for point in rejected["income_points"]] == [False, True]


def test_extras_and_fractional_percentage_are_counted_exactly_once():
    payload = draft("12.5", upper="12000")
    payload["extras_monthly"] = "100"
    payload["reserve_goal"] = {"cost_ceiling_annual": "9999999"}
    payload["normalized_annual"] = {"income_upper": "9999999"}
    accepted = normalize_scenario(payload)
    assert accepted["reserve_goal"]["cost_ceiling_annual"] == "10500"
    assert evaluate_county("9300", accepted)["financial_match"] is True
    assert evaluate_county("9300.01", accepted)["financial_match"] is False


@pytest.mark.parametrize("percent", ["0", "100"])
def test_zero_income_and_percentage_endpoints(percent):
    accepted = normalize_scenario(draft(percent, upper="0"))
    assert evaluate_county("0", accepted)["financial_match"] is True
    assert evaluate_county("0.01", accepted)["financial_match"] is False
    if percent == "100":
        accepted = normalize_scenario(draft(percent))
        assert evaluate_county("0", accepted)["financial_match"] is True
        assert evaluate_county("1", accepted)["financial_match"] is False


@pytest.mark.parametrize("percent", [True, 20, "-1", "100.01", "NaN", "20%", "1e1", [], "0.001"])
def test_invalid_percentages_are_field_errors(percent):
    with pytest.raises(ValidationError) as error:
        normalize_scenario(draft(percent))
    assert any(item["field"] == "reserve_percent" for item in error.value.errors)


def test_buffers_cannot_relax_a_reserve_goal():
    payload = draft()
    payload["buffers"]["upper"]["amount"] = "1"
    with pytest.raises(ValidationError) as error:
        normalize_scenario(payload)
    assert any(item["code"] == "reserve_buffer_conflict" for item in error.value.errors)


def test_goal_is_rejected_without_income():
    payload = draft()
    payload.update(income_context="costs_only", earners=None, buffers=None)
    with pytest.raises(ValidationError):
        normalize_scenario(payload)
    payload["reserve_percent"] = None
    accepted = normalize_scenario(payload)
    assert accepted["reserve_goal"] is None
    assert evaluate_county("20000", accepted)["income_points"] is None


@pytest.mark.parametrize("value", [None, "", "  "])
def test_inactive_goal_preserves_legacy_interval_matching(value):
    accepted = normalize_scenario(draft(value, lower="60000"))
    assert accepted["reserve_goal"] is None
    assert evaluate_county("50000", accepted)["financial_match"] is False
    assert evaluate_county("75000", accepted)["financial_match"] is True
    assert set(evaluate_county("75000", accepted)["income_points"][0]) == {"income", "balance"}
