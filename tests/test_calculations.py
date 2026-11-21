"""Independent financial examples, hostile input, and exactness regressions."""

from copy import deepcopy
from decimal import Decimal, Inexact, Rounded, localcontext
import json
from pathlib import Path

import pytest

from cole.calculations import (
    CLIMATE_MONTHS, COST_COMPONENTS, MAX_EARNERS, ValidationError, apply_custom_costs, derive_baseline,
    evaluate_component_limits, evaluate_county, evaluate_temperature,
    normalize_scenario,
)


REFERENCE = json.loads((Path(__file__).parent / "fixtures" / "step1_reference_cases.json").read_text(encoding="utf-8"))


def scenario(name="standard"):
    return deepcopy(REFERENCE["scenario_presets"][name])


def error_at(payload, field, code=None):
    with pytest.raises(ValidationError) as caught:
        normalize_scenario(payload)
    errors = caught.value.errors
    assert all(set(error) == {"field", "code", "message"} and error["message"] for error in errors)
    assert any(error["field"] == field and (code is None or error["code"] == code) for error in errors), errors
    json.dumps(errors, allow_nan=False)
    return errors


def component_costs(default="0", **overrides):
    result = {name: default for name in COST_COMPONENTS}
    result.update(overrides)
    return result


def test_custom_replacement_is_delta_from_published_total_and_zero_is_active():
    draft = scenario()
    draft.update(cost_mode="custom", custom_scope_acknowledged=True,
                 custom_costs_monthly={"childcare": "0", "food": "500.25"})
    accepted = normalize_scenario(draft)
    assert accepted["custom_costs_monthly"]["childcare"] == "0"
    assert accepted["custom_costs_monthly"]["housing"] is None
    source = component_costs(housing="12000", food="9000", transportation="8000",
                             healthcare="5000", other_necessities="4000", childcare="2000")
    custom = apply_custom_costs("40001", source, accepted)
    # The source components sum to 40000. The one-dollar source residual stays.
    assert custom["custom_baseline_annual"] == "35004"
    assert custom["custom_delta_annual"] == "-4997"
    assert custom["custom_components_annual"]["food"] == "6003"
    assert custom["custom_components_annual"]["childcare"] == "0"


def test_custom_budget_requires_a_replacement_and_explicit_geographic_assumption():
    draft = scenario()
    draft["cost_mode"] = "custom"
    error_at(draft, "custom_costs_monthly", "missing_custom_cost")
    draft["custom_costs_monthly"] = {"childcare": "0"}
    error_at(draft, "custom_scope_acknowledged", "scope_not_acknowledged")
    draft["custom_scope_acknowledged"] = True
    assert normalize_scenario(draft)["custom_costs_monthly"]["childcare"] == "0"
    draft["custom_costs_monthly"] = {"childcare": "0", "food": "75,000"}
    error_at(draft, "custom_costs_monthly.food", "invalid_custom_cost")


def climate(values=None, **overrides):
    values = values or [461, 504, 571, 639, 720, 784, 811, 805, 758, 653, 544, 484]
    result = {
        **{f"{month}_tenths_f": value for month, value in zip(CLIMATE_MONTHS, values)},
        "normal_period_start": 1991,
        "normal_period_end": 2020,
        "source_state_code": "01",
        "source_county_code": "001",
        "source_element_code": "02",
        "source_period_code": "0010",
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize("case", REFERENCE["cases"], ids=lambda case: case["case_id"])
def test_all_22_independent_step1_reference_cases(case):
    if case["kind"] == "validation":
        error_at(case["scenario"], case["expected"]["field"], case["expected"]["error_code"])
    elif case["kind"] == "baseline":
        assert derive_baseline(**case["given"]) == case["expected"]["baseline_annual"]
    else:
        result = evaluate_county(case["baseline_annual"], normalize_scenario(scenario(case["scenario_ref"])))
        assert {key: result[key] for key in case["expected"]} == case["expected"]


def test_normalized_annual_search_targets_are_exact():
    accepted = normalize_scenario(scenario())
    assert accepted["household"] == {"adults": 1, "children": 0}
    assert accepted["household_profile"] == "1p0c"
    assert accepted["income_range_mode"] == "bounded"
    assert accepted["normalized_annual"] == {
        "income_lower": "60000", "income_upper": "80000",
        "lower_buffer": "3000", "upper_buffer": "3000", "extras": "12000",
        "effective_combined_min": "57000", "effective_combined_max": "83000",
        "baseline_target_min": "45000", "baseline_target_max": "71000",
    }


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_lower_take_home_creates_upper_only_affordability(blank):
    payload = scenario()
    payload["earners"][0]["minimum"] = blank
    payload["extras_monthly"] = "0"
    payload["buffers"]["lower"]["amount"] = "0"
    payload["buffers"]["upper"]["amount"] = "0"
    accepted = normalize_scenario(payload)

    assert accepted["income_range_mode"] == "upper_only"
    assert accepted["earners"][0]["minimum"] is None
    assert accepted["normalized_annual"] == {
        "income_lower": None,
        "income_upper": "80000",
        "lower_buffer": None,
        "upper_buffer": "0",
        "extras": "0",
        "effective_combined_min": None,
        "effective_combined_max": "80000",
        "baseline_target_min": None,
        "baseline_target_max": "80000",
    }

    below = evaluate_county("1", accepted)
    exact = evaluate_county("80000", accepted)
    above = evaluate_county("80000.001", accepted)
    assert below["financial_status"] == "within_range" and below["financial_match"] is True
    assert exact["financial_status"] == "within_range" and exact["financial_match"] is True
    assert exact["income_points"] == [{"income": "80000", "balance": "0"}]
    assert above["financial_status"] == "outside_range" and above["financial_match"] is False


def test_upper_only_multiple_earners_annualize_and_sum_only_upper_values():
    payload = scenario("zero_income")
    payload["earners"] = [
        {"minimum": None, "maximum": "4000", "period": "monthly", "schedule": None},
        {
            "minimum": "",
            "maximum": "25",
            "period": "hourly",
            "schedule": {"hours_per_week": "40", "paid_weeks_per_year": "52"},
        },
    ]
    payload["buffers"]["upper"] = {"amount": "100", "period": "monthly", "schedule": None}
    accepted = normalize_scenario(payload)

    assert accepted["income_range_mode"] == "upper_only"
    assert accepted["normalized_annual"]["income_upper"] == "100000"
    assert accepted["normalized_annual"]["effective_combined_max"] == "101200"
    assert all(row["minimum"] is None for row in accepted["earners"])
    assert evaluate_county("100000", accepted)["financial_status"] == "within_range"
    included = evaluate_county("101200", accepted)
    assert included["financial_status"] == "above_buffer"
    assert included["financial_match"] is True
    assert included["income_points"] == [{"income": "100000", "balance": "-1200"}]
    assert evaluate_county("101200.001", accepted)["financial_status"] == "outside_range"


def test_mixed_blank_and_supplied_lower_take_home_is_rejected():
    payload = scenario()
    payload["earners"].append({
        "minimum": "", "maximum": "1000", "period": "annual", "schedule": None,
    })
    error_at(payload, "earners", "mixed_lower_bounds")


def test_explicit_zero_lower_take_home_remains_a_bounded_range():
    payload = scenario("zero_income")
    accepted = normalize_scenario(payload)
    assert accepted["income_range_mode"] == "bounded"
    assert accepted["earners"][0]["minimum"] == "0"
    assert accepted["normalized_annual"]["income_lower"] == "0"
    assert evaluate_county("0", accepted)["income_points"] == [{"income": "0", "balance": "0"}]


def test_upper_only_rejects_a_positive_lower_cost_buffer():
    payload = scenario()
    payload["earners"][0]["minimum"] = ""
    error_at(payload, "buffers.lower.amount", "lower_buffer_without_bound")


def test_lower_take_home_key_must_be_explicit_even_when_blank():
    payload = scenario()
    del payload["earners"][0]["minimum"]
    error_at(payload, "earners[0].minimum", "missing_lower_bound")


@pytest.mark.parametrize("adults", [1, 2])
@pytest.mark.parametrize("children", range(5))
def test_all_ten_households(adults, children):
    payload = scenario()
    payload["household"] = {"adults": adults, "children": children}
    assert normalize_scenario(payload)["household_profile"] == f"{adults}p{children}c"


@pytest.mark.parametrize("household", [None, [], {}, {"adults": True, "children": 0}, {"adults": 1.0, "children": 0}, {"adults": "1", "children": 0}, {"adults": 1, "children": False}, {"adults": 1, "children": -1}, {"adults": 1, "children": 5}])
def test_unsupported_households_do_not_coerce(household):
    payload = scenario()
    payload["household"] = household
    error_at(payload, "household", "unsupported_household")


@pytest.mark.parametrize("value", [True, False, 1, 1.5, Decimal("1.50"), float("nan"), float("inf"), [], {}, "NaN", "Infinity", "1e3", "1E+3", "-1", "-0", "+1", ".50", "1.", "1.000", "1,000", "$1000", "1_000", "1 000", "１２", "1\n2", "9" * 31])
def test_malformed_user_amounts_are_field_errors(value):
    payload = scenario()
    payload["earners"][0]["minimum"] = value
    error_at(payload, "earners[0].minimum", "invalid_amount")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_required_blanks_and_optional_blanks_differ(value):
    payload = scenario()
    payload["earners"][0]["maximum"] = value
    error_at(payload, "earners[0].maximum", "required_income")
    payload = scenario()
    payload["extras_monthly"] = value
    payload["buffers"]["lower"]["amount"] = value
    accepted = normalize_scenario(payload)
    assert accepted["extras_monthly"] == "0"
    assert accepted["normalized_annual"]["extras"] == "0"
    assert accepted["buffers"]["lower"]["amount"] == "0"


def test_component_limit_blanks_are_inactive_but_explicit_zero_is_active():
    payload = scenario()
    payload["component_limits_monthly"] = {
        "housing": "",
        "food": None,
        "childcare": "0",
    }
    accepted = normalize_scenario(payload)
    assert accepted["component_limits_monthly"] == {
        "housing": None,
        "food": None,
        "transportation": None,
        "healthcare": None,
        "other_necessities": None,
        "childcare": "0",
    }
    result = evaluate_component_limits(component_costs(), accepted)
    assert result["component_limits_match"] is True
    assert result["component_limit_matches"] == {"childcare": True}
    assert result["exceeded_component_limits"] == []


def test_component_limit_uses_the_same_cent_rounded_monthly_value_as_display():
    payload = scenario()
    payload["component_limits_monthly"] = {"transportation": "1121.83"}
    accepted = normalize_scenario(payload)
    costs = component_costs(transportation="13462")
    result = evaluate_component_limits(costs, accepted)
    assert result["component_costs_annual"]["transportation"] == "13462"
    assert result["component_limit_matches"] == {"transportation": True}

    payload["component_limits_monthly"]["transportation"] = "1121.82"
    result = evaluate_component_limits(costs, normalize_scenario(payload))
    assert result["component_limits_match"] is False
    assert result["exceeded_component_limits"] == ["transportation"]


@pytest.mark.parametrize("name", COST_COMPONENTS)
def test_each_component_limit_is_inclusive_and_multiple_limits_use_and(name):
    payload = scenario()
    payload["component_limits_monthly"] = {name: "100", "housing": "100"}
    equal = component_costs(**{name: "1200", "housing": "1200"})
    assert evaluate_component_limits(equal, normalize_scenario(payload))["component_limits_match"] is True
    over = dict(equal)
    over[name] = "1201"
    result = evaluate_component_limits(over, normalize_scenario(payload))
    assert result["component_limits_match"] is False
    assert name in result["exceeded_component_limits"]


@pytest.mark.parametrize("value", [False, 1, 1.5, [], {}, "-1", "1e3", "$100", "1.000"])
def test_malformed_component_limits_are_field_linked(value):
    payload = scenario()
    payload["component_limits_monthly"] = {"food": value}
    error_at(payload, "component_limits_monthly.food", "invalid_component_limit")


@pytest.mark.parametrize("value", [None, [], False, "housing"])
def test_component_limit_container_must_be_an_object(value):
    payload = scenario()
    payload["component_limits_monthly"] = value
    error_at(payload, "component_limits_monthly", "invalid_component_limits")


def test_unknown_component_limit_is_rejected():
    payload = scenario()
    payload["component_limits_monthly"] = {"restaurants": "100"}
    error_at(payload, "component_limits_monthly", "invalid_component_limits")


@pytest.mark.parametrize("value", [None, "", "NaN", "-1", "1e3", True])
def test_component_source_costs_are_never_fabricated(value):
    accepted = normalize_scenario(scenario())
    costs = component_costs(food=value)
    with pytest.raises(ValidationError) as caught:
        evaluate_component_limits(costs, accepted)
    assert caught.value.errors[0]["field"] == "published_annual_food"
    assert caught.value.errors[0]["code"] == "invalid_source_cost"


def test_component_rounding_is_isolated_from_the_ambient_decimal_context():
    payload = scenario()
    payload["component_limits_monthly"] = {"transportation": "1121.83"}
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        result = evaluate_component_limits(
            component_costs(transportation="13462"),
            normalize_scenario(payload),
        )
        assert context.prec == 3
    assert result["component_limits_match"] is True


def test_temperature_limit_blanks_are_inactive_and_negative_values_are_valid():
    payload = scenario()
    payload["temperature_limits_f"] = {
        "coldest_month_min": "-5.0",
        "warmest_month_max": "",
    }
    accepted = normalize_scenario(payload)
    assert accepted["temperature_limits_f"] == {
        "coldest_month_min": "-5",
        "warmest_month_max": None,
        "twelve_month_average_min": None,
        "twelve_month_average_max": None,
    }
    result = evaluate_temperature(climate(values=[-50] * 12), accepted)
    assert result["temperature_filter_matches"] == {"coldest_month_min": True}
    assert result["temperature_filters_match"] is True


def test_temperature_extrema_are_inclusive_and_twelve_month_context_is_derived():
    payload = scenario()
    payload["temperature_limits_f"] = {
        "coldest_month_min": "46.1",
        "warmest_month_max": "81.1",
        "twelve_month_average_min": "64.5",
        "twelve_month_average_max": "64.5",
    }
    result = evaluate_temperature(climate(), normalize_scenario(payload))
    assert result["temperature_filters_match"] is True
    assert result["temperature_filter_matches"] == {
        "coldest_month_min": True,
        "warmest_month_max": True,
        "twelve_month_average_min": True,
        "twelve_month_average_max": True,
    }
    assert result["climate"]["coldest_month_mean_f"] == "46.1"
    assert result["climate"]["coldest_months"] == ["january"]
    assert result["climate"]["warmest_month_mean_f"] == "81.1"
    assert result["climate"]["warmest_months"] == ["july"]
    assert result["climate"]["twelve_month_average_f"] == "64.5"
    assert result["climate"]["monthly_mean_f"]["december"] == "48.4"

    payload["temperature_limits_f"]["warmest_month_max"] = "81.0"
    missed = evaluate_temperature(climate(), normalize_scenario(payload))
    assert missed["temperature_filters_match"] is False
    assert missed["failed_temperature_filters"] == ["warmest_month_max"]

    payload["temperature_limits_f"]["warmest_month_max"] = "81.1"
    payload["temperature_limits_f"]["twelve_month_average_min"] = None
    payload["temperature_limits_f"]["twelve_month_average_max"] = "64.4"
    missed_average = evaluate_temperature(climate(), normalize_scenario(payload))
    assert missed_average["temperature_filters_match"] is False
    assert missed_average["failed_temperature_filters"] == ["twelve_month_average_max"]


def test_unavailable_climate_passes_without_a_limit_and_blocks_active_filter_only():
    no_limit = evaluate_temperature(None, normalize_scenario(scenario()))
    assert no_limit == {
        "climate_data_availability": "unavailable",
        "climate": None,
        "temperature_filter_matches": {},
        "temperature_filters_match": True,
        "failed_temperature_filters": [],
        "unavailable_temperature_filters": [],
    }
    payload = scenario()
    payload["temperature_limits_f"] = {"coldest_month_min": "32"}
    active = evaluate_temperature(None, normalize_scenario(payload))
    assert active["temperature_filters_match"] is False
    assert active["temperature_filter_matches"] == {"coldest_month_min": None}
    assert active["unavailable_temperature_filters"] == ["coldest_month_min"]


@pytest.mark.parametrize("value", [False, 1, 1.5, [], {}, "NaN", "1e2", "+32", ".5", "32.00", "151", "-101"])
def test_malformed_temperature_limits_are_field_linked(value):
    payload = scenario()
    payload["temperature_limits_f"] = {"coldest_month_min": value}
    error_at(payload, "temperature_limits_f.coldest_month_min", "invalid_temperature_limit")


@pytest.mark.parametrize("value", [None, [], False, "cold"])
def test_temperature_limit_container_must_be_an_object(value):
    payload = scenario()
    payload["temperature_limits_f"] = value
    error_at(payload, "temperature_limits_f", "invalid_temperature_limits")


@pytest.mark.parametrize("minimum,maximum", [
    ("coldest_month_min", "warmest_month_max"),
    ("twelve_month_average_min", "twelve_month_average_max"),
    ("coldest_month_min", "twelve_month_average_max"),
    ("twelve_month_average_min", "warmest_month_max"),
])
def test_impossible_temperature_limits_are_rejected_as_a_group(minimum, maximum):
    payload = scenario()
    payload["temperature_limits_f"] = {minimum: "80", maximum: "70"}
    error_at(payload, "temperature_limits_f", "impossible_temperature_limits")


@pytest.mark.parametrize("defect", [
    {"january_tenths_f": None},
    {"january_tenths_f": True},
    {"january_tenths_f": -501},
    {"normal_period_start": 1981},
])
def test_malformed_source_climate_is_never_fabricated(defect):
    source = climate(**defect)
    with pytest.raises(ValidationError) as caught:
        evaluate_temperature(source, normalize_scenario(scenario()))
    assert caught.value.errors[0]["code"] == "invalid_source_climate"


def test_canonicalization_and_purity():
    payload = scenario()
    payload["earners"][0]["minimum"] = " 060000.00 "
    payload["extras_monthly"] = "001000.00"
    original = deepcopy(payload)
    accepted = normalize_scenario(payload)
    accepted_original = deepcopy(accepted)
    result = evaluate_county("58000.000", accepted)
    assert result["combined_cost_annual"] == "70000"
    assert accepted["earners"][0]["minimum"] == "60000"
    assert accepted["extras_monthly"] == "1000"
    assert payload == original and accepted == accepted_original
    json.dumps(accepted, allow_nan=False)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("payload", [None, [], "", True, 10])
def test_non_object_payload(payload):
    error_at(payload, "scenario", "invalid_scenario")


@pytest.mark.parametrize("context", [None, [], {}, True, "", "no_income", "Financial"])
def test_context_must_be_explicit(context):
    payload = scenario()
    payload["income_context"] = context
    error_at(payload, "income_context", "incompatible_context")


@pytest.mark.parametrize("field,value", [("earners", []), ("earners", [{}]), ("earners", False), ("buffers", {}), ("buffers", False)])
def test_costs_only_rejects_leaked_financial_values(field, value):
    payload = scenario("costs_only")
    payload[field] = value
    error_at(payload, field, "incompatible_context")


@pytest.mark.parametrize("field", ["earners", "buffers"])
def test_costs_only_requires_explicit_nulls(field):
    payload = scenario("costs_only")
    del payload[field]
    error_at(payload, field, "incompatible_context")


def test_costs_only_normalization_has_no_income_outcomes():
    accepted = normalize_scenario(scenario("costs_only"))
    assert accepted["earners"] is None and accepted["buffers"] is None
    assert accepted["income_range_mode"] is None
    annual = accepted["normalized_annual"]
    assert annual["extras"] == "12000"
    assert all(value is None for key, value in annual.items() if key != "extras")
    result = evaluate_county("1.01234567890123456789", accepted)
    assert result["combined_cost_annual"] == "12001.01234567890123456789"
    assert result["income_points"] is None and result["financial_match"] is None


@pytest.mark.parametrize("earners", [None, [], {}, True, [None], [1], [True]])
def test_malformed_earner_rows(earners):
    payload = scenario()
    payload["earners"] = earners
    error_at(payload, "earners[0]" if isinstance(earners, list) and earners else "earners")


def test_partial_second_earner_is_not_ignored():
    payload = scenario()
    payload["earners"].append({"minimum": "10", "maximum": "", "period": "annual", "schedule": None})
    error_at(payload, "earners[1].maximum", "required_income")


def test_earner_count_is_not_coupled_to_adults():
    payload = scenario("zero_income")
    payload["earners"] *= 3
    assert len(normalize_scenario(payload)["earners"]) == 3
    payload["earners"] *= MAX_EARNERS
    error_at(payload, "earners", "input_limit")


@pytest.mark.parametrize("period", [None, [], {}, 1, "weekly", "yearly", ""])
def test_invalid_periods_return_validation_errors(period):
    payload = scenario()
    payload["earners"][0]["period"] = period
    error_at(payload, "earners[0].period", "invalid_period")


def test_nonhourly_schedule_cannot_silently_survive_period_change():
    payload = scenario()
    payload["earners"][0]["schedule"] = {"hours_per_week": "40", "paid_weeks_per_year": "52"}
    error_at(payload, "earners[0].schedule", "invalid_schedule")


@pytest.mark.parametrize("schedule", [None, [], True, {}, {"hours_per_week": "40"}])
def test_missing_or_malformed_hourly_schedule(schedule):
    payload = scenario()
    payload["earners"][0].update(period="hourly", schedule=schedule)
    with pytest.raises(ValidationError) as caught:
        normalize_scenario(payload)
    assert any(error["field"].startswith("earners[0].schedule") for error in caught.value.errors)


@pytest.mark.parametrize("key,value", [("hours_per_week", "0"), ("hours_per_week", "168.01"), ("hours_per_week", "1.001"), ("hours_per_week", True), ("paid_weeks_per_year", "0"), ("paid_weeks_per_year", "53.01"), ("paid_weeks_per_year", "-1"), ("paid_weeks_per_year", "1e1")])
def test_schedule_limits_and_numeric_rules(key, value):
    payload = scenario("mixed_periods")
    payload["earners"][0]["schedule"][key] = value
    error_at(payload, f"earners[0].schedule.{key}", "invalid_schedule")


def test_hourly_earner_requires_schedule_even_at_zero_income():
    payload = scenario("zero_income")
    payload["earners"][0]["period"] = "hourly"
    error_at(payload, "earners[0].schedule", "required_schedule")


def test_zero_hourly_buffer_requires_no_schedule():
    payload = scenario("zero_income")
    payload["buffers"]["lower"]["period"] = "hourly"
    accepted = normalize_scenario(payload)
    assert accepted["buffers"]["lower"] == {"amount": "0", "period": "hourly", "schedule": None}
    assert accepted["normalized_annual"]["lower_buffer"] == "0"


@pytest.mark.parametrize("buffers", [None, [], False, {}, {"lower": None}, {"lower": {"amount": "-1", "period": "annual"}, "upper": {}}])
def test_malformed_buffer_definitions(buffers):
    payload = scenario()
    payload["buffers"] = buffers
    with pytest.raises(ValidationError) as caught:
        normalize_scenario(payload)
    assert any(error["field"].startswith("buffers") for error in caught.value.errors)


def test_fractional_schedules_and_buffer_periods_are_independent():
    payload = scenario()
    payload["earners"] = [{"minimum": "10.25", "maximum": "12.75", "period": "hourly", "schedule": {"hours_per_week": "37.50", "paid_weeks_per_year": "52.50"}}]
    payload["extras_monthly"] = "10.01"
    payload["buffers"] = {
        "lower": {"amount": "0.25", "period": "hourly", "schedule": {"hours_per_week": "10.50", "paid_weeks_per_year": "20.50"}},
        "upper": {"amount": "8.50", "period": "monthly", "schedule": None},
    }
    accepted = normalize_scenario(payload)
    assert accepted["normalized_annual"] == {
        "income_lower": "20179.6875", "income_upper": "25101.5625",
        "lower_buffer": "53.8125", "upper_buffer": "102", "extras": "120.12",
        "effective_combined_min": "20125.875", "effective_combined_max": "25203.5625",
        "baseline_target_min": "20005.755", "baseline_target_max": "25083.4425",
    }
    result = evaluate_county("24981.442501", accepted)
    assert result["financial_status"] == "above_buffer"
    assert result["amount_above_maximum_annual"] == "0.000001"


def test_schedule_extremes_are_valid_and_not_rounded():
    payload = scenario("zero_income")
    payload["earners"] = [{"minimum": "1", "maximum": "1", "period": "hourly", "schedule": {"hours_per_week": "168", "paid_weeks_per_year": "53"}}]
    assert normalize_scenario(payload)["normalized_annual"]["income_lower"] == "8904"
    payload["earners"][0]["schedule"] = {"hours_per_week": "0.01", "paid_weeks_per_year": "0.01"}
    assert normalize_scenario(payload)["normalized_annual"]["income_lower"] == "0.0001"


def test_negative_derived_search_bounds_remain_visible():
    payload = scenario("zero_income")
    payload["extras_monthly"] = "1"
    payload["buffers"]["lower"]["amount"] = "100"
    accepted = normalize_scenario(payload)
    assert accepted["normalized_annual"]["effective_combined_min"] == "-100"
    assert accepted["normalized_annual"]["baseline_target_min"] == "-112"
    assert accepted["normalized_annual"]["baseline_target_max"] == "-12"


def test_ambient_decimal_context_cannot_change_financial_results():
    payload = scenario()
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        accepted = normalize_scenario(payload)
        result = evaluate_county("68000.000000000000000000000000000001", accepted)
        baseline = derive_baseline("70000.000000000000000000000000000001", "12000")
        assert context.prec == 3
    assert result["amount_above_maximum_annual"] == "0.000000000000000000000000000001"
    assert result["financial_status"] == "above_buffer"
    assert baseline == "58000.000000000000000000000000000001"


@pytest.mark.parametrize("traps_enabled", [False, True])
def test_ambient_decimal_context_cannot_change_temperature_matches(traps_enabled):
    payload = scenario()
    payload["temperature_limits_f"] = {"warmest_month_max": "46"}
    accepted = normalize_scenario(payload)
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = traps_enabled
        context.traps[Rounded] = traps_enabled
        original_flags = context.flags.copy()
        result = evaluate_temperature(climate(values=[461] * 12), accepted)
        assert context.prec == 2
        assert context.traps[Inexact] is traps_enabled
        assert context.traps[Rounded] is traps_enabled
        assert context.flags == original_flags
    assert set(result["climate"]["monthly_mean_f"].values()) == {"46.1"}
    assert result["climate"]["coldest_month_mean_f"] == "46.1"
    assert result["climate"]["warmest_month_mean_f"] == "46.1"
    assert result["climate"]["twelve_month_average_f"] == "46.1"
    assert result["temperature_filters_match"] is False
    assert result["failed_temperature_filters"] == ["warmest_month_max"]


def test_thirty_digit_user_input_and_many_earners_keep_low_order_digits():
    payload = scenario("zero_income")
    row = {"minimum": "999999999999999999999999999999.99", "maximum": "999999999999999999999999999999.99", "period": "annual", "schedule": None}
    payload["earners"] = [row] * 1000
    accepted = normalize_scenario(payload)
    assert accepted["normalized_annual"]["income_lower"] == "999999999999999999999999999999990"
    assert evaluate_county("999999999999999999999999999999989.999", accepted)["income_points"] == [{"income": "999999999999999999999999999999990", "balance": "0.001"}]


@pytest.mark.parametrize("baseline", [None, "", " ", True, False, 0, 100.0, float("nan"), Decimal("NaN"), Decimal("Infinity"), Decimal("1E+999999"), "NaN", "Infinity", "-1", "1e5", "0" * 129])
def test_missing_invalid_or_negative_source_cost_is_never_zero(baseline):
    with pytest.raises(ValidationError) as caught:
        evaluate_county(baseline, normalize_scenario(scenario()))
    assert caught.value.errors[0]["field"] == "baseline_annual"
    assert caught.value.errors[0]["code"] == "invalid_source_cost"


@pytest.mark.parametrize("total,taxes,field", [(None, "1", "published_annual_total"), ("1", None, "published_annual_taxes"), ("1", "2", "baseline_annual"), ("-1", "0", "published_annual_total"), ("1", "-2", "published_annual_taxes")])
def test_baseline_derivation_rejects_missing_and_surprising_values(total, taxes, field):
    with pytest.raises(ValidationError) as caught:
        derive_baseline(total, taxes)
    assert caught.value.errors[0]["field"] == field


def test_exact_decimal_source_and_zero_display_do_not_emit_exponents():
    assert derive_baseline(Decimal("70000.125"), Decimal("12000.12")) == "58000.005"
    result = evaluate_county(Decimal("-0.00"), normalize_scenario(scenario("zero_income")))
    assert result["baseline_annual"] == "0"
    assert result["income_points"] == [{"income": "0", "balance": "0"}]


def test_longest_source_fraction_is_classified_without_silent_rounding():
    payload = scenario("zero_income")
    payload["buffers"]["upper"]["amount"] = "1"
    accepted = normalize_scenario(payload)
    tiny = "0." + "0" * 125 + "1"
    result = evaluate_county(tiny, accepted)
    assert result["financial_status"] == "above_buffer"
    assert result["amount_above_maximum_annual"] == tiny
    assert result["income_points"] == [{"income": "0", "balance": "-" + tiny}]


def test_oversized_source_coefficient_and_derived_baseline_fail_explicitly():
    with pytest.raises(ValidationError):
        derive_baseline(Decimal("9" * 129), "0")
    # Each input fits, but their exact difference exceeds the documented
    # 128-character derived-amount limit; this is a data error, not rounding.
    with pytest.raises(ValidationError) as caught:
        derive_baseline("1" + "0" * 126, "0." + "0" * 125 + "1")
    assert caught.value.errors[0]["field"] == "baseline_annual"


def test_multiple_errors_are_returned_in_one_validation_pass():
    payload = scenario()
    payload["household"]["adults"] = 0
    payload["extras_monthly"] = "-1"
    payload["earners"][0]["maximum"] = ""
    errors = error_at(payload, "household")
    assert {error["field"] for error in errors} >= {"household", "extras_monthly", "earners[0].maximum"}
