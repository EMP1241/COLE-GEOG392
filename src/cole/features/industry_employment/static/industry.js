/* The industry module owns controls, profiles, navigation, requests and bounded caches. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const cache = new Map();
  const contexts = new WeakMap();
  const bound = new WeakSet();
  const requests = new Set();
  let overview, api, started = false, availability = {}, lastRow, lastAccepted;
  const title = (code) => overview?.sectors.find((item) => item.code === code)?.title || code;
  const selects = () => [...document.querySelectorAll('[data-industry-sector]')];
  const selectedCodes = () => [...new Set(selects().map((element) => element.value).filter(Boolean))].sort();
  const acceptedCodes = (scenario) => scenario?.industry_sectors || (scenario?.industry_sector ? [scenario.industry_sector] : []);
  const statuses = {
    top_five: 'In the top five by disclosed employment',
    outside_top_five: 'Outside the top five by disclosed employment',
    withheld: 'Employment withheld; top-five qualification is not established',
    no_source_row: 'No source row; top-five qualification is not established',
    county_unavailable: 'County industry data unavailable',
  };

  function selectedMessage(row, accepted) {
    const codes = acceptedCodes(accepted);
    if (!codes.length) return '';
    const rule = accepted?.industry_match_mode === 'any' ? 'Any selected' : 'All selected';
    const items = row.industry_sector_results || codes.map((code) => ({ code,
      status: row.industry_data_availability === 'unavailable' ? 'county_unavailable' :
        row.industry_filters_match ? 'top_five' : 'outside_top_five' }));
    return `<p><strong>Industry requirement: ${esc(rule)}.</strong> ${row.industry_filters_match ? 'Requirement met.' : 'Requirement not met.'}</p>
      <ul class="industry-selection-status">${items.map((item) => `<li><strong>${esc(title(item.code))}</strong>: ${esc(statuses[item.status] || 'Qualification not established')}.</li>`).join('')}</ul>`;
  }

  function fillOptions(select) {
    for (const sector of overview.sectors) {
      const option = document.createElement('option');
      option.value = sector.code;
      option.textContent = `${sector.code} · ${sector.title}`;
      select.append(option);
    }
  }

  function numberRows() {
    const rows = [...document.querySelectorAll('[data-industry-row]')];
    rows.forEach((row, index) => {
      const select = row.querySelector('[data-industry-sector]');
      select.id = index ? `industry-sector-${index + 1}` : 'industry-sector';
      row.querySelector('label').htmlFor = select.id;
      row.querySelector('[data-industry-label]').textContent = `Industry ${index + 1}`;
      const remove = row.querySelector('[data-industry-remove]');
      remove.hidden = rows.length === 1;
      remove.setAttribute('aria-label', `Remove industry ${index + 1}${select.value ? ': ' + title(select.value) : ''}`);
    });
  }

  function addRow() {
    const row = document.createElement('div');
    row.className = 'industry-selection-row'; row.dataset.industryRow = '';
    const label = document.createElement('label'); label.className = 'industry-select-label';
    const text = document.createElement('span'); text.dataset.industryLabel = '';
    const select = document.createElement('select');
    select.dataset.industrySector = ''; select.dataset.field = 'industry_sectors';
    const blank = document.createElement('option'); blank.value = ''; blank.textContent = 'Choose an industry';
    select.append(blank); fillOptions(select); label.append(text, select);
    const remove = document.createElement('button');
    remove.type = 'button'; remove.className = 'button secondary small';
    remove.dataset.industryRemove = ''; remove.textContent = 'Remove';
    row.append(label, remove); $('industry-selections').append(row);
    numberRows(); controls(availability); select.focus();
  }

  function cleanup() {
    for (const request of requests) {
      if (!request.root.isConnected) { request.controller.abort(); requests.delete(request); }
    }
  }

  function controls(values) {
    availability = values;
    if (values.incompatible) {
      for (const request of requests) {
        request.controller.abort();
        if (request.root.isConnected) {
          request.root.setAttribute('aria-busy', 'false');
          request.root.textContent = 'Reload COLE before exploring industry employment.';
        }
      }
      requests.clear();
    }
    const disabled = !overview?.available || !values.ready || values.busy || values.incompatible;
    const elements = selects(), codes = selectedCodes();
    numberRows();
    for (const element of elements) {
      element.disabled = disabled;
      for (const option of [...(element.options || [])])
        option.disabled = Boolean(option.value && option.value !== element.value && codes.includes(option.value));
    }
    for (const button of document.querySelectorAll('[data-industry-remove]')) button.disabled = disabled;
    $('industry-add').disabled = disabled || elements.length >= 20 || elements.some((element) => !element.value);
    if (!codes.length) $('industry-match-mode').value = 'all';
    $('industry-match-mode').disabled = disabled || !codes.length;
    const mode = $('industry-match-mode').value === 'any' ? 'Any selected' : 'All selected';
    $('industry-group-summary').textContent = codes.length ? `${codes.length} ${codes.length === 1 ? 'industry' : 'industries'} · ${mode}` : 'None active';
    document.querySelectorAll('.industry-feature .industry-explore').forEach((button) => { button.disabled = disabled; });
  }

  function list(items) {
    return `<ol class="industry-list">${items.map((item) => `<li>
      <strong>${esc(item.code)} · ${esc(item.title)}</strong>
      <p class="industry-value">${item.disclosed_employment == null ?
        item.source_status === 'withheld' ? 'Employment withheld' : 'No source row for this county' :
        `${Number(item.disclosed_employment).toLocaleString()} disclosed jobs${item.incomplete ? ' · incomplete total' : ''}`}
        ${item.top_five ? ` · rank ${item.rank}` : ''}</p>
      ${item.has_children ? `<button type="button" class="button secondary small industry-explore" data-industry-code="${esc(item.code)}"
        aria-label="View industry breakdown for ${esc(item.title)}">View industry breakdown</button>` : '<p class="hint">Most detailed category in this release.</p>'}
    </li>`).join('')}</ol>`;
  }

  function render(root, data, context, focus) {
    const ancestor = data.ancestry.at(-1);
    const heading = ancestor ? `${ancestor.code} · ${ancestor.title}` : 'Top five by disclosed employment';
    const top = data.items.filter((item) => item.top_five);
    root.innerHTML = `<h3 tabindex="-1">${esc(heading)}</h3>${selectedMessage(context.row, context.accepted)}
      <p class="hint">BLS QCEW ${data.year} annual average employment. Private, federal, state and local published counts combined.</p>
      ${data.ranking_incomplete ? '<p class="industry-warning">Some broad-sector employment counts are withheld. Rankings use disclosed counts only; actual totals and rankings may differ.</p>' : ''}
      ${ancestor ? `<div class="industry-navigation"><button type="button" class="button secondary small industry-explore" data-industry-code="">All broad sectors</button>
        ${ancestor.parent ? `<button type="button" class="button secondary small industry-explore" data-industry-code="${esc(ancestor.parent)}">Back to ${esc(ancestor.parent)}</button>` : ''}</div>` : ''}
      ${!data.available ? '<p>Industry employment is unavailable for this county. A nearby or statewide value has not been substituted.</p>' :
        ancestor ? `<p class="hint">Subcategories are shown at one level at a time. Withheld values remain unknown; these counts do not establish a complete parent-sector total.</p>${list(data.items)}` :
        `${top.length ? list(top) : '<p>No sector has a disclosed employment count to rank.</p>'}
          <details><summary>Explore other broad sectors</summary>${list(data.items.filter((item) => !item.top_five))}</details>`}
      <p class="hint">These are annual-average filled jobs covered by the source employment program, not job openings, jobs created, or a count of county residents. Fifth-place ties are included in the broad-sector ranking.</p>
      <p class="hint"><a href="https://www.bls.gov/cew/questions-and-answers.htm" target="_blank" rel="noopener noreferrer">BLS source definitions and disclosure guidance</a></p>`;
    root.setAttribute('aria-busy', 'false');
    controls(availability);
    if (focus) root.querySelector('h3')?.focus({ preventScroll: true });
  }

  async function navigate(root, parent = null, focus = false) {
    const context = contexts.get(root);
    if (!context || !overview?.available || !availability.ready || availability.incompatible) {
      if (availability.incompatible) root.textContent = 'Reload COLE before exploring industry employment.';
      return;
    }
    context.controller?.abort();
    const controller = new AbortController();
    context.controller = controller;
    context.parent = parent;
    const request = { root, controller };
    requests.add(request);
    const key = `${overview.contract}:${context.row.county_fips}:${parent || ''}`;
    root.setAttribute('aria-busy', 'true');
    root.innerHTML = '<p role="status">Loading industry employment…</p>';
    try {
      let data = cache.get(key);
      if (data) { cache.delete(key); cache.set(key, data); }
      else {
        const params = new URLSearchParams({ contract: overview.contract });
        if (parent) params.set('parent', parent);
        data = await api(`/api/features/industry-employment/county/${encodeURIComponent(context.row.county_fips)}?${params}`, undefined, controller.signal);
        if (data.contract !== overview.contract || data.county_fips !== context.row.county_fips || data.parent !== parent)
          throw new Error('The industry profile differs from the selected county or data release. Reload COLE.');
        if (controller.signal.aborted || !root.isConnected || contexts.get(root) !== context || context.controller !== controller) return;
        cache.set(key, data);
        if (cache.size > 64) cache.delete(cache.keys().next().value);
      }
      if (!controller.signal.aborted && root.isConnected && contexts.get(root) === context && context.controller === controller)
        render(root, data, context, focus);
    } catch (error) {
      if (error.name !== 'AbortError' && !controller.signal.aborted && root.isConnected && contexts.get(root) === context && context.controller === controller) {
        root.setAttribute('aria-busy', 'false');
        const message = Array.isArray(error.fields)
          ? error.fields.map((field) => field?.message).filter(Boolean).join(' ') || error.message
          : error.message;
        root.innerHTML = `<p role="status">${esc(message)}</p><button type="button" class="button secondary small industry-explore" data-industry-code="${esc(parent || '')}">Retry industry profile</button>`;
        controls(availability);
      }
    } finally { requests.delete(request); }
  }

  function attach(root, row, accepted) {
    const old = contexts.get(root);
    old?.controller?.abort();
    contexts.set(root, { row, accepted, parent: null });
    if (!bound.has(root)) {
      bound.add(root);
      root.addEventListener('click', (event) => {
        const button = event.target.closest('.industry-explore');
        if (button && root.contains(button) && !button.disabled)
          navigate(root, button.dataset.industryCode || null, true);
      });
    }
    navigate(root);
  }

  window.ColeFeatures.register('industry_employment', {
    async start(context) {
      if (started) return;
      started = true;
      api = context.api;
      try {
        const data = await api('/api/features/industry-employment');
        if (!data.available || data.feature_version !== '1.0.0' || data.selection_version !== 2 ||
            !['all', 'any'].every((mode) => data.match_modes?.includes(mode)) || data.sectors.length !== 20)
          throw new Error(data.error || 'The industry data does not match this screen. Extract a complete copy of COLE and restart.');
        overview = data;
        lastRow = lastAccepted = null;
        fillOptions($('industry-sector'));
        $('industry-selections').addEventListener('input', () => controls(availability));
        $('industry-match-mode').addEventListener('input', () => controls(availability));
        $('industry-add').addEventListener('click', () => {
          if ($('industry-add').disabled) return;
          addRow(); context.onDraftChange();
        });
        $('industry-selections').addEventListener('click', (event) => {
          const button = event.target.closest('[data-industry-remove]');
          if (!button || button.disabled || !button.closest('[data-industry-row]')) return;
          const rows = [...document.querySelectorAll('[data-industry-row]')];
          if (rows.length <= 1) return;
          const index = rows.indexOf(button.closest('[data-industry-row]'));
          rows[index].remove(); numberRows(); controls(availability);
          selects()[Math.min(index, selects().length - 1)]?.focus();
          context.onDraftChange();
        });
        $('industry-coverage').textContent = `${data.year} BLS employment records cover ${data.coverage.employment_counties.toLocaleString()} counties. County details include industry breakdowns.`;
        $('industry-feature-status').hidden = true;
        controls(availability);
        context.refreshDetails();
        context.onDraftChange();
      } catch (error) {
        overview = null;
        $('industry-feature-status').hidden = false;
        $('industry-feature-status').textContent = `Industry employment unavailable: ${error.message} Searches without an industry filter remain available.`;
        controls(availability);
      }
    },
    readScenario(scenario) {
      const sectors = selectedCodes();
      return overview?.available && sectors.length ? { ...scenario, industry_sectors: sectors,
        industry_match_mode: $('industry-match-mode').value, industry_feature_contract: overview.contract } : scenario;
    },
    summaryRows(scenario) {
      const codes = acceptedCodes(scenario);
      if (!codes.length) return [];
      const mode = scenario.industry_match_mode === 'any' ? 'Any selected' : 'All selected';
      return [['Industry employment', `${mode}: ${codes.map(title).join('; ')}. Top five by disclosed employment, including fifth-place ties.`]];
    },
    badge(row) {
      if (row.industry_filters_match == null) return '';
      if (row.industry_sector_results) {
        const label = row.industry_data_availability === 'unavailable' ? 'Industry data unavailable' :
          row.industry_filters_match ? (row.industry_match_mode === 'any' ? 'At least one industry qualifies' : 'All selected industries qualify') :
            'Industry requirement not met';
        return `<span class="badge ${row.industry_filters_match ? 'climate-match' : 'outside'}">${esc(label)}</span>`;
      }
      return `<span class="badge ${row.industry_filters_match ? 'climate-match' : 'outside'}">${row.industry_filters_match ? 'Top five by disclosed employment' :
        row.industry_data_availability === 'unavailable' ? 'Industry data unavailable' : 'Outside top five by disclosed employment'}</span>`;
    },
    details(row, accepted) {
      cleanup();
      $('industry-county-panel').hidden = !row;
      if (!row) {
        contexts.get($('industry-county-body'))?.controller?.abort();
        $('industry-county-body').replaceChildren();
        lastRow = lastAccepted = null;
        return;
      }
      if (row === lastRow && accepted === lastAccepted) return;
      lastRow = row; lastAccepted = accepted;
      $('industry-county-label').textContent = `${row.county_name}, ${row.state_abbr}`;
      if (overview?.available) attach($('industry-county-body'), row, accepted);
      else $('industry-county-body').textContent = 'Optional industry employment data is unavailable.';
    },
    comparison(row) {
      return `<details class="comparison-breakdown industry-feature"><summary>Industry employment</summary><div data-industry-profile="${esc(row.county_fips)}"></div></details>`;
    },
    comparisonReady(rows, accepted) {
      cleanup();
      document.querySelectorAll('[data-industry-profile]').forEach((root) => {
        const row = rows.find((item) => item.county_fips === root.dataset.industryProfile);
        if (availability.incompatible) root.textContent = 'Reload COLE before exploring industry employment.';
        else if (row && overview?.available && availability.ready) attach(root, row, accepted);
        else root.textContent = 'Optional industry employment data is unavailable.';
      });
    },
    mapBaseStyle(id, style, { resultMap }) {
      return resultMap.get(id)?.industry_filters_match === false ? { ...style, fillColor: '#c88e79' } : style;
    },
    syncControls: controls,
    reset() {
      for (const request of requests) request.controller.abort();
      requests.clear();
      for (const row of [...document.querySelectorAll('[data-industry-row]')].slice(1)) row.remove();
      $('industry-sector').value = '';
      $('industry-match-mode').value = 'all';
      lastRow = lastAccepted = null;
      controls(availability);
    },
  });
})();
