/* Age controls and cached presentation register through the shared feature host. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const groups = [
    ['under_18', 'Under 18'], ['18_24', '18–24'], ['25_34', '25–34'], ['35_44', '35–44'],
    ['45_54', '45–54'], ['55_64', '55–64'], ['65_plus', '65 and over'],
  ];
  const metrics = [{ key: 'median_age', label: 'Median age', unit: 'years' },
    ...groups.map(([key, label]) => ({ key: `pct_${key}`, label: `Residents aged ${label.toLowerCase()}`, unit: '%' }))];
  const inputs = () => [...document.querySelectorAll('[data-age-input]')];
  const active = (bounds) => bounds && [bounds.min, bounds.max].some((value) => value != null && value !== '');
  let overview = null, started = false;
  let availability = { ready: false, busy: false, incompatible: false };

  function controls(values) {
    availability = values;
    const disabled = !overview?.available || !values.ready || values.busy || values.incompatible;
    inputs().forEach((input) => { input.disabled = disabled || Boolean(input.closest('[data-age-group][hidden]')); });
    document.querySelectorAll('[data-age-control]').forEach((input) => { input.disabled = disabled; });
    for (const [key] of groups) {
      const option = $(`age-group-select`).querySelector(`option[value="${key}"]`);
      option.disabled = !$(`age-group-${key}`).hidden;
    }
    $('age-add-group').disabled = disabled || !$('age-group-select').value;
    const count = inputs().filter((input) => input.value.trim() !== '').length;
    $('age-summary').textContent = count ? `${count} active` : 'None active';
  }

  function boundsText(bounds, unit) {
    const suffix = unit === '%' ? '%' : ' years';
    if (bounds.min != null && bounds.max != null) return `${bounds.min}${suffix} to ${bounds.max}${suffix} (inclusive)`;
    return bounds.min != null ? `at least ${bounds.min}${suffix}` : `at most ${bounds.max}${suffix}`;
  }

  function submitted(row, accepted, key) {
    const bounds = accepted?.age_limits?.[key];
    if (!active(bounds)) return '';
    const matched = row.age_checks?.[key];
    const unavailable = row.unavailable_age_filters?.includes(key);
    return `<p>Submitted: ${esc(boundsText(bounds, key === 'median_age' ? 'years' : '%'))}</p>
      <span class="badge ${matched === true ? 'climate-match' : 'outside'}">${unavailable ? 'County estimate unavailable' : matched === true ? 'Within submitted limits' : 'Outside submitted limits'}</span>`;
  }

  function panel(row, accepted) {
    if (availability.incompatible) return '<p>Reload COLE before using age data.</p>';
    if (!overview?.available) return '<p>Optional age data is unavailable.</p>';
    const record = row.age_metrics || overview.records[row.county_fips];
    const count = (value) => value == null ? 'Unavailable' : value.toLocaleString();
    return `<h3>County age profile</h3>
      <p class="hint">Census ACS 2020–2024 five-year estimates for all county residents. Population and age-group counts are estimates.</p>
      ${!record?.covered ? '<p>Age data is unavailable for this county geography.</p>' : ''}
      <div class="age-values"><article><h4>Median age</h4>
        <p class="age-estimate">${record?.median_age_years == null ? 'Unavailable' : `${esc(record.median_age_years)} years`}</p>
        <p class="hint">${record?.median_age_moe_years == null ? 'Published median-age margin of error unavailable.' : `90% margin of error: ±${esc(record.median_age_moe_years)} years.`}</p>
        ${submitted(row, accepted, 'median_age')}</article>
      <article><h4>Total population</h4><p class="age-estimate">${esc(count(record?.population))}</p>
        <p class="hint">${record?.population_moe == null ? 'No numeric population margin of error is supplied.' : `90% margin of error: ±${esc(count(record.population_moe))} people.`}</p></article>
      ${groups.map(([key, label]) => {
        const group = record?.groups?.[key];
        return `<article><h4>${esc(label)}</h4>
          <p class="age-estimate">${group?.display_percent == null ? 'Unavailable' : `${esc(group.display_percent)}%`}</p>
          <p class="hint">${esc(count(group?.population))} residents${group?.population == null ? '' : ' (estimate)'}</p>
          ${submitted(row, accepted, `pct_${key}`)}</article>`;
      }).join('')}</div>
      <details><summary>Source, uncertainty and percentage precision</summary>
        <p class="hint">The seven age groups partition the population. Each share combines the group's male and female population estimates and divides by the total county population. Median age is the published total-population median.</p>
        <p class="hint">Shares above are rounded to one decimal. Filters compare the unrounded count-to-total ratio with your limits. They use point estimates; the whole uncertainty range is not tested. Derived age-group count and percentage margins of error have not been calculated.</p>
        <p class="hint">The eight legacy Connecticut counties are unavailable because this ACS release uses planning regions. A matching place name does not establish matching boundaries.</p>
        <p class="hint"><a href="https://api.census.gov/data/2024/acs/acs5/groups/B01001.html" target="_blank" rel="noopener noreferrer">Census age-group definitions (internet required)</a> · <a href="https://api.census.gov/data/2024/acs/acs5/groups/B01002.html" target="_blank" rel="noopener noreferrer">Census median-age definition (internet required)</a></p>
      </details>`;
  }

  window.ColeFeatures.register('age_demographics', {
    async start(context) {
      if (started) return;
      started = true;
      $('age-filter').addEventListener('input', () => controls(availability));
      $('age-group-select').addEventListener('change', () => controls(availability));
      $('age-add-group').addEventListener('click', () => {
        const key = $('age-group-select').value;
        if (!groups.some(([candidate]) => key === candidate) || $('age-add-group').disabled) return;
        $(`age-group-${key}`).hidden = false;
        $('age-group-select').value = '';
        controls(availability);
        $(`age-pct_${key}-min`).focus();
        context.onDraftChange();
      });
      $('age-filter').addEventListener('click', (event) => {
        const button = event.target.closest('[data-age-remove]');
        if (!button || button.disabled) return;
        const fieldset = $(`age-group-${button.dataset.ageRemove}`);
        fieldset.querySelectorAll('input').forEach((input) => { input.value = ''; input.removeAttribute('aria-invalid'); });
        fieldset.hidden = true;
        controls(availability);
        $('age-group-select').focus();
        context.onDraftChange();
      });
      try {
        const data = await context.api('/api/features/age');
        if (!data.available || data.feature_version !== '1.0.0' || !/^[0-9a-f]{64}$/.test(data.contract) ||
            data.period_start !== 2020 || data.period_end !== 2024 || data.median_age_moe_confidence_percent !== 90 ||
            data.derived_share_moe_available !== false || !data.records || Array.isArray(data.records) ||
            Object.keys(data.records).length !== data.coverage?.core_counties || data.metrics?.length !== 8 ||
            !metrics.every(({ key }) => data.metrics.some((metric) => metric.key === key)) ||
            data.groups?.length !== 7 || !groups.every(([key]) => data.groups.some((group) => group.key === key)))
          throw new Error(data.error || 'The age data does not match this screen. Extract a complete copy of COLE and restart.');
        overview = data;
        $('age-coverage').textContent = `2020–2024 ACS age data covers ${data.coverage.covered_core_counties.toLocaleString()} of ${data.coverage.core_counties.toLocaleString()} COLE counties.`;
        $('age-status').hidden = true;
        controls(availability);
        context.refreshDetails();
        context.onDraftChange();
      } catch (error) {
        overview = null;
        $('age-status').hidden = false;
        $('age-status').textContent = `Age data unavailable: ${error.message} Leave the age fields blank to search without this feature.`;
        controls(availability);
      }
    },
    readScenario(scenario) {
      const limits = Object.fromEntries(metrics.map(({ key }) => [key, {
        min: $(`age-${key}-min`).value.trim() || null, max: $(`age-${key}-max`).value.trim() || null,
      }]));
      return Object.values(limits).some(active) ? { ...scenario, age_limits: limits, age_feature_contract: overview?.contract || null } : scenario;
    },
    summaryRows(scenario) {
      return metrics.filter(({ key }) => active(scenario.age_limits?.[key])).map(({ key, label, unit }) =>
        [label, `${boundsText(scenario.age_limits[key], unit)} · ACS 2020–2024 estimate${unit === '%' ? ', unrounded share' : ''}`]);
    },
    badge(row) {
      if (row.age_filters_match == null) return '';
      const names = (keys) => keys.map((key) => metrics.find((metric) => metric.key === key)?.label || key).join(', ');
      return [row.failed_age_filters?.length ? `<span class="badge outside">Outside age limits: ${esc(names(row.failed_age_filters))}</span>` : '',
        row.unavailable_age_filters?.length ? `<span class="badge outside">Age data unavailable: ${esc(names(row.unavailable_age_filters))}</span>` : '',
        row.age_filters_match ? '<span class="badge climate-match">Age limits met</span>' : ''].join('');
    },
    details(row, accepted) {
      $('age-county-panel').hidden = !row;
      $('age-county-label').textContent = row ? `${row.county_name}, ${row.state_abbr}` : '';
      $('age-county-body').innerHTML = row ? panel(row, accepted) : '';
    },
    comparison(row, accepted) {
      return `<details class="comparison-breakdown age-feature"><summary>County age profile</summary>${panel(row, accepted)}</details>`;
    },
    mapBaseStyle(id, style, { resultMap }) {
      return resultMap.get(id)?.age_filters_match === false ? { ...style, fillColor: '#c88e79' } : style;
    },
    syncControls: controls,
    reset() {
      inputs().forEach((input) => { input.value = ''; input.removeAttribute('aria-invalid'); });
      groups.forEach(([key]) => { $(`age-group-${key}`).hidden = true; });
      $('age-group-select').value = '';
      controls(availability);
    },
  });
})();
