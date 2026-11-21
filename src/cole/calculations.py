"""Pure COLE scenario validation and exact annual financial calculations.

Money and schedule input use ordinary decimal strings (at most two decimal
places), never binary floats. Technical resource limits are 30 integer digits
per input, 1,000 earner rows, and 128 characters per source/derived amount.
These are processing defenses, not household or financial-selection rules. The
isolated 256-digit context traps unintended rounding. Two documented display
comparisons deliberately round half up: monthly component values to cents and
the derived 12-month temperature average to one decimal place.

The query/location wrapper and geographic scope belong to the API layer.
"""

# ===========================================================================
# PLAIN-ENGLISH GUIDE TO THIS FILE
# ===========================================================================
#
# WHAT IT DOES
# This file is the calculator. It contains no web code, no file reading, and
# no knowledge of maps or counties. You hand it (a) numbers the user typed and
# (b) numbers from our prepared data files, and it reports how the estimate
# compares with the submitted take-home, component, and temperature criteria.
#
# The word "pure" in the docstring above is a programming term meaning these
# functions only look at what you pass in and only return a result. They don't
# read files, don't touch the network, don't remember anything between calls.
# That is what makes this file easy to test and safe to reason about.
#
# THE TWO KINDS OF NUMBERS
#   1. INPUT  - typed by the user in the browser (take-home pay, limits).
#               Never trusted. Every one gets validated before use.
#   2. SOURCE - read from our own prepared data release (EPI cost estimates,
#               NOAA temperatures). Trusted more, but still checked, because a
#               corrupt data file should raise a loud error, not quietly
#               produce a wrong answer.
#
# WHY "Decimal" AND NOT NORMAL NUMBERS
# Computers store ordinary decimal numbers (called "floats") in binary, and
# binary cannot represent 0.1 exactly - the same way base 10 cannot write 1/3
# exactly. So in most languages 0.1 + 0.2 produces 0.30000000000000004. That is
# fine for physics; it is not fine for money, where a tiny representation error
# could put an estimate on the wrong side of a visible submitted threshold.
#
# Python's `Decimal` type stores numbers the way a human writes them, so the
# arithmetic is exact. Every money value in COLE travels as a Decimal or as a
# plain text string like "42350.75" - never as a float.
#
# WHAT "TRAPS" MEANS (see _EXACT_CONTEXT below)
# A "context" is a settings bundle for Decimal math: how many digits, how to
# round. We build one with 256 digits of precision and then switch on two
# alarms ("traps"): Inexact and Rounded. With those armed, if a calculation
# would ever silently lose precision, Python raises an error instead of
# returning a slightly-wrong number. We would rather crash loudly than
# misclassify a county against the criteria someone submitted.
#
# ROAD MAP - THE MAIN ENTRY POINTS
# Five functions are meant to be called from outside this file. Everything
# whose name starts with an underscore (_like_this) is an internal helper, a
# convention meaning "other files should not call this."
#
#   normalize_scenario(payload)
#       Takes an untrusted request payload from the browser or local API.
#       Validates every field,
#       converts monthly/hourly amounts to yearly, and returns one clean,
#       canonical dictionary. Raises ValidationError listing every problem at
#       once, so the user fixes all fields in one pass instead of one per try.
#
#   evaluate_county(baseline_annual, normalized)
#       The money verdict for one county: does the estimated cost land inside
#       the household's income range (optionally widened by buffers)?
#
#   evaluate_component_limits(source_components, normalized)
#       The per-category verdict: housing under $1,500/mo, child care under
#       $900/mo, and so on.
#
#   evaluate_temperature(source_climate, normalized)
#       The climate verdict: coldest month warm enough, warmest month cool
#       enough, yearly average in range.
#
#   derive_baseline(total, taxes)
#       A data-preparation helper. EPI publishes a household total that
#       includes modeled taxes; users enter pay that is already after tax, so
#       we subtract EPI's tax figure to get a comparable "baseline."
# ===========================================================================

from __future__ import annotations

# `re` = regular expressions: a mini-language for describing text patterns,
# used here to check that a typed value really looks like a number.
import re

# The Decimal machinery described in the guide above.
from decimal import Context, Decimal, Inexact, ROUND_HALF_UP, Rounded, localcontext

# `Any` is a type hint meaning "could be anything" - used for values that
# arrive from the browser before we have checked what they are.
from typing import Any

# ---------------------------------------------------------------------------
# CONSTANTS
# Names in ALL CAPS are a Python convention meaning "this value never changes."
# ---------------------------------------------------------------------------

# Stamped into every result. If the math in this file changes, this number
# changes too, and any browser page still holding older results is told to
# reload rather than mixing results from two different calculators.
CALCULATION_VERSION = "0.5.0"

# Defensive ceilings. These are NOT statements about real households - nobody
# has 1,000 jobs. They stop a malicious or broken request from making the app
# grind on absurd input.
MAX_EARNERS = 1000
MAX_INTEGER_DIGITS = 30
MAX_SOURCE_CHARACTERS = 128

# The six spending categories EPI publishes for each county. This tuple is the
# server-side source of truth shared by calculation, API, and data-loader code.
# The browser keeps a matching display configuration and verifies its keys
# against server metadata at startup. (A "tuple" is a list that cannot be
# modified.)
COST_COMPONENTS = (
    "housing",
    "food",
    "transportation",
    "healthcare",
    "other_necessities",
    "childcare",
)

# Month names, in calendar order. Order matters: climate.csv stores twelve
# values per county and we rely on reading them back in the same sequence.
CLIMATE_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)

# The four optional temperature filters a user can set. "coldest_month_min"
# reads as: the coldest month's average must be AT LEAST this value.
TEMPERATURE_LIMITS = (
    "coldest_month_min",
    "warmest_month_max",
    "twelve_month_average_min",
    "twelve_month_average_max",
)

# --- Text patterns used for validation -------------------------------------
# Reading a regular expression, piece by piece:
#   [0-9]{1,30}     one to thirty digits
#   (?:\.[0-9]{1,2})?   optionally a dot followed by one or two digits
#   \Z              end of the text - nothing may follow
# So "1200", "1200.5" and "1200.55" pass; "1,200", "$1200", "1200.555" and
# "1e5" all fail. Note there is no minus sign here: user money must be
# non-negative.
_INPUT_NUMBER = re.compile(r"[0-9]{1,30}(?:\.[0-9]{1,2})?\Z")

# Temperatures may be negative and carry at most one decimal: "-12.5", "98".
_TEMPERATURE_NUMBER = re.compile(r"-?[0-9]{1,3}(?:\.[0-9])?\Z")

# Numbers coming from our own prepared files. Looser than user input (any
# number of decimals) because we preserve whatever precision the source
# published rather than reshaping it.
_SOURCE_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?\Z")

# How an income or buffer amount can be expressed.
_PERIODS = ("annual", "monthly", "hourly")

# The exact-math settings bundle described in the guide at the top.
# prec=256 gives 256 significant digits - far more than any real budget needs,
# which is the point: there is room to be exact.
_EXACT_CONTEXT = Context(prec=256)
# Alarm 1: raise an error if a result cannot be represented exactly.
_EXACT_CONTEXT.traps[Inexact] = True
# Alarm 2: raise an error if digits would be dropped.
_EXACT_CONTEXT.traps[Rounded] = True

# Frequently used Decimal values, created once and reused. Note they are built
# from STRINGS: Decimal("0.01") is exactly one cent, while Decimal(0.01) would
# inherit the float's tiny binary error - exactly the bug this file avoids.
_ZERO = Decimal("0")
_TWELVE = Decimal("12")
_CENT = Decimal("0.01")
_TENTH = Decimal("0.1")


class ValidationError(ValueError):
    """A collection of JSON-safe, field-linked validation failures."""

    # A "class" is a custom type. This one is a custom error.
    #
    # Python signals problems by "raising" an exception, which stops the
    # current work and jumps to whoever is prepared to handle it. Built-in
    # errors carry only a message. This one also carries a structured list of
    # which FIELDS failed and why, e.g.:
    #
    #   [{"field": "earners[0].maximum",
    #     "code": "invalid_amount",
    #     "message": "Use a nonnegative decimal string..."}]
    #
    # app.py turns that list straight into JSON, and the web page uses the
    # "field" values to highlight the exact inputs that need fixing. "JSON-safe"
    # means every value is plain text, so it survives being sent to the browser.
    #
    # It extends ValueError so that code which only knows about ordinary Python
    # errors still catches it correctly.

    def __init__(self, errors: list[dict[str, str]]) -> None:
        # __init__ runs when the error object is created. `self` is the object
        # being built.
        #
        # dict(error) makes a COPY of each error entry. Without the copy, the
        # caller's original list and this error would point at the same data,
        # and a later edit to one would silently alter the other.
        self.errors = [dict(error) for error in errors]
        # Join every message into one human-readable sentence for logs and for
        # anyone printing the exception directly.
        super().__init__("; ".join(error["message"] for error in self.errors))


def _error(errors: list, field: str, code: str, message: str) -> None:
    # Record one validation failure without stopping.
    #
    # This is the heart of COLE's "collect everything, then report" strategy.
    # Instead of raising immediately on the first bad field, validators append to
    # a shared `errors` list and keep checking. The caller raises once at the
    # end. Result: a user with three mistakes sees all three, rather than fixing
    # one, resubmitting, and discovering the next.
    #
    # - field:   which input this is about, e.g. "buffers.lower.amount"
    # - code:    a stable machine-readable tag the web page can branch on
    # - message: the sentence shown to the person
    errors.append({"field": field, "code": code, "message": message})


def _canonical(value: Decimal) -> str:
    # Turn a Decimal into one predictable text form.
    #
    # "Canonical" means: of the many ways to write the same number, always pick
    # the same one. Decimal("1200.00"), Decimal("1200.0") and Decimal("1200") are
    # equal in value but print differently. This collapses them all to "1200".
    #
    # Why text rather than a number? Because these values get sent to the browser
    # as JSON, and JSON numbers are floats - the very thing we are avoiding. By
    # shipping "1200.50" as a string, the exact value survives the trip, and
    # money.js on the other side does its own exact arithmetic on it.
    #
    # The comment below is the original author's and explains a real trap:
    # Decimal.normalize() can round under the caller's ambient context.
    if value == 0:
        # Guard clause: without this, the stripping logic below would turn
        # "0.00" into "" (empty text). Also normalizes "-0" to "0".
        return "0"
    # format(value, "f") writes the number in plain positional notation,
    # never scientific notation - so a large figure stays "1000000", not "1E+6".
    result = format(value, "f")
    # Trim trailing zeros after the decimal point, then a bare trailing dot:
    #   "1200.500" -> "1200.5"      "1200.000" -> "1200." -> "1200"
    # The `if "." in result` check is essential: without it, "1200" would have
    # its meaningful trailing zeros stripped down to "12".
    return result.rstrip("0").rstrip(".") if "." in result else result


def _input_amount(
    value: Any,
    field: str,
    errors: list,
    *,
    optional: bool = False,
    required_code: str = "required_income",
    invalid_code: str = "invalid_amount",
) -> Decimal | None:
    # Validate one money amount typed by a user, and convert it to a Decimal.
    #
    # Returns the Decimal on success, or None on failure - and on failure it has
    # already appended an explanation to `errors`, so the caller just checks for
    # None and moves on.
    #
    # About the bare `*` in the parameter list: everything after it must be
    # passed BY NAME at the call site. So callers must write
    # `_input_amount(x, "f", errors, optional=True)`, never `..., True)`. That
    # stops a stray positional argument from flipping a behavioural switch by
    # accident, and it makes call sites self-documenting.
    #
    # `Decimal | None` in the signature is a type hint: "returns a Decimal, or
    # nothing." Hints are documentation for humans and tools; Python does not
    # enforce them at run time.
    # --- Case 1: the field is empty --------------------------------------
    # Covers a missing value (None) and a box the user left blank or filled
    # with spaces. `.strip()` removes surrounding whitespace.
    if value is None or (isinstance(value, str) and not value.strip()):
        if optional:
            # Blank is allowed here and means "none of this," i.e. zero.
            # Extra monthly expenses and buffers work this way.
            return _ZERO
        # Blank is not allowed: record the problem and report failure.
        _error(errors, field, required_code, "Enter a value for this field.")
        return None

    # --- Case 2: the field is filled but malformed ------------------------
    # Three separate rejections, in cheapest-check-first order:
    #   not a string      -> the browser sent a number or object, not text
    #   longer than 64    -> nothing legitimate is that long; stop early so the
    #                        pattern matcher never sees a huge input
    #   pattern mismatch  -> "$1,200", "-50", "1e5", "twelve" all land here
    if not isinstance(value, str) or len(value) > 64 or not _INPUT_NUMBER.fullmatch(value.strip()):
        _error(
            errors,
            field,
            invalid_code,
            "Enter 0 or a positive amount with at most 30 digits before the decimal point "
            "and two after it. Use digits without commas or a dollar sign (enter 75000 for $75,000).",
        )
        return None

    # --- Case 3: valid -----------------------------------------------------
    # Build the Decimal from the cleaned TEXT. This is the exactness guarantee
    # from the top of the file, applied at the boundary where user data enters.
    return Decimal(value.strip())


def _schedule(value: Any, field: str, errors: list) -> dict | None:
    # Validate the work schedule attached to an hourly amount.
    #
    # If someone reports pay as dollars-per-hour, that alone cannot be turned
    # into a yearly figure - we need hours per week and paid weeks per year.
    # COLE requires both explicitly rather than assuming the usual 40 x 52,
    # because that assumption is wrong for part-time, seasonal, and school-year
    # work, and a wrong assumption here quietly distorts every county result.
    #
    # Returns a dictionary with both values as canonical text, or None if
    # anything was missing or out of range.
    if value is None:
        _error(errors, field, "required_schedule", "Enter an explicit hourly schedule.")
        return None
    if not isinstance(value, dict):
        # A "dict" (dictionary) is a set of named values - here we expect
        # something shaped like {"hours_per_week": "40", "paid_weeks_per_year": "52"}.
        _error(errors, field, "invalid_schedule", "The hourly schedule must be an object.")
        return None

    result = {}
    # Loop over the two required entries and their upper bounds.
    #   168 = hours in a week (24 x 7). A person cannot work more than all of it.
    #    53 = the maximum number of weeks a calendar year can touch.
    # Again: sanity ceilings, not statements about reasonable work.
    for name, maximum in (("hours_per_week", "168"), ("paid_weeks_per_year", "53")):
        # Build the dotted path, e.g. "earners[0].schedule.hours_per_week", so
        # the web page can point at the exact box that needs attention.
        item_field = f"{field}.{name}"
        amount = _input_amount(
            value.get(name),
            item_field,
            errors,
            required_code="required_schedule",
            invalid_code="invalid_schedule",
        )
        if amount is not None:
            # Python allows chained comparison: this reads as
            # "zero < amount AND amount <= maximum".
            # Zero is excluded deliberately - zero hours would annualize an
            # hourly wage to nothing, which is almost certainly a typo rather
            # than an intent.
            if not _ZERO < amount <= Decimal(maximum):
                _error(
                    errors,
                    item_field,
                    "invalid_schedule",
                    f"Enter more than zero and no more than {maximum}.",
                )
            else:
                result[name] = _canonical(amount)
    # Only a complete schedule is usable. If either half failed, return None;
    # the reason is already sitting in `errors`.
    return result if len(result) == 2 else None


def _period_and_schedule(
    row: dict,
    field: str,
    errors: list,
    *,
    disabled: bool = False,
) -> tuple[str | None, dict | None]:
    # Check how an amount is expressed, and demand a schedule only if needed.
    #
    # Returns two values at once (a "tuple"): the period and the schedule.
    # Callers unpack them in one line: `period, schedule = _period_and_schedule(...)`.
    #
    # `disabled=True` is used for a buffer whose amount is zero: a zero buffer
    # has no effect, so we accept whatever period is set and skip the schedule
    # requirement rather than nagging about a field that cannot matter.
    period = row.get("period")
    if not isinstance(period, str) or period not in _PERIODS:
        _error(errors, f"{field}.period", "invalid_period", "Choose annual, monthly, or hourly.")
        return None, None
    if disabled:
        return period, None
    if period == "hourly":
        # Hourly REQUIRES a schedule - see _schedule above for why.
        return period, _schedule(row.get("schedule"), f"{field}.schedule", errors)
    # Annual and monthly amounts must NOT carry a schedule. This is stricter
    # than necessary on purpose: a leftover schedule attached to an annual
    # figure usually means the browser form is in an inconsistent state, and a
    # loud complaint is better than silently ignoring data the user entered.
    if row.get("schedule") is not None:
        _error(
            errors,
            f"{field}.schedule",
            "invalid_schedule",
            "Annual and monthly amounts must have a null schedule.",
        )
    return period, None


def _annualize(amount: Decimal, period: str, schedule: dict | None) -> Decimal:
    # Convert any amount to a yearly figure.
    #
    # COLE compares everything on a yearly basis internally, because yearly
    # numbers avoid repeated division and therefore avoid repeated rounding.
    # Monthly figures are produced only at the moment of display.
    if amount == 0:
        # Shortcut. Zero is zero in every period, and returning early avoids
        # needing a schedule for a zero hourly amount.
        return _ZERO
    if period == "monthly":
        return amount * _TWELVE
    if period == "hourly":
        # dollars/hour x hours/week x paid weeks/year = dollars/year.
        # The schedule values were stored as canonical text, so rebuild them as
        # Decimals to keep the multiplication exact.
        return (
            amount * Decimal(schedule["hours_per_week"]) * Decimal(schedule["paid_weeks_per_year"])
        )
    # Remaining case is "annual" - already in the units we want.
    return amount


def _component_limits(payload: dict, errors: list) -> dict[str, Decimal | None]:
    """Validate optional monthly EPI-component maximums.

    A blank or null value means no limit. An explicit zero remains an active
    zero-dollar maximum, so this cannot reuse optional amount handling that
    canonicalizes blanks to zero.
    """
    # The docstring above names a genuinely important distinction, worth
    # spelling out for anyone new:
    #
    #   blank box          -> None -> "I don't care about this category"
    #   the number 0 typed -> 0    -> "this category must cost nothing"
    #
    # Elsewhere in this file, `optional=True` turns blanks into zero. Doing
    # that here would convert every untouched category into an impossible
    # zero-dollar cap and return no counties at all. Hence the manual blank
    # check below instead of reusing that switch.

    # `.get(name, default)` reads a value, falling back to the default when the
    # key is absent - so an omitted section behaves like an empty one.
    raw = payload.get("component_limits_monthly", {})
    if not isinstance(raw, dict):
        _error(
            errors,
            "component_limits_monthly",
            "invalid_component_limits",
            "Provide monthly limits as an object; leave a category blank for no limit.",
        )
        # Return a complete structure with every category set to None. Callers
        # can then rely on all six keys existing even after a failure, which
        # keeps their code simple. The raised error still stops the request.
        return {name: None for name in COST_COMPONENTS}

    # Reject category names we do not publish. Silently ignoring an unknown key
    # would let a typo like "health_care" look accepted while doing nothing.
    unknown = [name for name in raw if name not in COST_COMPONENTS]
    if unknown:
        _error(
            errors,
            "component_limits_monthly",
            "invalid_component_limits",
            "Use only the supported EPI cost categories.",
        )

    limits = {}
    # Loop over OUR list, not the user's, so the result always has all six keys
    # in a known order regardless of what arrived.
    for name in COST_COMPONENTS:
        value = raw.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            limits[name] = None  # no limit for this category
        else:
            limits[name] = _input_amount(
                value,
                f"component_limits_monthly.{name}",
                errors,
                invalid_code="invalid_component_limit",
            )
    return limits


def _custom_costs(payload: dict, errors: list) -> tuple[str, dict[str, Decimal | None], bool]:
    """Validate monthly replacement costs without confusing blank with zero."""
    mode = payload.get("cost_mode", "epi")
    if mode not in ("epi", "custom"):
        _error(
            errors, "cost_mode", "invalid_cost_mode", "Choose EPI estimate or Advanced household budget."
        )
        mode = "epi"
    raw = payload.get("custom_costs_monthly", {})
    if not isinstance(raw, dict) or any(name not in COST_COMPONENTS for name in raw):
        _error(
            errors,
            "custom_costs_monthly",
            "invalid_custom_costs",
            "Use only the six supported cost categories.",
        )
        raw = {}
    costs = {}
    for name in COST_COMPONENTS:
        value = raw.get(name)
        costs[name] = (
            None
            if value is None or (isinstance(value, str) and not value.strip())
            else _input_amount(
                value, f"custom_costs_monthly.{name}", errors, invalid_code="invalid_custom_cost"
            )
        )
    active = any(value is not None for value in costs.values())
    if mode == "epi" and active:
        _error(
            errors,
            "custom_costs_monthly",
            "inactive_custom_costs",
            "Choose Advanced household budget to use replacement costs.",
        )
    if mode == "custom" and not active:
        _error(
            errors,
            "custom_costs_monthly",
            "missing_custom_cost",
            "Enter at least one monthly replacement cost, including 0 if that is your cost.",
        )
    acknowledged = payload.get("custom_scope_acknowledged", False)
    if type(acknowledged) is not bool:
        _error(
            errors,
            "custom_scope_acknowledged",
            "invalid_scope",
            "Confirm whether the same replacement costs apply in every searched county.",
        )
        acknowledged = False
    if mode == "custom" and not acknowledged:
        _error(
            errors,
            "custom_scope_acknowledged",
            "scope_not_acknowledged",
            "Confirm that COLE will apply these costs in every searched county; local benefits may not travel with you.",
        )
    return mode, costs, acknowledged


def _temperature_limits(payload: dict, errors: list) -> dict[str, Decimal | None]:
    """Validate optional Fahrenheit thresholds at source tenth-degree precision."""
    # Same blank-versus-zero care as _component_limits, plus one extra job:
    # catching combinations that can never be satisfied. Telling someone "no
    # counties matched" when the real answer is "your filters contradict each
    # other" is a bad experience, so we detect that up front.
    raw = payload.get("temperature_limits_f", {})
    if not isinstance(raw, dict):
        _error(
            errors,
            "temperature_limits_f",
            "invalid_temperature_limits",
            "Provide temperature limits as an object; leave a field blank for no limit.",
        )
        return {name: None for name in TEMPERATURE_LIMITS}
    if any(name not in TEMPERATURE_LIMITS for name in raw):
        # `any(...)` is True if at least one item satisfies the condition.
        _error(
            errors,
            "temperature_limits_f",
            "invalid_temperature_limits",
            "Use only the supported coldest-month, warmest-month, and 12-month-average limits.",
        )

    limits = {}
    for name in TEMPERATURE_LIMITS:
        value = raw.get(name)
        field = f"temperature_limits_f.{name}"
        if value is None or (isinstance(value, str) and not value.strip()):
            limits[name] = None
            # `continue` skips the rest of this loop pass and starts the next one.
            continue
        # Shape check: is it text that looks like a temperature at all?
        if (
            not isinstance(value, str)
            or len(value) > 16
            or not _TEMPERATURE_NUMBER.fullmatch(value.strip())
        ):
            _error(
                errors,
                field,
                "invalid_temperature_limit",
                "Use degrees Fahrenheit from -100 through 150 with at most one decimal place.",
            )
            limits[name] = None
            continue
        parsed = Decimal(value.strip())
        # Range check, separate from the shape check so the two failures can be
        # told apart. The pattern permits "999"; Earth's weather does not.
        if not Decimal("-100") <= parsed <= Decimal("150"):
            _error(
                errors,
                field,
                "invalid_temperature_limit",
                "Use degrees Fahrenheit from -100 through 150.",
            )
            limits[name] = None
        else:
            limits[name] = parsed

    # --- Contradiction detection ------------------------------------------
    # Pull the four values out into readable local names first.
    coldest, warmest = limits.get("coldest_month_min"), limits.get("warmest_month_max")
    average_min = limits.get("twelve_month_average_min")
    average_max = limits.get("twelve_month_average_max")
    conflicts = []
    # Each check runs only when BOTH values in the pair were supplied - an
    # unset filter cannot contradict anything.
    if coldest is not None and warmest is not None and coldest > warmest:
        # e.g. "coldest month at least 80" with "warmest month at most 60":
        # the coldest month is never warmer than the warmest month.
        conflicts.append("the coldest-month minimum exceeds the warmest-month maximum")
    if average_min is not None and average_max is not None and average_min > average_max:
        conflicts.append("the 12-month-average minimum exceeds its maximum")
    if coldest is not None and average_max is not None and coldest > average_max:
        # The yearly average is never below the coldest month, so demanding a
        # coldest month above the average ceiling is impossible.
        conflicts.append("the coldest-month minimum exceeds the 12-month-average maximum")
    if average_min is not None and warmest is not None and average_min > warmest:
        # Mirror of the previous case: the average never exceeds the warmest month.
        conflicts.append("the 12-month-average minimum exceeds the warmest-month maximum")
    if conflicts:
        # Report every contradiction found, joined into one sentence.
        _error(
            errors,
            "temperature_limits_f",
            "impossible_temperature_limits",
            "The temperature ranges conflict: " + "; ".join(conflicts) + ".",
        )
    return limits


def _monthly_display_amount(annual: Decimal) -> Decimal:
    """Return the cent-rounded monthly value shown and filtered by COLE."""
    # This is the ONE place in this file where rounding is allowed, and it
    # exists to keep the screen and the filter honest with each other.
    #
    # `localcontext(...)` temporarily switches on the exact-math settings for
    # the indented block, then restores whatever was in effect before. Inside,
    # we deliberately DISARM both alarms, because rounding here is the intent,
    # not a bug - if the alarms stayed armed, dividing $13,462 by 12 would
    # raise an error rather than produce $1,121.83.
    with localcontext(_EXACT_CONTEXT) as context:
        context.traps[Inexact] = False
        context.traps[Rounded] = False
        # ROUND_HALF_UP is the rounding people learn in school: exactly half a
        # cent rounds away from zero. Python's default is different (banker's
        # rounding, where 0.5 goes to the nearest even digit), which would look
        # wrong on a budget screen.
        context.rounding = ROUND_HALF_UP
        # `.quantize(_CENT)` forces the result to exactly two decimal places.
        return (annual / _TWELVE).quantize(_CENT)


def normalize_scenario(payload: Any) -> dict:
    """Validate a scenario and return canonical JSON-safe annual calculations.

    Every upper income value is required. Lower income values must either all
    be supplied or all be blank; an all-blank lower side creates upper-only
    financial matching rather than silently converting blank values to zero.
    Optional blank extras/buffer amounts become zero. Costs-only requires
    explicit null earners and buffers, so a partial financial form cannot
    silently lose its income context.
    """
    # ======================================================================
    # THE GATEKEEPER
    # ======================================================================
    # This is the largest function in the file and the most important one to
    # understand. Everything the user typed passes through here exactly once.
    # Nothing downstream re-validates it; downstream code trusts this output
    # completely. So if a check is missing here, it is missing everywhere.
    #
    # It has three phases:
    #   1. VALIDATE  - check every field, collecting problems rather than
    #                  stopping at the first one
    #   2. RAISE     - if anything failed, stop now and report all of it
    #   3. COMPUTE   - convert the clean values into yearly totals
    #
    # TWO MODES the user can be in:
    #   "financial"   - they entered take-home pay; compare it with estimates
    #   "costs_only"  - they just want to see what a place costs, no income
    #
    # The docstring's phrase "cannot silently lose its income context" points
    # at a real failure mode: if a half-filled income form were quietly treated
    # as costs_only, the user would get results that look fine but silently
    # ignored their money. So costs_only must be stated explicitly, with
    # earners and buffers sent as null.
    # ======================================================================

    # The running list of everything wrong. Phase 2 checks whether it is empty.
    errors: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        # Raise immediately rather than collecting: if the whole request is the
        # wrong shape, there are no individual fields left to report on.
        raise ValidationError(
            [
                {
                    "field": "scenario",
                    "code": "invalid_scenario",
                    "message": "The scenario must be an object.",
                }
            ]
        )

    # --- Household composition --------------------------------------------
    household = payload.get("household")
    # The conditional reads: take household["adults"] if household really is a
    # dictionary, otherwise None. This avoids crashing when `household` is
    # missing or is the wrong type.
    adults = household.get("adults") if isinstance(household, dict) else None
    children = household.get("children") if isinstance(household, dict) else None
    # `type(adults) is not int` is used instead of `isinstance(adults, int)` on
    # purpose. In Python, True and False are technically integers, so
    # isinstance(True, int) is True - and True would then sneak through as the
    # number 1 adult. The stricter check rejects it.
    #
    # `range(5)` means 0, 1, 2, 3, 4. The (1, 2) adults and 0-4 children limits
    # come from EPI: those are the ten household profiles they publish. This is
    # a data-coverage boundary, not a judgment about families.
    if (
        type(adults) is not int
        or adults not in (1, 2)
        or type(children) is not int
        or children not in range(5)
    ):
        _error(
            errors,
            "household",
            "unsupported_household",
            "Select one or two adults and zero through four children.",
        )

    # --- Which mode are we in? --------------------------------------------
    context = payload.get("income_context")
    if not isinstance(context, str) or context not in ("financial", "costs_only"):
        _error(
            errors,
            "income_context",
            "incompatible_context",
            "Choose whether to compare costs with take-home pay or view costs only.",
        )

    # --- Fields that apply in both modes ----------------------------------
    # Extra monthly expenses the user knows about that EPI does not model
    # (student loans, alimony, a car payment). optional=True, so blank is zero.
    extras = _input_amount(payload.get("extras_monthly"), "extras_monthly", errors, optional=True)
    component_limits = _component_limits(payload, errors)
    cost_mode, custom_costs, custom_scope_acknowledged = _custom_costs(payload, errors)
    temperature_limits = _temperature_limits(payload, errors)
    raw_reserve = payload.get("reserve_percent")
    reserve_percent = None
    if raw_reserve is not None and not (isinstance(raw_reserve, str) and not raw_reserve.strip()):
        reserve_percent = _input_amount(raw_reserve, "reserve_percent", errors)
        if reserve_percent is not None and reserve_percent > 100:
            _error(
                errors, "reserve_percent", "invalid_percentage", "Enter a percentage from 0 to 100."
            )
        if context != "financial":
            _error(
                errors,
                "reserve_percent",
                "incompatible_context",
                "A reserve target requires take-home income.",
            )

    # Prepare the income-related containers. They stay empty in costs_only mode.
    earners = []
    # Tracks, for each earner row, whether the LOWER take-home box was blank.
    # Used after the loop to enforce all-or-nothing (see below).
    lower_bound_blanks: list[bool] = []
    income_range_mode = None
    buffers = {}

    if context == "costs_only":
        # Demand that both income sections be present AND explicitly null.
        # `field not in payload` catches an omitted key; `payload[field] is not
        # None` catches a key holding real data. Requiring the key to exist is
        # the strict part: it proves the browser meant costs_only, rather than
        # having dropped the income section through a bug.
        for field in ("earners", "buffers"):
            if field not in payload or payload[field] is not None:
                _error(
                    errors,
                    field,
                    "incompatible_context",
                    f"Send {field} as null in costs-only mode.",
                )

    elif context == "financial":
        # ------------------------------------------------------------------
        # EARNERS: one row per income source in the household
        # ------------------------------------------------------------------
        raw_earners = payload.get("earners")
        if not isinstance(raw_earners, list) or not raw_earners:
            # `not raw_earners` is True for an empty list: financial mode needs
            # at least one income.
            _error(errors, "earners", "required_income", "Enter at least one complete earner row.")
        elif len(raw_earners) > MAX_EARNERS:
            _error(
                errors,
                "earners",
                "input_limit",
                f"A request may contain at most {MAX_EARNERS} earner rows.",
            )
        else:
            # `enumerate` gives both the position and the item, so error
            # messages can say which row, e.g. "earners[2].maximum".
            for index, row in enumerate(raw_earners):
                field = f"earners[{index}]"
                if not isinstance(row, dict):
                    _error(errors, field, "invalid_income", "Each earner row must be an object.")
                    continue

                # The minimum must be SENT, even if it is sent empty. A missing
                # key is ambiguous - did the user skip it, or did the form
                # break? Requiring the key removes the guess.
                if "minimum" not in row:
                    _error(
                        errors,
                        f"{field}.minimum",
                        "missing_lower_bound",
                        "Send the lower take-home field explicitly; use null or a blank string when no lower bound applies.",
                    )
                raw_minimum = row.get("minimum")
                # Blank means "no lower bound for this earner" - distinct from
                # the number zero, which would mean "this earner may earn $0."
                minimum_blank = raw_minimum is None or (
                    isinstance(raw_minimum, str) and not raw_minimum.strip()
                )
                # Validate the minimum only when it is not blank.
                minimum = (
                    None
                    if minimum_blank
                    else _input_amount(raw_minimum, f"{field}.minimum", errors)
                )
                # The maximum is always required - it is the ceiling every
                # search is measured against.
                maximum = _input_amount(row.get("maximum"), f"{field}.maximum", errors)
                period, schedule = _period_and_schedule(row, field, errors)

                lower_bound_blanks.append(minimum_blank)

                # Keep the row only if it is coherent: a valid maximum, and a
                # minimum that is either deliberately blank or valid.
                if maximum is not None and (minimum_blank or minimum is not None):
                    if minimum is not None and minimum > maximum:
                        _error(
                            errors,
                            field,
                            "lower_exceeds_upper",
                            "The minimum cannot exceed the maximum.",
                        )
                    # Store canonical text, not Decimals, so this structure can
                    # be sent to the browser and echoed back unchanged.
                    earners.append(
                        {
                            "minimum": None if minimum_blank else _canonical(minimum),
                            "maximum": _canonical(maximum),
                            "period": period,
                            "schedule": schedule,
                        }
                    )

            if lower_bound_blanks:
                # ALL-OR-NOTHING RULE on the lower bounds.
                # `any(...) and not all(...)` is True exactly when the rows are
                # mixed - some blank, some filled. That mixture has no sensible
                # meaning: adding a filled minimum to a blank one would treat
                # the blank earner as contributing zero, understating the
                # household floor and returning counties that leave a larger
                # shortfall than the submitted lower amount allows.
                if any(lower_bound_blanks) and not all(lower_bound_blanks):
                    _error(
                        errors,
                        "earners",
                        "mixed_lower_bounds",
                        "Enter a lower take-home value for every earner, or leave every lower value blank to search up to the household maximum.",
                    )
                # Record which kind of search this is:
                #   "upper_only" - every minimum blank; search everything up to
                #                  the ceiling
                #   "bounded"    - every minimum supplied; search a range
                income_range_mode = "upper_only" if all(lower_bound_blanks) else "bounded"

        # ------------------------------------------------------------------
        # BUFFERS: deliberate slack around the cost range
        # ------------------------------------------------------------------
        # A buffer widens which COSTS are considered acceptable. It never
        # changes the user's income. An upper buffer of $3,000 means "also show
        # me counties costing up to $3,000 more than my ceiling, so I can see
        # what is just out of reach."
        raw_buffers = payload.get("buffers")
        if not isinstance(raw_buffers, dict):
            _error(
                errors, "buffers", "invalid_buffer", "Provide lower and upper buffer definitions."
            )
        else:
            for name in ("lower", "upper"):
                field = f"buffers.{name}"
                row = raw_buffers.get(name)
                if not isinstance(row, dict):
                    _error(
                        errors,
                        field,
                        "invalid_buffer",
                        "Provide an amount, period, and schedule for this buffer.",
                    )
                    continue
                # optional=True: a blank buffer box means no buffer, i.e. zero.
                amount = _input_amount(row.get("amount"), f"{field}.amount", errors, optional=True)
                # `disabled=amount == 0` reads as `disabled=(amount == 0)`.
                # A zero buffer does nothing, so skip the schedule requirement.
                period, schedule = _period_and_schedule(row, field, errors, disabled=amount == 0)
                if amount is not None:
                    buffers[name] = {
                        "amount": _canonical(amount),
                        "period": period,
                        "schedule": schedule,
                    }

        # A lower buffer extends the search BELOW the household minimum - but
        # in upper_only mode there is no minimum to extend below. Rather than
        # ignoring the value the user typed, say so plainly.
        if (
            income_range_mode == "upper_only"
            and isinstance(buffers.get("lower"), dict)
            and buffers["lower"].get("amount") != "0"
        ):
            _error(
                errors,
                "buffers.lower.amount",
                "lower_buffer_without_bound",
                "A lower-cost buffer requires lower take-home amounts. Enter a lower amount for every earner or set this buffer to zero.",
            )

    if reserve_percent is not None and any(Decimal(row["amount"]) != 0 for row in buffers.values()):
        _error(
            errors,
            "buffers",
            "reserve_buffer_conflict",
            "Cost-range buffers must be zero when a reserve target is active.",
        )

    # ======================================================================
    # PHASE 2: STOP IF ANYTHING FAILED
    # ======================================================================
    # Every problem found above is reported in a single response. Nothing below
    # this line runs on invalid data.
    if errors:
        raise ValidationError(errors)

    # ======================================================================
    # PHASE 3: COMPUTE YEARLY TOTALS
    # ======================================================================
    # Exact-math alarms ARMED for this whole block. Any silent precision loss
    # here becomes a crash rather than a wrong financial comparison.
    with localcontext(_EXACT_CONTEXT):
        annual_extras = extras * _TWELVE
        reserve_goal = None

        # `dict.fromkeys(...)` creates all these keys at once, each set to None.
        # Pre-creating them means the returned structure always has the same
        # shape whether or not income was supplied - so the browser and the API
        # never have to test for a missing key.
        annual = dict.fromkeys(
            (
                "income_lower",
                "income_upper",
                "lower_buffer",
                "upper_buffer",
                "effective_combined_min",
                "effective_combined_max",
                "baseline_target_min",
                "baseline_target_max",
            )
        )
        annual["extras"] = _canonical(annual_extras)

        if context == "financial":
            # Add up every earner's yearly maximum. `sum(..., _ZERO)` starts the
            # running total at Decimal zero rather than the integer 0, so the
            # arithmetic stays Decimal the whole way.
            upper = sum(
                (
                    _annualize(Decimal(row["maximum"]), row["period"], row["schedule"])
                    for row in earners
                ),
                _ZERO,
            )
            # The upper buffer, converted to a yearly amount the same way.
            above = _annualize(
                Decimal(buffers["upper"]["amount"]),
                buffers["upper"]["period"],
                buffers["upper"]["schedule"],
            )
            annual.update(
                {
                    # What the household actually brings in, at the top of its range.
                    "income_upper": _canonical(upper),
                    "upper_buffer": _canonical(above),
                    # The highest total cost still considered a match.
                    "effective_combined_max": _canonical(upper + above),
                    # The same ceiling with the user's extra expenses removed - this
                    # is what an EPI county baseline is compared against, since the
                    # extras are not part of EPI's published figure.
                    "baseline_target_max": _canonical(upper + above - annual_extras),
                }
            )
            if income_range_mode == "bounded":
                # Mirror of the above for the bottom of the range. Only computed
                # when every earner supplied a minimum.
                lower = sum(
                    (
                        _annualize(Decimal(row["minimum"]), row["period"], row["schedule"])
                        for row in earners
                    ),
                    _ZERO,
                )
                below = _annualize(
                    Decimal(buffers["lower"]["amount"]),
                    buffers["lower"]["period"],
                    buffers["lower"]["schedule"],
                )
                annual.update(
                    {
                        "income_lower": _canonical(lower),
                        "lower_buffer": _canonical(below),
                        # Note the SUBTRACTION: the lower buffer reaches downward.
                        # This can legitimately go negative, which is why the reader
                        # of this value later passes signed=True.
                        "effective_combined_min": _canonical(lower - below),
                        "baseline_target_min": _canonical(lower - below - annual_extras),
                    }
                )

            if reserve_percent is not None:
                basis = Decimal(
                    annual["income_lower"]
                    if income_range_mode == "bounded"
                    else annual["income_upper"]
                )
                target = basis * reserve_percent / Decimal("100")
                ceiling = basis - target
                reserve_goal = {
                    "percent": _canonical(reserve_percent),
                    "income_basis": "lower" if income_range_mode == "bounded" else "upper",
                    "income_annual": _canonical(basis),
                    "target_annual": _canonical(target),
                    "cost_ceiling_annual": _canonical(ceiling),
                }
                annual.update(
                    effective_combined_min=None,
                    effective_combined_max=_canonical(ceiling),
                    baseline_target_min=None,
                    baseline_target_max=_canonical(ceiling - annual_extras),
                )

    # ======================================================================
    # THE RESULT
    # ======================================================================
    # One clean dictionary, entirely made of text, numbers, and None - all
    # JSON-safe, so it can be sent to the browser, stored, and sent back.
    # Some values are intentionally duplicated (household / adults / children)
    # because different consumers expect different shapes.
    return {
        "household": {"adults": adults, "children": children},
        "adults": adults,
        "children": children,
        # A compact label like "2p1c" = two adults, one child. This is the key
        # used to look up the matching EPI row for a county.
        "household_profile": f"{adults}p{children}c",
        "income_context": context,
        "income_range_mode": income_range_mode,
        "reserve_percent": None if reserve_percent is None else _canonical(reserve_percent),
        "reserve_goal": reserve_goal,
        # `None` rather than an empty list in costs_only mode: absent, not empty.
        "earners": earners if context == "financial" else None,
        "extras_monthly": _canonical(extras),
        "component_limits_monthly": {
            name: _canonical(value) if value is not None else None
            for name, value in component_limits.items()
        },
        "cost_mode": cost_mode,
        "custom_costs_monthly": {
            name: _canonical(value) if value is not None else None
            for name, value in custom_costs.items()
        },
        "custom_scope_acknowledged": custom_scope_acknowledged,
        "temperature_limits_f": {
            name: _canonical(value) if value is not None else None
            for name, value in temperature_limits.items()
        },
        "buffers": buffers if context == "financial" else None,
        "normalized_annual": annual,
        # Stamped so a result can always be traced to the calculator that made it.
        "calculation_version": CALCULATION_VERSION,
    }


def _source_amount(value: Any, field: str, *, signed: bool = False) -> Decimal:
    # Validate a number that came from our own data files or from earlier math.
    #
    # The user-input validators above collect errors politely. This one RAISES
    # immediately, and that difference is deliberate. A bad user entry is normal
    # and gets a friendly message. A bad value in our own prepared release means
    # the data is corrupt, and the right response is to stop rather than compute
    # a plausible-looking answer on top of broken input.
    #
    # `signed=True` permits negative values, and is used only where a negative
    # result is legitimate - notably effective_combined_min, which can dip below
    # zero when a lower buffer exceeds the household minimum.
    if isinstance(value, Decimal):
        # The comment below is the original author's:
        # Bound the exponent before formatting; avoid expanding huge exponents.
        #
        # Worth expanding for a learner: a Decimal can hold something like
        # 1E+900000. Calling format() on that would try to write out nearly a
        # million characters and stall the app. So we inspect the number's
        # internal parts FIRST and reject anything oversized before formatting.
        #   .is_finite()      - rejects infinity and NaN ("not a number")
        #   .as_tuple().exponent - where the decimal point sits
        #   .as_tuple().digits   - how many digits there are
        if (
            not value.is_finite()
            or abs(value.as_tuple().exponent) > MAX_SOURCE_CHARACTERS
            or len(value.as_tuple().digits) > MAX_SOURCE_CHARACTERS
        ):
            # Set to None so the shared rejection below produces the message.
            value = None
        else:
            value = format(value, "f")

    # By this point `value` should be text. Check type, length, and pattern.
    if (
        not isinstance(value, str)
        or len(value) > MAX_SOURCE_CHARACTERS
        or not _SOURCE_NUMBER.fullmatch(value)
    ):
        raise ValidationError(
            [
                {
                    "field": field,
                    "code": "invalid_source_cost",
                    "message": "A required source/derived amount must be a finite decimal string of at most 128 characters.",
                }
            ]
        )

    result = Decimal(value)
    if not signed and result < 0:
        # The message states the policy outright: a negative cost is a data
        # problem to investigate, not something to quietly clamp to zero. A
        # clamp would hide the bug and ship a wrong number to a user.
        raise ValidationError(
            [
                {
                    "field": field,
                    "code": "invalid_source_cost",
                    "message": "Negative source costs require data review; they are not clamped to zero.",
                }
            ]
        )
    return result


def derive_baseline(published_annual_total: Any, published_annual_taxes: Any) -> str:
    """Subtract original source taxes, preserving source precision and errors."""
    # ======================================================================
    # WHY THIS SUBTRACTION EXISTS - the central idea of the whole app
    # ======================================================================
    # EPI publishes a household total that INCLUDES modeled taxes: what a
    # household needs to earn before tax.
    #
    # COLE asks users for take-home pay, which is what lands in the bank AFTER
    # tax. Comparing those two directly would double-count taxes and make every
    # county look far more expensive than it is.
    #
    # So we remove EPI's tax figure once, here, during data preparation, and
    # store the result as the county's "baseline." Everything downstream
    # compares after-tax money against after-tax costs.
    #
    # repository.py re-runs this on every row at load time and refuses to start
    # if the stored baseline does not match - so a hand-edited or corrupted
    # data file is caught before it can affect anyone's results.
    # ======================================================================
    total = _source_amount(published_annual_total, "published_annual_total")
    taxes = _source_amount(published_annual_taxes, "published_annual_taxes")
    with localcontext(_EXACT_CONTEXT):
        baseline = total - taxes
    # Validate the RESULT as well as the inputs. If taxes somehow exceeded the
    # total, the subtraction would go negative, and _source_amount rejects that
    # - catching a data problem right where it is introduced.
    return _canonical(_source_amount(baseline, "baseline_annual"))


def evaluate_component_limits(source_components: Any, normalized: dict) -> dict:
    """Validate six EPI components and evaluate the submitted monthly caps.

    The source publishes annual whole-dollar components. Filtering compares
    the user's monthly maximum with the same cent-rounded monthly value shown
    in the interface. This prevents a displayed value such as $1,121.83 from
    failing an equal $1,121.83 limit because its annual source was $13,462.
    """
    # The docstring describes a subtle bug that this design avoids, and it is
    # worth walking through slowly because it is the kind of thing that erodes
    # trust in a tool:
    #
    #   EPI publishes housing as $13,462 per year.
    #   Divided by 12 that is $1,121.8333...
    #   The screen shows $1,121.83 (rounded to cents).
    #   The user sees $1,121.83 and types $1,121.83 as their limit.
    #
    # If we compared the user's limit against the UNROUNDED $1,121.8333, the
    # county would fail its own displayed number, and the user would rightly
    # conclude the app is broken.
    #
    # The fix: filter against exactly the value shown on screen. Hence
    # _monthly_display_amount is used on both sides of the comparison.
    if not isinstance(source_components, dict):
        raise ValidationError(
            [
                {
                    "field": "component_costs",
                    "code": "invalid_source_cost",
                    "message": "Required EPI component costs are unavailable.",
                }
            ]
        )

    # Validate all six yearly figures from the data file up front.
    annual = {
        name: _source_amount(
            source_components.get(name),
            f"published_annual_{name}",
        )
        for name in COST_COMPONENTS
    }

    limits = normalized.get("component_limits_monthly")
    # Defensive check that the scenario really came from normalize_scenario.
    # If some future caller hand-assembles a scenario and forgets a category,
    # this stops it rather than treating the missing one as "no limit."
    if not isinstance(limits, dict) or any(name not in limits for name in COST_COMPONENTS):
        raise ValidationError(
            [
                {
                    "field": "component_limits_monthly",
                    "code": "invalid_component_limits",
                    "message": "The normalized scenario has no complete component-limit definition.",
                }
            ]
        )

    matches = {}
    for name in COST_COMPONENTS:
        limit = limits[name]
        if limit is not None:
            # Only categories with an active limit get an entry. Categories the
            # user left blank are simply absent from `matches`, which is what
            # makes the all() check below behave correctly.
            matches[name] = _monthly_display_amount(annual[name]) <= _source_amount(
                limit,
                f"component_limits_monthly.{name}",
            )

    return {
        # Every yearly figure, echoed back for display.
        "component_costs_annual": {name: _canonical(value) for name, value in annual.items()},
        # Per-category pass/fail, for the ones that were limited.
        "component_limit_matches": matches,
        # `all(...)` on an EMPTY collection is True in Python. That is exactly
        # the behaviour we want: a user who set no limits passes trivially.
        "component_limits_match": all(matches.values()),
        # Names of the categories that failed, so the page can highlight them.
        # `matches.get(name) is False` is deliberately not `not matches.get(name)`:
        # a category with no limit returns None, and `not None` would wrongly
        # report it as exceeded.
        "exceeded_component_limits": [
            name for name in COST_COMPONENTS if matches.get(name) is False
        ],
    }


def evaluate_temperature(source_climate: Any, normalized: dict) -> dict:
    """Evaluate optional seasonal and derived-average temperature thresholds.

    Prepared values are signed integer tenths of a degree Fahrenheit. The
    annual context value is the half-up one-decimal arithmetic mean of the 12
    monthly means. Seasonal filtering uses the source-tenth extrema; average
    filtering uses the same one-decimal derived value that the interface shows.
    """
    # "Tenths of a degree" means the data file stores 723 to mean 72.3F. Whole
    # numbers are stored instead of decimals because whole numbers cannot drift
    # or be misread - the conversion to degrees happens once, here.
    #
    # Note the two-tier design in the docstring:
    #   - Coldest/warmest filters use the SOURCE values directly (exact).
    #   - The 12-month average filter uses the DISPLAYED one-decimal value,
    #     for the same "must match what the user sees" reason as the cost
    #     limits above.
    limits = normalized.get("temperature_limits_f")
    if not isinstance(limits, dict) or any(name not in limits for name in TEMPERATURE_LIMITS):
        raise ValidationError(
            [
                {
                    "field": "temperature_limits_f",
                    "code": "invalid_temperature_limits",
                    "message": "The normalized scenario has no complete temperature-limit definition.",
                }
            ]
        )

    # Which filters the user actually set.
    active = [name for name in TEMPERATURE_LIMITS if limits[name] is not None]

    # --- Case: we have no climate data for this county --------------------
    # A handful of counties have no NOAA coverage. Rather than dropping them or
    # pretending they pass, we report honestly that the filter could not be
    # applied, and the web page marks them as unavailable.
    if source_climate is None:
        return {
            "climate_data_availability": "unavailable",
            "climate": None,
            "temperature_filter_matches": {name: None for name in active},
            # `not active` is True when the user set no temperature filters. So
            # a county with no climate data still matches a search that did not
            # ask about climate - but fails one that did.
            "temperature_filters_match": not active,
            "failed_temperature_filters": [],
            "unavailable_temperature_filters": active,
        }

    if not isinstance(source_climate, dict):
        raise ValidationError(
            [
                {
                    "field": "climate",
                    "code": "invalid_source_climate",
                    "message": "The prepared county climate record is malformed.",
                }
            ]
        )

    # --- Read and convert the twelve monthly values -----------------------
    monthly = {}
    for month in CLIMATE_MONTHS:
        value = source_climate.get(f"{month}_tenths_f")
        # `type(value) is not int` again avoids True/False sneaking through.
        # The -500 to 1400 window is -50.0F to 140.0F, NOAA's documented range.
        if type(value) is not int or not -500 <= value <= 1400:
            raise ValidationError(
                [
                    {
                        "field": f"climate.{month}_tenths_f",
                        "code": "invalid_source_climate",
                        "message": "A prepared monthly temperature is missing or outside NOAA's documented range.",
                    }
                ]
            )
        # Tenths back to degrees. Decimal division by Decimal("10") is exact.
        with localcontext(_EXACT_CONTEXT):
            monthly[month] = Decimal(value) / Decimal("10")

    # Confirm this row really is the 1991-2020 normals period we support.
    # Comparing against a different period would be quietly comparing apples to
    # oranges across counties.
    for field, expected in (("normal_period_start", 1991), ("normal_period_end", 2020)):
        if source_climate.get(field) != expected:
            raise ValidationError(
                [
                    {
                        "field": f"climate.{field}",
                        "code": "invalid_source_climate",
                        "message": "The prepared climate period differs from the supported release.",
                    }
                ]
            )

    # The coldest and warmest MONTHLY AVERAGES - not record lows and highs.
    # A county whose coldest month averages 35F still has nights below freezing.
    coldest = min(monthly.values())
    warmest = max(monthly.values())

    # The yearly average, rounded to one decimal the same way the screen shows
    # it. Alarms disarmed here for the same reason as _monthly_display_amount:
    # rounding is the intent.
    with localcontext(_EXACT_CONTEXT) as context:
        context.traps[Inexact] = False
        context.traps[Rounded] = False
        context.rounding = ROUND_HALF_UP
        twelve_month_average = (sum(monthly.values(), _ZERO) / _TWELVE).quantize(_TENTH)

    # Re-validate the limits coming out of the normalized scenario. They were
    # already checked on the way in; this catches a scenario that was tampered
    # with or hand-built in between.
    parsed_limits = {}
    for name in active:
        value = limits[name]
        if not isinstance(value, str) or not _TEMPERATURE_NUMBER.fullmatch(value):
            raise ValidationError(
                [
                    {
                        "field": f"temperature_limits_f.{name}",
                        "code": "invalid_temperature_limits",
                        "message": "A normalized temperature limit is malformed.",
                    }
                ]
            )
        parsed_limits[name] = Decimal(value)

    # --- Apply each active filter -----------------------------------------
    # Only filters the user set appear in `matches`, mirroring the component
    # logic: an unset filter is absent, not a passing True.
    matches = {}
    if "coldest_month_min" in parsed_limits:
        # "at least this warm in the coldest month"
        matches["coldest_month_min"] = coldest >= parsed_limits["coldest_month_min"]
    if "warmest_month_max" in parsed_limits:
        # "no hotter than this in the warmest month"
        matches["warmest_month_max"] = warmest <= parsed_limits["warmest_month_max"]
    if "twelve_month_average_min" in parsed_limits:
        matches["twelve_month_average_min"] = (
            twelve_month_average >= parsed_limits["twelve_month_average_min"]
        )
    if "twelve_month_average_max" in parsed_limits:
        matches["twelve_month_average_max"] = (
            twelve_month_average <= parsed_limits["twelve_month_average_max"]
        )

    return {
        "climate_data_availability": "available",
        "climate": {
            # All twelve values, for the detail panel's month-by-month display.
            "monthly_mean_f": {month: _canonical(value) for month, value in monthly.items()},
            "coldest_month_mean_f": _canonical(coldest),
            # A LIST, because two months can tie for coldest.
            "coldest_months": [month for month, value in monthly.items() if value == coldest],
            "warmest_month_mean_f": _canonical(warmest),
            "warmest_months": [month for month, value in monthly.items() if value == warmest],
            "twelve_month_average_f": _canonical(twelve_month_average),
            "normal_period_start": source_climate["normal_period_start"],
            "normal_period_end": source_climate["normal_period_end"],
            # NOAA's own identifiers, carried through so a curious user can
            # trace any number back to the published source record.
            "source": {
                key: source_climate.get(key)
                for key in (
                    "source_state_code",
                    "source_county_code",
                    "source_element_code",
                    "source_period_code",
                )
            },
        },
        "temperature_filter_matches": matches,
        # Again: all() of nothing is True, so no filters means automatic pass.
        "temperature_filters_match": all(matches.values()),
        "failed_temperature_filters": [
            name for name in TEMPERATURE_LIMITS if matches.get(name) is False
        ],
        "unavailable_temperature_filters": [],
    }


def apply_custom_costs(baseline_annual: Any, source_components: dict, normalized: dict) -> dict:
    """Replace selected EPI components while retaining its published total."""
    baseline = _source_amount(baseline_annual, "baseline_annual")
    if normalized["cost_mode"] != "custom":
        return {
            "custom_baseline_annual": None,
            "custom_delta_annual": "0",
            "custom_components_annual": None,
        }
    with localcontext(_EXACT_CONTEXT):
        delta = _ZERO
        components = {}
        for name in COST_COMPONENTS:
            original = _source_amount(source_components[name], f"published_annual_{name}")
            replacement = normalized["custom_costs_monthly"][name]
            amount = (
                original
                if replacement is None
                else _source_amount(replacement, f"custom_costs_monthly.{name}") * _TWELVE
            )
            components[name] = _canonical(amount)
            delta += amount - original
        adjusted = baseline + delta
        # -Team: EPI rounds its six components separately from the published
        # total. If someone enters $0 for all six, a few source rows land a
        # dollar or two below zero. That is source rounding, not a negative
        # household cost, so only that tiny remainder becomes zero. -Brandon
        if adjusted < 0:
            if adjusted < Decimal("-3"):
                _source_amount(adjusted, "custom_baseline_annual")
            adjusted = _ZERO
        return {
            "custom_baseline_annual": _canonical(adjusted),
            "custom_delta_annual": _canonical(adjusted - baseline),
            "custom_components_annual": components,
        }


def evaluate_county(baseline_annual: Any, normalized: dict) -> dict:
    """Evaluate a source baseline against normalize_scenario's accepted output.

    This function expects a trusted normalized scenario, never a client-supplied
    normalized_annual object. Request handlers must call normalize_scenario.
    """
    # ======================================================================
    # THE MONEY VERDICT
    # ======================================================================
    # The warning in the docstring is a security rule, not a style note. This
    # function TRUSTS the yearly totals it is handed. If a request handler ever
    # passed the browser's own numbers straight through, a user could send an
    # invented income ceiling and get back results that were never validated.
    # The rule is: normalize_scenario runs first, always.
    #
    # The five possible outcomes:
    #   within_range   - cost sits inside the household's income range
    #   above_buffer   - above the ceiling, but inside the upper buffer
    #   below_buffer   - below the floor, but inside the lower buffer
    #   outside_range  - no match
    #   estimated_cost - costs_only mode; no take-home comparison is requested
    # ======================================================================
    baseline = _source_amount(baseline_annual, "baseline_annual")
    annual = normalized["normalized_annual"]
    extras = _source_amount(annual["extras"], "normalized_annual.extras")

    # Exact-math alarms armed for the whole comparison.
    with localcontext(_EXACT_CONTEXT):
        # The county's EPI baseline plus the user's own extra expenses: the
        # full yearly cost of living there, as far as COLE can estimate it.
        combined = baseline + extras

        # Build the result with the costs_only answer already filled in. The
        # financial branch below overwrites these fields if income was given.
        result = {
            "baseline_annual": _canonical(baseline),
            "extras_annual": _canonical(extras),
            "combined_cost_annual": _canonical(combined),
            "financial_status": "estimated_cost",
            "financial_match": None,
            "amount_above_maximum_annual": None,
            "income_points": None,
            "calculation_version": CALCULATION_VERSION,
        }
        if normalized["income_context"] == "costs_only":
            # No income to compare against - the cost figures alone are the
            # answer. Early return keeps the rest of the function focused.
            return result

        # --- Financial mode ------------------------------------------------
        upper = _source_amount(annual["income_upper"], "normalized_annual.income_upper")
        expanded_upper = _source_amount(
            annual["effective_combined_max"], "normalized_annual.effective_combined_max"
        )

        goal = normalized.get("reserve_goal")
        if goal is not None:
            status = "within_range" if combined <= expanded_upper else "outside_range"
            lower = annual["income_lower"]
            income_values = (
                [upper] if lower is None or Decimal(lower) == upper else [Decimal(lower), upper]
            )
        elif normalized["income_range_mode"] == "upper_only":
            # One-sided search: everything from $0 up to the ceiling.
            if combined <= upper:
                status = "within_range"
            elif combined <= expanded_upper:
                # Over the ceiling but inside the buffer: shown, and flagged.
                status = "above_buffer"
            else:
                status = "outside_range"
            income_values = [upper]
        else:
            # Two-sided search: a floor and a ceiling.
            lower = _source_amount(annual["income_lower"], "normalized_annual.income_lower")
            # signed=True here because the lower buffer reaches downward and
            # this figure can legitimately land below zero.
            expanded_lower = _source_amount(
                annual["effective_combined_min"],
                "normalized_annual.effective_combined_min",
                signed=True,
            )
            if lower <= combined <= upper:
                status = "within_range"
            elif expanded_lower <= combined < lower:
                status = "below_buffer"
            elif upper < combined <= expanded_upper:
                status = "above_buffer"
            else:
                status = "outside_range"
            # If the floor and ceiling are identical, show one figure rather
            # than the same number twice.
            income_values = [lower] if lower == upper else [lower, upper]

        result.update(
            {
                # A match is anything that is not flatly outside - so buffered
                # results count as matches, and carry their status for the label.
                "financial_status": status,
                "financial_match": status != "outside_range",
                # How far over the ceiling the cost runs. `max(..., _ZERO)` floors
                # it at zero so a county at or below the submitted ceiling reports
                # 0 rather than a confusing negative overage.
                "amount_above_maximum_annual": _canonical(max(combined - upper, _ZERO)),
                # What is left over (or short) at each end of the income range.
                # A negative balance is a shortfall and is reported as such, not
                # hidden - a county can sit inside the selected range and still
                # leave the household short at its low end.
                "income_points": [
                    {"income": _canonical(income), "balance": _canonical(income - combined)}
                    for income in income_values
                ],
            }
        )
        if goal is not None:
            percentage = Decimal(goal["percent"]) / Decimal("100")
            for point in result["income_points"]:
                target = Decimal(point["income"]) * percentage
                after_goal = Decimal(point["balance"]) - target
                point.update(
                    reserve_target_annual=_canonical(target),
                    balance_after_reserve_annual=_canonical(after_goal),
                    reserve_goal_met=after_goal >= 0,
                )
        return result
