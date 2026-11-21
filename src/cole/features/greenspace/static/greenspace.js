/* Feature-owned controls and presentation; the host owns accepted scenarios. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const groups = [['federal', 'Federal'], ['state', 'State'], ['local_regional', 'Local / regional'],
    ['territorial', 'Territorial'], ['shared_public', 'Shared public ownership']];
  const inputs = () => [...document.querySelectorAll('[data-greenspace-input]')];
  const active = (bounds) => bounds && [bounds.min, bounds.max].some((value) => value != null && value !== '');
  const number = (value, places) => Number(value).toLocaleString(undefined, {
    minimumFractionDigits: places, maximumFractionDigits: places,
  });
  const percent = (value) => Number(value) > 0 && Number(value) < 0.005 ? '<0.01%' : `${number(value, 2)}%`;
  const acres = (value) => Number(value) > 0 && Number(value) < 0.05 ? '<0.1 acre' : `${number(value, 1)} acres`;
  let overview = null, started = false;
  let availability = { ready: false, busy: false, incompatible: false };

  function controls(values) {
    availability = values;
    const disabled = !overview?.available || !values.ready || values.busy || values.incompatible;
    inputs().forEach((input) => { input.disabled = disabled; });
    $('greenspace-summary').textContent = inputs().some((input) => input.value.trim() !== '') ? 'Limits active' : 'None active';
  }

  function boundsText(bounds) {
    if (bounds.min != null && bounds.max != null) return `${bounds.min}% to ${bounds.max}% (inclusive)`;
    return bounds.min != null ? `at least ${bounds.min}%` : `at most ${bounds.max}%`;
  }

  function panel(row, accepted) {
    if (availability.incompatible) return '<p>Reload COLE before using Public outdoor areas data.</p>';
    if (!overview?.available) return '<p>Optional Public outdoor areas data is unavailable.</p>';
    const record = row.greenspace_metrics || overview.records[row.county_fips];
    if (!record) return '<p>Public outdoor areas data is unavailable for this county.</p>';
    const bounds = accepted?.greenspace_limits;
    const submitted = active(bounds) ? `<p>Submitted: ${esc(boundsText(bounds))}</p>
      <span class="badge ${row.greenspace_filters_match === true ? 'climate-match' : 'outside'}">${row.greenspace_filters_match === true ? 'Within submitted limits' : 'Outside submitted limits'}</span>` : '';
    return `<h3>Public parks and outdoor areas</h3>
      <p class="hint">USGS PAD-US 4.1 · estimated documented public outdoor space, with private, military and restricted/closed land excluded.</p>
      <p class="greenspace-estimate">${esc(percent(record.percent))} of county area · ${esc(acres(record.acres))}</p>
      <p class="hint">Percentage of the whole county, including water. Total county area: ${esc(acres(record.county_area_acres))}.</p>
      ${record.coverage_status === 'no_qualifying_mapped_area' ? '<p class="greenspace-coverage"><strong>No qualifying mapped area.</strong> This does not mean there are no parks or outdoor opportunities in this county. This mapped value is treated as 0% when filtering.</p>' : ''}
      ${record.trace_area ? '<p class="greenspace-coverage"><strong>Trace mapped area, below 1 m².</strong> This tiny value may result from numerical processing. The area is too small to interpret confidently; filters still use the stored estimate.</p>' : ''}
      ${submitted}
      <div class="greenspace-values">${groups.map(([key, label]) => `<article><h4>${esc(label)}</h4>
        <p>${esc(acres(record.ownership[key].acres))} · ${esc(percent(record.ownership[key].percent))} of county</p></article>`).join('')}</div>
      <details><summary>Definition, source and limits</summary>
        <p class="hint">Counts the overlap of documented public ownership and Open public access. Parks, forests, refuges, beaches and other outdoor areas can qualify. Private and nonprofit ownership, military land, and Restricted or Closed access are excluded. Unknown evidence alone contributes no area.</p>
        <p class="hint">Overlapping records count once. Federal, state, local/regional and territorial amounts are exclusive ownership categories. Areas with overlapping ownership claims from multiple public groups appear under shared public ownership. Management is not used as a substitute for ownership.</p>
        <p class="hint">The inventory combines different source dates and may omit local parks or contain outdated access information. This is not vegetation cover, travel distance, recreation quality or a guarantee of current opening hours. Check the managing agency before visiting.</p>
        <p class="hint">The estimate uses full 2021 Census county boundaries, including water, in an equal-area projection. Percentages are displayed to two decimals and acreage to one; rounded ownership values may not sum exactly. Filters compare the stored percentage before display rounding.</p>
        <p class="hint"><a href="https://www.usgs.gov/programs/gap-analysis-project/science/pad-us-data-overview" target="_blank" rel="noopener noreferrer">USGS PAD-US overview (internet required)</a></p>
      </details>`;
  }

  window.ColeFeatures.register('greenspace', {
    async start(context) {
      if (started) return;
      started = true;
      $('greenspace-filter').addEventListener('input', () => controls(availability));
      try {
        const data = await context.api('/api/features/greenspace');
        if (!data.available || data.feature_version !== '1.0.0' || !/^[0-9a-f]{64}$/.test(data.contract) ||
            data.dataset !== 'PAD-US 4.1' || data.county_vintage !== 2021 || data.denominator !== 'whole_county_including_water' ||
            !data.records || Array.isArray(data.records) || Object.keys(data.records).length !== data.coverage?.core_counties ||
            data.ownership?.length !== 5 || !groups.every(([key]) => data.ownership.some((group) => group.key === key)))
          throw new Error(data.error || 'The Public outdoor areas data does not match this screen. Extract a complete copy of COLE and restart.');
        overview = data;
        $('greenspace-coverage').textContent = `PAD-US 4.1 summaries for ${data.coverage.core_counties.toLocaleString()} COLE counties; ${data.coverage.no_qualifying_mapped_area.toLocaleString()} have no qualifying mapped area.`;
        $('greenspace-status').hidden = true;
        controls(availability);
        context.refreshDetails();
        context.onDraftChange();
      } catch (error) {
        overview = null;
        $('greenspace-status').hidden = false;
        $('greenspace-status').textContent = `Public outdoor areas data unavailable: ${error.message} Leave the Public outdoor areas fields blank to search without this feature.`;
        controls(availability);
      }
    },
    readScenario(scenario) {
      const limits = { min: $('greenspace-min').value.trim() || null, max: $('greenspace-max').value.trim() || null };
      return active(limits) ? { ...scenario, greenspace_limits: limits, greenspace_feature_contract: overview?.contract || null } : scenario;
    },
    summaryRows(scenario) {
      return active(scenario.greenspace_limits) ? [['Public outdoor areas', `${boundsText(scenario.greenspace_limits)} of whole county, including water · PAD-US 4.1 mapped estimate`]] : [];
    },
    badge(row) {
      if (row.greenspace_filters_match == null) return '';
      return `<span class="badge ${row.greenspace_filters_match ? 'climate-match' : 'outside'}">${row.greenspace_filters_match ? 'Public outdoor areas limits met' : 'Outside Public outdoor areas limits'}</span>`;
    },
    details(row, accepted) {
      $('greenspace-county-panel').hidden = !row;
      $('greenspace-county-label').textContent = row ? `${row.county_name}, ${row.state_abbr}` : '';
      $('greenspace-county-body').innerHTML = row ? panel(row, accepted) : '';
    },
    comparison(row, accepted) {
      return `<details class="comparison-breakdown greenspace-feature"><summary>Public parks and outdoor areas</summary>${panel(row, accepted)}</details>`;
    },
    mapBaseStyle(id, style, { resultMap }) {
      return resultMap.get(id)?.greenspace_filters_match === false ? { ...style, fillColor: '#c88e79' } : style;
    },
    syncControls: controls,
    reset() {
      inputs().forEach((input) => { input.value = ''; input.removeAttribute('aria-invalid'); });
      controls(availability);
    },
  });
})();
