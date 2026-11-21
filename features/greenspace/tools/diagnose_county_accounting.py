"""Preserve one county's failed area-accounting operands for investigation."""

import inspect
import importlib.util
import json
from pathlib import Path
import textwrap

import shapely
import aggregate_counties as preparation


def main():
    # The investigated calculation is archived; later parallel execution changes
    # must not silently replace the historical diagnostic's calculation body.
    global preparation
    current = preparation
    snapshot = current.RUN / "aggregate_counties-overlay-v2.py"
    spec = importlib.util.spec_from_file_location("preserved_county_overlay_v2", snapshot)
    preparation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preparation)
    preparation.HOME, preparation.RUN, preparation.REPAIR = current.HOME, current.RUN, current.REPAIR
    code = "02013"
    target = preparation.RUN / "accounting-diagnostic-02013"
    target.mkdir()
    frame, core, _ = preparation.counties()
    county = frame.loc[frame.GEOID == code].iloc[0]
    summary = json.loads((preparation.REPAIR / "repair-summary.json").read_text(encoding="utf-8"))
    source = preparation.ro(preparation.RUN / "source_stage.sqlite3")
    # Execute the exact calculation body already used, changing only the failed
    # assertion into a recorded diagnostic. No production result is written.
    body = inspect.getsource(preparation.aggregate)
    body = body[body.index("        boundary = county.geometry"):body.index("        with output:", body.index("        boundary = county.geometry"))]
    body = textwrap.dedent(body)
    needle = 'raise RuntimeError(f"County area/exclusion accounting differs {code}")'
    if body.count(needle) != 1:
        raise RuntimeError("Diagnostic extraction differs")
    body = body.replace(needle, "accounting_assertion_failed = True")
    scope = {**vars(preparation), "code": code, "county": county, "core": core,
             "allowances": summary["county_review_allowances"], "source": source,
             "accounting_assertion_failed": False}
    exec(compile(body, "<preserved-county-calculation>", "exec"), scope)
    source.close()
    polygons = {name: scope[name] for name in ("boundary", "public", "before", "eligible", "excluded", "shared")}
    polygons.update({f"exclusive-{i}": g for i, g in enumerate(scope["exclusive"])})
    polygons["eligible-intersects-excluded"] = scope["eligible"].intersection(scope["excluded"])
    polygons["eligible-outside-public"] = scope["eligible"].difference(scope["public"])
    report = {"county_fips": code, "calculation_code_sha256": preparation.sha(Path(preparation.__file__)),
              "accounting_assertion_failed": scope["accounting_assertion_failed"],
              "residual_m2": scope["residual"], "numeric_tolerance_m2": scope["numeric_tolerance"],
              "denominator_m2": scope["denominator"], "operands": {}}
    for name, geometry in polygons.items():
        path = target / (name + ".wkb")
        path.write_bytes(shapely.to_wkb(geometry))
        report["operands"][name] = {"area_m2": geometry.area, "valid": geometry.is_valid,
                                   "kind": geometry.geom_type, "sha256": preparation.sha(path)}
    preparation.write(target / "diagnosis.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
