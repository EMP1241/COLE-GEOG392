"""Real-release API regression checks; calculations stay server authoritative."""

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import re

import pytest

from cole import __version__
from cole.app import SCHEMA_VERSION, create_app
from cole.calculations import CALCULATION_VERSION, COST_COMPONENTS, TEMPERATURE_LIMITS
from cole.repository import Repository


REFERENCE = json.loads((Path(__file__).parent / "fixtures" / "step1_reference_cases.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def repository():
    return Repository()


@pytest.fixture(scope="module")
def application(repository):
    return create_app(repository, instance_id="a" * 48)


@pytest.fixture
def client(application):
    return application.test_client()


def payload(*, query_kind="discovery", state=None, costs_only=False, lookup=None):
    result = deepcopy(REFERENCE["scenario_presets"]["costs_only" if costs_only else "standard"])
    result["component_limits_monthly"] = {name: None for name in COST_COMPONENTS}
    result["temperature_limits_f"] = {name: None for name in TEMPERATURE_LIMITS}
    result.update(query_kind=query_kind, state_filter=state, lookup=lookup, comparison_county_ids=[])
    return result


def broad_query(**kwargs):
    result = payload(**kwargs)
    result["earners"][0].update(minimum="0", maximum="1000000")
    result["extras_monthly"] = "0"
    result["buffers"]["lower"]["amount"] = "0"
    result["buffers"]["upper"]["amount"] = "0"
    return result


def post_query(client, request_payload):
    response = client.post("/api/query", json=request_payload)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_live_preview_matches_submitted_count_without_returning_financial_details(client):
    for request_payload in (broad_query(state="TX"), payload()):
        if request_payload["state_filter"] is None:
            request_payload["household"] = {"adults": 2, "children": 4}
            request_payload["earners"][0].update(minimum="", maximum="34000")
            request_payload["extras_monthly"] = "0"
            request_payload["buffers"]["lower"]["amount"] = "0"
            request_payload["buffers"]["upper"]["amount"] = "0"
        preview = client.post("/api/preview", json=request_payload)
        assert preview.status_code == 200, preview.get_json()
        assert set(preview.get_json()) == {"total", "epi_total"}
        assert preview.headers["Cache-Control"] == "no-store"
        assert preview.get_json()["total"] == post_query(client, request_payload)["total"]
        assert preview.get_json()["epi_total"] == preview.get_json()["total"]
        if request_payload["household"] == {"adults": 2, "children": 4}:
            assert preview.get_json()["total"] == 0


def test_reserve_discovery_preview_and_personal_housing_share_the_goal(client, repository):
    scenario = broad_query()
    scenario["earners"][0].update(minimum="", maximum="90000")
    scenario["reserve_percent"] = "20"
    expected = {county for (county, profile), row in repository.households.items()
                if profile == "1p0c" and Decimal(row["baseline_annual"]) <= Decimal("72000")}
    result = post_query(client, scenario)
    assert {row["county_fips"] for row in result["results"]} == expected
    assert client.post("/api/preview", json=scenario).get_json()["total"] == len(expected)
    assert all(row["income_points"][0]["reserve_goal_met"] for row in result["results"])
    scenario.update(cost_mode="custom", custom_scope_acknowledged=True,
                    custom_costs_monthly={"housing": "1200"})
    personal = post_query(client, scenario)
    preview = client.post("/api/preview", json=scenario).get_json()
    assert preview["total"] == personal["total"]
    assert preview["epi_total"] == personal["epi_total"] == len(expected)
    for row in personal["results"]:
        assert row["custom_components_annual"]["housing"] == "14400"
        assert Decimal(row["combined_cost_annual"]) <= Decimal("72000")
        assert row["income_points"][0]["reserve_target_annual"] == "18000"


def test_live_preview_rejects_invalid_or_unsupported_drafts(client):
    request_payload = broad_query()
    request_payload["earners"][0]["maximum"] = "75,000"
    assert_field_error(client.post("/api/preview", json=request_payload), "earners[0].maximum")
    request_payload = broad_query()
    request_payload["household"] = {"adults": 3, "children": 2}
    assert_field_error(client.post("/api/preview", json=request_payload), "household")
    request_payload = payload(query_kind="lookup", costs_only=True, lookup={"kind": "county", "id": "01001"})
    assert_field_error(client.post("/api/preview", json=request_payload), "query_kind")


def test_custom_budget_changes_costs_but_preserves_pay_and_epi_comparison(client, repository):
    draft = broad_query(state="GA")
    draft["household"] = {"adults": 2, "children": 4}
    draft["earners"][0].update(minimum="", maximum="84000")
    draft.update(cost_mode="custom", custom_costs_monthly={"childcare": "0"},
                 custom_scope_acknowledged=True)
    preview = client.post("/api/preview", json=draft)
    assert preview.status_code == 200, preview.get_json()
    result = post_query(client, draft)
    assert preview.get_json() == {"total": result["total"], "epi_total": result["epi_total"]}
    assert result["accepted_scenario"]["normalized_annual"]["income_upper"] == "84000"
    assert result["total"] > result["epi_total"]
    county = result["results"][0]
    original = repository.households[county["county_fips"], "2p4c"]
    assert Decimal(county["custom_baseline_annual"]) == Decimal(original["baseline_annual"]) - Decimal(original["published_annual_childcare"])
    assert county["baseline_annual"] == original["baseline_annual"]
    assert county["source"]["published_annual_childcare"] == original["published_annual_childcare"]
    assert county["epi_combined_cost_annual"] == original["baseline_annual"]
    assert county["custom_components_annual"]["childcare"] == "0"


def assert_field_error(response, field):
    assert response.status_code == 400, response.get_json()
    body = response.get_json()
    assert body["error"]["code"] == "validation_failed"
    assert any(item["field"] == field for item in body["error"]["fields"])


def test_interface_defines_take_home_and_renter_contract(client):
    response = client.get("/")
    assert response.status_code == 200
    html = " ".join(response.get_data(as_text=True).split())
    assert '<html lang="en" data-theme="dark" data-palette="aggieland">' in html
    assert 'id="theme-picker-open"' in html
    assert 'id="theme-dialog"' in html
    assert 'aria-label="Color themes"' in html
    assert 'id="theme-select"' not in html
    assert 'Runs on your computer' not in html
    for palette in ('aggieland', 'reveille', 'midnight-yell', 'century-oak', 'aggie-maroon', 'light'):
        assert f'data-theme-option="{palette}"' in html
    assert '/static/theme.css' in html
    assert client.get('/static/theme.css').status_code == 200
    for text in (
        "Find a place that fits your life and your budget.",
        "Start a search",
        "How COLE works",
        "We remove EPI's estimated taxes because your take-home pay is already after tax.",
        "EPI's housing estimate is for renters.",
        "A 20% target on $90,000 leaves $18,000 for your goals and up to $72,000 for costs.",
        "An income range uses the lower amount for this target.",
        "Take-home pay minus estimated costs and your extra expenses.",
        "Your replacement costs apply to every county in a search.",
        "Costs come from EPI's 2026 estimates in 2025 dollars",
        "NOAA's 1991 to 2020 monthly county averages",
        "does not save your financial entries",
        "Back to criteria",
        "Total household take-home pay",
        "Annual or monthly after-tax totals only.",
        "Do not enter gross salary or an hourly wage.",
        "Use the take-home total expected in the destination",
        "COLE adds every earner into one household total.",
        "a state with no income tax needs no extra adjustment.",
        "Housing basis: renter estimate.",
        "Homeownership costs such as a mortgage, property tax",
        "does not subtract taxes again",
        "Widen the cost search",
        "Both start at $0",
        "The lower-cost buffer searches below your minimum",
        "They never increase your take-home pay.",
        "Limits on monthly estimates",
        "Leave a field blank for no limit; entering $0 is an active zero-dollar limit.",
        "Transportation combines auto and transit costs; it does not measure transit availability or walkability.",
        "Typical temperatures",
        "Coldest month: at least",
        "Warmest month: at most",
        "Year-round average: at least",
        "Year-round average: at most",
        "not average daily low or high temperatures",
        "1991–2020 monthly averages, not daily highs and lows.",
    ):
        assert text in html
    def opening_section(screen_id):
        prefix = html.split(f'id="{screen_id}"', 1)[0]
        return "<section" + prefix.rsplit("<section", 1)[1] + html.split(f'id="{screen_id}"', 1)[1].split(">", 1)[0] + ">"

    assert " hidden" not in opening_section("splash-screen")
    for screen_id in ("how-screen", "criteria-screen", "results-screen"):
        assert " hidden" in opening_section(screen_id)
    assert html.index('id="results-screen"') < html.index('id="map"')
    assert html.count('id="map"') == 1
    dialog_markup = re.sub(r'<!--.*?-->', '', html, flags=re.S)
    triggers = re.findall(r'<button\b[^>]*data-dialog="([^"]+)"[^>]*>', dialog_markup)
    assert len(triggers) >= 15
    for target in triggers:
        assert len(re.findall(fr'<dialog\b[^>]*id="{re.escape(target)}"', dialog_markup)) == 1
    for tag in re.findall(r'<button\b[^>]*data-dialog="[^"]+"[^>]*>', dialog_markup):
        target = re.search(r'data-dialog="([^"]+)"', tag).group(1)
        assert f'aria-controls="{target}"' in tag
        assert 'aria-haspopup="dialog"' in tag
        assert 'type="button"' in tag
    for tag in re.findall(r'<dialog\b[^>]*>', dialog_markup):
        title = re.search(r'aria-labelledby="([^"]+)"', tag).group(1)
        assert f'id="{title}"' in html
    assert html.count('aria-controls="') >= html.count('data-dialog="')
    assert '<details id="buffer-details">' not in html
    for group_id in ("component-filter", "temperature-filter", "buffer-section"):
        tag = html.split(f'id="{group_id}"', 1)[0].rsplit("<details", 1)[1] + html.split(f'id="{group_id}"', 1)[1].split(">", 1)[0]
        assert " open" not in tag
    assert '<details id="buffer-section"' in html

    script_response = client.get("/static/app.js")
    assert script_response.status_code == 200
    script = script_response.get_data(as_text=True)
    for text in (
        "Lower take-home pay",
        "Take-home pay per ${period} (or range maximum)",
        "Take-home total period",
        "Total household take-home pay / year",
        "EPI renter costs (estimated taxes removed)",
        "+ extras / year",
        "EPI does not include county or city income taxes",
        "Include costs below my minimum",
        "Include costs above my maximum",
        "Amount below your minimum",
        "Amount above your maximum",
        "Extra below minimum / year",
        "Extra above maximum / year",
        "Limits on monthly estimates",
        "EPI cost breakdown",
        "A monthly cost at or below your limit passes.",
        "Long-term temperature profile",
        "NOAA calls these long-term averages",
    ):
        assert text in script
    for absent in (
        '<option value="hourly">',
        "Per hour",
        "Hours per week",
        "Paid weeks per year",
        "scheduleFields",
        "updateSchedule",
    ):
        assert absent not in script

    styles_response = client.get("/static/styles.css")
    assert styles_response.status_code == 200
    styles = " ".join(styles_response.get_data(as_text=True).split())
    styles = re.sub(r"\s*([{}:;,()>=])\s*", r"\1", styles).replace(";}", "}")
    assert ".component-filter-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))" in styles
    assert "@media(max-width:420px){.component-filter-grid{grid-template-columns:1fr}" in styles
    assert ".component-table{display:block;min-width:0}" in styles
    assert ".component-table tbody td::before{content:attr(data-label)" in styles
    assert ".table-scroll{width:100%;overflow-x:auto" in styles
    assert ".temperature-filter-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))" in styles
    assert ".temperature-months{display:grid;grid-template-columns:repeat(auto-fit,minmax(64px,1fr))" in styles
    assert "@media(max-width:420px){.temperature-filter-grid{grid-template-columns:1fr}" in styles
    assert ".criteria-option-group>summary{display:flex" in styles
    assert ".criteria-option-group[open]>summary" in styles


def test_health_identifies_application_release_and_instance(client, repository):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.get_json() == {"app": "COLE", "app_version": __version__,
                                   "data_release_id": repository.release_id,
                                   "release_contract_sha256": repository.manifest_contract_sha256,
                                   "instance_id": "a" * 48, "ready": True}
    assert response.headers["Cache-Control"] == "no-store"


def test_metadata_has_all_supported_profiles_and_release(client, repository):
    response = client.get("/api/meta")
    assert response.status_code == 200
    meta = response.get_json()
    assert meta["manifest"]["data_release_id"] == repository.release_id
    assert meta["release_contract_sha256"] == repository.manifest_contract_sha256
    assert len(meta["states"]) == 51
    assert meta["cost_components"] == list(COST_COMPONENTS)
    assert meta["temperature_limits"] == list(TEMPERATURE_LIMITS)
    assert {row["id"] for row in meta["household_profiles"]} == {f"{a}p{c}c" for a in (1, 2) for c in range(5)}
    assert meta["supports_shutdown"] is False


def test_national_discovery_returns_every_covered_county_in_stable_order(client, repository):
    body = post_query(client, broad_query())
    assert body["total"] == 3143
    assert body["visible_result_ids"] == repository.ordered_counties
    assert [row["county_fips"] for row in body["results"]] == body["visible_result_ids"]
    assert all(row["financial_match"] and row["discovery_match"] and row["in_state_scope"] and row["data_availability"] == "available" for row in body["results"])
    assert all(row["component_limits_match"] and row["component_limit_matches"] == {} and row["exceeded_component_limits"] == [] for row in body["results"])
    assert all(row["temperature_filters_match"] and row["temperature_filter_matches"] == {} for row in body["results"])
    assert next(row for row in body["results"] if row["county_fips"] == "51678")["climate_data_availability"] == "unavailable"
    first = body["results"][0]
    source = repository.households[first["county_fips"], first["household_profile"]]
    assert first["component_costs_annual"] == {
        name: source[f"published_annual_{name}"] for name in COST_COMPONENTS
    }
    assert body["result_kind"] == "discovery"


def test_blank_lower_bound_returns_every_county_up_to_the_upper_take_home_amount(client, repository):
    costs = {
        county: Decimal(repository.households[county, "1p0c"]["published_annual_total"])
        - Decimal(repository.households[county, "1p0c"]["published_annual_taxes"])
        for county in repository.ordered_counties
    }
    ceiling = sorted(costs.values())[len(costs) // 2]
    expected = [county for county in repository.ordered_counties if costs[county] <= ceiling]
    excluded = [county for county in repository.ordered_counties if costs[county] > ceiling]
    assert expected and excluded and any(cost == ceiling for cost in costs.values())

    request_payload = broad_query()
    request_payload["earners"][0].update(minimum="", maximum=format(ceiling, "f"))
    request_payload["comparison_county_ids"] = [excluded[0]]
    request_payload["inspected_county_id"] = expected[-1]
    # A client-supplied mode is ignored; the server derives it from the earner rows.
    request_payload["income_range_mode"] = "bounded"
    body = post_query(client, request_payload)

    assert body["visible_result_ids"] == expected
    accepted = body["accepted_scenario"]
    assert accepted["income_range_mode"] == "upper_only"
    assert accepted["earners"][0]["minimum"] is None
    assert accepted["normalized_annual"]["income_lower"] is None
    assert accepted["normalized_annual"]["lower_buffer"] is None
    assert accepted["normalized_annual"]["effective_combined_min"] is None
    assert accepted["normalized_annual"]["baseline_target_min"] is None
    assert accepted["normalized_annual"]["income_upper"] == format(ceiling, "f")
    assert all(row["financial_status"] == "within_range" for row in body["results"])
    assert all(row["financial_match"] and row["discovery_match"] for row in body["results"])
    for row in body["results"]:
        assert len(row["income_points"]) == 1
        assert row["income_points"][0]["income"] == format(ceiling, "f")
        assert Decimal(row["income_points"][0]["balance"]) == ceiling - costs[row["county_fips"]]
    assert body["comparison"][0]["financial_status"] == "outside_range"
    assert body["comparison"][0]["financial_match"] is False
    assert body["inspected"] == body["results"][-1]

    accepted["income_range_mode"] = "bounded"
    accepted["normalized_annual"]["income_lower"] = "999999999"
    detail = client.post("/api/county", json={"scenario": accepted, "county_fips": expected[0]})
    assert detail.status_code == 200
    county = detail.get_json()["county"]
    assert county["financial_status"] == "within_range"
    assert county["income_points"][0]["income"] == format(ceiling, "f")


def test_monthly_component_limit_narrows_discovery_and_explains_retained_county(client, repository):
    request_payload = broad_query(state="AL")
    request_payload["component_limits_monthly"]["housing"] = "1000"
    annual_ceiling = Decimal("1000")
    expected = []
    excluded = []
    for county in repository.ordered_counties:
        row = repository.households[county, "1p0c"]
        if row["state_abbr"] != "AL":
            continue
        monthly = (Decimal(row["published_annual_housing"]) / Decimal("12")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        (expected if monthly <= annual_ceiling else excluded).append(county)
    assert expected and excluded
    request_payload["comparison_county_ids"] = [excluded[0]]
    body = post_query(client, request_payload)
    assert body["visible_result_ids"] == expected
    assert all(row["component_limits_match"] and row["discovery_match"] for row in body["results"])
    retained = body["comparison"][0]
    assert retained["financial_match"] is True
    assert retained["component_limits_match"] is False
    assert retained["component_limit_matches"] == {"housing": False}
    assert retained["exceeded_component_limits"] == ["housing"]
    assert retained["out_of_query"] is True


def test_lookup_preserves_associated_counties_while_marking_component_failures(client):
    request_payload = payload(query_kind="lookup", costs_only=True, lookup={"kind": "zcta", "id": "77807"})
    request_payload["component_limits_monthly"]["housing"] = "0"
    body = post_query(client, request_payload)
    assert set(body["visible_result_ids"]) == {"48041", "48395"}
    assert body["total"] == 2
    assert all(row["financial_match"] is None and row["discovery_match"] is None for row in body["results"])
    assert all(row["component_limits_match"] is False for row in body["results"])
    assert all(row["component_limit_matches"] == {"housing": False} for row in body["results"])
    assert all(row["in_current_query"] and not row["out_of_query"] for row in body["results"])


def test_temperature_limits_narrow_discovery_with_an_independent_tenths_oracle(client, repository):
    request_payload = broad_query(state="AL")
    request_payload["temperature_limits_f"] = {
        "coldest_month_min": "42",
        "warmest_month_max": "80",
    }
    expected = []
    excluded = []
    for county in repository.ordered_counties:
        if repository.counties[county]["state_abbr"] != "AL":
            continue
        climate = repository.climate[county]
        values = [value for key, value in climate.items() if key.endswith("_tenths_f")]
        (expected if min(values) >= 420 and max(values) <= 800 else excluded).append(county)
    assert expected and excluded
    request_payload["comparison_county_ids"] = [excluded[0]]
    body = post_query(client, request_payload)
    assert body["visible_result_ids"] == expected
    assert all(row["temperature_filters_match"] and row["discovery_match"] for row in body["results"])
    retained = body["comparison"][0]
    assert retained["temperature_filters_match"] is False
    assert retained["failed_temperature_filters"]
    assert retained["out_of_query"] is True


def test_derived_twelve_month_average_limits_use_the_displayed_one_decimal_value(client, repository):
    request_payload = broad_query(state="AL")
    averages = {}
    for county in repository.ordered_counties:
        if repository.counties[county]["state_abbr"] != "AL":
            continue
        climate = repository.climate[county]
        values = [value for key, value in climate.items() if key.endswith("_tenths_f")]
        averages[county] = (Decimal(sum(values)) / Decimal("120")).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP,
        )
    ordered = sorted(set(averages.values()))
    lower, upper = ordered[len(ordered) // 3], ordered[(2 * len(ordered)) // 3]
    request_payload["temperature_limits_f"] = {
        "twelve_month_average_min": format(lower, "f"),
        "twelve_month_average_max": format(upper, "f"),
    }
    expected = [county for county in repository.ordered_counties if county in averages and lower <= averages[county] <= upper]
    assert expected and len(expected) < len(averages)
    body = post_query(client, request_payload)
    assert body["visible_result_ids"] == expected
    for row in body["results"]:
        assert Decimal(row["climate"]["twelve_month_average_f"]) == averages[row["county_fips"]]
        assert row["temperature_filter_matches"] == {
            "twelve_month_average_min": True,
            "twelve_month_average_max": True,
        }


def test_financial_state_component_and_all_temperature_filters_combine_with_and(client, repository):
    request_payload = broad_query(state="AL")
    request_payload["earners"][0].update(minimum="30000", maximum="100000")
    request_payload["component_limits_monthly"]["housing"] = "1200"
    request_payload["temperature_limits_f"] = {
        "coldest_month_min": "42",
        "warmest_month_max": "80",
        "twelve_month_average_min": "55",
        "twelve_month_average_max": "70",
    }
    expected = []
    excluded = []
    for county in repository.ordered_counties:
        row = repository.households[(county, "1p0c")]
        if row["state_abbr"] != "AL":
            continue
        baseline = Decimal(row["published_annual_total"]) - Decimal(row["published_annual_taxes"])
        housing_monthly = (Decimal(row["published_annual_housing"]) / Decimal("12")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        )
        values = [Decimal(repository.climate[county][f"{month}_tenths_f"]) / Decimal("10") for month in (
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        )]
        average = (sum(values, Decimal("0")) / Decimal("12")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        matches = (Decimal("30000") <= baseline <= Decimal("100000")
                   and housing_monthly <= Decimal("1200")
                   and min(values) >= Decimal("42") and max(values) <= Decimal("80")
                   and Decimal("55") <= average <= Decimal("70"))
        (expected if matches else excluded).append(county)
    assert expected and excluded
    request_payload["comparison_county_ids"] = [excluded[0]]
    body = post_query(client, request_payload)
    assert body["visible_result_ids"] == expected
    assert all(row["financial_match"] and row["component_limits_match"] and row["temperature_filters_match"] for row in body["results"])
    retained = body["comparison"][0]
    assert retained["out_of_query"] is True
    assert not (retained["financial_match"] and retained["component_limits_match"] and retained["temperature_filters_match"])


def test_temperature_equality_is_inclusive_and_dc_special_source_key_is_visible(client):
    request_payload = broad_query(state="DC")
    request_payload["temperature_limits_f"] = {
        "coldest_month_min": "35.0",
        "warmest_month_max": "78.4",
        "twelve_month_average_min": "56.8",
        "twelve_month_average_max": "56.8",
    }
    body = post_query(client, request_payload)
    assert body["visible_result_ids"] == ["11001"]
    row = body["results"][0]
    assert row["temperature_filter_matches"] == {
        "coldest_month_min": True,
        "warmest_month_max": True,
        "twelve_month_average_min": True,
        "twelve_month_average_max": True,
    }
    assert row["climate"]["source"] == {
        "source_state_code": "18",
        "source_county_code": "511",
        "source_element_code": "02",
        "source_period_code": "0010",
    }


def test_lookup_retains_lexington_when_active_temperature_data_are_unavailable(client):
    request_payload = payload(
        query_kind="lookup", costs_only=True,
        lookup={"kind": "county", "id": "51678"},
    )
    request_payload["temperature_limits_f"]["coldest_month_min"] = "32"
    body = post_query(client, request_payload)
    assert body["visible_result_ids"] == ["51678"]
    row = body["results"][0]
    assert row["climate"] is None
    assert row["climate_data_availability"] == "unavailable"
    assert row["temperature_filters_match"] is False
    assert row["temperature_filter_matches"] == {"coldest_month_min": None}
    assert row["unavailable_temperature_filters"] == ["coldest_month_min"]
    assert row["in_current_query"] and not row["out_of_query"]


def test_invalid_temperature_limit_is_linked_to_its_input(client):
    request_payload = broad_query()
    request_payload["temperature_limits_f"]["warmest_month_max"] = "80.00"
    assert_field_error(client.post("/api/query", json=request_payload), "temperature_limits_f.warmest_month_max")


@pytest.mark.parametrize("kind,identity,expected", [("zcta", "77807", {"48041", "48395"}), ("place", "4850472", {"48041", "48185"})])
def test_real_lookup_preserves_every_county_relationship(client, kind, identity, expected):
    body = post_query(client, payload(query_kind="lookup", costs_only=True, lookup={"kind": kind, "id": identity}))
    assert set(body["visible_result_ids"]) == expected
    assert body["total"] == len(expected)
    assert body["result_kind"] == "lookup"
    assert all(row["income_points"] is None and row["financial_match"] is None and row["amount_above_maximum_annual"] is None and row["discovery_match"] is None for row in body["results"])
    assert all(row["financial_status"] == "estimated_cost" for row in body["results"])


def test_location_search_keeps_multiplicity_and_state_disambiguation(client):
    zcta = client.get("/api/locations?kind=zcta&q=77807").get_json()
    assert zcta["items"][0]["id"] == "77807"
    assert zcta["items"][0]["county_fips"] == ["48041", "48395"]
    place = client.get("/api/locations?kind=place&q=Navasota%20TX").get_json()
    assert place["total"] == 1
    assert place["items"][0]["id"] == "4850472"
    assert place["items"][0]["county_fips"] == ["48041", "48185"]
    assert client.get("/api/locations?kind=place&q=").get_json() == {"items": [], "total": 0, "truncated": False}
    assert client.get("/api/locations?kind=place&q=not-a-real-place-XYZ").get_json() == {"items": [], "total": 0, "truncated": False}


def test_lookup_does_not_silently_change_state_filter(client):
    request_payload = broad_query(query_kind="lookup", state="AL", lookup={"kind": "zcta", "id": "77807"})
    body = post_query(client, request_payload)
    assert body["accepted_scenario"]["state_filter"] == "AL"
    assert set(body["visible_result_ids"]) == {"48041", "48395"}
    assert all(row["financial_match"] and not row["in_state_scope"] and not row["discovery_match"] and row["in_current_query"] for row in body["results"])


def test_retained_comparison_remains_distinct_from_inspection_and_search(client):
    request_payload = broad_query(state="AL")
    request_payload.update(comparison_county_ids=["48041", "01001"], inspected_county_id="48395")
    body = post_query(client, request_payload)
    assert all(row["state_abbr"] == "AL" for row in body["results"])
    assert [row["county_fips"] for row in body["comparison"]] == ["48041", "01001"]
    assert body["inspected"]["county_fips"] == "48395"
    assert body["comparison"][0]["financial_match"] is True
    assert body["comparison"][0]["discovery_match"] is False
    assert body["comparison"][0]["out_of_query"] is True
    assert body["comparison"][1]["out_of_query"] is False
    assert body["inspected"]["out_of_query"] is True


def test_household_and_extras_recalculate_retained_comparison(client, repository):
    request_payload = broad_query(query_kind="lookup", lookup={"kind": "county", "id": "48041"})
    request_payload["comparison_county_ids"] = ["01001"]
    request_payload["inspected_county_id"] = "48041"
    first = post_query(client, request_payload)
    assert first["results"][0]["baseline_annual"] == "39138"
    assert first["comparison"][0]["baseline_annual"] == "39733"
    request_payload["household"] = {"adults": 2, "children": 4}
    request_payload["extras_monthly"] = "100.25"
    second = post_query(client, request_payload)
    for row in second["results"] + second["comparison"] + [second["inspected"]]:
        source = repository.households[row["county_fips"], "2p4c"]
        expected = Decimal(source["published_annual_total"]) - Decimal(source["published_annual_taxes"]) + Decimal("1203")
        assert Decimal(row["combined_cost_annual"]) == expected
        assert row["household_profile"] == "2p4c"
        assert row["extras_annual"] == "1203"
    assert second["comparison"][0]["combined_cost_annual"] != first["comparison"][0]["combined_cost_annual"]


def test_normalized_annual_tampering_never_changes_query_or_detail(client):
    request_payload = broad_query(query_kind="lookup", lookup={"kind": "county", "id": "48041"})
    request_payload["normalized_annual"] = {"extras": "999999", "income_lower": "999999999", "income_upper": "0"}
    body = post_query(client, request_payload)
    assert body["results"][0]["combined_cost_annual"] == "39138"
    assert body["accepted_scenario"]["normalized_annual"]["income_upper"] == "1000000"
    accepted = body["accepted_scenario"]
    accepted["normalized_annual"] = request_payload["normalized_annual"]
    response = client.post("/api/county", json={"scenario": accepted, "county_fips": "48041"})
    assert response.status_code == 200
    assert response.get_json()["county"]["combined_cost_annual"] == "39138"
    assert response.get_json()["county"]["income_points"][1]["income"] == "1000000"


@pytest.mark.parametrize("key,value", [("data_release_id", "stale"), ("calculation_version", "stale"), ("schema_version", "stale"), ("data_release_id", None), ("calculation_version", None), ("schema_version", None)])
def test_detail_rejects_old_release_calculation_or_schema(client, key, value):
    accepted = post_query(client, broad_query(query_kind="lookup", lookup={"kind": "county", "id": "48041"}))["accepted_scenario"]
    accepted[key] = value
    response = client.post("/api/county", json={"county_fips": "48041", "scenario": accepted})
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "stale_scenario"


def test_current_detail_scenario_is_reproduced(client, repository):
    accepted = post_query(client, broad_query(query_kind="lookup", lookup={"kind": "county", "id": "48041"}))["accepted_scenario"]
    assert accepted["data_release_id"] == repository.release_id
    assert accepted["calculation_version"] == CALCULATION_VERSION
    response = client.post("/api/county", json={"county_fips": "01001", "scenario": accepted})
    assert response.status_code == 200
    county = response.get_json()["county"]
    assert county["combined_cost_annual"] == "39733"
    assert county["out_of_query"] is True
    assert county["source"]["source_sheet"] == "County"


def test_zero_result_search_is_valid_and_keeps_available_comparison(client):
    request_payload = broad_query()
    request_payload["earners"][0]["maximum"] = "0"
    request_payload["comparison_county_ids"] = ["01001"]
    body = post_query(client, request_payload)
    assert body["total"] == 0 and body["results"] == [] and body["visible_result_ids"] == []
    county = body["comparison"][0]
    assert county["data_availability"] == "available" and county["baseline_annual"] == "39733"
    assert county["financial_status"] == "outside_range" and county["financial_match"] is False


def test_independent_browser_sessions_do_not_share_financial_state(application):
    one, two = application.test_client(), application.test_client()
    first = broad_query(query_kind="lookup", lookup={"kind": "county", "id": "48041"})
    accepted = post_query(one, first)["accepted_scenario"]
    second = deepcopy(first)
    second["household"] = {"adults": 2, "children": 4}
    second["extras_monthly"] = "1000"
    post_query(two, second)
    response = one.post("/api/county", json={"county_fips": "48041", "scenario": accepted})
    assert response.get_json()["county"]["combined_cost_annual"] == "39138"
    assert response.get_json()["county"]["household_profile"] == "1p0c"


@pytest.mark.parametrize("field,value", [("query_kind", None), ("query_kind", []), ("state_filter", "XX"), ("state_filter", True), ("state_filter", []), ("comparison_county_ids", ["01001", "01001"]), ("comparison_county_ids", ["01001", "48041", "48395", "48185"]), ("comparison_county_ids", [["01001"]]), ("comparison_county_ids", None), ("inspected_county_id", 1001), ("inspected_county_id", "00000")])
def test_query_wrapper_returns_field_linked_errors(client, field, value):
    request_payload = broad_query()
    request_payload[field] = value
    assert_field_error(client.post("/api/query", json=request_payload), field)


@pytest.mark.parametrize("lookup", [None, [], {}, {"kind": [], "id": "01001"}, {"kind": "county", "id": 1001}, {"kind": "county", "id": "00000"}, {"kind": "zcta", "id": "00000"}])
def test_lookup_requires_a_supported_typed_selection(client, lookup):
    assert_field_error(client.post("/api/query", json=payload(query_kind="lookup", lookup=lookup)), "lookup")


def test_discovery_rejects_costs_only_and_lookup_selection(client):
    assert_field_error(client.post("/api/query", json=payload(costs_only=True)), "query_kind")
    assert_field_error(client.post("/api/query", json=payload(lookup={"kind": "county", "id": "01001"})), "query_kind")


@pytest.mark.parametrize("document", [None, [], True, "hello"])
def test_nonobject_query_is_validation_failure(client, document):
    response = client.post("/api/query", data=json.dumps(document), content_type="application/json")
    assert_field_error(response, "scenario")


def test_invalid_financial_values_are_never_server_errors(client):
    request_payload = payload()
    request_payload["earners"][0]["minimum"] = True
    assert_field_error(client.post("/api/query", json=request_payload), "earners[0].minimum")
    request_payload["earners"][0]["minimum"] = float("nan")
    assert_field_error(client.post("/api/query", json=request_payload), "earners[0].minimum")


@pytest.mark.parametrize("base_url", ["http://evil.example", "http://127.0.0.1.evil.example", "http://0.0.0.0"])
def test_nonlocal_hosts_are_rejected(client, base_url):
    response = client.get("/api/health", base_url=base_url)
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "invalid_host"


def test_post_requires_local_origin_and_json(client):
    response = client.post("/api/query", json=payload(), headers={"Origin": "https://evil.example"})
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "invalid_origin"
    response = client.post("/api/query", data="{}", content_type="text/plain")
    assert response.status_code == 415
    response = client.post("/api/query", json=payload(), headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 415
    response = client.post("/api/query", json=payload(), base_url="http://127.0.0.1:8080", headers={"Origin": "http://127.0.0.1:8080"})
    assert response.status_code == 200


def test_malformed_and_oversized_json_returns_explicit_http_error(client):
    assert client.post("/api/query", data="{", content_type="application/json").status_code == 400
    response = client.post("/api/query", data='{"padding":"' + "a" * (1024 * 1024) + '"}', content_type="application/json")
    assert response.status_code == 413
    assert response.get_json()["error"]["code"] == "request_entity_too_large"


def test_api_errors_and_success_have_security_headers(client):
    for response in (client.get("/api/health"), client.get("/api/does-not-exist")):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]
        assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
        assert "Access-Control-Allow-Origin" not in response.headers


def test_shutdown_controller_is_explicit_and_origin_protected(client, repository):
    assert client.post("/api/shutdown", json={}).status_code == 409
    calls = []
    controlled = create_app(repository, on_shutdown=lambda: calls.append(True)).test_client()
    assert controlled.post("/api/shutdown", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert calls == []
    assert controlled.post("/api/shutdown", json={}).get_json() == {"stopping": True}
    assert calls == [True]


def expected_identity_headers(client):
    response = client.get("/api/meta")
    return {f"X-COLE-Expected-{key}": response.headers[f"X-COLE-{key}"] for key in
            ("App-Version", "Schema-Version", "Calculation-Version", "Data-Release", "Release-Contract")}


@pytest.mark.parametrize("path", ["/api/meta", "/api/geography/counties", "/api/geography/states", "/api/locations?kind=county&q=Brazos", "/api/missing"])
def test_all_api_responses_identify_loaded_application(client, repository, path):
    response = client.get(path)
    assert response.headers["X-COLE-App-Version"] == __version__
    assert response.headers["X-COLE-Schema-Version"] == SCHEMA_VERSION
    assert response.headers["X-COLE-Calculation-Version"] == CALCULATION_VERSION
    assert response.headers["X-COLE-Data-Release"] == repository.release_id
    assert response.headers["X-COLE-Release-Contract"] == repository.manifest_contract_sha256


@pytest.mark.parametrize("key", ["App-Version", "Schema-Version", "Calculation-Version", "Data-Release", "Release-Contract"])
def test_old_tab_cannot_query_or_quit_replacement_application(repository, key):
    calls = []
    client = create_app(repository, on_shutdown=lambda: calls.append(True)).test_client()
    headers = expected_identity_headers(client)
    headers[f"X-COLE-Expected-{key}"] = "older-value"
    for path in ("/api/query", "/api/county", "/api/shutdown"):
        response = client.post(path, json={}, headers=headers)
        assert response.status_code == 409
        assert response.get_json()["error"]["code"] == "incompatible_application"
    assert client.get("/api/geography/counties", headers=headers).status_code == 409
    assert calls == []


def test_current_tab_identity_can_query_and_partial_identity_is_rejected(client):
    headers = expected_identity_headers(client)
    assert client.post("/api/query", json=broad_query(state="TX"), headers=headers).status_code == 200
    headers.pop("X-COLE-Expected-Data-Release")
    assert client.post("/api/query", json=broad_query(), headers=headers).status_code == 409


def test_initial_html_and_metadata_identify_same_application(client):
    headers = expected_identity_headers(client)
    html = client.get("/").get_data(as_text=True)
    for key, value in headers.items():
        assert f'name="cole-{key.removeprefix("X-COLE-Expected-").lower()}" content="{value}"' in html
    meta = client.get("/api/meta").get_json()
    assert meta["schema_version"] == SCHEMA_VERSION and meta["calculation_version"] == CALCULATION_VERSION


@pytest.mark.parametrize("path", ["/api/query", "/api/county"])
def test_extremely_nested_json_is_bad_input_not_server_failure(client, path):
    response = client.post(path, data="[" * 5000 + "0" + "]" * 5000, content_type="application/json")
    assert_field_error(response, "request")
    assert "nested" in response.get_json()["error"]["fields"][0]["message"]
