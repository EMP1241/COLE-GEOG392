"""Stateless local HTTP interface to COLE's calculation and prepared data."""

# ===========================================================================
# PLAIN-ENGLISH GUIDE TO THIS FILE
# ===========================================================================
#
# WHAT IT DOES
# This is the switchboard. The browser page sends requests here; this file
# figures out what was asked, hands the real work to calculations.py and
# repository.py, and sends back the answer as JSON.
#
# Calculation rules live in calculations.py and prepared-data validation lives
# in repository.py. This file owns the HTTP boundary: it routes requests,
# validates request shape and identity, coordinates queries and details, and
# applies the local-service protections described below.
#
# KEY TERMS
#   FLASK     - a small Python library for answering web requests.
#   ROUTE     - a URL the app responds to, e.g. "/api/query". The lines that
#               start with @app.get(...) or @app.post(...) declare them.
#   DECORATOR - a line starting with @ that attaches behaviour to the function
#               below it. @app.post("/api/query") means "run this function when
#               a POST request arrives at /api/query."
#   GET/POST  - GET asks for something (safe, repeatable). POST submits
#               something. COLE uses POST for searches because the scenario is
#               too large and too private to sit in a URL.
#   JSON      - the text format used to exchange data with the browser.
#   STATELESS - the word in the docstring above, and the central design choice:
#               no per-user scenario or financial session is stored by the
#               server. The prepared repository and release identity stay in
#               memory for the run, while each request carries its own scenario.
#               Close the browser and the entered financial data is gone.
#
# THE ROUTES, AT A GLANCE
#   GET  /                   the web page itself
#   GET  /api/health         "are you alive?" - used by the launcher at startup
#   GET  /api/meta           dropdown options, version info, the manifest
#   GET  /api/geography/...  the county and state map shapes
#   GET  /api/locations      search for a county, place, or ZIP area
#   POST /api/query          THE MAIN ONE: run a search, get matching counties
#   POST /api/preview        count draft discovery matches without returning counties
#   POST /api/county         details for a single county
#   POST /api/shutdown       quit the application
# ===========================================================================

from __future__ import annotations

# `secrets` generates unguessable random values (used for IDs below).
import secrets

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from . import __version__
from .calculations import (
    CALCULATION_VERSION,
    COST_COMPONENTS,
    TEMPERATURE_LIMITS,
    ValidationError,
    apply_custom_costs,
    evaluate_component_limits,
    evaluate_county,
    evaluate_temperature,
    normalize_scenario,
)
from .repository import Repository, manifest_contract_sha256
from .features import register_features
from .notices import notice_path

# The shape of the request/response format. Bumped when the browser and server
# must agree on something new. Like the other version numbers, a mismatch
# forces a reload rather than letting two incompatible halves talk.
SCHEMA_VERSION = "1.6"


def invalid(field, message, code="invalid_request"):
    # Shorthand for rejecting one field with a single error.
    # Raising (rather than returning) lets any validation give up from deep
    # inside a helper; the error handler registered below catches it and turns
    # it into a proper 400 response.
    raise ValidationError([{"field": field, "code": code, "message": message}])


def create_app(repository=None, *, instance_id=None, on_shutdown=None, hosted_origin=None):
    # =======================================================================
    # THE APPLICATION FACTORY
    # =======================================================================
    # Rather than building the app when this file loads, everything lives
    # inside this function. That is a standard Flask pattern called an
    # "application factory," and it matters for two reasons:
    #   1. Tests can create a throwaway app with fake data.
    #   2. The routes below can close over `repository` and `identity` - they
    #      can see those variables without them being global.
    #
    # `on_shutdown` is a function the launcher passes in so the Quit button can
    # stop the server. When it is None (as in tests) the shutdown route politely
    # refuses, which is why a test server cannot be killed by a stray request.
    # =======================================================================
    from .hosting import HostedPolicy
    hosted = HostedPolicy(hosted_origin) if hosted_origin is not None else None
    if hosted and on_shutdown is not None:
        raise ValueError("Hosted COLE cannot have a shutdown controller.")
    repository = repository or Repository()
    app = Flask(__name__)
    features = register_features(app, repository)
    if hosted:
        if any(feature.error for feature in features._features):
            raise RuntimeError("Hosted startup requires all five verified feature datasets.")
        hosted.install(app)
    # MAX_CONTENT_LENGTH caps request size at 1 MB - generous for a scenario,
    # small enough that nothing can flood memory.
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    # Preserve the order fields were written in, so responses stay readable and
    # predictable for anyone inspecting them.
    app.json.sort_keys = False
    # A random ID for this particular run of the program. Lets the launcher
    # confirm it is talking to the server it just started, not a leftover one.
    instance_id = instance_id or secrets.token_hex(24)
    # Use the fingerprint the repository already computed if it has one;
    # otherwise compute it now. (`getattr` with a default avoids crashing on a
    # stand-in repository object in tests.)
    release_contract_sha256 = getattr(repository, "manifest_contract_sha256", None)
    if release_contract_sha256 is None:
        release_contract_sha256 = manifest_contract_sha256(repository.manifest)
    # THE IDENTITY BUNDLE - five values that together say exactly which app and
    # which data are running. These get attached to every API response, baked
    # into the web page, and compared on every API request. If any one of them
    # changes, the session is stale and COLE stops rather than mixing results
    # from two different versions.
    identity = {
        "app-version": __version__,
        "schema-version": SCHEMA_VERSION,
        "calculation-version": CALCULATION_VERSION,
        "data-release": repository.release_id,
        "release-contract": release_contract_sha256,
    }

    @app.before_request
    def protect_local_service():
        # Security gate. Runs before EVERY request, whatever the route.
        # The original comment explains the threat model:
        # Host and origin checks prevent DNS rebinding and remote sites submitting
        # local scenarios or quitting the app. No financial state is persisted.
        #
        # Expanded for newcomers: COLE runs a real web server on your computer.
        # Anything your browser can reach, a malicious website open in another
        # tab could potentially try to reach too. "DNS rebinding" is an attack
        # where a hostile site tricks the browser into treating its own domain
        # as your local machine, letting it talk to local apps. These checks
        # reduce that local-browser attack surface; they are not a claim that a
        # non-browser client cannot construct arbitrary HTTP headers.

        # CHECK 1 - the request must be addressed to this machine.
        # `request.host` might be "127.0.0.1:8080", so split off the port.
        if hosted:
            rejected = hosted.check_request()
            if rejected is not None:
                return rejected
        elif request.host.split(":", 1)[0].lower() not in ("127.0.0.1", "localhost"):
            return (
                jsonify(
                    error={
                        "code": "invalid_host",
                        "message": "COLE accepts loopback requests only.",
                        "fields": [],
                    }
                ),
                403,
            )

        # CHECK 2 - anything that CHANGES something (POST) must come from
        # COLE's own page. GET/HEAD/OPTIONS only read, so they skip this.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # "Origin" is supplied by the browser. Page JavaScript cannot freely
            # choose that protected header, so a browser request that includes
            # it must identify COLE's own address. Non-browser clients can forge
            # headers, which is why this is one layer rather than authentication.
            origin = request.headers.get("Origin")
            if (hosted and origin != hosted.origin) or (not hosted and origin and origin != request.host_url.rstrip("/")):
                return (
                    jsonify(
                        error={
                            "code": "invalid_origin",
                            "message": "Submit from this COLE window.",
                            "fields": [],
                        }
                    ),
                    403,
                )
            # Belt and braces: reject anything the browser labels cross-site,
            # and require a JSON body. Requiring JSON matters because a plain
            # HTML form can be submitted cross-site without permission, whereas
            # a JSON request cannot.
            if request.headers.get("Sec-Fetch-Site") == "cross-site" or not request.is_json:
                return (
                    jsonify(
                        error={
                            "code": "invalid_content_type",
                            "message": "Send a same-origin JSON request.",
                            "fields": [],
                        }
                    ),
                    415,
                )

        # CHECK 3 - version agreement (the identity bundle from above).
        # The page sends what it EXPECTS in X-COLE-Expected-* headers. If it
        # sends any of them, all five must match exactly.
        if request.path.startswith("/api/"):
            expected = {key: request.headers.get(f"X-COLE-Expected-{key}") for key in identity}
            # `any(... is not None ...)` means "the page bothered to state its
            # expectations." A page that states none is not forced to match,
            # which keeps simple tools and tests workable.
            # 409 = Conflict.
            if any(value is not None for value in expected.values()) and expected != identity:
                return (
                    jsonify(
                        error={
                            "code": "incompatible_application",
                            "message": "The application or data release changed. Reload COLE before continuing.",
                            "fields": [],
                        }
                    ),
                    409,
                )

    @app.after_request
    def response_headers(response):
        # Attach security and identity headers to every outgoing response.
        # Stop the browser guessing a file's type from its contents - a classic
        # way to trick a browser into running data as code.
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Never tell another site where a user came from.
        response.headers["Referrer-Policy"] = "no-referrer"
        # THE CONTENT SECURITY POLICY - a contract telling the browser what the
        # page is allowed to load. Reading the important parts:
        #   default-src 'self'   only load things from COLE itself
        #   script-src 'self'    no third-party JavaScript, ever
        #   connect-src 'self'   the page cannot phone home to any other server
        #   img-src 'self' data: images from COLE, or embedded inline
        #   object-src 'none'    no Flash/Java-style plugins
        #   frame-ancestors 'none'  no other site may embed COLE in a frame
        # Same-origin processing is local in desktop mode and remote in hosted mode.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
        )
        # Stamp the identity bundle on API responses so the page can detect a
        # version change immediately.
        if request.path.startswith("/api/"):
            for key, value in identity.items():
                response.headers[f"X-COLE-{key}"] = value
        # "no-store" forbids caching. Applied to every API route EXCEPT
        # /api/geography/, because map shapes are large, unchanging, and carry
        # no personal data - so caching them is a pure win. Everything else may
        # contain the user's financial scenario and must never be written to
        # the browser's disk cache.
        if request.path.startswith("/api/") and not request.path.startswith("/api/geography/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # --- Error handlers ----------------------------------------------------
    # These catch exceptions from anywhere in the app and convert them into a
    # consistent JSON shape: {"error": {"code", "message", "fields"}}. Because
    # every failure looks the same, the browser has exactly one path for
    # displaying problems.

    @app.errorhandler(ValidationError)
    def validation_error(exc):
        # The user's input was wrong. 400 = Bad Request.
        # `exc.errors` is the field-by-field list built in calculations.py, so
        # the page can highlight each offending input box.
        return (
            jsonify(
                error={
                    "code": "validation_failed",
                    "message": "Correct the indicated fields and submit again.",
                    "fields": exc.errors,
                }
            ),
            400,
        )

    @app.errorhandler(HTTPException)
    def http_error(exc):
        # Standard web errors, e.g. 404 Not Found, 405 Method Not Allowed.
        # Turns "Not Found" into the code "not_found" for consistency.
        return (
            jsonify(
                error={
                    "code": exc.name.lower().replace(" ", "_"),
                    "message": exc.description,
                    "fields": [],
                }
            ),
            exc.code,
        )

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        # A bug. 500 = Internal Server Error.
        # Log the full technical detail locally...
        if hosted:
            app.logger.error("Unhandled COLE request failure (%s)", type(exc).__name__)
        else:
            app.logger.exception("Unhandled COLE request failure")
        # ...but tell the user something calm and useful. Note the reassurance
        # that previous results survive: because the server is stateless, a
        # failed request cannot corrupt what is already on screen.
        return (
            jsonify(
                error={
                    "code": "internal_error",
                    "message": ("COLE could not complete this request. Your previous results remain available. Try again shortly." if hosted else "COLE could not complete this request. Your previous results remain available. See the local application log."),
                    "fields": [],
                }
            ),
            500,
        )

    def read_json():
        # Parse the request body, guarding against deeply nested input.
        try:
            return request.get_json()
        except RecursionError:
            # A JSON document nested thousands of levels deep can exhaust
            # Python's call stack while parsing. Catch that and return a normal
            # validation message instead of a crash.
            invalid("request", "The request contains too many nested objects or arrays.")

    def normalize_request(payload):
        # Validate the scenario plus the query wrapper around it.
        # calculations.py handles the household and money. This adds the parts
        # that need to know about geography, which calculations.py deliberately
        # knows nothing about.
        scenario = normalize_scenario(payload)

        # Which kind of search?
        #   discovery - sweep every county, return the matches
        #   lookup    - show the counties belonging to one named place
        kind = payload.get("query_kind")
        if kind not in ("discovery", "lookup"):
            invalid("query_kind", "Choose discovery or lookup.")

        # Optional "only show me counties in this state".
        state = payload.get("state_filter")
        if state is not None and (
            not isinstance(state, str) or state not in repository.state_abbreviations
        ):
            invalid("state_filter", "Choose a supported state or all states.")

        lookup = payload.get("lookup")
        if kind == "discovery":
            # Discovery only makes sense with income: without it there is no
            # criterion to sweep against, and every county would "match."
            if scenario["income_context"] != "financial" or lookup is not None:
                invalid(
                    "query_kind",
                    "Discovery requires complete financial input and no lookup selection.",
                )
        else:
            # Lookup requires a real, known location. Both halves are checked
            # against the loaded data, so a made-up ID cannot get through.
            if (
                not isinstance(lookup, dict)
                or not isinstance(lookup.get("kind"), str)
                or lookup["kind"] not in repository.locations
            ):
                invalid("lookup", "Select a county, place, or ZCTA from the location results.")
            if (
                not isinstance(lookup.get("id"), str)
                or lookup["id"] not in repository.locations[lookup["kind"]]
            ):
                invalid(
                    "lookup",
                    "Choose a supported location from the search results.",
                    "unknown_location",
                )
            # Rebuild the lookup from scratch with ONLY the two fields we want.
            # This discards anything extra the browser sent, so no unexpected
            # field can ride along into the rest of the request.
            lookup = {"kind": lookup["kind"], "id": lookup["id"]}

        # Record what was accepted, and stamp the versions. Echoing the accepted
        # scenario back lets the page show "here is exactly what was searched,"
        # rather than the user having to trust that their form was read right.
        scenario.update(
            query_kind=kind,
            state_filter=state,
            lookup=lookup,
            schema_version=SCHEMA_VERSION,
            data_release_id=repository.release_id,
        )
        if lookup:
            selected = repository.locations[lookup["kind"]][lookup["id"]]
            scenario["lookup_label"] = f"{selected['name']}, {selected['state_abbr']}"
        return features.normalize_request(payload, scenario)

    def validate_county(county, field):
        # Require a county ID we actually have data for.
        if not isinstance(county, str) or county not in repository.counties:
            invalid(field, "Choose a covered county identifier.", "unknown_county")
        return county

    def county_result(county, scenario, visible_ids=None):
        # Build the complete answer for ONE county. The workhorse function.
        # Called once per county in a discovery sweep - potentially thousands of
        # times per request - so it stays lean and does no I/O.

        # Look up the row matching this county AND this household shape.
        row = repository.households[county, scenario["household_profile"]]

        # Three independent verdicts from calculations.py, merged into one
        # result dictionary: money, per-category costs, and climate.
        source_components = {name: row[f"published_annual_{name}"] for name in COST_COMPONENTS}
        epi_result = evaluate_county(row["baseline_annual"], scenario)
        custom = apply_custom_costs(row["baseline_annual"], source_components, scenario)
        result = (
            evaluate_county(custom["custom_baseline_annual"], scenario)
            if scenario["cost_mode"] == "custom"
            else epi_result
        )
        result.update(custom)
        result.update(
            baseline_annual=epi_result["baseline_annual"],
            epi_combined_cost_annual=epi_result["combined_cost_annual"],
            epi_financial_status=epi_result["financial_status"],
            epi_financial_match=epi_result["financial_match"],
        )
        result.update(evaluate_component_limits(source_components, scenario))
        # `.get(county)` returns None when a county has no climate data;
        # evaluate_temperature handles that case explicitly.
        result.update(evaluate_temperature(repository.climate.get(county), scenario))
        feature_match = features.evaluate_county(result, county, scenario)

        # The state filter. `in (None, ...)` means "no filter set, or it matches."
        in_state = scenario["state_filter"] in (None, row["state_abbr"])

        # A county is a discovery match only if EVERY criterion passes. When
        # there is no income context this is None rather than False - an honest
        # "not applicable" instead of a misleading "failed."
        discovery_match = (
            result["financial_match"]
            and result["component_limits_match"]
            and result["temperature_filters_match"]
            and in_state
            and feature_match
            if scenario["income_context"] == "financial"
            else None
        )
        epi_discovery_match = (
            result["epi_financial_match"]
            and result["component_limits_match"]
            and result["temperature_filters_match"]
            and in_state
            and feature_match
            if scenario["income_context"] == "financial"
            else None
        )

        # Is this county part of the current result set?
        if visible_ids is None:
            # First pass: work it out from the criteria.
            in_query = (
                bool(discovery_match)
                if scenario["query_kind"] == "discovery"
                else county
                in repository.locations[scenario["lookup"]["kind"]][scenario["lookup"]["id"]][
                    "county_fips"
                ]
            )
        else:
            # Second pass (comparisons and inspections): the result set is
            # already known, so just check membership. This is what lets a user
            # inspect or compare a county OUTSIDE their results - it gets
            # scored against their criteria and flagged as out-of-query, rather
            # than being hidden.
            in_query = county in visible_ids

        # Attach the county's identity.
        result.update(
            {
                key: row[key]
                for key in ("county_fips", "county_name", "state_abbr", "household_profile")
            }
        )
        result.update(
            in_state_scope=in_state,
            discovery_match=discovery_match,
            epi_discovery_match=epi_discovery_match,
            in_current_query=in_query,
            out_of_query=not in_query,
            data_availability="available",
            data_release_id=repository.release_id,
        )
        # Every original published figure and provenance field, so the detail
        # panel can show a full audit trail back to the source spreadsheet.
        result["source"] = {
            key: value
            for key, value in row.items()
            if key.startswith(("published_annual_", "source_"))
        }
        return result

    # =======================================================================
    # THE ROUTES
    # =======================================================================

    def discovery_results(scenario):
        for county in repository.ordered_counties:
            if scenario["state_filter"] in (None, repository.counties[county]["state_abbr"]):
                yield county_result(county, scenario)

    @app.get("/")
    def index():
        # Serve the web page, with the identity bundle baked into it.
        # Those values become <meta> tags the JavaScript reads and then sends
        # back as X-COLE-Expected-* headers on every API call.
        return render_template("index.html", application_identity=identity, hosted=bool(hosted))

    @app.get("/license")
    def license_notice():
        return send_file(notice_path("LICENSE"), mimetype="text/plain", conditional=False)

    @app.get("/third-party-notices")
    def third_party_notices():
        return send_file(notice_path("THIRD_PARTY_NOTICES.md"), mimetype="text/plain", conditional=False)

    @app.get("/api/health")
    def health():
        # Startup heartbeat. The launcher polls this before opening a browser.
        return jsonify(
            app="COLE",
            app_version=__version__,
            data_release_id=repository.release_id,
            release_contract_sha256=release_contract_sha256,
            instance_id=instance_id,
            ready=True,
        )

    @app.get("/api/meta")
    def metadata():
        # Everything the page needs to build its form, fetched once at startup.
        return jsonify(
            app_version=__version__,
            schema_version=SCHEMA_VERSION,
            calculation_version=CALCULATION_VERSION,
            manifest=repository.manifest,
            release_contract_sha256=release_contract_sha256,
            # Tells the page whether to show a Quit button - there is
            # no point offering it on a test server that cannot quit.
            supports_shutdown=on_shutdown is not None,
            states=[
                {"abbr": abbr, "name": repository.state_names.get(abbr, abbr)}
                for abbr in repository.state_abbreviations
            ],
            cost_components=list(COST_COMPONENTS),
            temperature_limits=list(TEMPERATURE_LIMITS),
            # The ten household profiles, generated rather than typed
            # out, so they can never drift from the data.
            household_profiles=[
                {"id": f"{a}p{c}c", "adults": a, "children": c} for a in (1, 2) for c in range(5)
            ],
        )

    @app.get("/api/geography/<kind>")
    def geography(kind):
        # Serve the map shape files.
        # `<kind>` in the route is a placeholder captured as an argument.
        # The whitelist below is essential security: without it, a request for
        # "../../../etc/passwd" would be built into a path and served.
        if kind not in ("counties", "states"):
            invalid("geography", "Unknown geography.")
        # conditional=True enables "has this changed?" caching, so the browser
        # re-downloads these large files only when they actually differ.
        return send_file(
            repository.directory / f"{kind}.geojson",
            mimetype="application/geo+json",
            conditional=True,
        )

    @app.get("/api/locations")
    def locations():
        # Type-ahead search for a county, place, or ZIP area.
        kind, query = request.args.get("kind"), request.args.get("q", "").strip()
        if kind not in repository.locations:
            invalid("kind", "Choose county, place, or zcta.")
        if len(query) > 120:
            invalid("q", "Use a shorter location search.")
        if not query:
            # An empty box returns nothing rather than everything - otherwise
            # the first keystroke would try to render every location.
            return jsonify(items=[], total=0, truncated=False)
        return jsonify(repository.search_locations(kind, query))

    @app.post("/api/query")
    def query():
        # THE MAIN SEARCH. Everything else supports this route.
        payload = read_json()
        scenario = normalize_request(payload)

        # Up to three counties pinned for side-by-side comparison.
        compare_ids = payload.get("comparison_county_ids", [])
        if not isinstance(compare_ids, list) or len(compare_ids) > 3:
            invalid("comparison_county_ids", "Compare at most three distinct counties.")
        for county in compare_ids:
            validate_county(county, "comparison_county_ids")
        # A set drops duplicates, so a shorter set means the list repeated one.
        if len(set(compare_ids)) != len(compare_ids):
            invalid("comparison_county_ids", "Each comparison county must be distinct.")

        # Optionally, one county the user has open in the detail panel.
        inspected_id = payload.get("inspected_county_id")
        if inspected_id is not None:
            validate_county(inspected_id, "inspected_county_id")

        if scenario["query_kind"] == "discovery":
            # Narrow by state FIRST, so the expensive per-county evaluation runs
            # on as few counties as possible.
            # The `:=` is the "walrus operator": it assigns and tests in one go.
            # Read as: compute the result, keep it, and include it only if it
            # matched. Without this, every county would have to be evaluated
            # twice - once to test, once to keep.
            results = []
            epi_total = 0
            for result in discovery_results(scenario):
                if result["discovery_match"]:
                    results.append(result)
                epi_total += bool(result["epi_discovery_match"])
        else:
            # Lookup: the counties belonging to the chosen place. Iterating over
            # ordered_counties rather than the raw set keeps display order
            # consistent with discovery results.
            candidates = set(
                repository.locations[scenario["lookup"]["kind"]][scenario["lookup"]["id"]][
                    "county_fips"
                ]
            )
            results = [
                county_result(county, scenario)
                for county in repository.ordered_counties
                if county in candidates
            ]
            epi_total = None

        # Now that the result set is known, comparisons and the inspected county
        # are re-evaluated against it (that is the `visible_ids` second pass
        # described in county_result), so each can be correctly flagged as
        # inside or outside the current results.
        visible_ids = {result["county_fips"] for result in results}
        return jsonify(
            query_id=secrets.token_hex(12),
            accepted_scenario=scenario,
            result_kind=scenario["query_kind"],
            results=results,
            comparison=[county_result(county, scenario, visible_ids) for county in compare_ids],
            inspected=county_result(inspected_id, scenario, visible_ids) if inspected_id else None,
            visible_result_ids=[result["county_fips"] for result in results],
            total=len(results),
            epi_total=epi_total,
            manifest=repository.manifest,
        )

    @app.post("/api/preview")
    def preview():
        # -Team
        # I want the live number to follow the same rules as Find counties.
        # Reuse our normal validation and county verdict so a preview cannot
        # promise matches that disappear on submit. Send back only the count;
        # we do not need to echo someone's draft pay or county rows here.
        # -Brandon
        scenario = normalize_request(read_json())
        if scenario["query_kind"] != "discovery":
            invalid("query_kind", "Live match preview is available for Find counties only.")
        total = epi_total = 0
        for result in discovery_results(scenario):
            total += bool(result["discovery_match"])
            epi_total += bool(result["epi_discovery_match"])
        return jsonify(total=total, epi_total=epi_total)

    @app.post("/api/county")
    def county_detail():
        # Details for one county, scored against the user's submitted criteria.
        payload = read_json()
        if not isinstance(payload, dict):
            invalid("request", "Send a request object.")

        # STALENESS CHECK. The page sends back the scenario it is holding. If it
        # was produced by a different data release or a different calculator,
        # refuse rather than silently mixing old and new numbers in the same
        # view. 409 = Conflict.
        supplied = payload.get("scenario")
        if isinstance(supplied, dict) and any(
            supplied.get(key) != value
            for key, value in (
                ("data_release_id", repository.release_id),
                ("calculation_version", CALCULATION_VERSION),
                ("schema_version", SCHEMA_VERSION),
            )
        ):
            return (
                jsonify(
                    error={
                        "code": "stale_scenario",
                        "message": "The application or data release changed. Reload COLE before inspecting counties.",
                        "fields": [],
                    }
                ),
                409,
            )

        # Note this re-validates from scratch rather than trusting the numbers
        # the page sent back - the rule from calculations.py that request
        # handlers must always call normalize_scenario.
        scenario = normalize_request(supplied)
        county = validate_county(payload.get("county_fips"), "county_fips")
        return jsonify(county=county_result(county, scenario))

    @app.post("/api/shutdown")
    def shutdown():
        # The Quit button. Stops the local server.
        # Only works if the launcher supplied a way to stop. A test server has
        # no shutdown controller, so it declines instead.
        if on_shutdown is None:
            return (
                jsonify(
                    error={
                        "code": "shutdown_unavailable",
                        "message": "Close this browser tab to leave COLE." if hosted else "This test server has no shutdown controller.",
                        "fields": [],
                    }
                ),
                409,
            )
        # This sets a flag the launcher's main loop is watching; it does not
        # kill the process outright, so in-flight requests finish cleanly.
        on_shutdown()
        return jsonify(stopping=True)

    return app
