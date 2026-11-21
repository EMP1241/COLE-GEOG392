// ===========================================================================
// PLAIN-ENGLISH GUIDE TO THIS FILE
// ===========================================================================
//
// WHAT IT DOES
// This is the entire user interface. Everything the person sees and clicks is
// driven from here: the screens, the form, the map, the result list, the
// county detail panel, and the comparison cards.
//
// It is the biggest file in the project. Its main responsibilities are to:
//   1. READ the form into a "scenario" object
//   2. SEND that to the local Python server
//   3. STORE what comes back in `state`
//   4. REDRAW the major result, detail, comparison, and map sections
//   5. MANAGE form feedback, focus, dialogs, and other direct UI updates
//
// THE CENTRAL RULE: `state` IS THE TRUTH
// `state` (defined near the top) is authoritative for accepted criteria,
// results, inspection, and comparisons. The render* functions rebuild those
// major sections from it. Form controls, status messages, and accessibility
// feedback are also updated directly when an interaction calls for it.
//
// THE SECOND RULE: NOTHING IS SAVED
// There is no localStorage, no cookie, no file. The user's income exists only
// in this page's memory. Close the tab and it is gone. That is a promise made
// on the intro screen, and it is kept by simply never writing it anywhere.
//
// DRAFT vs ACCEPTED - the most important idea in the file
// What the user is typing (the "draft") and what produced the results on
// screen (the "accepted scenario") are deliberately kept apart. Editing the
// form does NOT change the results. It shows a note saying "you have
// unapplied changes." Results only change when a submission SUCCEEDS.
// This is why a failed search never destroys the results you were looking at.
//
// JAVASCRIPT NOTATION YOU WILL SEE CONSTANTLY
//   const f = x => ...       an "arrow function": a compact way to write a
//                            function. `x` is the input, `...` is the result.
//   `text ${value} more`     a "template string" - backticks let you drop
//                            values straight into text.
//   a?.b                     "optional chaining": read b only if a exists,
//                            otherwise give undefined instead of crashing.
//   a ?? b                   "use a, unless it is null/undefined, then b".
//   x ? y : z                "if x then y else z", written inline.
//   {...thing, key:value}    copy `thing`, then override `key`.
//   async / await            this operation takes time (usually a network
//                            request); wait for it without freezing the page.
//
// WHY THE CODE IS SO TIGHTLY PACKED
// The original author wrote many statements per line. That is a style choice,
// not something you should copy - and it is exactly why these comments exist.
// Read the comment blocks first; treat the code beneath each as the detail.
// ===========================================================================

/* COLE browser state: editable form, accepted scenario, inspection, comparison. */

// The whole file is wrapped in an "IIFE" - a function that is defined and
// immediately run. Everything inside is private to COLE, so none of these
// names can collide with anything else on the page.
// 'use strict' turns on JavaScript's stricter rules: a typo that would
// silently create a new global variable becomes an error instead.
(() => {
  'use strict';

  // --- Tiny helpers used everywhere ---------------------------------------
  // $ is shorthand for "find the element with this id". Used hundreds of times
  // below, which is why it has a one-character name.
  //
  // esc() is a SECURITY function and the most important line in this section.
  // Wherever text is inserted into the page as HTML, it must pass through here
  // first. It replaces the characters that would otherwise be read as markup
  // (< > & " ') with harmless equivalents. Without it, a county name containing
  // something like <script> could be executed as code. This is called escaping,
  // and its absence is one of the most common web vulnerabilities.
  //
  // money() and degrees() format values for display. Note money() delegates to
  // ColeMoney (money.js) rather than doing its own arithmetic - JavaScript's
  // ordinary numbers cannot represent money exactly.
  // degrees() forces a trailing ".0" so a column of temperatures lines up.
  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value ?? '').replace(
      /[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
    );
  const money = (value, decimals = 0, divisor = 1) =>
    value == null ? 'Not applicable' : ColeMoney.format(value, { decimals, divisor });
  const degrees = (value) =>
    value == null ? 'Unavailable' : `${String(value).includes('.') ? value : `${value}.0`} °F`;

  // =========================================================================
  // THE STATE OBJECT - everything the interface knows
  // =========================================================================
  // Reading the important fields:
  //   view            which screen is showing: splash / how / criteria / results
  //   accepted        the scenario that produced the CURRENT results (not the
  //                   one being typed). Null until a successful submission.
  //   results         the counties returned by that submission
  //   resultMap       the same rows keyed by county id, for instant lookup
  //   comparison      up to three counties pinned side by side
  //   inspected       the county open in the detail panel
  //   pendingInspectedId / inspectionPending
  //                   a county whose details are still loading. Kept separate
  //                   from `inspected` so the map can highlight it immediately
  //                   while the panel still shows the previous county.
  //   busy            a submission is in flight; most controls disable
  //   ready           startup finished (data and map loaded)
  //   incompatible    the app or data changed underneath us; reload required
  //   map / layers    the Leaflet map and one shape per county
  //
  // THE *Controller AND *Generation FIELDS - how COLE avoids stale answers.
  // If a user searches "Austin", then quickly searches "Boston", two requests
  // are in flight and they can come back in either order. Without protection,
  // the slower Austin reply could overwrite the Boston results.
  // Two defences are used together:
  //   AbortController - lets an older request be cancelled outright
  //   generation      - a counter bumped on each new request; when a reply
  //                     arrives, its generation is compared against the current
  //                     one and simply discarded if it is out of date
  // You will see this pattern repeated in every function that talks to the
  // server. It is the single most-repeated idea in this file.
  const state = {
    theme: 'dark',
    palette: 'aggieland',
    view: 'splash',
    accepted: null,
    epiTotal: null,
    results: [],
    resultMap: new Map(),
    comparison: [],
    inspected: null,
    inspectedId: null,
    inspectionPending: false,
    pendingInspectedId: null,
    page: 0,
    busy: false,
    generation: 0,
    detailGeneration: 0,
    queryController: null,
    detailController: null,
    previewController: null,
    previewGeneration: 0,
    lookupController: null,
    lookupGeneration: 0,
    selectedLocation: null,
    inspectLookupController: null,
    inspectLookupGeneration: 0,
    meta: null,
    ready: false,
    incompatible: false,
    map: null,
    layers: new Map(),
    sequence: 0,
  };

  // The five identity values baked into the page by app.py, read out of the
  // <meta> tags. Every request sends them back so the server can detect that
  // this page belongs to a different version than the one now running.
  const identityKeys = [
    'app-version',
    'schema-version',
    'calculation-version',
    'data-release',
    'release-contract',
  ];
  const loadedIdentity = Object.fromEntries(
    identityKeys.map((key) => [key, document.querySelector(`meta[name="cole-${key}"]`)?.content])
  );

  // How many counties appear on one page of results. The full national list can
  // be thousands of rows; rendering them all at once would be slow and
  // unreadable, so they are paged.
  const PAGE_SIZE = 30;

  // The six EPI cost categories, tying each to its label and its input box id.
  // This list MUST match the server's list - start() checks that explicitly and
  // refuses to run if they differ.
  const COST_COMPONENTS = [
    { key: 'housing', label: 'Housing', input: 'component-limit-housing' },
    { key: 'food', label: 'Food', input: 'component-limit-food' },
    { key: 'transportation', label: 'Transportation', input: 'component-limit-transportation' },
    { key: 'healthcare', label: 'Health care', input: 'component-limit-healthcare' },
    {
      key: 'other_necessities',
      label: 'Other necessities',
      input: 'component-limit-other-necessities',
    },
    { key: 'childcare', label: 'Child care', input: 'component-limit-childcare' },
  ];

  // The four optional temperature filters. `relation` supplies the wording used
  // in summaries ("Coldest-month average at least 40.0 °F").
  const TEMPERATURE_LIMITS = [
    {
      key: 'coldest_month_min',
      label: 'Coldest-month average',
      input: 'temperature-coldest-min',
      relation: 'at least',
    },
    {
      key: 'warmest_month_max',
      label: 'Warmest-month average',
      input: 'temperature-warmest-max',
      relation: 'at most',
    },
    {
      key: 'twelve_month_average_min',
      label: '12-month average temperature',
      input: 'temperature-average-min',
      relation: 'at least',
    },
    {
      key: 'twelve_month_average_max',
      label: '12-month average temperature',
      input: 'temperature-average-max',
      relation: 'at most',
    },
  ];
  const MONTH_LABELS = {
    january: 'Jan',
    february: 'Feb',
    march: 'Mar',
    april: 'Apr',
    may: 'May',
    june: 'Jun',
    july: 'Jul',
    august: 'Aug',
    september: 'Sep',
    october: 'Oct',
    november: 'Nov',
    december: 'Dec',
  };

  // Sort measures have no built-in direction. The separate Order control
  // applies to known values; missing data remains last in either direction.
  const RESULT_SORTS = {
    total: { path: ['combined_cost_annual'], help: 'Uses total annual cost, including extras and any personal category replacements.' },
    housing: { path: ['component_costs_annual', 'housing'], label: 'Housing', kind: 'epi' },
    food: { path: ['component_costs_annual', 'food'], label: 'Food', kind: 'epi' },
    transportation: { path: ['component_costs_annual', 'transportation'], label: 'Transportation', kind: 'epi' },
    healthcare: { path: ['component_costs_annual', 'healthcare'], label: 'Health care', kind: 'epi' },
    other_necessities: { path: ['component_costs_annual', 'other_necessities'], label: 'Other necessities', kind: 'epi' },
    childcare: { path: ['component_costs_annual', 'childcare'], label: 'Child care', kind: 'epi' },
    coldest: { path: ['climate', 'coldest_month_mean_f'], help: "Uses the coldest of the county's 12 long-term monthly average temperatures (°F)." },
    warmest: { path: ['climate', 'warmest_month_mean_f'], help: "Uses the warmest of the county's 12 long-term monthly average temperatures (°F)." },
    average: { path: ['climate', 'twelve_month_average_f'], help: "Uses the average of the county's 12 long-term monthly temperature averages (°F)." },
    name: { help: 'Orders by state abbreviation, then county name.' },
  };

  function updateSortSetting() {
    const select = $('result-sort');
    if (!select.value) select.value = 'total';
    const alphabetical = select.value === 'name';
    const order = $('result-order');
    if (!['asc', 'desc'].includes(order.value)) order.value = 'asc';
    order.querySelectorAll('option').forEach((option) => {
      option.textContent = alphabetical
        ? option.value === 'asc' ? 'A to Z' : 'Z to A'
        : option.value === 'asc' ? 'Lowest first' : 'Highest first';
    });
    const measure = RESULT_SORTS[select.value];
    const explanation = measure?.kind === 'epi'
      ? `Uses EPI's original ${measure.label.toLowerCase()} estimate, before your category replacements or extra expenses.`
      : measure?.help || select.selectedOptions?.[0]?.dataset.sortHelp || 'Orders the counties by the selected measure.';
    $('sort-help').textContent = `${explanation} Sorting changes list order only.${alphabetical ? '' : ' Missing values stay last.'}`;
  }

  // say() writes into a hidden element that screen readers announce aloud.
  // This keeps blind users informed of results arriving, pages changing, and
  // errors appearing.
  const say = (text) => {
    $('announcer').textContent = text;
  };

  // zero() treats blank, "0", "0.00" and "000" as all meaning "no amount".
  // Needed because a buffer of zero is inactive, but the box is rarely empty.
  const zero = (value) => !value.trim() || /^0+(\.0+)?$/.test(value.trim());
  const screenIds = {
    splash: 'splash-screen',
    how: 'how-screen',
    criteria: 'criteria-screen',
    results: 'results-screen',
  };
  const screenHeadingIds = {
    splash: 'splash-heading',
    how: 'how-heading',
    criteria: 'criteria-screen-heading',
    results: 'results-screen-heading',
  };

  // --- SCREEN NAVIGATION ---------------------------------------------------
  // COLE is a single page that shows one of four screens at a time.
  function syncScreenActions() {
    $('view-current-results').hidden = !state.accepted;
    $('view-current-results').disabled = state.busy || !state.accepted;
    $('criteria-back').disabled = state.busy;
  }

  // Leaflet maps break if they are sized while hidden: the map measures a
  // container of zero height and renders a grey box. invalidateSize() tells it
  // to re-measure once the screen is actually visible.
  // requestAnimationFrame waits until the browser has finished laying the page
  // out, which is the earliest moment the real size is known.
  function refreshVisibleMap(fitMap = false) {
    const refresh = () => {
      if (state.view !== 'results' || !state.map) return;
      state.map.invalidateSize({ pan: false });
      if (fitMap) {
        if (state.results.length) fitResults();
        else homeMap();
      }
    };
    if (typeof window.requestAnimationFrame === 'function') window.requestAnimationFrame(refresh);
    else refresh();
  }

  // Show one screen and hide the rest.
  // `inert` is the important part: a hidden screen must not merely be invisible,
  // it must be unreachable by keyboard. Without it, tabbing would walk
  // invisibly through the hidden screens' fields.
  // Moving focus to the new screen's heading is what tells a screen-reader user
  // that the page changed at all.
  function showScreen(view, { focus = true, fitMap = false } = {}) {
    if (!screenIds[view] || (view === 'results' && !state.accepted)) return false;
    if (view !== 'criteria') cancelPreview();
    state.view = view;
    Object.entries(screenIds).forEach(([name, id]) => {
      const inactive = name !== view,
        screen = $(id);
      screen.hidden = inactive;
      screen.inert = inactive;
    });
    syncScreenActions();
    if (typeof window.scrollTo === 'function')
      window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
    if (view === 'results') refreshVisibleMap(fitMap);
    if (view === 'criteria') schedulePreview();
    if (focus) $(screenHeadingIds[view]).focus({ preventScroll: true });
    return true;
  }

  // Going back to the form: cancel any in-flight detail request and bump the
  // generation so a late reply is ignored. Note what is NOT cleared - results,
  // comparisons and form entries all survive, which the announcement says
  // explicitly so the user is not left wondering.
  function returnToCriteria() {
    state.detailController?.abort();
    ++state.detailGeneration;
    discardPendingInspection();
    clearInspectLookup(false);
    $('map-status').textContent = '';
    showScreen('criteria');
    say('Criteria shown. Your entries and submitted results are preserved.');
  }

  // Wire up the "?" help buttons. Each remembers which button opened it so that
  // focus returns there on close - otherwise a keyboard user would be dumped
  // back at the top of the page.
  // The click handler comparing event.target to the dialog itself is the
  // standard way to detect a click on the backdrop rather than the contents.
  function setupHelpDialogs() {
    document.querySelectorAll('[data-dialog]').forEach((trigger) =>
      trigger.addEventListener('click', () => {
        const dialog = $(trigger.dataset.dialog);
        if (!dialog) return;
        dialog.returnFocus = trigger;
        dialog.showModal();
        dialog.scrollTop = 0;
        dialog.querySelector('[data-theme-option][aria-pressed="true"]')?.focus();
      })
    );
    document.querySelectorAll('.help-dialog').forEach((dialog) => {
      dialog.querySelector('.dialog-close')?.addEventListener('click', () => dialog.close());
      dialog.addEventListener('click', (event) => {
        if (event.target !== dialog) return;
        const bounds = dialog.getBoundingClientRect();
        if (
          event.clientX < bounds.left ||
          event.clientX > bounds.right ||
          event.clientY < bounds.top ||
          event.clientY > bounds.bottom
        )
          dialog.close();
      });
      dialog.addEventListener('close', () => {
        if (dialog.returnFocus && !dialog.returnFocus.disabled) dialog.returnFocus.focus();
      });
    });
  }

  // A replacement for the browser's built-in confirm(), which cannot be styled
  // and blocks the whole page. The dialog is created once on first use and
  // reused after that.
  // It returns a Promise, so callers can write `await confirmChoice(...)` and
  // read the answer as if it were an ordinary value.
  function confirmChoice(message, title = 'Confirm change') {
    let dialog = $('confirm-dialog');
    if (!dialog) {
      dialog = document.createElement('dialog');
      dialog.id = 'confirm-dialog';
      dialog.setAttribute('aria-labelledby', 'confirm-title');
      dialog.innerHTML =
        '<form method="dialog"><h2 id="confirm-title"></h2><p id="confirm-message"></p><div class="confirm-actions"><button class="button secondary" value="cancel" autofocus>Cancel</button><button class="button primary" value="confirm">Continue</button></div></form>';
      document.body.append(dialog);
    }
    $('confirm-title').textContent = title;
    $('confirm-message').textContent = message;
    dialog.returnValue = 'cancel';
    return new Promise((resolve) => {
      dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), {
        once: true,
      });
      dialog.showModal();
    });
  }

  // =========================================================================
  // api() - THE ONE PLACE THAT TALKS TO THE SERVER
  // =========================================================================
  // Every request in the app goes through here, which is what makes the version
  // checking and error handling consistent.
  //
  // It does five things in order:
  //   1. attaches the X-COLE-Expected-* identity headers
  //   2. sends the request (POST when there is a body, GET otherwise)
  //   3. turns a network failure into a readable sentence, preserving the
  //      reassurance that existing entries and results are kept
  //   4. CHECKS THE IDENTITY HEADERS THAT CAME BACK - if the server now reports
  //      a different version or data release, it stops everything and demands a
  //      reload rather than mixing results from two versions
  //   5. unpacks the error's `fields` list so the form can highlight the exact
  //      inputs that failed
  //
  // AbortError is deliberately re-thrown untouched: a cancelled request is not
  // a failure, it is a newer request superseding an older one.
  async function api(path, body, signal) {
    const options = { signal, headers: { 'Accept': 'application/json' } };
    for (const key of identityKeys)
      if (loadedIdentity[key]) options.headers[`X-COLE-Expected-${key}`] = loadedIdentity[key];
    if (body !== undefined) {
      options.method = 'POST';
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new Error(
        document.body?.dataset?.hosted === 'true'
          ? 'COLE cannot reach its server. Your entries and earlier results remain in this tab. Try again shortly.'
          : 'COLE cannot reach its local service. Your entries and earlier results remain in this tab. Restart COLE if its local service has stopped, then try again.'
      );
    }
    if (
      identityKeys.some(
        (key) =>
          !loadedIdentity[key] || response.headers.get(`X-COLE-${key}`) !== loadedIdentity[key]
      )
    ) {
      requireReload();
      throw new Error('The application or data release changed. Reload COLE before continuing.');
    }
    let result;
    try {
      result = await response.json();
    } catch {
      throw new Error('COLE returned an unreadable response. Try again.');
    }
    if (!response.ok) {
      const error = new Error(result.error?.message || 'COLE could not complete the request.');
      error.fields = result.error?.fields || [];
      throw error;
    }
    return result;
  }

  // The point of no return. Called when the app or data has changed underneath
  // this page. It cancels everything in flight, bumps every generation counter
  // so no late reply can land, disables the controls, and explains the
  // situation - including that any results still on screen belong to the OLD
  // version, which is a detail an honest tool has to state.
  function requireReload() {
    state.incompatible = true;
    state.ready = false;
    cancelPreview();
    state.queryController?.abort();
    state.detailController?.abort();
    state.lookupController?.abort();
    state.inspectLookupController?.abort();
    ++state.generation;
    ++state.detailGeneration;
    ++state.lookupGeneration;
    ++state.inspectLookupGeneration;
    discardPendingInspection();
    $('lookup-options').replaceChildren();
    clearInspectLookup(false);
    $('map-status').textContent = '';
    setBusy(false);
    renderResults();
    $('startup-status').hidden = false;
    $('startup-status').classList.add('error');
    $('startup-status').textContent =
      'This window loaded a different version of COLE or its data. Reload the application to continue. Any results shown belong to the previously loaded version.';
    $('reload').hidden = false;
    $('quit').disabled = true;
    say('Application changed. Reload is required before continuing.');
  }

  // --- BUILDING THE FORM ---------------------------------------------------
  // Earner rows and buffer cards are created in JavaScript rather than written
  // into the HTML, because their number changes as the user adds and removes
  // earners.
  //
  // Note "Per year" and "Per month" only. The hourly option the server supports
  // is deliberately not offered here: COLE asks for TAKE-HOME pay, and an hourly
  // wage is a pre-tax figure, so accepting it would invite exactly the mistake
  // the income help text warns against.
  function periodOptions() {
    return '<option value="annual">Per year</option><option value="monthly">Per month</option>';
  }

  // Build one labelled input as HTML text. Everything variable is passed through
  // esc() first - the same escaping rule as everywhere else.
  function field(label, key, id, value = '', options = {}) {
    const placeholder = options.placeholder ? ` placeholder="${esc(options.placeholder)}"` : '';
    const describedBy = options.describedBy
      ? ` aria-describedby="${esc(options.describedBy)}"`
      : '';
    const required = options.required ? ' aria-required="true"' : '';
    const incomeLabel = options.incomeLabel ? ` data-income-label="${esc(key)}"` : '';
    return `<div class="field"><label for="${id}"${incomeLabel}>${label}</label><input id="${id}" data-key="${key}" type="text" inputmode="decimal" value="${value}" autocomplete="off"${placeholder}${describedBy}${required}></div>`;
  }

  // Add one earner row. `state.sequence` only ever increases, so ids stay unique
  // even after rows are removed and added again.
  // role="group" plus aria-labelledby is what makes a screen reader announce
  // "Earner 2" when entering the row, rather than reading two unlabelled boxes.
  function addEarner() {
    const previousPeriod = $('earners').lastElementChild?.querySelector('[data-key="period"]').value;
    const id = `earner-${++state.sequence}`;
    const card = document.createElement('div');
    card.className = 'earner-card';
    card.setAttribute('role', 'group');
    card.setAttribute('aria-labelledby', id + '-label');
    card.innerHTML = `<div class="earner-top"><strong class="earner-label" id="${id}-label"></strong><button class="button quiet remove-earner" type="button">Remove</button></div>
      <div class="field"><label for="${id}-period">Take-home total period</label><select id="${id}-period" data-key="period" data-previous="annual" aria-describedby="take-home-help">${periodOptions()}</select></div>
      <div class="field-grid">${field('Lower take-home pay <span class="optional">optional range</span>', 'minimum', id + '-min', '', { placeholder: 'Leave blank for fixed pay', describedBy: 'take-home-help income-bound-setting', incomeLabel: true })}${field('Take-home pay (or range maximum) <span class="required-note">required</span>', 'maximum', id + '-max', '', { placeholder: 'Required', describedBy: 'take-home-help income-bound-setting', required: true, incomeLabel: true })}</div>`;
    const period = card.querySelector('[data-key="period"]');
    period.value = previousPeriod || 'annual';
    period.dataset.previous = period.value;
    $('earners').append(card);
    labelEarners();
    return card;
  }

  // Renumber the earner rows after any addition or removal. Three jobs:
  // visible labels, the Remove button (disabled when only one row is left, since
  // financial mode needs at least one), and the data-field paths that let server
  // error messages point at the right box - "earners[1].maximum" must match the
  // row that is now second.
  function labelEarners() {
    [...$('earners').children].forEach((card, index, array) => {
      card.querySelector('.earner-label').textContent = `Earner ${index + 1}`;
      card.querySelector('.remove-earner').disabled = array.length === 1;
      card.querySelector('.remove-earner').setAttribute('aria-label', `Remove earner ${index + 1}`);
      card.querySelectorAll('[data-key]').forEach((input) => {
        input.dataset.field = `earners[${index}].${input.dataset.key}`;
      });
    });
  }
  const LOWER_BUFFER_ACTIVE_HELP =
    'Also include costs below your minimum.';
  const LOWER_BUFFER_INACTIVE_HELP =
    'Only used with a pay range. Enter a lower pay amount for each earner to use it.';
  const LOWER_BUFFER_CLEAR_HELP =
    'Enter 0 here, or add a lower pay amount for every earner.';

  // Which kind of income search is the DRAFT form currently describing?
  //   upper_only - no lower amounts entered; search everything up to the ceiling
  //   bounded    - every earner has a lower amount; search a range
  //   mixed      - some but not all; not a valid state, and the user is told so
  // This mirrors the all-or-nothing rule enforced in calculations.py. Checking
  // it here too means the user gets told immediately rather than on submit.
  function draftIncomeRangeMode() {
    const cards = [...$('earners').children];
    const supplied = cards.filter((card) => Boolean(value(card, 'minimum'))).length;
    return supplied === 0 ? 'upper_only' : supplied === cards.length ? 'bounded' : 'mixed';
  }

  function reserveRequested() {
    return context() === 'financial' && $('reserve-percent').value.trim() !== '';
  }

  // Keep the live "here is what your income search currently means" sentence in
  // step with the form, and enable or disable the lower buffer to match.
  //
  // The lower buffer extends the search BELOW the household minimum - so with no
  // minimum entered it has nothing to extend below. Three cases are handled
  // distinctly, which is why there are three help strings above:
  //   inactive   - no minimum and the buffer is already zero: grey it out
  //   needsClear - no minimum but an amount IS entered: flag it, because
  //                silently ignoring a number someone typed would be wrong
  //   active     - normal operation
  function updateIncomeBoundSetting() {
    const cards = [...$('earners').children],
      rangeMode = draftIncomeRangeMode();
    cards.forEach((card) => {
      const period = value(card, 'period') === 'monthly' ? 'month' : 'year';
      card.querySelector('[data-income-label="minimum"]').innerHTML =
        `Lower take-home pay per ${period} <span class="optional">optional range</span>`;
      card.querySelector('[data-income-label="maximum"]').innerHTML =
        `Take-home pay per ${period} (or range maximum) <span class="required-note">required</span>`;
    });
    const missingUpper = cards.filter((card) => !value(card, 'maximum')).length;
    const upperInstruction = missingUpper
      ? ` Enter the required take-home pay for ${missingUpper === cards.length ? 'each earner' : `${missingUpper} ${missingUpper === 1 ? 'earner' : 'earners'}`}.`
      : '';
    const descriptions = {
      upper_only:
        "Before widening: show costs up to your total take-home pay.",
      bounded:
        "Before widening: show costs between your lower and upper pay amounts. Cheaper counties below that range are excluded.",
      mixed:
        'For a pay range, enter a lower amount for every earner. Otherwise leave all lower amounts blank.',
    };
    const reserve = reserveRequested();
    $('income-bound-setting').textContent =
      reserve && rangeMode !== 'mixed'
        ? `Goal for money left: keep at least ${$('reserve-percent').value.trim()}% of ${rangeMode === 'bounded' ? 'the lower' : 'the upper'} household take-home amount after estimated costs and extras. Cheaper counties can match. Widened cost searching is off.${upperInstruction}`
        : `${descriptions[rangeMode]}${upperInstruction}`;
    const upperCard = document.querySelector('.buffer-card[data-side="upper"]');
    upperCard?.querySelectorAll('[data-key]').forEach((input) => {
      input.disabled = reserve;
    });

    const card = document.querySelector('.buffer-card[data-side="lower"]');
    if (!card) return rangeMode;
    const amount = value(card, 'amount');
    const inactive = reserve || (rangeMode === 'upper_only' && zero(amount));
    const needsClear = !reserve && rangeMode === 'upper_only' && !zero(amount);
    card.classList.toggle('inactive', inactive);
    card.classList.toggle('needs-attention', needsClear);
    if (inactive) card.setAttribute('aria-disabled', 'true');
    else card.removeAttribute('aria-disabled');
    card.querySelectorAll('[data-key]').forEach((input) => {
      input.disabled = inactive;
    });
    const help = $('buffer-lower-help');
    help.textContent = reserve
      ? 'Widened cost searching is off while a goal for money left is set.'
      : inactive
        ? LOWER_BUFFER_INACTIVE_HELP
        : needsClear
          ? LOWER_BUFFER_CLEAR_HELP
          : LOWER_BUFFER_ACTIVE_HELP;
    return rangeMode;
  }

  // Build the two buffer cards. Written as one template per side rather than
  // duplicated markup, so the two stay in step.
  function createBuffers() {
    $('buffers').innerHTML = ['lower', 'upper']
      .map((side, index) => {
        const lower = index === 0;
        const heading = lower ? 'Include costs below my minimum' : 'Include costs above my maximum';
        const amountLabel = lower ? 'Amount below your minimum' : 'Amount above your maximum';
        const amountHelp = lower
          ? LOWER_BUFFER_ACTIVE_HELP
          : 'These costs may be higher than your take-home pay.';
        return `<div class="buffer-card" data-side="${side}" role="group" aria-labelledby="buffer-${side}-label" aria-describedby="buffer-${side}-help"><h5 id="buffer-${side}-label">${heading}</h5>
        <div class="field"><label for="buffer-${side}-amount">${amountLabel}</label><div class="money-input"><span aria-hidden="true">$</span><input id="buffer-${side}-amount" data-key="amount" type="text" inputmode="decimal" value="0" autocomplete="off" aria-describedby="buffer-${side}-help"></div><p id="buffer-${side}-help" class="hint">${amountHelp}</p></div><div class="field"><label for="buffer-${side}-period">Amount is per</label><select id="buffer-${side}-period" data-key="period" data-previous="annual">${periodOptions()}</select></div></div>`;
      })
      .join('');
    document.querySelectorAll('.buffer-card').forEach((card) =>
      card.querySelectorAll('[data-key]').forEach((input) => {
        input.dataset.field = `buffers.${card.dataset.side}.${input.dataset.key}`;
      })
    );
    updateIncomeBoundSetting();
    updateBufferSetting();
  }

  // Keep the buffers' plain-English summary current. Also re-validates the
  // amounts as the user types, which is why the same pattern as the server's
  // appears here - immediate feedback, with the server still the final word.
  function updateBufferSetting() {
    if (reserveRequested()) {
      $('buffer-setting').textContent =
        'Widened cost searching is off. Clear the goal for money left to use it again.';
      $('buffer-group-summary').textContent = 'Off with goal for money left';
      return;
    }
    const periodLabel = { annual: 'per year', monthly: 'per month' };
    const describe = (side, label, direction) => {
      const card = document.querySelector(`.buffer-card[data-side="${side}"]`);
      if (!card) return `No ${label} allowance`;
      const amount = value(card, 'amount');
      if (zero(amount)) return `No ${label} allowance`;
      if (!/^\d{1,30}(?:\.\d{1,2})?$/.test(amount))
        return `${label[0].toUpperCase() + label.slice(1)} amount must be nonnegative with no more than two decimal places`;
      return `${money(amount, 2)} ${periodLabel[value(card, 'period')]} ${direction}`;
    };
    const rangeMode = draftIncomeRangeMode();
    const lowerCard = document.querySelector('.buffer-card[data-side="lower"]');
    const lowerAmount = lowerCard ? value(lowerCard, 'amount') : '0';
    const lowerNeedsClear = rangeMode === 'upper_only' && !zero(lowerAmount);
    const lowerDescription =
      rangeMode === 'upper_only'
        ? lowerNeedsClear
          ? 'Enter a lower pay amount first'
          : 'No minimum pay entered; lower-cost option is off'
        : describe('lower', 'lower-cost', 'below your minimum');
    const descriptions = [lowerDescription, describe('upper', 'higher-cost', 'above your maximum')];
    const invalid = descriptions.some((description) =>
      description.includes(' must be nonnegative')
    );
    $('buffer-setting').textContent =
      `Search allowance: ${descriptions.join('; ')}. ${invalid ? 'Correct the amount before submitting.' : lowerNeedsClear ? 'Clear the lower-cost allowance to $0, or enter a lower take-home amount for every earner, before submitting.' : 'Submit to apply.'}`;
    const activeCount = ['lower', 'upper'].filter((side) => {
      const card = document.querySelector(`.buffer-card[data-side="${side}"]`);
      return card && !zero(value(card, 'amount'));
    }).length;
    $('buffer-group-summary').textContent =
      invalid || lowerNeedsClear
        ? 'Needs attention'
        : activeCount
          ? `${activeCount} active`
          : 'None active';
  }

  // --- READING THE FORM ----------------------------------------------------
  // Note `amount || null`: an empty box becomes null, meaning "no limit", NOT
  // zero. This is the blank-versus-zero distinction from calculations.py, and
  // the browser has to honour it too. A typed 0 stays 0 - an active
  // zero-dollar limit.
  function readComponentLimits() {
    return Object.fromEntries(
      COST_COMPONENTS.map((component) => {
        const amount = $(component.input).value.trim();
        return [component.key, amount || null];
      })
    );
  }

  function costMode() {
    return document.querySelector('[name="cost_mode"]:checked').value;
  }
  function readCustomCosts() {
    return Object.fromEntries(
      COST_COMPONENTS.map((component) => [
        component.key,
        costMode() === 'custom'
          ? $(`custom-${component.key.replaceAll('_', '-')}`).value.trim() || null
          : null,
      ])
    );
  }
  function updateSimpleBudgetSetting() {
    const extras = $('extras').value.trim();
    $('simple-budget-summary').textContent =
      extras && !/^\d{1,30}(?:\.\d{1,2})?$/.test(extras)
        ? 'Needs attention'
        : zero(extras)
          ? 'No extras'
          : 'Extras added';
  }
  function updateCustomBudgetSetting() {
    const custom = costMode() === 'custom';
    $('custom-budget-fields').hidden = !custom;
    const count = Object.values(readCustomCosts()).filter((value) => value !== null).length;
    $('custom-budget-summary').textContent = custom
      ? `${count} ${count === 1 ? 'cost' : 'costs'} replaced`
      : 'Published estimate';
  }

  // Which limits were actually set in a given scenario? Used to decide which
  // badges and summary lines to show.
  function activeComponentLimits(scenario = state.accepted) {
    const limits = scenario?.component_limits_monthly || {};
    return COST_COMPONENTS.filter(
      (component) => limits[component.key] !== null && limits[component.key] !== undefined
    );
  }

  // Live validation and summary for the cost limits, mirroring the server's
  // rules so mistakes surface while typing.
  function updateComponentLimitSetting() {
    const limits = readComponentLimits(),
      active = [],
      invalid = [];
    COST_COMPONENTS.forEach((component) => {
      const amount = limits[component.key];
      if (amount === null) return;
      if (!/^\d{1,30}(?:\.\d{1,2})?$/.test(amount)) invalid.push(component.label);
      else active.push(`${component.label} at or below ${money(amount, 2)} per month`);
    });
    const current = active.length ? active.join('; ') : 'no monthly EPI category limits';
    $('component-limit-setting').textContent =
      `Current setting: ${current}. ${invalid.length ? `Correct ${invalid.join(', ')} before submitting.` : 'Matching counties must meet every entered limit. Submit the search to apply your changes.'}`;
    $('component-group-summary').textContent = invalid.length
      ? 'Needs attention'
      : active.length
        ? `${active.length} active`
        : 'None active';
  }
  function readTemperatureLimits() {
    return Object.fromEntries(
      TEMPERATURE_LIMITS.map((limit) => {
        const amount = $(limit.input).value.trim();
        return [limit.key, amount || null];
      })
    );
  }
  function activeTemperatureLimits(scenario = state.accepted) {
    const limits = scenario?.temperature_limits_f || {};
    return TEMPERATURE_LIMITS.filter(
      (limit) => limits[limit.key] !== null && limits[limit.key] !== undefined
    );
  }

  // The temperature equivalent, plus the contradiction check: the same four
  // impossible pairings that calculations.py rejects (coldest month warmer than
  // warmest month, and so on). Catching them here means the user is told the
  // filters conflict, rather than being left to puzzle over an empty result
  // list.
  function updateTemperatureLimitSetting() {
    const limits = readTemperatureLimits(),
      active = [],
      invalid = [];
    TEMPERATURE_LIMITS.forEach((limit) => {
      const amount = limits[limit.key];
      if (amount === null) return;
      if (!/^-?\d{1,3}(?:\.\d)?$/.test(amount) || Number(amount) < -100 || Number(amount) > 150)
        invalid.push(limit.label);
      else active.push(`${limit.label} ${limit.relation} ${degrees(amount)}`);
    });
    const pairs = [
      ['coldest_month_min', 'warmest_month_max'],
      ['twelve_month_average_min', 'twelve_month_average_max'],
      ['coldest_month_min', 'twelve_month_average_max'],
      ['twelve_month_average_min', 'warmest_month_max'],
    ];
    const impossible =
      !invalid.length &&
      pairs.some(
        ([minimum, maximum]) =>
          limits[minimum] !== null &&
          limits[maximum] !== null &&
          Number(limits[minimum]) > Number(limits[maximum])
      );
    const current = active.length ? active.join('; ') : 'no temperature limits';
    $('temperature-limit-setting').textContent =
      `Current setting: ${current}. ${invalid.length ? `Correct ${invalid.join(', ')}; use -100 through 150 with one decimal place at most.` : impossible ? 'The entered temperature minimums and maximums conflict.' : 'Matching counties must meet every entered limit. Submit the search to apply your changes.'}`;
    $('temperature-group-summary').textContent =
      invalid.length || impossible
        ? 'Needs attention'
        : active.length
          ? `${active.length} active`
          : 'None active';
  }

  // Read one named input inside a card, trimmed of stray spaces.
  const value = (card, key) => card.querySelector(`[data-key="${key}"]`).value.trim();

  // Read a whole row into the shape the server expects.
  // schedule is always null because this interface never offers hourly pay -
  // see the note at periodOptions().
  function readRow(card, buffer = false) {
    const period = value(card, 'period');
    const result = buffer
      ? { amount: value(card, 'amount'), period }
      : { minimum: value(card, 'minimum'), maximum: value(card, 'maximum'), period };
    result.schedule = null;
    return result;
  }

  // Which search mode, and which income context? In discovery mode the context
  // is forced to financial, because discovery without income has no criterion
  // to search against.
  function mode() {
    return document.querySelector('[name="query_kind"]:checked').value;
  }
  function context() {
    return mode() === 'discovery'
      ? 'financial'
      : document.querySelector('[name="income_context"]:checked').value;
  }

  // Assemble the entire form into one scenario object - the exact shape
  // normalize_scenario() in calculations.py expects. This is the boundary
  // between "what is on screen" and "what gets sent".
  function readScenario() {
    const financial = context() === 'financial';
    const scenario = {
      query_kind: mode(),
      income_context: context(),
      household: {
        adults: $('adults').value ? Number($('adults').value) : null,
        children: $('children').value ? Number($('children').value) : null,
      },
      earners: financial ? [...$('earners').children].map((card) => readRow(card)) : null,
      reserve_percent: financial ? $('reserve-percent').value.trim() || null : null,
      buffers: financial
        ? reserveRequested()
          ? {
              lower: { amount: '0', period: 'annual', schedule: null },
              upper: { amount: '0', period: 'annual', schedule: null },
            }
          : Object.fromEntries(
              [...$('buffers').children].map((card) => [card.dataset.side, readRow(card, true)])
            )
        : null,
      component_limits_monthly: readComponentLimits(),
      cost_mode: costMode(),
      custom_costs_monthly: readCustomCosts(),
      custom_scope_acknowledged: costMode() === 'custom' && $('custom-scope-acknowledged').checked,
      temperature_limits_f: readTemperatureLimits(),
      extras_monthly: $('extras').value.trim(),
      state_filter: $('state-filter').value || null,
      lookup:
        mode() === 'lookup' && state.selectedLocation
          ? { kind: state.selectedLocation.kind, id: state.selectedLocation.id }
          : null,
    };
    return window.ColeFeatures?.readScenario(scenario) ?? scenario;
  }

  // -Team
  // I want people to see a live count while they enter pay, but their draft
  // must never replace results they already submitted. Wait briefly after
  // typing, cancel the older check, and ignore any late reply for an old draft.
  // -Brandon
  let previewTimer = null;
  let rejectedPreviewDraft = null;
  function cancelPreview() {
    if (previewTimer !== null) clearTimeout(previewTimer);
    previewTimer = null;
    state.previewController?.abort();
    state.previewController = null;
    ++state.previewGeneration;
  }
  function schedulePreview() {
    cancelPreview();
    const panel = $('live-preview'),
      status = $('live-preview-status');
    panel.hidden = mode() !== 'discovery';
    panel.classList.remove('zero');
    if (panel.hidden) return;
    if (!state.ready || state.busy || state.incompatible) {
      status.textContent =
        'Preview is unavailable while COLE is applying a search or loading data.';
      return;
    }
    const scenario = readScenario();
    const snapshot = JSON.stringify(scenario);
    if (snapshot === rejectedPreviewDraft) {
      status.textContent = 'Check your entries before the preview can count matches.';
      return;
    }
    if (
      !scenario.household.adults ||
      scenario.household.children === null ||
      scenario.earners.some((row) => !row.maximum)
    ) {
      status.textContent =
        'Choose a household and enter the required upper take-home amount to see a match count.';
      return;
    }
    const moneyEntries = [
      ...scenario.earners.flatMap((row) => [row.minimum, row.maximum]),
      scenario.extras_monthly,
      ...Object.values(scenario.buffers).map((row) => row.amount),
      ...Object.values(scenario.component_limits_monthly),
      ...Object.values(scenario.custom_costs_monthly),
    ];
    if (moneyEntries.some((entry) => typeof entry === 'string' && entry.includes(','))) {
      status.textContent = 'Enter money without commas: $75,000 is 75000.';
      return;
    }
    status.textContent = 'Checking the current household, pay, and filters…';
    const generation = state.previewGeneration;
    previewTimer = setTimeout(async () => {
      previewTimer = null;
      state.previewController = new AbortController();
      try {
        const data = await api('/api/preview', scenario, state.previewController.signal);
        if (generation !== state.previewGeneration || snapshot !== JSON.stringify(readScenario()))
          return;
        const total = data.total;
        panel.classList.toggle('zero', total === 0);
        const basis =
          scenario.cost_mode === 'custom' ? 'your household budget' : 'the published estimate';
        status.textContent =
          total === 0
            ? `0 counties match this draft using ${basis}. ${scenario.cost_mode === 'custom' ? `${data.epi_total.toLocaleString()} match using published costs under the same filters. ` : ''}Estimated costs or another limit may exclude counties. Submit the search to inspect a county.`
            : `${total.toLocaleString()} ${total === 1 ? 'county matches' : 'counties match'} using ${basis}${scenario.cost_mode === 'custom' ? `; ${data.epi_total.toLocaleString()} using published costs` : ''}. Submit this search to view the results.`;
      } catch (error) {
        if (error.name !== 'AbortError' && generation === state.previewGeneration) {
          if (error.fields?.length) rejectedPreviewDraft = snapshot;
          status.textContent = error.fields?.length
            ? 'Check your entries before the preview can count matches.'
            : error.message;
        }
      }
    }, 400);
  }

  // A JSON snapshot of the scenario that produced the current results. Compared
  // against the live form to detect unapplied edits.
  let submittedDraft = null;

  // THE DRAFT/ACCEPTED COMPARISON.
  // Serialising both scenarios to JSON and comparing the text is a simple,
  // reliable way to ask "has anything changed?" without checking field by field.
  // When they differ, the "unapplied changes" note appears and the summary is
  // marked stale - so the numbers on screen are never silently mismatched with
  // the form above them.
  function markDraft(action = '') {
    const draftNote = $('draft-note'),
      wasDirty = !draftNote.hidden;
    const dirty = Boolean(state.accepted && JSON.stringify(readScenario()) !== submittedDraft);
    draftNote.hidden = !dirty;
    $('results-draft-note').hidden = !dirty;
    $('accepted-summary').classList.toggle('stale', dirty);
    const transition =
      dirty && !wasDirty
        ? 'Changes not applied. Displayed results still use your previous submission.'
        : !dirty && wasDirty
          ? 'Changes now match the displayed results.'
          : '';
    if (action || transition) say([action, transition].filter(Boolean).join(' '));
    if (state.view === 'criteria') schedulePreview();
  }

  // Show or hide sections to match the chosen mode, and renumber the visible
  // steps - the income section is step 2 in discovery but step 3 in lookup.
  function updateMode() {
    updateSimpleBudgetSetting();
    updateCustomBudgetSetting();
    $('lookup-section').hidden = mode() !== 'lookup';
    $('financial-section').hidden = context() !== 'financial';
    $('scenario-form').classList.toggle('costs-only', context() !== 'financial');
    $('buffer-section').hidden = context() !== 'financial';
    $('submit').textContent = state.busy
      ? 'Applying…'
      : mode() === 'lookup'
        ? 'Show counties for this location'
        : 'Find counties';
    $('income-step').textContent = mode() === 'lookup' ? '3' : '2';
    $('options-step').textContent =
      mode() === 'lookup' ? (context() === 'financial' ? '4' : '3') : '3';
    $('lookup-search').disabled = !state.ready;
    updateIncomeBoundSetting();
    updateBufferSetting();
  }

  // Changing "per year" to "per month" would silently reinterpret an entered
  // number - $3,000 a year is very different from $3,000 a month. So if the row
  // holds a meaningful amount, confirm first and then clear it.
  // The value is put back to `prior` while the dialog is open so the dropdown
  // does not appear changed before the user has agreed.
  async function changePeriod(input) {
    const card = input.closest('.earner-card,.buffer-card');
    const prior = input.dataset.previous,
      next = input.value;
    const meaningful = card.classList.contains('earner-card')
      ? Boolean(value(card, 'minimum') || value(card, 'maximum'))
      : !zero(value(card, 'amount'));
    if (meaningful) {
      input.value = prior;
      if (!(await confirmChoice('Changing the period clears this row’s amounts. Continue?')))
        return;
      input.value = next;
    }
    if (meaningful)
      card
        .querySelectorAll('input')
        .forEach((el) => (el.value = el.dataset.key === 'amount' ? '0' : ''));
    input.dataset.previous = input.value;
  }

  // --- ERROR DISPLAY -------------------------------------------------------
  // Turn a server error into something actionable. The server reports failures
  // by field path ("buffers.lower.amount"); this maps each path back to the
  // actual input, marks it aria-invalid, opens the collapsed section containing
  // it, and links the input to its message via aria-describedby so a screen
  // reader announces the reason on focus.
  // It also expands group paths into friendly names, so "earners" is announced
  // as "Income entries" rather than as a variable name.
  function showError(error, markFields = true) {
    clearErrors();
    const matches = (field) =>
      [...document.querySelectorAll('[data-field]')].filter(
        (input) => input.dataset.field === field || input.dataset.field.startsWith(field + '.')
      );
    const fieldLabel = (field) => {
      const groupNames = {
        scenario: 'Criteria',
        household: 'Household',
        earners: 'Income entries',
        buffers: 'Widened cost searching',
        component_limits_monthly: 'Limits on monthly estimates',
        custom_costs_monthly: 'Your monthly costs',
        custom_scope_acknowledged: 'Where my costs apply',
        cost_mode: 'Cost basis',
        temperature_limits_f: 'Typical temperatures',
        query_kind: 'Search mode',
        lookup: 'Location',
        request: 'Request',
        comparison_county_ids: 'Comparison counties',
        inspected_county_id: 'County selection',
      };
      if (groupNames[field]) return groupNames[field];
      const input = matches(field)[0];
      if (input) {
        const label = input.labels?.[0]?.textContent.trim() || 'Value';
        const group = input.closest('[role="group"]');
        const groupLabel = group && $(group.getAttribute('aria-labelledby'))?.textContent;
        return groupLabel ? `${groupLabel}: ${label}` : label;
      }
      return 'Criteria';
    };
    $('form-errors').innerHTML =
      `<strong>${esc(error.message)}</strong>` +
      (error.fields?.length
        ? `<ul>${error.fields.map((item, index) => `<li id="field-error-${index}">${esc(fieldLabel(item.field))}: ${esc(item.message)}</li>`).join('')}</ul>`
        : '');
    if (markFields)
      (error.fields || []).forEach((item, index) =>
        matches(item.field).forEach((input) => {
          const group = input.closest?.('.criteria-option-group');
          if (group) group.open = true;
          input.setAttribute('aria-invalid', 'true');
          input.setAttribute(
            'aria-describedby',
            `${input.getAttribute('aria-describedby') || ''} field-error-${index}`.trim()
          );
        })
      );
    $('form-errors').hidden = false;
    $('form-errors').focus();
  }

  // Remove all error marks. Note it only strips the ids it added
  // (field-error-*), leaving any permanent help-text links intact.
  function clearErrors() {
    $('form-errors').hidden = true;
    $('form-errors').replaceChildren();
    document
      .querySelectorAll('[aria-invalid]')
      .forEach((input) => input.removeAttribute('aria-invalid'));
    document.querySelectorAll('[aria-describedby]').forEach((input) => {
      const ids = input
        .getAttribute('aria-describedby')
        .split(/\s+/)
        .filter((id) => !id.startsWith('field-error-'));
      if (ids.length) input.setAttribute('aria-describedby', ids.join(' '));
      else input.removeAttribute('aria-describedby');
    });
  }

  // =========================================================================
  // LOCATION SEARCH
  // =========================================================================
  // The generation/AbortController pattern in full: cancel the previous search,
  // claim a new generation number, and on return discard the reply if a newer
  // search has since started. Without this, typing quickly could leave the
  // results of an earlier keystroke on screen.
  // encodeURIComponent() makes the typed text safe to put in a URL.
  async function findLocations() {
    if (!state.ready) return;
    state.lookupController?.abort();
    const generation = ++state.lookupGeneration;
    state.lookupController = new AbortController();
    const kind = $('lookup-kind').value;
    const query = $('lookup-text').value.trim();
    $('lookup-options').replaceChildren();
    $('lookup-status').textContent = query
      ? 'Searching local locations…'
      : 'Enter a location name or code.';
    if (!query) return;
    try {
      const data = await api(
        `/api/locations?kind=${encodeURIComponent(kind)}&q=${encodeURIComponent(query)}`,
        undefined,
        state.lookupController.signal
      );
      if (generation !== state.lookupGeneration) return;
      $('lookup-status').textContent = data.total
        ? `${data.total.toLocaleString()} ${data.total === 1 ? 'location' : 'locations'} found.${data.truncated ? ' Showing the first 60. Add a state or a more specific name to narrow the search.' : ' Choose one.'}`
        : 'No covered location found. Check the selected search type, name or code.';
      data.items.forEach((item) => {
        const li = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'lookup-option';
        button.textContent = `${item.name}, ${item.state_abbr} · ${item.id} · ${item.county_fips.length} ${item.county_fips.length === 1 ? 'county' : 'counties'}`;
        button.addEventListener('click', () => {
          state.selectedLocation = { ...item, kind };
          $('lookup-selected').textContent = `Selected: ${button.textContent}`;
          $('lookup-selected').hidden = false;
          $('lookup-options').replaceChildren();
          $('lookup-status').textContent = 'Location selected. Submit to apply it.';
          $('lookup-selected').tabIndex = -1;
          $('lookup-selected').focus({ preventScroll: true });
          markDraft();
        });
        li.append(button);
        $('lookup-options').append(li);
      });
    } catch (error) {
      if (error.name !== 'AbortError' && generation === state.lookupGeneration)
        $('lookup-status').textContent = error.message;
    }
  }

  // Clear the chosen location. Called whenever the search text or kind changes,
  // so a stale selection cannot be submitted with a different search visible.
  function clearLocation() {
    state.lookupController?.abort();
    ++state.lookupGeneration;
    state.selectedLocation = null;
    $('lookup-options').replaceChildren();
    $('lookup-selected').hidden = true;
    $('lookup-selected').textContent = '';
    $('lookup-status').textContent = '';
    markDraft();
  }

  // --- INSPECTION SEARCH ---------------------------------------------------
  // A separate search that lets the user pull up ANY covered county, including
  // one outside their current results. It has its own controller and generation
  // so it cannot interfere with the location search above.
  function clearInspectLookup(clearText = true) {
    state.inspectLookupController?.abort();
    ++state.inspectLookupGeneration;
    $('inspect-search-options').replaceChildren();
    $('inspect-search-status').textContent = '';
    if (clearText) $('inspect-search-text').value = '';
  }
  function updateInspectLookupAvailability() {
    const disabled = !state.ready || !state.accepted || state.busy;
    $('inspect-search-text').disabled = disabled;
    $('inspect-search').disabled = disabled;
  }

  // Same shape as findLocations, deliberately: same cancellation rules, same
  // truncation message, same error handling.
  async function findInspectableCounties() {
    if (!state.ready || !state.accepted || state.busy) return;
    state.inspectLookupController?.abort();
    const generation = ++state.inspectLookupGeneration;
    state.inspectLookupController = new AbortController();
    const query = $('inspect-search-text').value.trim();
    $('inspect-search-options').replaceChildren();
    $('inspect-search-status').textContent = query
      ? 'Searching covered counties…'
      : 'Enter a county name or five-digit county code (FIPS).';
    if (!query) return;
    try {
      const data = await api(
        `/api/locations?kind=county&q=${encodeURIComponent(query)}`,
        undefined,
        state.inspectLookupController.signal
      );
      if (generation !== state.inspectLookupGeneration) return;
      $('inspect-search-status').textContent = data.total
        ? `${data.total.toLocaleString()} ${data.total === 1 ? 'county' : 'counties'} found.${data.truncated ? ' Showing the first 60. Add a state or a more specific name to narrow the search.' : ' Choose one to inspect it.'}`
        : 'No county with EPI cost data found. Try a county name, state abbreviation, or five-digit county code (FIPS).';
      data.items.forEach((item) => {
        const li = document.createElement('li'),
          button = document.createElement('button');
        button.type = 'button';
        button.className = 'lookup-option';
        button.textContent = `${item.name}, ${item.state_abbr} · ${item.id}`;
        button.addEventListener('click', () => {
          clearInspectLookup();
          inspect(item.id);
        });
        li.append(button);
        $('inspect-search-options').append(li);
      });
    } catch (error) {
      if (error.name !== 'AbortError' && generation === state.inspectLookupGeneration)
        $('inspect-search-status').textContent = error.message;
    }
  }

  // =========================================================================
  // RENDERING - turning `state` into what is on screen
  // =========================================================================
  // The plain-English verdict for one county. Note how the wording changes for
  // an upper-only search: "Above upper take-home amount" rather than "Above
  // take-home range", because there is no range to be above.
  function statusText(row) {
    if (state.accepted?.reserve_goal && row.financial_match !== null)
      return row.financial_match
        ? `Estimated costs leave at least ${state.accepted.reserve_goal.percent}% at the ${state.accepted.reserve_goal.income_basis} take-home amount`
        : `Estimated costs do not meet the ${state.accepted.reserve_goal.percent}% reserve target`;
    const upperOnly = state.accepted?.income_range_mode === 'upper_only';
    if (row.financial_status === 'estimated_cost' && state.accepted?.cost_mode === 'custom')
      return 'Personal estimated costs + extras';
    if (row.financial_status === 'above_buffer')
      return `${upperOnly ? 'Costs above upper take-home amount' : 'Costs above take-home range'} · extra search includes ${money(row.amount_above_maximum_annual)} above maximum / year`;
    if (row.financial_status === 'within_range')
      return upperOnly
        ? 'Costs at or below upper take-home amount'
        : 'Costs within take-home range';
    if (row.financial_status === 'outside_range' && upperOnly)
      return 'Costs above upper take-home amount';
    return (
      {
        below_buffer: 'Costs below take-home range · included by widened search',
        outside_range: 'Costs outside take-home range',
        estimated_cost: 'Published estimated costs + extras',
      }[row.financial_status] || 'Unavailable'
    );
  }

  // The coloured badges on each result. Up to four: the money verdict, whether
  // the county is outside the chosen state, whether cost limits were met, and
  // whether temperature limits were met.
  // Cost and temperature badges appear ONLY when the user set such limits -
  // showing "limits met" to someone who set none would be noise.
  // A county with no climate data gets its own badge rather than being shown as
  // a pass or a fail, because neither would be true.
  function badge(row) {
    const type = row.financial_status.includes('buffer')
      ? 'buffer'
      : row.financial_status === 'outside_range'
        ? 'outside'
        : row.financial_status === 'estimated_cost'
          ? 'estimate'
          : '';
    const active = activeComponentLimits();
    const exceeded = new Set(row.exceeded_component_limits || []);
    const componentBadge = !active.length
      ? ''
      : exceeded.size
        ? `<span class="badge outside">Over ${esc(
            active
              .filter((component) => exceeded.has(component.key))
              .map((component) => component.label)
              .join(', ')
          )} ${exceeded.size === 1 ? 'limit' : 'limits'}</span>`
        : '<span class="badge component-match">EPI category limits met</span>';
    const activeTemperatures = activeTemperatureLimits();
    const failedTemperatures = new Set(row.failed_temperature_filters || []);
    const temperatureBadge = !activeTemperatures.length
      ? ''
      : row.climate_data_availability !== 'available'
        ? '<span class="badge outside">Climate unavailable for selected limit</span>'
        : failedTemperatures.size
          ? `<span class="badge outside">Outside ${esc(
              activeTemperatures
                .filter((limit) => failedTemperatures.has(limit.key))
                .map((limit) => limit.label.toLowerCase())
                .join(' and ')
            )}</span>`
          : '<span class="badge climate-match">Temperature limits met</span>';
    return `<span class="badge ${type}">${esc(statusText(row))}</span>${!row.in_state_scope ? '<span class="badge outside">Outside selected state</span>' : ''}${componentBadge}${temperatureBadge}${window.ColeFeatures?.badge(row) ?? ''}`;
  }

  // The submitted-criteria summary. Its purpose is accountability: it restates
  // exactly what was searched so the user never has to trust that their form was
  // read correctly. It reads from `accepted`, never from the live form - which
  // is what lets the "stale" marker be meaningful.
  function renderSummary() {
    $('accepted-summary').hidden = !state.accepted;
    $('welcome').hidden = Boolean(state.accepted);
    if (!state.accepted) {
      $('accepted-summary').replaceChildren();
      $('accepted-summary').classList.remove('stale');
      return;
    }
    const s = state.accepted,
      a = s.normalized_annual;
    const rows = [
      [
        'Household',
        `${s.household.adults} ${s.household.adults === 1 ? 'adult' : 'adults'}, ${s.household.children} ${s.household.children === 1 ? 'child' : 'children'}`,
      ],
      ['State restriction', s.state_filter || 'All supported states'],
      ['Extra expenses', `${money(a.extras)} / year`],
    ];
    rows.push([
      'Costs used',
      s.cost_mode === 'custom' ? 'Your monthly costs (your entered costs)' : 'EPI renter estimate',
    ]);
    if (s.cost_mode === 'custom')
      rows.push([
        'Monthly replacements',
        COST_COMPONENTS.filter((component) => s.custom_costs_monthly[component.key] !== null)
          .map(
            (component) => `${component.label}: ${money(s.custom_costs_monthly[component.key], 2)}`
          )
          .join('; '),
      ]);
    const activeLimits = activeComponentLimits(s);
    rows.push([
      'Limits on monthly estimates',
      activeLimits.length
        ? activeLimits
            .map(
              (component) =>
                `${component.label} at or below ${money(s.component_limits_monthly[component.key], 2)}`
            )
            .join('; ')
        : 'None',
    ]);
    const activeTemperatures = activeTemperatureLimits(s);
    rows.push([
      'Typical temperature limits',
      activeTemperatures.length
        ? activeTemperatures
            .map(
              (limit) =>
                `${limit.label} ${limit.relation} ${degrees(s.temperature_limits_f[limit.key])}`
            )
            .join('; ')
        : 'None',
    ]);
    const featureRows = window.ColeFeatures?.summaryRows(s) ?? [];
    rows.push(...featureRows);
    if (s.lookup)
      rows.push([
        'Selected location',
        `${s.lookup_label || s.lookup.id} (${s.lookup.kind}, ${s.lookup.id})`,
      ]);
    const upperOnly = s.income_context === 'financial' && s.income_range_mode === 'upper_only';
    if (s.reserve_goal)
      rows.push(
        [
          'Reserve target',
          `${s.reserve_goal.percent}% of ${s.reserve_goal.income_basis} take-home`,
        ],
        ['Income used for goal / year', money(s.reserve_goal.income_annual, 2)],
        ['Amount to keep / year', money(s.reserve_goal.target_annual, 2)],
        ['Result cost ceiling / year', money(s.reserve_goal.cost_ceiling_annual, 2)]
      );
    else if (upperOnly)
      rows.push(
        ['Maximum total household take-home pay / year', money(a.income_upper, 2)],
        ['Extra above maximum / year', money(a.upper_buffer, 2)],
        ['Result cost ceiling / year', money(a.effective_combined_max, 2)]
      );
    else if (s.income_context === 'financial')
      rows.push(
        [
          'Total household take-home pay / year',
          `${money(a.income_lower, 2)} to ${money(a.income_upper, 2)}`,
        ],
        ['Extra below minimum / year', money(a.lower_buffer, 2)],
        ['Extra above maximum / year', money(a.upper_buffer, 2)],
        [
          'Result cost range / year',
          `${money(a.effective_combined_min, 2)} to ${money(a.effective_combined_max, 2)}`,
        ]
      );
    else rows.push(['Income context', 'Costs only; no income evaluation']);
    const discoveryScope = s.reserve_goal
      ? 'These counties leave at least your chosen percentage after estimated costs and extras. An income range uses the lower amount. Cheaper counties can still match. All other selected filters also apply.'
      : upperOnly
        ? 'Results are at or below the submitted upper take-home amount, or within the submitted higher-cost allowance, and meet all other selected filters.'
        : 'Estimated costs match the submitted take-home cost range and all other selected filters.';
    const lookupScope =
      s.income_context !== 'financial'
        ? 'All supported counties linked to this location are shown. No income check is applied. Other filters are checked; missed limits and missing data are marked.'
        : s.reserve_goal
          ? 'All supported counties linked to this location are shown. Each is checked against your reserve goal at the income shown above, along with your other filters.'
          : upperOnly
            ? 'All supported counties linked to this location are shown, including those with costs above your upper take-home amount or outside your other filters. Missed limits and missing data are marked.'
            : 'All supported counties linked to this location are shown, including those with costs outside your take-home range or other filters. Missed limits and missing data are marked.';
    const primaryLabels = new Set([
      'Household',
      'Costs used',
      'Total household take-home pay / year',
      'Maximum total household take-home pay / year',
      'Income context',
      'Reserve target',
      'Income used for goal / year',
    ]);
    const activeLabels = new Set(featureRows.map(([label]) => label));
    if (s.state_filter) activeLabels.add('State restriction');
    if (s.lookup) activeLabels.add('Selected location');
    if (s.cost_mode === 'custom') activeLabels.add('Monthly replacements');
    if (activeLimits.length) activeLabels.add('Limits on monthly estimates');
    if (activeTemperatures.length) activeLabels.add('Typical temperature limits');
    if (ColeMoney.sign(a.extras) !== 0) activeLabels.add('Extra expenses');
    if (a.lower_buffer != null && ColeMoney.sign(a.lower_buffer) !== 0)
      activeLabels.add('Extra below minimum / year');
    if (a.upper_buffer != null && ColeMoney.sign(a.upper_buffer) !== 0)
      activeLabels.add('Extra above maximum / year');
    if (s.reserve_goal || activeLabels.has('Extra above maximum / year'))
      primaryLabels.add('Result cost ceiling / year');
    const cells = (items) =>
      items
        .map(([label, text]) => `<div><dt>${esc(label)}</dt><dd>${esc(text)}</dd></div>`)
        .join('');
    const activeRows = rows.filter(([label]) => activeLabels.has(label));
    $('accepted-summary').innerHTML =
      `<p class="submitted-label">Submitted criteria · ${s.query_kind === 'lookup' ? 'Location lookup' : 'County search'}</p>
      <dl class="summary-grid summary-primary">${cells(rows.filter(([label]) => primaryLabels.has(label)))}</dl>
      ${activeRows.length ? `<dl class="summary-grid active-criteria" aria-label="Applied preferences">${cells(activeRows)}</dl>` : ''}
      <details class="submitted-details"><summary>Full submitted criteria and calculation rules</summary><dl class="summary-grid">${cells(rows)}</dl>
      <p class="summary-subtext">${s.query_kind === 'lookup' ? lookupScope : discoveryScope} ${s.income_context === 'financial' ? 'COLE adds every earner into this household total. ' : ''}${s.cost_mode === 'custom' ? 'COLE uses each replacement cost in every county you search. Local benefits may not apply everywhere. ' : ''}Category limits use the original published estimates before your extra expenses. ${s.reserve_goal ? 'Widened cost searching is off while you use a reserve goal.' : s.income_context === 'financial' ? 'Search allowances widen the cost range without increasing your take-home pay.' : ''}</p></details>`;
  }

  // The result list. Sorting happens here, in the browser, from the rows the
  // server already sent, so changing the sort needs no new request.
  // Two details worth noting:
  //   - ColeMoney.compare is used rather than JavaScript's normal comparison,
  //     because these are exact decimal strings, not numbers.
  //   - `identity` is the tiebreaker, so counties with equal values always
  //     appear in the same order rather than shuffling between renders.
  //   - Missing values sort last regardless of direction: a county with no
  //     climate data should not win a "coldest month" sort by default.
  function renderResults() {
    updateSortSetting();
    $('results-list').replaceChildren();
    if (!state.accepted) {
      $('results-heading').textContent = 'Counties';
      $('result-count-summary').hidden = true;
      $('result-count-summary').textContent = '';
      $('zero-results-banner').hidden = true;
      $('results-status').textContent = 'Submit your criteria to see results.';
      $('results-empty').hidden = true;
      $('map-description').textContent = 'Local county boundaries. No internet basemap needed.';
      $('result-sort').disabled = true;
      $('result-order').disabled = true;
      $('results-pages').hidden = true;
      return;
    }
    const total = state.results.length;
    const custom = state.accepted.cost_mode === 'custom';
    $('result-count-summary').textContent =
      `${total.toLocaleString()} ${total === 1 ? 'county' : 'counties'}${custom && state.epiTotal !== null ? ` using your budget · ${state.epiTotal.toLocaleString()} using EPI` : ''}`;
    $('result-count-summary').hidden = false;
    $('zero-results-banner').hidden = total !== 0;
    if (total === 0 && custom)
      $('zero-results-banner').innerHTML =
        '<strong>No counties met every selected limit using your household budget.</strong> Your replacement costs apply everywhere searched. <a href="#inspect-search-text">Inspect a county</a> to compare the EPI and personal estimates.';
    else if (total === 0)
      $('zero-results-banner').innerHTML =
        '<strong>No counties matched all your search settings.</strong> EPI models a modest renter budget; your entered take-home pay or other filters may exclude all counties. <a href="#results-empty">See why and how to inspect a county</a>.';
    const identity = (left, right) =>
      left.state_abbr < right.state_abbr
        ? -1
        : left.state_abbr > right.state_abbr
          ? 1
          : left.county_name < right.county_name
            ? -1
            : left.county_name > right.county_name
              ? 1
              : left.county_fips < right.county_fips
                ? -1
                : left.county_fips > right.county_fips
                  ? 1
                  : 0;
    const sortKey = $('result-sort').value;
    const selectedSort = RESULT_SORTS[sortKey];
    const direction = $('result-order').value === 'desc' ? -1 : 1;
    const ordered = [...state.results].sort((left, right) => {
      if (sortKey === 'name') return identity(left, right) * direction;
      const featureOrder = window.ColeFeatures?.compare(left, right, sortKey, direction);
      if (featureOrder != null) return featureOrder || identity(left, right);
      if (!selectedSort?.path) return identity(left, right);
      const read = (row) => selectedSort.path.reduce((value, key) => value?.[key], row);
      const leftValue = read(left),
        rightValue = read(right);
      if (leftValue == null && rightValue == null) return identity(left, right);
      if (leftValue == null) return 1;
      if (rightValue == null) return -1;
      const compared = ColeMoney.compare(leftValue, rightValue) * direction;
      return compared || identity(left, right);
    });
    $('results-heading').textContent =
      `${total.toLocaleString()} ${state.accepted.query_kind === 'lookup' ? 'associated' : 'matching'} ${total === 1 ? 'county' : 'counties'}`;
    $('results-empty').hidden = total !== 0;
    const submitted = state.accepted;
    const basis = custom
      ? 'Your household budget replaces the selected EPI categories; the original amounts for those categories are not added again.'
      : 'EPI estimates a modest renter budget for your selected household.';
    const goal = submitted.reserve_goal;
    const activeLimit = goal
      ? `Your submitted ${esc(goal.percent)}% reserve target keeps ${money(goal.target_annual, 2, 12)} per month from ${money(goal.income_annual, 2, 12)} monthly take-home. Estimated costs plus extras must be at or below ${money(goal.cost_ceiling_annual, 2, 12)} per month (${money(goal.cost_ceiling_annual, 2)} per year).`
      : submitted.income_context === 'financial'
        ? 'The submitted take-home limits and any active widened cost searching apply to estimated costs plus extra expenses.'
        : 'This is a costs-only search; no take-home pay or reserve target is applied.';
    $('results-empty-basis').innerHTML = `${basis} ${activeLimit} Other selected filters also apply. Zero matches means no county passed every submitted criterion; it does not identify which limit excluded each county.`;
    const upperOnly =
      state.accepted.income_context === 'financial' &&
      state.accepted.income_range_mode === 'upper_only';
    const emptyAdvice = state.accepted.reserve_goal
      ? 'Review the reserve target, your personal cost estimates, and other filters. Inspect a county to see the gap to your goal.'
      : upperOnly
        ? 'Check the take-home pay you entered and review your expenses and filters. A higher-cost allowance can show counties costing more than your take-home pay.'
        : state.accepted.income_context === 'financial'
          ? 'Adjust the take-home range or buffers, or relax other selected filters and submit again.'
          : 'Relax the selected filters and submit again.';
    $('results-status').textContent =
      state.accepted.query_kind === 'lookup'
        ? `${total.toLocaleString()} ${total === 1 ? 'county is' : 'counties are'} linked to this location. Looking up a place also shows counties outside your filters. Select one to see which settings it meets.`
        : total
          ? `${total.toLocaleString()} ${total === 1 ? 'county matches' : 'counties match'} ${custom ? 'your household budget' : 'published costs'}.${custom && state.epiTotal !== null ? ` ${state.epiTotal.toLocaleString()} match published costs under the same filters.` : ''} Select a county to inspect it.`
          : `No counties match these criteria. ${custom && state.epiTotal !== null ? `${state.epiTotal.toLocaleString()} match published costs under the same filters. ` : ''}${emptyAdvice}`;
    const covered = state.meta?.manifest?.coverage?.counties;
    $('map-description').textContent =
      state.accepted.query_kind === 'lookup'
        ? `Showing ${total.toLocaleString()} associated ${total === 1 ? 'county' : 'counties'}; other covered counties are pale gray.`
        : `Showing ${total.toLocaleString()} matching ${total === 1 ? 'county' : 'counties'}${Number.isInteger(covered) ? ` from ${covered.toLocaleString()} counties with EPI cost data` : ''}; counties not returned are pale gray.`;
    ordered.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE).forEach((row) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'result-row';
      button.dataset.county = row.county_fips;
      button.disabled = !state.ready || state.busy;
      button.setAttribute('aria-pressed', String(row.county_fips === state.inspectedId));
      const climateLine = row.climate
        ? `Coldest-month average ${degrees(row.climate.coldest_month_mean_f)} · Warmest-month average ${degrees(row.climate.warmest_month_mean_f)} · ${row.climate.normal_period_start}–${row.climate.normal_period_end}`
        : 'Temperature data unavailable';
      button.innerHTML = `<span><span class="result-name">${esc(row.county_name)}, ${row.state_abbr}</span><span class="result-location">${esc(climateLine)}</span>${badge(row)}</span><span class="result-number"><strong>${money(row.combined_cost_annual)}</strong><small> ${custom ? 'Personal budget' : 'EPI budget'} + extras / year</small></span>`;
      button.addEventListener('click', () => inspect(row.county_fips));
      $('results-list').append(button);
    });
    const pages = Math.ceil(total / PAGE_SIZE);
    $('results-pages').hidden = pages <= 1;
    $('page-label').textContent = `Page ${state.page + 1} of ${pages}`;
    $('result-sort').disabled = !state.ready || state.busy || !total;
    updateResultAvailability();
  }

  // Enable or disable result controls to match busy/ready. Kept separate so it
  // can be called without rebuilding the whole list.
  function updateResultAvailability() {
    const pages = Math.ceil(state.results.length / PAGE_SIZE);
    document
      .querySelectorAll('.result-row')
      .forEach((button) => (button.disabled = !state.ready || state.busy));
    $('result-sort').disabled = !state.ready || state.busy || !state.results.length;
    $('result-order').disabled = $('result-sort').disabled;
    $('previous-page').disabled = !state.ready || state.busy || state.page === 0;
    $('next-page').disabled = !state.ready || state.busy || state.page + 1 >= pages;
  }

  // Page navigation. After changing page, focus moves to the first result -
  // otherwise a keyboard user would be left focused on a button whose page has
  // changed underneath them - and the change is announced with exact row
  // numbers.
  function changePage(delta) {
    if (state.busy) {
      say('Wait for the current submission to finish before changing result pages.');
      return;
    }
    const total = state.results.length,
      pages = Math.ceil(total / PAGE_SIZE),
      next = Math.max(0, Math.min(pages - 1, state.page + delta));
    if (!total || next === state.page) return;
    state.page = next;
    renderResults();
    $('results-list').scrollTop = 0;
    const first = $('results-list').querySelector('.result-row');
    if (first) first.focus({ preventScroll: true });
    else {
      $('results-heading').tabIndex = -1;
      $('results-heading').focus({ preventScroll: true });
    }
    const firstNumber = state.page * PAGE_SIZE + 1,
      lastNumber = Math.min(total, (state.page + 1) * PAGE_SIZE);
    say(
      `Page ${state.page + 1} of ${pages}. Showing counties ${firstNumber} through ${lastNumber} of ${total}.`
    );
  }

  // "At this income, how much is left over?" A negative balance is shown as a
  // SHORTFALL rather than hidden, including for counties that matched the
  // search: a county inside the selected range can still leave the household
  // short at the low end of its income. Saying so is the honest choice.
  function balances(row) {
    if (!row.income_points)
      return '<p class="hint">Costs only. Enter income to see how much would remain.</p>';
    return `<div class="balance-grid">${row.income_points
      .map((point) => {
        const sign = ColeMoney.sign(point.balance);
        const abs = point.balance.replace(/^-/, '');
        const goal = point.reserve_target_annual !== undefined;
        const afterGoal = point.balance_after_reserve_annual;
        const goalNote = goal
          ? `<p>Target: ${money(point.reserve_target_annual, 2, 12)} / month (${esc(state.accepted.reserve_goal.percent)}% of take-home)</p><strong>${point.reserve_goal_met ? 'Goal met' : 'Goal unmet'}: ${money(afterGoal.replace(/^-/, ''), 2, 12)} / month ${point.reserve_goal_met ? 'beyond your target' : 'short of your target'}</strong><p class="hint">The balance after costs and extras is before setting aside your reserve target.</p>`
          : '';
        return `<div class="balance-card ${sign < 0 ? 'negative' : sign === 0 ? 'zero' : ''} ${goal && !point.reserve_goal_met ? 'goal-unmet' : ''}"><p>At ${money(point.income, 2)} household take-home / year</p><strong>${money(abs, 2)} ${sign < 0 ? 'shortfall' : sign > 0 ? 'remaining' : 'balance'} / year</strong><p>${money(abs, 2, 12)} ${sign < 0 ? 'shortfall' : sign > 0 ? 'remaining' : 'balance'} / month after estimated costs and extra expenses</p>${goalNote}</div>`;
      })
      .join(
        ''
      )}</div><p class="hint balance-note">Remaining money is not guaranteed savings. EPI does not include savings. Count expenses and savings only once if they're already deducted from pay or included in extras.</p>`;
  }

  // The three-line cost summary: EPI baseline, the user's own extras, and the
  // total. Monthly and annual side by side, since people think in both.
  function costTable(row) {
    const custom = state.accepted?.cost_mode === 'custom';
    const rows = custom
      ? [
          ['EPI renter costs (estimated taxes removed)', row.baseline_annual],
          ['published costs plus your extra expenses', row.epi_combined_cost_annual],
          ['Costs with your replacements', row.custom_baseline_annual],
          ['Your extra expenses', row.extras_annual],
          ['Your budget: costs + extra expenses', row.combined_cost_annual],
        ]
      : [
          ['EPI renter costs (estimated taxes removed)', row.baseline_annual],
          ['Your extra expenses', row.extras_annual],
          ['Total estimated costs + extra expenses', row.combined_cost_annual],
        ];
    return `<p class="detail-note"><strong>EPI ${state.meta.manifest.edition_year} estimates · ${state.meta.manifest.cost_year} dollars.</strong> County-level renter estimates with estimated taxes removed. COLE does not subtract taxes from your entered take-home pay. ${custom ? 'Your replacements substitute selected categories in every searched county. The EPI rows are reference figures, not additional expenses. ' : ''}Actual household expenses can differ.</p><table class="cost-table"><thead><tr><th scope="col">Household estimate</th><th scope="col">Monthly average</th><th scope="col">Annual</th></tr></thead><tbody>${rows.map(([label, amount], i) => `<tr class="${i === rows.length - 1 ? 'total' : ''}"><th scope="row">${label}</th><td>${money(amount, 2, 12)}</td><td>${money(amount, 2)}</td></tr>`).join('')}</tbody></table><p class="detail-note">Monthly figures are annual totals divided by 12, not a prediction of each month's bills.</p>`;
  }

  // The six-category breakdown, with each category's limit result.
  // The note about components summing to within $3 of the published total is a
  // real quirk of EPI's source data, disclosed rather than smoothed over - and
  // COLE keeps the published total rather than substituting its own sum.
  function componentTable(row, includeHeading = true) {
    const limits = state.accepted?.component_limits_monthly || {};
    const custom = state.accepted?.cost_mode === 'custom';
    const matches = row.component_limit_matches || {};
    const costs = row.component_costs_annual || {};
    const rows = COST_COMPONENTS.map((component) => {
      const limit = limits[component.key];
      const active = limit !== null && limit !== undefined;
      const result = !active
        ? 'No limit'
        : `${money(limit, 2)} · ${matches[component.key] === false ? 'Exceeds' : 'Meets'}`;
      const cost = costs[component.key] ?? null;
      const replacement = state.accepted?.custom_costs_monthly?.[component.key];
      const personal = row.custom_components_annual?.[component.key];
      return `<tr class="${active && matches[component.key] === false ? 'limit-exceeded' : ''}"><th scope="row">${component.label}</th><td data-label="EPI monthly average">${money(cost, 2, 12)}</td>${custom ? `<td data-label="Your monthly average">${money(personal, 2, 12)}${replacement !== null ? ' (replaced)' : ''}</td>` : ''}<td data-label="EPI annual">${money(cost, 2)}</td><td data-label="Your monthly EPI limit">${result}</td></tr>`;
    }).join('');
    return `<div class="component-breakdown">${includeHeading ? '<h3>EPI cost breakdown</h3>' : ''}<div class="table-scroll" tabindex="0" role="region" aria-label="EPI cost breakdown table"><table class="component-table"><thead><tr><th scope="col">EPI category</th><th scope="col">EPI monthly average</th>${custom ? '<th scope="col">Your monthly average</th>' : ''}<th scope="col">EPI annual</th><th scope="col">Your monthly EPI limit</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="detail-note">COLE divides each published annual EPI cost by 12 and rounds to cents, rounding half a cent up. A monthly cost at or below your limit passes. EPI publishes its categories and total separately; adding the categories can differ from that total by up to $3 per year. COLE uses the published total for the renter estimate.</p>
      ${custom ? '<p class="detail-note">Your entered monthly amount replaces the published estimate for that category. Blank categories keep their published estimates. Replacements apply to every searched county. EPI category filters still use the original EPI amounts.</p>' : ''}
      <p class="detail-note"><strong>Category meaning:</strong> Housing is fair-market rent plus tenant-paid utilities. Food is primarily groceries prepared at home. Transportation combines auto ownership, auto use and transit costs; it is not transit availability or walkability. Health care, child care and other necessities follow EPI's household assumptions. Extra expenses are not allocated to these categories.</p></div>`;
  }

  // The temperature panel. When climate data is missing it says so plainly, and
  // the wording is deliberate: COLE does not substitute a neighbouring county, a
  // climate division, or a nearby station. Filling the gap with something
  // plausible would be more useful-looking and less true.
  function climatePanel(row, includeHeading = true) {
    const heading = includeHeading ? '<h3>Long-term temperature profile</h3>' : '';
    if (row.climate_data_availability !== 'available' || !row.climate) {
      return `<div class="climate-breakdown">${heading}<div class="climate-unavailable" role="note"><strong>County climate data unavailable.</strong><p>No NOAA county-area temperature record is available for this county in the selected release. COLE leaves the value unavailable instead of substituting a neighbor, climate division, or station.</p></div></div>`;
    }
    const climate = row.climate,
      months = climate.monthly_mean_f || {};
    const monthNames = (names) => names.map((name) => MONTH_LABELS[name] || name).join(', ');
    const active = activeTemperatureLimits();
    const matches = row.temperature_filter_matches || {};
    const limitResults = active.length
      ? `<ul class="climate-limit-results">${active
          .map((limit) => {
            const passed = matches[limit.key] === true;
            return `<li class="${passed ? 'limit-met' : 'limit-missed'}"><strong>${esc(limit.label)}</strong> ${esc(limit.relation)} ${esc(degrees(state.accepted.temperature_limits_f[limit.key]))}: ${passed ? 'Meets' : 'Outside limit'}</li>`;
          })
          .join('')}</ul>`
      : '<p class="detail-note">No temperature limit was applied in this submission.</p>';
    return `<div class="climate-breakdown">${heading}<p class="detail-note"><strong>NOAA nClimDiv · ${esc(climate.normal_period_start)}–${esc(climate.normal_period_end)} monthly county-area averages (°F).</strong> These are not daily highs and lows or a forecast.</p><div class="climate-summary-grid">
      <div><span>12-month average temperature</span><strong>${esc(degrees(climate.twelve_month_average_f))}</strong></div>
      <div><span>Coldest-month average</span><strong>${esc(degrees(climate.coldest_month_mean_f))}</strong><small>${esc(monthNames(climate.coldest_months || []))}</small></div>
      <div><span>Warmest-month average</span><strong>${esc(degrees(climate.warmest_month_mean_f))}</strong><small>${esc(monthNames(climate.warmest_months || []))}</small></div>
      </div>${limitResults}<div class="temperature-months" role="list" aria-label="Monthly average temperatures">${Object.entries(
        months
      )
        .map(
          ([month, value]) =>
            `<div role="listitem"><span>${esc(MONTH_LABELS[month] || month)}</span><strong>${esc(degrees(value))}</strong></div>`
        )
        .join('')}</div>
      <p class="detail-note">NOAA nClimDiv estimates average temperatures for county areas from ${climate.normal_period_start} to ${climate.normal_period_end}. COLE averages the twelve monthly values for the 12-month figure and rounds half up to one decimal place. That figure is calculated here, rather than taken from a separate NOAA annual normal. Seasonal limits use the coldest and warmest monthly values at NOAA's published one-decimal precision. The 12-month limits use the rounded value shown here. A value exactly on a limit passes.</p></div>`;
  }

  // The county detail panel, assembled from the pieces above, plus a collapsed
  // provenance section showing the EPI workbook sheet and row, the modeled tax
  // amount removed, and the NOAA source key - so every number on screen can be
  // traced back to its origin.
  function renderDetails() {
    const row = state.inspected;
    window.ColeFeatures?.details(row, state.accepted);
    $('county-details').hidden = !row;
    $('county-details-empty').hidden = Boolean(row);
    $('county-analysis').hidden = !row;
    if (!row) {
      $('details-heading').textContent = '';
      $('details-body').replaceChildren();
      $('county-cost-breakdown').replaceChildren();
      $('county-climate-breakdown').replaceChildren();
      $('cost-county-label').textContent = '';
      $('climate-county-label').textContent = '';
      return;
    }
    $('details-heading').textContent = `${row.county_name}, ${row.state_abbr}`;
    $('cost-county-label').textContent = `${row.county_name}, ${row.state_abbr}`;
    $('climate-county-label').textContent = `${row.county_name}, ${row.state_abbr}`;
    $('county-cost-breakdown').innerHTML = componentTable(row);
    $('county-climate-breakdown').innerHTML = climatePanel(row);
    const source = row.source || {};
    $('details-body').innerHTML =
      `${badge(row)}${row.out_of_query ? `<p class="hint">This county is outside the current result set.${state.accepted?.income_context === 'financial' ? ' The badges show how it compares with your submitted criteria.' : ''}</p>` : ''}<div class="financial-overview"><div class="cost-headline"><p>${state.accepted.cost_mode === 'custom' ? 'Your budget: costs + extra expenses' : 'Total estimated costs + extra expenses'}</p><strong>${money(row.combined_cost_annual, 2, 12)}</strong><span> / month</span><p>${money(row.combined_cost_annual, 2)} / year · ${state.accepted.cost_mode === 'custom' ? 'Personal household budget' : 'EPI renter estimate'}</p></div><div>${balances(row)}</div></div>${costTable(row)}
      <details><summary>Source and calculation details</summary><p class="hint">EPI ${state.meta.manifest.edition_year}; costs in ${state.meta.manifest.cost_year} dollars. County ${row.county_fips}; household ${row.household_profile}.</p>
      <p class="hint">EPI published gross-income budget / year: ${money(source.published_annual_total, 2)}. EPI modeled taxes removed / year for COLE's take-home comparison: ${money(source.published_annual_taxes, 2)}. The modeled tax amount includes federal income tax, state income tax and the employee share of Social Security and Medicare; EPI does not include county or city income taxes. Workbook ${esc(source.source_sheet)}, row ${esc(source.source_row)}. Original published totals are preserved even when component sums differ.</p>
      <p class="hint">${row.climate ? `NOAA source key ${esc(row.climate.source.source_state_code)}${esc(row.climate.source.source_county_code)}, element ${esc(row.climate.source.source_element_code)}, normal-period code ${esc(row.climate.source.source_period_code)}.` : 'No NOAA county climate source row is available for this county.'}</p>
      <p class="hint">Release: ${esc(row.data_release_id)}. Financial range matching uses exact annual values. Monthly component limits use the displayed annual-divided-by-12 value rounded half up to cents. Seasonal temperature limits use source tenth-degree monthly extrema; 12-month-average limits use COLE's displayed one-decimal arithmetic mean.</p></details>`;
    const added = state.comparison.some((item) => item.county_fips === row.county_fips);
    $('add-comparison').disabled =
      !state.ready || state.busy || added || state.comparison.length >= 3;
    $('add-comparison').textContent = added
      ? 'Already compared'
      : state.comparison.length >= 3
        ? 'Remove a county to add another'
        : 'Add to comparison';
  }

  // The comparison cards. Note the focus management when one is removed: focus
  // moves to the next Remove button, or to the heading if none are left, so a
  // keyboard user is never stranded on a button that no longer exists.
  function renderComparison() {
    $('comparison-count').textContent = `${state.comparison.length} / 3`;
    $('comparison-grid').replaceChildren();
    $('comparison-status').textContent =
      state.comparison.length === 3
        ? 'Three counties selected. Remove one before adding another.'
        : state.comparison.length
          ? 'Your next search will update the counties already in your comparison.'
          : 'No counties added yet.';
    state.comparison.forEach((row, index) => {
      const card = document.createElement('article');
      card.className = 'comparison-item';
      card.innerHTML = `<div class="compare-heading"><div class="compare-top"><span>FIPS ${row.county_fips}</span><button class="button quiet small remove-compare" type="button" aria-label="Remove ${esc(row.county_name)} from comparison">Remove</button></div>
        <h3>${esc(row.county_name)}, ${row.state_abbr}</h3>${row.out_of_query ? '<p class="hint">Outside current results</p>' : ''}${badge(row)}</div>
        <div class="compare-headline"><p class="compare-cost">${money(row.combined_cost_annual)}</p><p class="compare-period">${state.accepted?.cost_mode === 'custom' ? 'Personal budget' : 'EPI budget'} + extras / year</p></div><div class="compare-balances">${balances(row)}</div><div class="compare-costs">${costTable(row)}</div><div class="compare-support"><details class="comparison-breakdown"><summary>EPI cost breakdown${state.accepted?.cost_mode === 'custom' ? ' and your costs' : ''}</summary>${componentTable(row, false)}</details><details class="comparison-breakdown"><summary>Long-term temperature profile</summary>${climatePanel(row, false)}</details><button class="button secondary small inspect-compare" type="button">Inspect county</button></div>`;
      card.querySelector('.compare-support').insertAdjacentHTML(
        'beforeend',
        window.ColeFeatures?.comparison(row, state.accepted) ?? ''
      );
      card.querySelector('.remove-compare').disabled = state.busy || !state.ready;
      card.querySelector('.remove-compare').addEventListener('click', () => {
        if (state.busy || !state.ready) return;
        state.comparison = state.comparison.filter((item) => item.county_fips !== row.county_fips);
        renderComparison();
        renderDetails();
        const controls = $('comparison-grid').querySelectorAll('.remove-compare');
        if (controls.length) controls[Math.min(index, controls.length - 1)].focus();
        else {
          $('comparison-heading').tabIndex = -1;
          $('comparison-heading').focus();
        }
      });
      card.querySelector('.inspect-compare').disabled = state.busy || !state.ready;
      card
        .querySelector('.inspect-compare')
        .setAttribute('aria-label', `Inspect ${row.county_name}, ${row.state_abbr}`);
      card
        .querySelector('.inspect-compare')
        .addEventListener('click', () => inspect(row.county_fips));
      $('comparison-grid').append(card);
    });
    window.ColeFeatures?.comparisonReady(state.comparison, state.accepted);
  }

  // =========================================================================
  // THE MAP
  // =========================================================================
  // Colour for one county. Counties not in the current results are pale and
  // semi-transparent; the inspected one gets a thick dark outline.
  // The colour order matters: a failure of ANY criterion wins over the money
  // status, so a county within the submitted financial range but outside a
  // temperature limit is not shown in the "matched" colour.
  function mapStyle(id) {
    const row = state.resultMap.get(id),
      inspected = id === (state.pendingInspectedId || state.inspectedId);
    const color = !row
      ? '#e1e7e5'
      : row.in_state_scope === false ||
          row.component_limits_match === false ||
          row.temperature_filters_match === false ||
          row.financial_status === 'outside_range'
        ? '#c88e79'
        : row.financial_status.includes('buffer')
          ? '#d9b676'
          : row.financial_status === 'estimated_cost'
            ? '#75a6ca'
            : '#5caa8b';
    const style = {
      fillColor: color,
      fillOpacity: row ? 0.8 : 0.25,
      color: inspected
        ? state.theme === 'dark'
          ? '#f1f7f8'
          : '#172f42'
        : row
          ? '#28483f'
          : state.theme === 'dark'
            ? '#91aaa3'
            : '#728c86',
      weight: inspected ? 2.5 : 1,
      opacity: 1,
    };
    return (
      window.ColeFeatures?.mapStyle(id, style, {
        accepted: state.accepted,
        resultMap: state.resultMap,
      }) ?? style
    );
  }

  // Alaska's Aleutian Islands cross the 180th meridian, so part of the state has
  // longitudes near +180 and the rest near -180. On a map with no base tiles
  // that splits Alaska across opposite edges of the screen.
  // Subtracting 360 from the positive ones puts the state back together. This
  // affects DISPLAY ONLY - the underlying data and all county keys are
  // untouched, which is what the original comment below is asserting.
  function displayGeometry(collection) {
    // Equivalent wrapped longitudes keep Alaska's Aleutians together on the
    // tile-free map. Published WGS84 GeoJSON and all data keys stay unchanged.
    const unwrap = (coordinates) =>
      typeof coordinates[0] === 'number'
        ? [coordinates[0] > 0 ? coordinates[0] - 360 : coordinates[0], ...coordinates.slice(1)]
        : coordinates.map(unwrap);
    return {
      ...collection,
      features: collection.features.map((feature) =>
        feature.properties.state_abbr === 'AK'
          ? {
              ...feature,
              geometry: { ...feature.geometry, coordinates: unwrap(feature.geometry.coordinates) },
            }
          : feature
      ),
    };
  }

  // Restyle every county shape from the current state, and lift the inspected
  // one to the front so its outline is not hidden under its neighbours.
  function updateMap(fit = false) {
    state.layers.forEach((layer, id) => layer.setStyle(mapStyle(id)));
    window.ColeFeatures?.mapSync({
      accepted: state.accepted,
      resultMap: state.resultMap,
      layers: state.layers,
    });
    const selected = state.layers.get(state.pendingInspectedId || state.inspectedId);
    selected?.bringToFront();
    $('fit-results').disabled = !state.results.length;
    if (fit && state.results.length) fitResults();
  }

  // Zoom to fit the current results, capped at zoom 9 so a single-county result
  // does not zoom in uselessly far.
  function fitResults() {
    if (!state.ready || !state.map) return;
    const ids = state.results.map((row) => row.county_fips);
    const bounds = L.latLngBounds([]);
    ids.forEach((id) => {
      const layer = state.layers.get(id);
      if (layer) bounds.extend(layer.getBounds());
    });
    if (bounds.isValid())
      state.map.fitBounds(bounds, { padding: [15, 15], maxZoom: 9, animate: false });
  }

  // Reset the view to the contiguous United States.
  function homeMap() {
    state.map?.fitBounds(
      [
        [24, -125],
        [50, -66],
      ],
      { animate: false }
    );
  }

  // Abandon a detail request that is still loading, and put the map and list
  // back to showing the previously inspected county.
  function discardPendingInspection() {
    if (!state.inspectionPending) return false;
    state.inspectionPending = false;
    state.pendingInspectedId = null;
    renderDetails();
    renderResults();
    updateMap();
    return true;
  }

  // Open one county's details.
  // The four guard clauses at the top all speak rather than failing silently,
  // so a click that does nothing still explains itself.
  // The `known ||` line is a quiet optimisation: if the county is already in the
  // results or comparison, its data is reused and no request is made at all.
  // The triple check before committing the result is the anti-stale-reply
  // pattern again, now guarding three things at once: a newer detail request, a
  // newer submission, and a change of which county is wanted.
  async function inspect(id) {
    if (state.incompatible) {
      say('Reload the application before inspecting counties.');
      return;
    }
    if (state.busy) {
      say('Wait for the current submission to finish before inspecting counties.');
      return;
    }
    if (!state.ready) {
      say('Wait for COLE to finish loading before inspecting counties.');
      return;
    }
    if (!state.accepted) {
      say('Submit criteria before inspecting counties.');
      return;
    }
    state.detailController?.abort();
    const generation = ++state.detailGeneration,
      acceptedGeneration = state.generation;
    state.detailController = new AbortController();
    state.inspectionPending = true;
    state.pendingInspectedId = id;
    renderDetails();
    renderResults();
    updateMap();
    $('map-status').textContent = 'Loading county details…';
    try {
      const known =
        state.resultMap.get(id) || state.comparison.find((row) => row.county_fips === id);
      const row =
        known ||
        (
          await api(
            '/api/county',
            { scenario: state.accepted, county_fips: id },
            state.detailController.signal
          )
        ).county;
      if (
        generation !== state.detailGeneration ||
        acceptedGeneration !== state.generation ||
        state.pendingInspectedId !== id
      )
        return;
      state.inspected = row;
      state.inspectedId = id;
      state.pendingInspectedId = null;
      state.inspectionPending = false;
      $('map-status').textContent = '';
      renderDetails();
      renderResults();
      updateMap();
      $('details-heading').focus({ preventScroll: true });
      $('county-details').scrollIntoView({ behavior: 'auto', block: 'nearest' });
      say(`${row.county_name} details loaded.`);
    } catch (error) {
      if (error.name !== 'AbortError' && generation === state.detailGeneration) {
        $('map-status').textContent = error.message;
        discardPendingInspection();
      }
    }
  }

  // One switch that disables or enables everything during a submission, so the
  // busy state can never be applied inconsistently across the page.
  function setBusy(busy) {
    state.busy = busy;
    window.ColeFeatures?.syncControls({
      ready: state.ready,
      busy,
      incompatible: state.incompatible,
    });
    $('submit').disabled = busy || !state.ready;
    updateMode();
    updateInspectLookupAvailability();
    updateResultAvailability();
    renderDetails();
    renderComparison();
    syncScreenActions();
    if (!busy && state.view === 'criteria') schedulePreview();
  }

  // =========================================================================
  // submit() - THE MAIN EVENT
  // =========================================================================
  // This is where the draft/accepted separation earns its keep.
  //
  // Notice the order: state.accepted, state.results and the rest are only
  // replaced AFTER the request succeeds. If it fails, the catch block puts the
  // user back on the criteria screen with their errors marked, and the previous
  // results remain intact and visible.
  //
  // The `unchanged` check handles a subtle case: if the user edited the form
  // while the request was in flight, marking fields as invalid would point at
  // boxes they have already changed. So in that case the errors are reported
  // without marking, and the message says their newer edits were kept.
  async function submit(event) {
    event.preventDefault();
    clearErrors();
    if (!state.ready || state.busy) return;
    cancelPreview();
    const scenario = readScenario();
    const capturedDraft = JSON.stringify(scenario);
    state.queryController?.abort();
    state.detailController?.abort();
    ++state.detailGeneration;
    clearInspectLookup();
    discardPendingInspection();
    $('map-status').textContent = '';
    const generation = ++state.generation;
    state.queryController = new AbortController();
    setBusy(true);
    $('results-status').textContent =
      'Applying criteria… Previous results, if any, remain visible until this succeeds.';
    try {
      const data = await api(
        '/api/query',
        {
          ...scenario,
          comparison_county_ids: state.comparison.map((row) => row.county_fips),
          inspected_county_id: state.inspectedId,
        },
        state.queryController.signal
      );
      if (generation !== state.generation) return;
      state.accepted = data.accepted_scenario;
      submittedDraft = capturedDraft;
      state.results = data.results;
      state.epiTotal = data.epi_total;
      state.resultMap = new Map(data.results.map((row) => [row.county_fips, row]));
      state.comparison = data.comparison;
      state.inspected = data.inspected;
      state.inspectedId = data.inspected?.county_fips || null;
      state.pendingInspectedId = null;
      state.inspectionPending = false;
      state.page = 0;
      renderSummary();
      renderResults();
      renderDetails();
      renderComparison();
      updateMap();
      markDraft();
      showScreen('results', { focus: false, fitMap: true });
      $('results-heading').tabIndex = -1;
      $('results-heading').focus({ preventScroll: true });
      say(`${data.total} ${data.total === 1 ? 'county' : 'counties'} returned.`);
    } catch (error) {
      if (error.name !== 'AbortError' && generation === state.generation) {
        const unchanged = JSON.stringify(readScenario()) === capturedDraft;
        if (unchanged && error.fields?.length) rejectedPreviewDraft = capturedDraft;
        if (!unchanged)
          error.message = `The submitted criteria failed. Your newer edits have been kept; submit them to try again. ${error.message}`;
        showScreen('criteria', { focus: false });
        showError(error, unchanged);
        renderResults();
        if (state.accepted)
          $('results-status').textContent +=
            ' These are earlier results; the new submission failed.';
      }
    } finally {
      if (generation === state.generation) {
        setBusy(false);
      }
    }
  }

  // Start over. Everything is cancelled, every generation bumped, and the form
  // rebuilt from scratch.
  // HTMLFormElement.prototype.reset.call(...) is used rather than form.reset()
  // because there is a button with id="reset" inside the form, and that button
  // shadows the form's own reset method - a genuine browser quirk.
  function reset() {
    rejectedPreviewDraft = null;
    cancelPreview();
    state.queryController?.abort();
    state.detailController?.abort();
    state.lookupController?.abort();
    state.inspectLookupController?.abort();
    ++state.generation;
    ++state.detailGeneration;
    ++state.lookupGeneration;
    ++state.inspectLookupGeneration;
    state.accepted = null;
    state.epiTotal = null;
    submittedDraft = null;
    state.results = [];
    state.resultMap.clear();
    state.comparison = [];
    state.inspected = null;
    state.inspectedId = null;
    state.pendingInspectedId = null;
    state.inspectionPending = false;
    state.page = 0;
    HTMLFormElement.prototype.reset.call($('scenario-form'));
    window.ColeFeatures?.reset();
    $('reserve-percent').value = '';
    $('earners').replaceChildren();
    addEarner();
    createBuffers();
    updateComponentLimitSetting();
    updateSimpleBudgetSetting();
    updateCustomBudgetSetting();
    updateTemperatureLimitSetting();
    clearLocation();
    clearErrors();
    document.querySelectorAll('.criteria-option-group').forEach((group) => (group.open = false));
    document
      .querySelectorAll('.support-disclosure,.method-reference')
      .forEach((panel) => (panel.open = false));
    $('result-sort').value = 'total';
    $('result-order').value = 'asc';
    $('draft-note').hidden = true;
    $('results-draft-note').hidden = true;
    clearInspectLookup();
    $('map-status').textContent = '';
    $('results-heading').textContent = 'Counties';
    renderSummary();
    renderResults();
    renderDetails();
    renderComparison();
    setBusy(false);
    updateMap();
    showScreen('criteria');
    say('All criteria, results and comparisons reset.');
  }

  function setTheme(palette) {
    const palettes = [
      'aggieland',
      'reveille',
      'midnight-yell',
      'century-oak',
      'aggie-maroon',
      'light',
    ];
    state.palette = palettes.includes(palette) ? palette : 'aggieland';
    state.theme = state.palette === 'light' ? 'light' : 'dark';
    document.documentElement.dataset.theme = state.theme;
    document.documentElement.dataset.palette = state.palette;
    document.querySelectorAll('[data-theme-option]').forEach((option) => {
      const selected = option.dataset.themeOption === state.palette;
      option.setAttribute('aria-pressed', String(selected));
      if (selected)
        $('theme-picker-open').setAttribute('aria-label', `Choose color theme, current: ${option.dataset.themeName}`);
    });
    state.layers.forEach((layer, id) => layer.setStyle(mapStyle(id)));
  }

  // =========================================================================
  // start() - STARTUP
  // =========================================================================
  // Runs once when the page loads. Two halves:
  //   1. Build the form and attach every event listener
  //   2. Fetch metadata and geography, build the map, mark the app ready
  //
  // The CONTRACT CHECKS partway down are worth noting: the server's list of cost
  // categories and temperature filters must match this file's lists exactly. If
  // someone adds a seventh EPI category to the server and forgets the interface,
  // COLE refuses to start rather than silently omitting it from every screen.
  //
  // Until state.ready is true the submit button stays disabled, so nothing can
  // be submitted against a half-loaded application.
  async function start() {
    setTheme(state.palette);
    document.querySelectorAll('[data-theme-option]').forEach((option) => {
      option.addEventListener('click', () => {
        setTheme(option.dataset.themeOption);
        say(`${option.dataset.themeName} color theme applied.`);
      });
    });
    showScreen('splash', { focus: false });
    setupHelpDialogs();
    addEarner();
    createBuffers();
    updateComponentLimitSetting();
    updateSimpleBudgetSetting();
    updateCustomBudgetSetting();
    updateTemperatureLimitSetting();
    $('splash-next').addEventListener('click', () => showScreen('criteria'));
    $('splash-how').addEventListener('click', () => showScreen('how'));
    $('how-back').addEventListener('click', () => showScreen('splash'));
    $('how-continue').addEventListener('click', () => showScreen('criteria'));
    $('criteria-back').addEventListener('click', () => {
      if (!state.busy) showScreen('how');
    });
    $('view-current-results').addEventListener('click', () => {
      if (state.accepted && !state.busy) showScreen('results', { fitMap: false });
    });
    $('results-back').addEventListener('click', returnToCriteria);
    $('scenario-form').addEventListener('submit', submit);
    $('reserve-example').addEventListener('click', () => {
      $('reserve-percent').value = '20';
      updateIncomeBoundSetting();
      updateBufferSetting();
      markDraft();
    });
    $('scenario-form').addEventListener('input', (event) => {
      const incomeCard = event.target.closest('.earner-card,.buffer-card');
      if (incomeCard || event.target.id === 'reserve-percent') {
        updateIncomeBoundSetting();
        updateBufferSetting();
      }
      if (event.target.dataset.component) updateComponentLimitSetting();
      if (event.target.id === 'extras') updateSimpleBudgetSetting();
      if (event.target.dataset.custom) updateCustomBudgetSetting();
      if (event.target.dataset.temperature) updateTemperatureLimitSetting();
      if (event.target.dataset.key !== 'period') markDraft();
    });
    $('scenario-form').addEventListener('change', async (event) => {
      if (event.target.dataset.key === 'period') await changePeriod(event.target);
      updateMode();
      if (event.target.closest('.earner-card,.buffer-card')) {
        updateIncomeBoundSetting();
        updateBufferSetting();
      }
      markDraft();
    });
    $('earners').addEventListener('click', (event) => {
      if (event.target.closest('.remove-earner') && $('earners').children.length > 1) {
        const card = event.target.closest('.earner-card'),
          removedLabel = card.querySelector('.earner-label').textContent,
          index = [...$('earners').children].indexOf(card);
        card.remove();
        labelEarners();
        updateIncomeBoundSetting();
        updateBufferSetting();
        const cards = $('earners').children;
        cards[Math.min(index, cards.length - 1)].querySelector('[data-key="minimum"]').focus();
        markDraft(`${removedLabel} removed.`);
      }
    });
    $('add-earner').addEventListener('click', () => {
      const card = addEarner(),
        label = card.querySelector('.earner-label').textContent;
      updateIncomeBoundSetting();
      updateBufferSetting();
      card.querySelector('[data-key="period"]').focus();
      markDraft(`${label} added.`);
    });
    $('reset').addEventListener('click', reset);
    $('lookup-search').addEventListener('click', findLocations);
    $('lookup-text').addEventListener('input', clearLocation);
    $('lookup-kind').addEventListener('change', clearLocation);
    $('lookup-text').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        findLocations();
      }
    });
    $('previous-page').addEventListener('click', () => changePage(-1));
    $('next-page').addEventListener('click', () => changePage(1));
    ['result-sort', 'result-order'].forEach((id) => $(id).addEventListener('change', () => {
      state.page = 0;
      renderResults();
      const label = $('result-sort').selectedOptions?.[0]?.textContent || 'selected measure';
      const order = $('result-order').selectedOptions?.[0]?.textContent || 'selected order';
      say(`Results sorted by ${label}, ${order}.`);
    }));
    $('inspect-search').addEventListener('click', findInspectableCounties);
    $('inspect-search-text').addEventListener('input', () => clearInspectLookup(false));
    $('inspect-search-text').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        findInspectableCounties();
      }
    });
    $('fit-results').addEventListener('click', fitResults);
    $('map-home').addEventListener('click', homeMap);
    $('add-comparison').addEventListener('click', () => {
      if (
        state.ready &&
        !state.busy &&
        state.inspected &&
        state.comparison.length < 3 &&
        !state.comparison.some((row) => row.county_fips === state.inspectedId)
      ) {
        state.comparison.push(state.inspected);
        renderComparison();
        renderDetails();
        const heading = $('comparison-grid').lastElementChild.querySelector('h3');
        heading.tabIndex = -1;
        heading.focus();
        say('County added to comparison.');
      }
    });
    $('quit').addEventListener('click', async () => {
      if (
        !(await confirmChoice(
          'This stops COLE for all open COLE tabs and clears the entries and results in this tab. Continue?',
          'Quit COLE'
        ))
      )
        return;
      try {
        await api('/api/shutdown', {});
        reset();
        document.querySelectorAll('button,input,select').forEach((el) => (el.disabled = true));
        $('startup-status').hidden = false;
        $('startup-status').textContent =
          'COLE has stopped. You can close this tab. Open COLE again to start a new session.';
      } catch (error) {
        showError(error);
      }
    });
    $('reload').addEventListener('click', async () => {
      if (
        await confirmChoice(
          'Reloading clears the current entries and results, then loads the complete current application. Continue?',
          'Reload COLE'
        )
      )
        location.reload();
    });
    try {
      const meta = await api('/api/meta');
      const [counties, states] = await Promise.all([
        api('/api/geography/counties'),
        api('/api/geography/states'),
      ]);
      state.meta = meta;
      if (
        JSON.stringify(meta.cost_components) !==
        JSON.stringify(COST_COMPONENTS.map((component) => component.key))
      )
        throw new Error(
          document.body?.dataset?.hosted === 'true' ? 'COLE has changed. Reload the page and try again.' : 'The cost categories in this copy of COLE do not match this screen. Extract a complete copy of the application and restart.'
        );
      if (
        JSON.stringify(meta.temperature_limits) !==
        JSON.stringify(TEMPERATURE_LIMITS.map((limit) => limit.key))
      )
        throw new Error(
          document.body?.dataset?.hosted === 'true' ? 'COLE has changed. Reload the page and try again.' : 'The temperature filters in this copy of COLE do not match this screen. Extract a complete copy of the application and restart.'
        );
      $('quit').hidden = !meta.supports_shutdown;
      meta.states.forEach((item) => {
        const option = document.createElement('option');
        option.value = item.abbr;
        option.textContent = `${item.name} (${item.abbr})`;
        $('state-filter').append(option);
      });
      state.map = L.map('map', { preferCanvas: true, zoomControl: true, minZoom: 2 });
      L.geoJSON(displayGeometry(states), {
        interactive: false,
        style: { color: '#6c7e83', weight: 1, fillOpacity: 0 },
      }).addTo(state.map);
      L.geoJSON(displayGeometry(counties), {
        style: (feature) => mapStyle(feature.properties.county_fips),
        onEachFeature: (feature, layer) => {
          const id = feature.properties.county_fips;
          state.layers.set(id, layer);
          const tooltip = document.createElement('span');
          tooltip.textContent = `${feature.properties.county_name}, ${feature.properties.state_abbr}`;
          layer.bindTooltip(tooltip, { sticky: true });
          layer.on('click', () => inspect(id));
        },
      }).addTo(state.map);
      homeMap();
      $('source-notes').innerHTML =
        `<p>Core data sources: Economic Policy Institute, Family Budget Calculator ${meta.manifest.edition_year}; costs in ${meta.manifest.cost_year} dollars. NOAA nClimDiv county average-temperature estimates from ${meta.manifest.climate_normal_period_start} to ${meta.manifest.climate_normal_period_end}. Census ${meta.manifest.geography_vintage} boundaries and ${meta.manifest.relationship_vintage} relationships.</p>
        <p><a href="https://www.epi.org/resources/budget/" target="_blank" rel="noopener noreferrer">EPI source and methodology</a> · EPI data: <a href="https://creativecommons.org/licenses/by-nc-sa/4.0/" target="_blank" rel="noopener noreferrer">CC BY-NC-SA 4.0</a> · <a href="https://www.ncei.noaa.gov/monitoring-content/data/us/climdiv/monthly/current/" target="_blank" rel="noopener noreferrer">NOAA nClimDiv source directory</a> · <a href="https://www.ncei.noaa.gov/monitoring-content/data/us/climdiv/monthly/current/normals-readme.txt" target="_blank" rel="noopener noreferrer">NOAA normals documentation</a>. Links require internet; the application does not.</p>
        <details><summary>Temperature method and coverage</summary><p>The climate profile uses area-weighted county estimates built by NOAA from gridded station observations. NOAA calls these long-term averages “normals” for convenience and states that they are not official NCEI Normals Program normals. COLE selects average-temperature element 02 and period code 0010, then stores the twelve monthly values at their published tenth-degree precision. Lexington city, Virginia has no county record in this source release; COLE leaves it unavailable. A temperature limit excludes an unavailable county from discovery, while a place lookup retains and marks it.</p></details>
        <details><summary>Tax and housing assumptions</summary><p>EPI assumes renting and employed adults. Housing uses local fair-market rent plus tenant-paid utilities. COLE does not estimate homeowner mortgage, property tax, insurance, maintenance or HOA costs. Two-adult profiles assume married partners. Children are ages 4; 4 and 8; 4, 8 and 12; or 4, 8, 12 and 16, depending on the selected count. Infant care is excluded.</p><p>EPI's modeled tax amount varies with state and family budget and includes federal income tax, state income tax and employee Social Security/Medicare. It excludes county and city income taxes. COLE removes that amount because users enter take-home pay; adding it again would count taxes twice. Sales taxes remain embedded in spending categories. <a href="https://www.epi.org/publication/family-budget-calculator-documentation/" target="_blank" rel="noopener noreferrer">Read EPI's technical documentation</a>.</p></details>`;
      $('manifest-details').textContent = JSON.stringify(meta.manifest, null, 2);
      const readyMessage = `${document.body?.dataset?.hosted === 'true' ? 'County' : 'Local'} data ready: ${meta.manifest.coverage.counties.toLocaleString()} counties, all ten household profiles; ${meta.manifest.coverage.climate_counties.toLocaleString()} counties have NOAA temperature profiles.`;
      $('startup-status').textContent = readyMessage;
      $('startup-status').hidden = true;
      say(readyMessage);
      state.ready = true;
      setBusy(false);
      window.ColeFeatures?.start({
        api,
        onDraftChange: markDraft,
        onMapChange: updateMap,
        refreshDetails: () => {
          renderDetails();
          renderComparison();
        },
      });
      window.addEventListener('resize', () => state.map.invalidateSize({ pan: false }));
    } catch (error) {
      if (!state.incompatible) {
        $('startup-status').hidden = false;
        $('startup-status').classList.add('error');
        $('startup-status').textContent =
          document.body?.dataset?.hosted === 'true'
            ? `COLE could not load: ${error.message} Try reloading shortly. Your current entries remain in this tab.`
            : `COLE could not load its local data or map: ${error.message} Restart COLE from the complete application folder. If the problem continues, extract a fresh copy of the complete package.`;
      }
    }
  }

  // Go.
  start();
})();
