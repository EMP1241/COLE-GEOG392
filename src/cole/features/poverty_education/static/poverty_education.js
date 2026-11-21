/* County ACS controls and presentation belong to this feature; no other module's state is accessed. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const measures = [
    { key: 'poverty', label: 'Poverty rate' },
    { key: 'education', label: "Bachelor's degree or higher (age 25+)" },
  ];
  let overview = null, started = false;
  let availability = { ready: false, busy: false, incompatible: false };
  const inputs = () => [...document.querySelectorAll('[data-poverty-education]')];
  const active = (bounds) => bounds && [bounds.min, bounds.max].some((value) => value != null && value !== '');

  function controls(values) {
    availability = values;
    const disabled = !overview?.available || !values.ready || values.busy || values.incompatible;
    inputs().forEach((input) => { input.disabled = disabled; });
    const count = inputs().filter((input) => input.value.trim() !== '').length;
    $('poverty-education-summary').textContent = count ? `${count} active` : 'None active';
  }

  function limitsText(bounds) {
    if (bounds.min != null && bounds.max != null) return `${bounds.min}% to ${bounds.max}% (inclusive)`;
    return bounds.min != null ? `at least ${bounds.min}%` : `at most ${bounds.max}%`;
  }

  function panel(row, accepted) {
    if (availability.incompatible) return '<p>Reload COLE before using poverty and education data.</p>';
    if (!overview?.available) return '<p>Optional poverty and education data is unavailable.</p>';
    const records = row.poverty_education_metrics || overview.records[row.county_fips];
    return `<h3>Poverty and educational attainment</h3>
      <p class="hint">Census ACS 2020–2024 five-year estimates. Margins of error use 90% confidence and are given in percentage points.</p>
      <p class="hint">Poverty is the percentage below the poverty level among people whose poverty status is determined. Degree attainment is the percentage of adults age 25 and over with a bachelor's degree or higher. A match checks the published estimate, not its whole uncertainty range.</p>
      <div class="poverty-education-values">${measures.map(({ key, label }) => {
        const record = records?.[key];
        const bounds = accepted?.poverty_education_limits?.[key];
        const unavailable = record?.estimate_percent == null;
        const matched = row.poverty_education_checks?.[key];
        const status = unavailable ? 'County estimate unavailable' : matched === true ? 'Within submitted limits' : 'Outside submitted limits';
        return `<article><h4>${esc(label)}</h4>
          <p class="poverty-education-estimate">${unavailable ? 'Unavailable' : `${esc(record.estimate_percent)}%`}</p>
          <p class="hint">${unavailable ? 'No value has been substituted from another county or geography.' :
            record.margin_of_error_points == null ? 'Margin of error unavailable.' : `Margin of error: ±${esc(record.margin_of_error_points)} percentage points.`}</p>
          ${active(bounds) ? `<p>Submitted: ${esc(limitsText(bounds))}</p><span class="badge ${matched === true ? 'climate-match' : 'outside'}">${esc(status)}</span>` : ''}
        </article>`;
      }).join('')}</div>
      <details><summary>What these figures mean</summary>
        <p class="hint">Poverty is measured among people whose poverty status is determined. Education describes adults age 25 and over with a bachelor's degree or higher, not a full degree breakdown or school quality. These are county population estimates, not your personal finances.</p>
        <p class="hint">Filters compare the published estimates with your limits. The margins of error show sampling uncertainty; a match does not establish that the whole confidence interval is within your range. Zero is preserved as zero even when its margin of error is large. Some counties use different geographic boundaries and remain unavailable here.</p>
        <p class="hint">College dormitory residents are excluded from the poverty measure. Off-campus students can affect a county's poverty rate.</p>
        <p class="hint"><a href="https://www.census.gov/programs-surveys/acs/guidance/estimates.html" target="_blank" rel="noopener noreferrer">Census ACS guidance (internet required)</a></p>
      </details>`;
  }

  window.ColeFeatures.register('poverty_education', {
    async start(context) {
      if (started) return;
      started = true;
      $('poverty-education-filter').addEventListener('input', () => controls(availability));
      try {
        const data = await context.api('/api/features/poverty-education');
        if (!data.available || data.feature_version !== '1.0.0' || !/^[0-9a-f]{64}$/.test(data.contract) ||
            data.period_start !== 2020 || data.period_end !== 2024 || data.moe_confidence_percent !== 90 ||
            !data.records || Array.isArray(data.records) || Object.keys(data.records).length !== data.coverage?.cole_counties ||
            data.metrics?.length !== 2 || !measures.every(({ key }) => data.metrics.some((metric) => metric.key === key)))
          throw new Error(data.error || 'The poverty and education data does not match this screen. Extract a complete copy of COLE and restart.');
        overview = data;
        const coverage = data.coverage.metrics;
        $('poverty-education-coverage').textContent = `2020–2024 ACS data covers ${coverage.poverty.covered_core_counties.toLocaleString()} counties for poverty and ${coverage.education.covered_core_counties.toLocaleString()} for educational attainment.`;
        $('poverty-education-status').hidden = true;
        controls(availability);
        context.refreshDetails();
        context.onDraftChange();
      } catch (error) {
        overview = null;
        $('poverty-education-status').hidden = false;
        $('poverty-education-status').textContent = `Poverty and education unavailable: ${error.message} Leave these fields blank to search without this feature.`;
        controls(availability);
      }
    },
    readScenario(scenario) {
      const limits = {};
      for (const { key } of measures) {
        limits[key] = { min: $(`${key}-min`).value.trim() || null, max: $(`${key}-max`).value.trim() || null };
      }
      return Object.values(limits).some(active) ? { ...scenario, poverty_education_limits: limits, poverty_education_feature_contract: overview?.contract || null } : scenario;
    },
    summaryRows(scenario) {
      return measures.filter(({ key }) => active(scenario.poverty_education_limits?.[key])).map(({ key, label }) =>
        [label, `${limitsText(scenario.poverty_education_limits[key])} · ACS 2020–2024 published estimate`]);
    },
    badge(row) {
      if (row.poverty_education_filters_match == null) return '';
      const names = (keys) => keys.map((key) => measures.find((measure) => measure.key === key)?.label || key).join(', ');
      return [row.failed_poverty_education_filters?.length ? `<span class="badge outside">Outside submitted limits: ${esc(names(row.failed_poverty_education_filters))}</span>` : '',
        row.unavailable_poverty_education_filters?.length ? `<span class="badge outside">County data unavailable: ${esc(names(row.unavailable_poverty_education_filters))}</span>` : '',
        row.poverty_education_filters_match ? '<span class="badge climate-match">Poverty/education limits met</span>' : ''].join('');
    },
    details(row, accepted) {
      $('poverty-education-county-panel').hidden = !row;
      $('poverty-education-county-label').textContent = row ? `${row.county_name}, ${row.state_abbr}` : '';
      $('poverty-education-county-body').innerHTML = row ? panel(row, accepted) : '';
    },
    comparison(row, accepted) {
      return `<details class="comparison-breakdown poverty-education-feature"><summary>Poverty and educational attainment</summary>${panel(row, accepted)}</details>`;
    },
    mapBaseStyle(id, style, { resultMap }) {
      return resultMap.get(id)?.poverty_education_filters_match === false ? { ...style, fillColor: '#c88e79' } : style;
    },
    syncControls: controls,
    reset() {
      inputs().forEach((input) => { input.value = ''; });
      controls(availability);
    },
  });
})();
