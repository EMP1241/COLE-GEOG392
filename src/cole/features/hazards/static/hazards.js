// Hazard model/presentation: Bingqian. Application logic and integration: Brandon Santor, 2026-10-12.
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value ?? '').replace(
      /[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
    );
  const HAZARD_TYPES = [
    { key: 'overall', label: 'All hazards combined' },
    { key: 'hurricane', label: 'Hurricane' },
    { key: 'wildfire', label: 'Wildfire' },
    { key: 'inland_flooding', label: 'Inland flooding' },
    { key: 'coastal_flooding', label: 'Coastal flooding' },
    { key: 'tornado', label: 'Tornado' },
    { key: 'earthquake', label: 'Earthquake' },
    { key: 'winter_weather', label: 'Winter weather' },
    { key: 'heat_wave', label: 'Heat wave', measure: 'life' },
  ];
  // Which measure each hazard's limit filters on: property, except heat wave,
  // whose harm is almost entirely to people. Checked against the server.
  const filterMeasure = (key) =>
    HAZARD_TYPES.find((hazard) => hazard.key === key)?.measure || 'property';
  const MEASURE_NAMES = { property: 'building-loss rate', life: 'population-loss rate' };
  const HAZARD_CLASS_LABELS = {
    very_low: 'Very Low',
    low: 'Low',
    moderate: 'Moderate',
    high: 'High',
    very_high: 'Very High',
    not_applicable: 'Not applicable',
    no_rating: 'No modeled loss',
    insufficient_data: 'Insufficient data',
  };
  const HAZARD_LIMIT_CHOICES = ['very_low', 'low', 'moderate', 'high'];
  const HAZARD_CLASSES = ['very_low', 'low', 'moderate', 'high', 'very_high'];
  // FEMA's composite community risk rating, shown for context only.
  const COMMUNITY_RISK_LABELS = {
    very_low: 'Very Low',
    relatively_low: 'Relatively Low',
    relatively_moderate: 'Relatively Moderate',
    relatively_high: 'Relatively High',
    very_high: 'Very High',
    insufficient_data: 'Insufficient Data',
  };
  // One-hue ordinal ramp, light to dark (validated: monotone lightness, single
  // hue, lightest step clears 2:1 on the page). Neutral gray = no exposure;
  // darker gray = no data. The legend and tooltips always name the rating.
  const HAZARD_COLORS = {
    very_low: '#d6a88f',
    low: '#d4875f',
    moderate: '#bf6236',
    high: '#91451f',
    very_high: '#5d2b13',
    not_applicable: '#f3efe8',
    no_rating: '#f3efe8',
    insufficient_data: '#8f989b',
    missing: '#8f989b',
  };
  // Counties outside the current results, in hazard shading.
  const HAZARD_NOT_RETURNED = '#c9d2d0';
  function hazardLimitText(value) {
    return HAZARD_CLASS_LABELS[value] + ' or lower';
  }
  function hazardSwatch(value) {
    return `<i class="swatch${value === 'not_applicable' || value === 'no_rating' ? ' hazard-neutral' : ''}" style="background:${HAZARD_COLORS[value] || HAZARD_COLORS.missing}" aria-hidden="true"></i>`;
  }
  // A percentile in words, from its one-decimal string (no float maths).
  // The shown value is rounded, so the sentence is stated conservatively:
  // a value ending in .0 may have been rounded up (96.97 -> 97.0), so it
  // claims one point less. A large tie can still lift a midrank past the
  // share strictly below it, so tools/prepare_hazards.py checks every
  // sentence against exact counts (hazards.plain_rank_claim mirrors this).
  // 98.1 -> "more than 98% of other counties", 97.0 -> "more than 96% ...",
  // 7.3 -> "less than 92% ...", 7.0 -> "less than 92% ...".
  function plainRank(percentile) {
    const [whole, tenth] = percentile.split('.');
    const floor = Number(whole),
      rounded = tenth === '0';
    if (floor >= 50) return `more than ${rounded ? floor - 1 : floor}% of other counties`;
    // Below 50 the share of counties above is 100 - p; rounded or not, it
    // stays above 99 - floor(p).
    return `less than ${99 - floor}% of other counties`;
  }
  // A small 0-100 bar in the class colours with a marker at the county.
  function hazardMiniScale(percentile) {
    return `<span class="hazard-mini" aria-hidden="true">${HAZARD_CLASSES.map((value) => `<i style="background:${HAZARD_COLORS[value]}"></i>`).join('')}<b style="left:${esc(percentile)}%"></b></span>`;
  }
  function hazardTile(hazard, property, life) {
    // Plain names for the two measures; "No expected ..." names its exposure,
    // because a rated hazard can still spare buildings or people.
    const measures = {
      property: {
        entry: property,
        name: 'Estimated building damage',
        none: 'No modeled building loss',
        noun: 'building-loss rate',
      },
      life: {
        entry: life,
        name: 'Estimated harm to people',
        none: 'No modeled population loss',
        noun: 'population-loss rate',
      },
    };
    const label = (measure) =>
      measure.entry.class === 'no_rating'
        ? measure.none
        : esc(HAZARD_CLASS_LABELS[measure.entry.class] || measure.entry.class);
    // The measure this hazard's limit uses leads the tile; the other follows.
    const primary = filterMeasure(hazard.key),
      secondary = primary === 'property' ? 'life' : 'property';
    const lead = measures[primary],
      other = measures[secondary];
    const leadDetail = lead.entry.percentile
      ? `${hazardMiniScale(lead.entry.percentile)}<small class="hazard-plain">${lead.noun[0].toUpperCase() + lead.noun.slice(1)} ${plainRank(lead.entry.percentile)}</small><small class="hazard-exact">Percentile ${esc(lead.entry.percentile)}</small>`
      : '<small class="hazard-plain">&nbsp;</small>';
    return (
      `<div role="listitem" class="hazard-tile${hazard.key === 'overall' ? ' overall' : ''}"><span>${esc(hazard.label)}</span>` +
      `<small class="hazard-measure-label">${lead.name} · what this hazard's limit checks</small>` +
      `<strong>${hazardSwatch(lead.entry.class)}${label(lead)}</strong>${leadDetail}` +
      `<small class="hazard-life-label">${other.name}</small>` +
      `<small class="hazard-life">${hazardSwatch(other.entry.class)}${label(other)}${other.entry.percentile ? ` · ${plainRank(other.entry.percentile)}` : ''}</small></div>`
    );
  }
  // HAZARD ADD-ON: the "how to read these numbers" key at the top of the
  // county's hazard section, for readers with no background in the data.
  function hazardLegend() {
    const ranges = ['0–<20', '20–<40', '40–<60', '60–<80', '80–100'];
    const segments = HAZARD_CLASSES.map(
      (value, index) =>
        `<span><i style="background:${HAZARD_COLORS[value]}"></i><b>${HAZARD_CLASS_LABELS[value]}</b><small>${ranges[index]}</small></span>`
    ).join('');
    return `<details class="hazard-legend-card filter-details"><summary>How to read hazard levels</summary>
      <div class="hazard-scale" role="img" aria-label="Five classes: Very Low below 20, Low 20 to below 40, Moderate 40 to below 60, High 60 to below 80, Very High 80 through 100. Darker means a higher modeled loss rate.">${segments}</div>
      <p class="hazard-scale-ends" aria-hidden="true"><span>← Lower modeled loss rate</span><span>Higher modeled loss rate →</span></p>
      <ul>
        <li><strong>The number is a rank, not a chance of disaster.</strong> Percentile 98 means this county has a higher modeled loss rate than about 98 of every 100 other covered counties with positive modeled loss for this hazard and measure.</li>
        <li><strong>Building-loss rate</strong> is the share of building value expected to be lost each year. <strong>Population-loss rate</strong> is expected deaths per resident each year, with each injury counted as a tenth of a death.</li>
        <li><strong>"What this hazard's limit checks"</strong> marks the number a hazard limit uses: building-loss rate for most hazards, population-loss rate for heat wave.</li>
        <li><strong>Not applicable</strong>: not applicable in this FEMA release. <strong>No modeled loss</strong>: no modeled loss in this FEMA release. <strong>Insufficient data</strong>: FEMA could not estimate it (for example heat waves in Alaska), so selecting a filter for that hazard excludes the county from matching results. Looking up a place still shows the county and flags the missing data.</li>
      </ul></details>`;
  }
  function hazardPanel(row, includeHeading = true) {
    const heading = includeHeading
      ? '<h3>Natural hazards</h3>'
      : '';
    if (row.hazard_data_availability !== 'available' || !row.hazards) {
      const reason =
        row.state_abbr === 'CT'
          ? ' NRI covers Connecticut by its planning regions, while COLE uses the legacy counties.'
          : '';
      return `<div class="climate-breakdown hazard-breakdown">${heading}<div class="climate-unavailable" role="note"><strong>Hazard data unavailable.</strong><p>The FEMA National Risk Index release has no record under this county code.${reason} COLE leaves the value unavailable instead of substituting a neighboring area.</p></div></div>`;
    }
    const hazards = row.hazards,
      active = activeHazardLimits(),
      matches = row.hazard_filter_matches || {};
    const limitResults = active.length
      ? `<ul class="climate-limit-results">${active
          .map((hazard) => {
            const match = matches[hazard.key];
            return `<li class="${match === true ? 'limit-met' : 'limit-missed'}"><strong>${esc(hazard.label)}</strong> (${MEASURE_NAMES[filterMeasure(hazard.key)]}), your limit ${esc(hazardLimitText(state.accepted.hazard_limits[hazard.key]))}: ${match === true ? 'Meets' : match === false ? 'Over limit' : 'Data unavailable'}</li>`;
          })
          .join('')}</ul>`
      : '<p class="detail-note">No natural-hazard limit was applied in this submission.</p>';
    const tiles = HAZARD_TYPES.map((hazard) =>
      hazardTile(hazard, hazards.property[hazard.key], hazards.life[hazard.key])
    ).join('');
    const smallNote = hazards.small_population
      ? `<p class="detail-note hazard-caution"><strong>Small population (${esc(Number(hazards.population).toLocaleString())} residents):</strong> modeled population-loss rates can be sensitive to small expected counts. The 5,000-person caution is a COLE heuristic, not a validated uncertainty threshold.</p>`
      : '';
    return `<div class="climate-breakdown hazard-breakdown">${heading}${includeHeading ? hazardLegend() : ''}<p class="hint">Lower levels mean lower estimated losses than other counties. These are county estimates, not a forecast for your home.</p>${limitResults}<div class="hazard-tiles" role="list" aria-label="Modeled annual loss-rate ranks by hazard">${tiles}</div>${smallNote}</div>`;
  }
  // The hazard source and FEMA's community rating, for the collapsed source
  // section alongside the EPI and NOAA provenance.
  function hazardSourceNotes(row) {
    const hazards = row.hazards;
    if (row.hazard_data_availability !== 'available' || !hazards)
      return '<p class="hint">No FEMA National Risk Index county record is available for this county.</p>';
    const community = hazards.community_risk_rating;
    return `<p class="hint">Hazards: FEMA National Risk Index ${esc(hazards.nri_version || '')}, modeled annual loss rates, ranked among counties with positive modeled loss for each hazard and measure. Heat wave's limit uses the population-loss rate; other limits use the building-loss rate. Agricultural losses are not included.</p>
      ${community ? `<p class="hint">FEMA's composite community risk rating for this county: <strong>${esc(COMMUNITY_RISK_LABELS[community] || community)}</strong>. That rating is based on total dollar losses, social vulnerability and community resilience, COLE shows it for context and does not filter on it.</p>` : ''}`;
  }

  let overview = null;
  let started = false;
  let shading = 'results';
  let availability = { ready: false, busy: false, incompatible: false };
  const state = { accepted: null };
  const baseTooltips = new WeakMap();

  function activeHazardLimits(scenario = state.accepted) {
    return HAZARD_TYPES.filter((hazard) => scenario?.hazard_limits?.[hazard.key] != null);
  }

  function presentation(row) {
    if (row.hazards) return row;
    const record = overview?.records?.[row.county_fips];
    if (!record) return { ...row, hazard_data_availability: 'unavailable', hazards: null };
    return {
      ...row,
      hazard_data_availability: 'available',
      hazards: {
        ...record,
        property: record.measures.property,
        life: record.measures.life,
      },
    };
  }

  function badge(row) {
    if (row.hazard_filters_match == null) return '';
    const failed = (row.failed_hazard_filters || []).map(hazardLabel);
    const missing = (row.unavailable_hazard_filters || []).map(hazardLabel);
    return [
      failed.length
        ? `<span class="badge outside">Over hazard limit: ${esc(failed.join(', '))}</span>`
        : '',
      missing.length
        ? `<span class="badge outside">No hazard data for ${esc(missing.join(', '))}</span>`
        : '',
      !failed.length && !missing.length
        ? '<span class="badge hazard-match">Hazard limits met</span>'
        : '',
    ].join('');
  }

  function hazardLabel(key) {
    return HAZARD_TYPES.find((hazard) => hazard.key === key)?.label || key;
  }

  function panel(row, accepted, heading = true) {
    state.accepted = accepted;
    const shown = presentation(row);
    const source =
      '<p class="hint hazard-credit">Natural-hazard add-on contributed by Bingqian Chen. FEMA National Risk Index, December 2025. This product uses the Federal Emergency Management Agency’s National Risk Index dataset API or downloadable datasets but is not endorsed by FEMA. The Federal Government or FEMA cannot vouch for the data or analyses derived from these data after the data have been retrieved from the Agency&#39;s website(s). County averages are not forecasts, insurance ratings, or address-level predictions. NRI v1.20 source retrieved October 7, 2026 at 17:31:26 UTC.</p>';
    return (
      hazardPanel(shown, heading) +
      `<details><summary>Hazard source and method</summary>${hazardSourceNotes(shown)}${source}<p class="hint">Ranks use positive modeled loss-rate counties separately for each hazard and measure. The 2025 NRI release uses 2021 Census geography except Connecticut’s 2024 planning regions, and December 2024 dollar valuations. Those vintages differ from COLE’s household cost data.</p><p><a href="https://www.fema.gov/about/openfema/data-sets/national-risk-index-data" target="_blank" rel="noopener noreferrer">FEMA NRI source</a></p></details>`
    );
  }

  function syncControls(values) {
    availability = values;
    const disabled = !overview?.available || !values.ready || values.incompatible;
    document.querySelectorAll('[data-hazard]').forEach((input) => {
      input.disabled = disabled;
    });
    $('hazard-map-shading').disabled = disabled || values.busy;
    const selected = [...document.querySelectorAll('[data-hazard]')].filter((input) => input.value);
    $('hazard-group-summary').textContent = selected.length
      ? `${selected.length} active`
      : 'None active';
  }

  function mapSync({ accepted, resultMap, layers }) {
    const enabled = shading !== 'results' && overview?.available;
    $('hazard-map-legend').hidden = !enabled;
    $('hazard-map-note').hidden = !enabled;
    $('results-map-legend').hidden = enabled;
    document.querySelector('.map-legend-help').hidden = enabled;
    if (enabled) {
      $('hazard-map-legend').innerHTML =
        [...HAZARD_CLASSES, 'not_applicable', 'no_rating', 'insufficient_data']
          .map((key) => `<span>${hazardSwatch(key)}${esc(key === 'insufficient_data' ? 'Unavailable or insufficient data' : HAZARD_CLASS_LABELS[key])}</span>`)
          .join('') +
        `<span><i class="swatch" style="background:${HAZARD_NOT_RETURNED}" aria-hidden="true"></i>Not returned by this search</span>`;
      const measure = filterMeasure(shading);
      const count = overview.manifest.ranking_universe_sizes?.[`${shading}_${measure}`];
      $('hazard-map-note').textContent =
        `${hazardLabel(shading)}: ${MEASURE_NAMES[measure]}, ranked among ${count ?? 'covered'} counties with positive modeled loss. Fixed percentile classes: below 20; 20–<40; 40–<60; 60–<80; 80–100. Darker means a higher modeled loss rate. Gray counties were not returned; unavailable data has a separate darker gray. Alaska and Hawaii are repositioned for display. Compare ranks, not polygon areas.`;
    }
    layers.forEach((layer, id) => {
      const tooltip = layer.getTooltip?.()?.getContent();
      if (!tooltip || typeof tooltip !== 'object') return;
      if (!baseTooltips.has(tooltip)) baseTooltips.set(tooltip, tooltip.textContent);
      const entry = overview?.records?.[id]?.measures?.[filterMeasure(shading)]?.[shading];
      tooltip.textContent =
        baseTooltips.get(tooltip) +
        (enabled
          ? ` · ${hazardLabel(shading)}: ${HAZARD_CLASS_LABELS[entry?.class] || 'No NRI record'}${accepted && !resultMap.has(id) ? ' (not returned by this search)' : ''}`
          : '');
    });
  }

  window.ColeFeatures.register('hazards', {
    async start({ api, onDraftChange, onMapChange, refreshDetails }) {
      if (started) return;
      started = true;
      try {
        const data = await api('/api/features/hazards');
        if (
          JSON.stringify(data.hazard_types.map((item) => [item.key, item.measure])) !==
            JSON.stringify(HAZARD_TYPES.map((item) => [item.key, filterMeasure(item.key)])) ||
          JSON.stringify(data.limit_choices.map((item) => item.key)) !==
            JSON.stringify(HAZARD_LIMIT_CHOICES)
        )
          throw new Error('The hazard data does not match this screen. Extract a complete copy of COLE and restart.');
        overview = data;
        if (!data.available) throw new Error(data.error);
        $('hazard-controls').innerHTML = HAZARD_TYPES.map(
          (hazard) =>
            `<label class="limit-field" for="hazard-${hazard.key}"><span>${esc(hazard.label)} <small>(${filterMeasure(hazard.key) === 'life' ? 'People' : 'Buildings'})</small></span><select id="hazard-${hazard.key}" data-hazard="${hazard.key}" data-field="hazard_limits.${hazard.key}" aria-describedby="hazard-filter-help"><option value="">No limit</option>${HAZARD_LIMIT_CHOICES.map((key) => `<option value="${key}">${esc(hazardLimitText(key))}</option>`).join('')}</select></label>`
        ).join('');
        HAZARD_TYPES.forEach((hazard) => {
          const option = document.createElement('option');
          option.value = hazard.key;
          option.textContent = `Hazard loss-rate rank: ${hazard.label}`;
          $('hazard-map-shading').append(option);
        });
        const sort = document.createElement('option');
        sort.value = 'hazard';
        sort.textContent = 'All-hazard building-loss rate rank';
        sort.dataset.sortHelp = "Uses COLE's all-hazard building-loss rate rank calculated from FEMA data. No modeled loss and Not applicable precede ranked values in Lowest first order.";
        const group = document.createElement('optgroup');
        group.label = 'Natural hazards';
        group.append(sort);
        $('result-sort').append(group);
        $('hazard-controls').addEventListener('input', () => syncControls(availability));
        $('hazard-map-shading').addEventListener('change', () => {
          shading = $('hazard-map-shading').value;
          onMapChange();
        });
        $('hazard-coverage').textContent =
          `${data.manifest.coverage.hazard_counties.toLocaleString()} counties have FEMA records; ${data.manifest.coverage.hazard_unavailable_county_fips.length} Connecticut legacy counties are unavailable. Alaska has no heat-wave estimates.`;
        $('hazard-feature-status').hidden = true;
        syncControls(availability);
        refreshDetails();
        onMapChange();
        onDraftChange();
      } catch (error) {
        overview = null;
        $('hazard-feature-status').hidden = false;
        $('hazard-feature-status').textContent =
          `Natural-hazard feature unavailable: ${error.message} Searches without hazard limits remain available.`;
        syncControls(availability);
      }
    },
    readScenario(scenario) {
      if (!overview?.available) return scenario;
      const limits = Object.fromEntries(
        [...document.querySelectorAll('[data-hazard]')].map((input) => [
          input.dataset.hazard,
          input.value || null,
        ])
      );
      return Object.values(limits).some(Boolean)
        ? { ...scenario, hazard_limits: limits, hazard_feature_contract: overview.contract }
        : scenario;
    },
    badge,
    summaryRows(scenario) {
      const active = activeHazardLimits(scenario);
      return active.length
        ? [
            [
              'Natural-hazard limits',
              active
                .map(
                  (hazard) =>
                    `${hazard.label}: ${hazardLimitText(scenario.hazard_limits[hazard.key])}`
                )
                .join('; '),
            ],
          ]
        : [];
    },
    details(row, accepted) {
      $('hazard-county-panel').hidden = !row;
      $('hazard-county-label').textContent = row ? `${row.county_name}, ${row.state_abbr}` : '';
      $('hazard-county-body').innerHTML =
        row && overview?.available
          ? panel(row, accepted)
          : row
            ? '<p>Optional hazard data is unavailable.</p>'
            : '';
    },
    comparison(row, accepted) {
      return overview?.available
        ? `<details class="comparison-breakdown hazard-feature"><summary>Natural-hazard exposure</summary>${panel(row, accepted, false)}</details>`
        : '';
    },
    compare(left, right, key, direction = 1) {
      if (key !== 'hazard' || !overview?.available) return null;
      const rank = (row) => {
        const entry = overview.records[row.county_fips]?.measures.property.overall;
        if (entry?.class === 'no_rating' || entry?.class === 'not_applicable') return -1;
        return entry?.percentile == null ? null : Number(entry.percentile);
      };
      const a = rank(left),
        b = rank(right);
      if (a == null && b == null) return 0;
      if (a == null) return 1;
      if (b == null) return -1;
      return a === b ? 0 : (a < b ? -1 : 1) * direction;
    },
    mapStyle(id, original, { accepted, resultMap }) {
      if (shading === 'results' || !overview?.available) {
        return resultMap.get(id)?.hazard_filters_match === false
          ? { ...original, fillColor: '#c88e79' }
          : original;
      }
      const entry = overview.records[id]?.measures?.[filterMeasure(shading)]?.[shading];
      return {
        ...original,
        fillColor:
          accepted && !resultMap.has(id)
            ? HAZARD_NOT_RETURNED
            : HAZARD_COLORS[entry?.class] || HAZARD_COLORS.missing,
        fillOpacity: 0.9,
      };
    },
    mapSync,
    syncControls,
    reset() {
      shading = 'results';
      $('hazard-map-shading').value = 'results';
      state.accepted = null;
      syncControls(availability);
    },
  });
})();
