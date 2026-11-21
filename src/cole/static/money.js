// ===========================================================================
// PLAIN-ENGLISH GUIDE TO THIS FILE
// ===========================================================================
//
// WHAT IT DOES
// This is the browser's money formatter. It turns the exact decimal strings
// the Python side sends ("13462", "1121.8333") into what the user reads
// ("$13,462", "$1,121.83"), and compares two amounts correctly.
//
// It is tiny but it is the front-end counterpart to calculations.py, and it
// exists for the same reason: JAVASCRIPT CANNOT DO MONEY WITH NORMAL NUMBERS.
//
// In JavaScript, 0.1 + 0.2 is 0.30000000000000004. Every ordinary number is
// stored in binary, and binary cannot express one tenth exactly - the same way
// decimal cannot express one third. For physics that is fine. For a household
// budget it means a county could be displayed as $1 over a limit it actually
// meets.
//
// THE FIX: BigInt
// BigInt is a JavaScript type for exact whole-number arithmetic, limited in
// practice by available memory rather than floating-point precision. It is
// written with an "n" suffix: 10n, 12n, 0n. Formatting can still perform the
// explicit half-up rounding requested by the caller.
//
// So this file never converts money to a normal number. Instead it splits the
// text "1121.83" into two BigInts:
//     units = 112183n     (all the digits, decimal point removed)
//     scale = 100n        (what to divide by to get the real value)
// All arithmetic happens on whole numbers, and the decimal point is put back
// only at the moment of printing. That is why the original author's comment
// below says no floating-point conversion participates in money formatting.
//
// WHY THE WHOLE FILE IS WRAPPED IN (function (root) { ... })(window)
// That pattern is an "IIFE" - a function defined and immediately run. Anything
// declared inside it is invisible to the rest of the page, so COLE cannot
// accidentally collide with a name used elsewhere. At the bottom it publishes
// exactly three functions and nothing else.
// ===========================================================================

(function (root) {
  // 'use strict' turns on JavaScript's stricter rules: typos that would
  // silently create a new global variable become errors instead.
  'use strict';

  // -------------------------------------------------------------------------
  // decimal(value)
  // Parse a canonical decimal string into exact whole-number parts.
  // "1121.83"  -> {negative: false, units: 112183n, scale: 100n}
  // "-50"      -> {negative: true,  units: 50n,     scale: 1n}
  // -------------------------------------------------------------------------
  function decimal(value) {
    const text = String(value).trim();
    // Pattern, piece by piece:
    //   ^           start of the text
    //   (-?)        group 1: an optional minus sign
    //   (\d+)       group 2: one or more digits (the whole part)
    //   (?:\.(\d+))?  optionally a dot plus group 3: the fractional digits
    //   $           end of the text
    // So "1,200", "$50", "1e5" and "" are all rejected.
    const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(text);
    // Throwing rather than returning a fallback is deliberate: a bad money
    // value should surface as a bug, not be silently rendered as $0.
    if (!match) throw new TypeError('A canonical decimal string is required.');
    const fraction = match[3] || ''; // '' when there was no decimal part
    // THE KEY TRICK, in `units`: glue the whole and fractional digits together
    // as TEXT first, then make one BigInt. "1121" + "83" -> "112183" -> 112183n.
    // No division happens, so nothing can round.
    // `scale` is 10 to the power of however many fractional digits there were:
    // two digits -> 100n. That records where the decimal point belongs.
    // `negative` is tracked separately so the sign never affects the digits.
    return {
      negative: match[1] === '-',
      units: BigInt(match[2] + fraction),
      scale: 10n ** BigInt(fraction.length),
    };
  }

  // -------------------------------------------------------------------------
  // sign(value) -> -1, 0, or 1
  // Used by the interface to colour a balance red or green, without having to
  // convert the amount to a number first.
  // -------------------------------------------------------------------------
  function sign(value) {
    const number = decimal(value);
    // Checks units, not the minus sign, so "-0" correctly reports 0 rather
    // than -1. Reads as: if zero -> 0, else if negative -> -1, else 1.
    return number.units === 0n ? 0 : number.negative ? -1 : 1;
  }

  // -------------------------------------------------------------------------
  // compare(left, right) -> -1, 0, or 1
  // "Is the first amount less than, equal to, or greater than the second?"
  // Used for sorting result lists and for range checks.
  // -------------------------------------------------------------------------
  function compare(leftValue, rightValue) {
    const left = decimal(leftValue),
      right = decimal(rightValue);
    // Both zero: equal regardless of any minus signs.
    if (left.units === 0n && right.units === 0n) return 0;
    // Different signs: the negative one is smaller, no arithmetic needed.
    if (left.negative !== right.negative) return left.negative ? -1 : 1;

    // Same sign, so compare magnitudes. The two values may have different
    // numbers of decimal places, so put them on a COMMON SCALE by
    // cross-multiplying - the standard way to compare fractions without
    // dividing. Comparing "1.5" and "1.50":
    //     left  = 15n  / 10n
    //     right = 150n / 100n
    //     leftScaled  = 15n  * 100n = 1500n
    //     rightScaled = 150n * 10n  = 1500n   -> equal, correctly
    // Multiplication keeps everything a whole number, so nothing rounds.
    const leftScaled = left.units * right.scale;
    const rightScaled = right.units * left.scale;
    const magnitude = leftScaled < rightScaled ? -1 : leftScaled > rightScaled ? 1 : 0;
    // For two negative numbers the larger magnitude is the SMALLER value,
    // so flip the answer: -5 is less than -3 even though 5 > 3.
    return left.negative ? -magnitude : magnitude;
  }

  // -------------------------------------------------------------------------
  // format(value, options) -> a display string like "$1,121.83"
  //   options.decimals: 0 (whole dollars) or 2 (cents)
  //   options.divisor:  1 (leave as is) or 12 (annual -> monthly)
  // -------------------------------------------------------------------------
  function format(value, options = {}) {
    const decimals = options.decimals === undefined ? 0 : options.decimals;
    const divisor = options.divisor === undefined ? 1 : options.divisor;
    // Only four combinations are legal, and anything else throws. Restricting
    // the options this tightly is what lets the rounding below stay provably
    // correct; a free-form formatter would need far more care.
    if (![0, 2].includes(decimals) || ![1, 12].includes(divisor))
      throw new RangeError('Use decimals 0 or 2 and divisor 1 or 12.');

    const number = decimal(value);
    const precision = 10n ** BigInt(decimals); // 1n or 100n
    const denominator = number.scale * BigInt(divisor);
    const numerator = number.units * precision;

    // Original comment: Round the absolute value half up, then restore its
    // sign. No floating-point conversion participates in money formatting or
    // annual-to-monthly division.
    //
    // The formula (2*n + d) / (2*d) is integer round-half-up, and it is worth
    // understanding because it looks cryptic:
    //   BigInt division always truncates toward zero (13n / 5n is 2n, not 2.6).
    //   Adding half the denominator before dividing converts truncation into
    //   correct rounding: anything at or above the halfway point is pushed up
    //   to the next whole unit.
    //   Everything is doubled so that "half" is itself a whole number and no
    //   fractional value ever appears.
    // This matches Python's ROUND_HALF_UP in calculations.py exactly, which is
    // essential - the two sides must agree on every displayed cent, or a limit
    // the user typed from the screen would fail against the screen's own value.
    const rounded = (2n * numerator + denominator) / (2n * denominator);

    // Show a minus only for genuinely negative, non-zero amounts, so "-0"
    // never appears.
    const prefix = number.negative && number.units !== 0n ? '-' : '';

    // HONESTY GUARD: a small non-zero amount that rounds to nothing would
    // print as "$0", implying free. Print "<$1" or "<$0.01" instead.
    if (rounded === 0n && number.units !== 0n) return prefix + (decimals === 0 ? '<$1' : '<$0.01');

    // Turn the rounded whole number back into text and reinsert the decimal
    // point. padStart guarantees enough digits to slice - without it, 5n with
    // decimals=2 would become "5" and the slicing below would break.
    const digits = rounded.toString().padStart(decimals + 1, '0');
    // Split off the fractional digits from the end, then insert thousands
    // separators into what remains.
    // The pattern /\B(?=(\d{3})+(?!\d))/g means: any position that is not a
    // word boundary, followed by a group of three digits repeated to the end.
    // In practice: put a comma every three digits from the right.
    const whole = (decimals ? digits.slice(0, -decimals) : digits).replace(
      /\B(?=(\d{3})+(?!\d))/g,
      ','
    );
    const fraction = decimals ? '.' + digits.slice(-decimals) : '';
    return prefix + '$' + whole + fraction;
  }

  // -------------------------------------------------------------------------
  // PUBLISH exactly three functions. `decimal` stays private.
  // Object.freeze prevents anything else on the page from replacing them -
  // a small guard against another script quietly changing how money is shown.
  // -------------------------------------------------------------------------
  const api = Object.freeze({ format, sign, compare });
  // Support being loaded by Node.js (for tests) as well as by a browser.
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  // In the browser, publish as window.ColeMoney for app.js to use.
  if (root) root.ColeMoney = api;
  // The argument passed in: the browser's `window`, or null under Node.
})(typeof window !== 'undefined' ? window : null);
