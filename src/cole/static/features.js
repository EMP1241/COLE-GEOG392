/* Host-owned dispatch; feature implementations register without owning each other. */
(() => {
  'use strict';
  const modules = new Map();
  let startPromise;
  const call = (name, ...args) => [...modules.values()].map((feature) => feature[name]?.(...args));
  const chain = (name, initial, ...args) => [...modules.values()].reduce(
    (value, feature) => feature[name]?.(value, ...args) ?? value, initial
  );
  window.ColeFeatures = Object.freeze({
    register(name, feature) {
      if (startPromise || modules.has(name)) throw new Error(`Duplicate or late feature registration: ${name}`);
      modules.set(name, Object.freeze(feature));
    },
    registered: () => [...modules.keys()],
    start(context) {
      if (startPromise) return startPromise;
      document.querySelectorAll('[data-feature-status]').forEach((element) => {
        if (!modules.has(element.dataset.featureStatus))
          element.textContent = 'Optional feature assets are unavailable. Restore the complete application and restart.';
      });
      startPromise = Promise.allSettled([...modules.values()].map((feature) =>
        Promise.resolve().then(() => feature.start?.(context))
      ));
      return startPromise;
    },
    readScenario: (scenario) => chain('readScenario', scenario),
    summaryRows: (scenario) => call('summaryRows', scenario).flatMap((rows) => rows ?? []),
    badge: (row) => call('badge', row).join(''),
    comparison: (row, accepted) => call('comparison', row, accepted).join(''),
    compare(left, right, key, direction = 1) {
      for (const feature of modules.values()) {
        const verdict = feature.compare?.(left, right, key, direction);
        if (verdict != null) return verdict;
      }
      return null;
    },
    mapStyle(id, style, context) {
      for (const name of ['mapBaseStyle', 'mapStyle']) {
        for (const feature of modules.values()) style = feature[name]?.(id, style, context) ?? style;
      }
      return style;
    },
    details: (...args) => call('details', ...args),
    comparisonReady: (...args) => call('comparisonReady', ...args),
    mapSync: (...args) => call('mapSync', ...args),
    syncControls: (...args) => call('syncControls', ...args),
    reset: () => call('reset'),
  });
})();
