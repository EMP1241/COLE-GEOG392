"""Prepare county public-outdoor-space metrics from preserved PAD-US polygons.

Run with the installed GIS Python. No source is modified. Stage and aggregation
are separately resumable; a runtime database is published only after completion.
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import types

import numpy as np
import pyogrio
import pyproj
import shapely

HOME = Path(__file__).resolve().parents[1]
RUN = HOME / "working/county-aggregation-v2-2026-10-22"
RESULT_NAME = "county_results-v8.sqlite3"
REPAIR = HOME / "working/national-batch-prep-v2-2026-10-22"
ARCHIVE_SHA = "62ed113335e359509ad68a731146c74aa873c5a92da4f3fda572e54bc9b823ab"
REPAIR_SHA = "d9c3303be42852a72a5482c89a168ee3dbbac41bc596460e9797eee186d9e808"
CENSUS_SHA = "26890ff92275ff1392cd205c7021ba510b7f63f2ff985ca327b9f5fd5e9ecc90"
AUDIT_SHA = "14eba76179510e801eb9d40cd12383e2f95c26b09a23597c08e7c701838578ea"
SUMMARY_SHA = "b7e4e819490852db5e338ff248d69d7f4c731b5f4a24570c69950f8b95998838"
FIELDS = ["Category", "Unit_Nm", "Own_Type", "Own_Name", "Mang_Type", "Mang_Name", "Des_Tp", "Pub_Access", "State_Nm", "SHAPE_Area"]
ACRE = 4046.8564224
NUMERIC_ACCOUNTING = {"comparison_floor_m2": 1.0, "relative_county_area_tolerance": 1e-10,
                      "reserved_comparison_allowances_per_county": 8,
                      "interpretation": "Floating overlay accounting reference and engineering reserve; all observed discrepancies are additionally charged without cancellation against the county materiality budget. Not a bound on all source errors; no clamping"}
CREDITS = {'application_author': 'Brandon Santor', 'concept_functionality_and_logic': 'Brandon Santor', 'baseline_python_code': 'Brandon Santor', 'application_development_and_integration': 'Brandon Santor', 'data_curation_screening_validation': 'Brandon Santor', 'feature_addon_contributor': 'Margaret', 'publishers': ['U.S. Geological Survey', 'U.S. Census Bureau']}
POLICY = {
    "metric": "Estimated documented public outdoor space; total county area including water",
    "denominator": "Full 2021 Census county polygon area in ESRI:102039; same extent and projection as numerator",
    "ownership": "Explicit FED/STAT/LOC/DIST/TERR in Fee, Easement or Marine; planning/designation boundaries alone establish no ownership",
    "access": "Spatial intersection of confirmed public ownership with documented OA coverage",
    "exclusions": "Subtract PVT/NGO ownership, DOD owner/manager, MIL designation, and RA/XA access across all source layers; Glades v2 mask subtracts only",
    "unknowns": "UK/unknown alone adds no eligible area; broad unknown planning boundaries do not erase independently confirmed public OA",
    "overlap": "Union once. Federal/state/local-regional/territorial exclusive areas plus shared-public ownership area; no arbitrary owner priority",
    "coverage": "Mapped estimate, not exhaustive inventory. Zero qualifying mapped area does not establish no outdoor opportunity",
    "materiality": "Stop for unbounded geometry/accounting or cumulative unresolved county sensitivity >0.01 percentage points",
    "source_period": "PAD-US 4.1 inventory; contributing records have heterogeneous source dates, not a common observation year",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def ro(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def check(path, expected):
    actual = sha(path)
    if actual != expected:
        raise RuntimeError(f"Input fingerprint differs: {path}")
    return {"bytes": path.stat().st_size, "sha256": actual}


def counties():
    source = HOME / "source/census-2021/tl_2021_us_county.zip"
    check(source, CENSUS_SHA)
    frame = pyogrio.read_dataframe(source, columns=["GEOID", "NAME", "STATEFP", "ALAND", "AWATER"]).to_crs("ESRI:102039")
    corefile = HOME.parents[1] / "data/prepared/counties.geojson"
    core = {x["properties"]["county_fips"] for x in json.loads(corefile.read_text(encoding="utf-8"))["features"]}
    if len(core) != 3143 or len(frame) != 3234 or frame.GEOID.duplicated().any() or core - set(frame.GEOID):
        raise RuntimeError("County identity/coverage differs")
    if not shapely.is_valid(frame.geometry.array).all() or (frame.geometry.area <= 0).any():
        raise RuntimeError("Invalid projected county geometry")
    return frame, core, corefile


def flags(layer, attrs):
    # Designations and outer planning boundaries do not convey title.
    owner = attrs["Own_Type"]
    group = {"FED": 1, "STAT": 2, "LOC": 3, "DIST": 3, "TERR": 4}.get(owner, 0)
    if layer in {"PADUS4_1Designation", "PADUS4_1Proclamation"}:
        group = 0
    private = owner in {"PVT", "NGO"}
    military = attrs["Own_Name"] == "DOD" or attrs["Mang_Name"] == "DOD" or attrs["Des_Tp"] == "MIL"
    blocked = attrs["Pub_Access"] in {"RA", "XA"}
    opened = attrs["Pub_Access"] == "OA"
    return group, int(opened), int(private), int(military), int(blocked)


def stage():
    if RUN.exists():
        raise RuntimeError("Refuse to replace existing county preparation; use --aggregate after completed staging")
    RUN.mkdir()
    manifest = {"started_utc": now(), "status": "staging", "credits": CREDITS, "policy": POLICY,
                "versions": {"pyogrio": pyogrio.__version__, "gdal": pyogrio.__gdal_version_string__,
                             "shapely": shapely.__version__, "geos": shapely.geos_version_string, "pyproj": pyproj.__version__}}
    write(RUN / "stage-manifest.json", manifest)
    print(json.dumps({"phase": "check_inputs"}), flush=True)
    manifest["archive"] = check(HOME / "source/padus-4.1/PADUS4_1Geodatabase.zip", ARCHIVE_SHA)
    manifest["repair_database"] = check(REPAIR / "repair_audit.sqlite3", REPAIR_SHA)
    manifest["geometry_audit"] = check(HOME / "verification/national-batch-final-audit-2026-10-22.json", AUDIT_SHA)
    manifest["repair_summary"] = check(REPAIR / "repair-summary.json", SUMMARY_SHA)
    audit = json.loads((HOME / "verification/national-batch-final-audit-2026-10-22.json").read_text())
    if audit["status"] != "passed" or audit["discrepancies"]:
        raise RuntimeError("National geometry audit has not passed")
    summary = json.loads((REPAIR / "repair-summary.json").read_text())
    if summary["status"] != "completed_within_review_thresholds":
        raise RuntimeError("Incomplete geometry pass")
    # The prior summary's complete extraction fingerprints remain authoritative.
    gdb = HOME / "working/padus-4.1/PADUS4_1Geodatabase.gdb"
    from repair_geometry import source_tree
    original_tree = source_tree(gdb)
    # Check direct byte agreement with the preserved ZIP as well as the final run.
    from zipfile import ZipFile
    with ZipFile(HOME / "source/padus-4.1/PADUS4_1Geodatabase.zip") as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            name = member.filename.replace("\\", "/")
            if not name.startswith("PADUS4_1Geodatabase.gdb/"):
                continue
            relative = name.split("/", 1)[1]
            with archive.open(member) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if original_tree[relative]["sha256"] != digest:
                raise RuntimeError("Original extraction differs from preserved archive")
    manifest["source_extraction"] = original_tree
    frame, core, corefile = counties()
    manifest["core_counties"] = len(core)
    manifest["core_geography"] = {"sha256": sha(corefile)}
    tree = shapely.STRtree(frame.geometry.array)
    connection = sqlite3.connect(RUN / "source_stage.sqlite3")
    connection.executescript("""
        PRAGMA journal_mode=DELETE;
        CREATE TABLE source_records(id INTEGER PRIMARY KEY,layer TEXT NOT NULL,source_fid INTEGER NOT NULL,
          attributes_json TEXT NOT NULL,owner_group INTEGER NOT NULL,opened INTEGER NOT NULL,
          private INTEGER NOT NULL,military INTEGER NOT NULL,blocked INTEGER NOT NULL,
          original_sha256 TEXT NOT NULL,geometry_sha256 TEXT NOT NULL,repaired INTEGER NOT NULL,
          geometry_wkb BLOB NOT NULL,UNIQUE(layer,source_fid));
        CREATE TABLE county_sources(county_fips TEXT NOT NULL,source_id INTEGER NOT NULL,
          PRIMARY KEY(county_fips,source_id)) WITHOUT ROWID;
        CREATE INDEX source_counties ON county_sources(source_id);
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
    """)
    repairs = ro(REPAIR / "repair_audit.sqlite3")
    repairs.row_factory = sqlite3.Row
    used = set()
    counts = {}
    record_id = 0
    for layer, kind in pyogrio.list_layers(gdb):
        if not kind or "Combined_" in layer:
            continue
        expected = summary["layers"][layer]["features"]
        seen = 0
        stats = Counter()
        for start in range(0, expected, 10000):
            source = pyogrio.read_dataframe(gdb, layer=layer, columns=FIELDS, skip_features=start,
                                          max_features=10000, fid_as_index=True)
            if not pyproj.CRS(source.crs).equals(pyproj.CRS("ESRI:102039")):
                raise RuntimeError("Source projection differs")
            for fid, row in source.iterrows():
                attrs = {k: None if row[k] is None or row[k] != row[k] else str(row[k]) for k in FIELDS}
                geometry = row.geometry
                if geometry is None or geometry.is_empty:
                    raise RuntimeError(f"Missing source geometry {layer}/{fid}")
                body = shapely.to_wkb(geometry, byte_order=1)
                original_sha = hashlib.sha256(body).hexdigest()
                repaired = 0
                if not geometry.is_valid:
                    record = repairs.execute("SELECT * FROM repairs WHERE layer=? AND source_fid=?", (layer, int(fid))).fetchone()
                    if record is None or record["original_sha256"] != original_sha or json.loads(record["attributes_json"]) != attrs:
                        raise RuntimeError(f"Unmatched original repair record {layer}/{fid}")
                    local = repairs.execute("SELECT mask_wkb FROM conservative_local_exclusions WHERE layer=? AND source_fid=?", (layer, int(fid))).fetchone()
                    body = local[0] if local is not None else record["polygon_wkb"]
                    geometry = shapely.from_wkb(body)
                    repaired = 1
                    used.add((layer, int(fid)))
                    if local is not None and not flags(layer, attrs)[2]:
                        raise RuntimeError("Conservative local mask may only be used for private exclusions")
                if not geometry.is_valid or geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
                    raise RuntimeError(f"Unusable derived polygon {layer}/{fid}")
                record_id += 1
                group, opened, private, military, blocked = flags(layer, attrs)
                if layer == "PADUS4_1Fee" and int(fid) == 77431:
                    # No public/access evidence can be inferred from the conservative mask.
                    group, opened, military, blocked = 0, 0, 0, 0
                connection.execute("INSERT INTO source_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (record_id, layer, int(fid), json.dumps(attrs, sort_keys=True), group, opened, private,
                     military, blocked, original_sha, hashlib.sha256(body).hexdigest(), repaired, body))
                indices = tree.query(geometry.envelope, predicate="intersects")
                connection.executemany("INSERT INTO county_sources VALUES(?,?)", [(str(frame.iloc[i].GEOID), record_id) for i in indices])
                stats.update({"records": 1, "repaired": repaired, "public_owner_records": int(group > 0),
                              "open_records": opened, "private_records": private, "military_records": military,
                              "restricted_closed_records": blocked})
                seen += 1
            connection.commit()
            print(json.dumps({"phase": "stage", "layer": layer, "read": seen, "expected": expected}), flush=True)
        if seen != expected:
            raise RuntimeError("Source row count differs")
        counts[layer] = dict(stats)
    if len(used) != 2218 or record_id != 656986:
        raise RuntimeError("National source/repair accounting differs")
    if source_tree(gdb) != original_tree:
        raise RuntimeError("Original extraction changed during staging")
    manifest.update({"status": "staged", "completed_utc": now(), "source_records": record_id,
                     "repairs_freshly_matched": len(used), "layers": counts,
                     "source_code": {Path(__file__).name: sha(Path(__file__))}})
    connection.execute("INSERT INTO metadata VALUES(?,?)", ("stage_manifest", json.dumps(manifest, sort_keys=True)))
    connection.commit()
    connection.close()
    repairs.close()
    manifest["stage_database"] = {"bytes": (RUN / "source_stage.sqlite3").stat().st_size, "sha256": sha(RUN / "source_stage.sqlite3")}
    write(RUN / "stage-manifest.json", manifest)
    print(json.dumps({"phase": "staged", "records": record_id}), flush=True)


def polygonal(geometry):
    """Area analysis discards zero-area point/line contacts, never polygon area."""
    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        return geometry
    polygons = []
    stack = [geometry]
    while stack:
        item = stack.pop()
        if item.geom_type == "Polygon" and not item.is_empty:
            polygons.append(item)
        elif hasattr(item, "geoms"):
            stack.extend(item.geoms)
    return shapely.union_all(polygons) if polygons else shapely.Polygon()


def union(items):
    return polygonal(shapely.union_all(items)) if len(items) else shapely.Polygon()


def intersect(left, right):
    if left.is_empty or right.is_empty:
        return shapely.Polygon()
    return polygonal(shapely.intersection(left, right))


def subtract(left, right):
    if left.is_empty or right.is_empty:
        return left
    return polygonal(shapely.difference(left, right))


def component_overlay(left, right, intersection):
    """Distribute Boolean operations over disjoint polygon components.

    A spatial index excludes irrelevant right-hand components. This avoids
    building a global overlay graph for thousands of near-coincident islands;
    no grid, coordinate rounding, buffer or source simplification is applied.
    """
    right_parts = shapely.get_parts(right)
    tree = shapely.STRtree(right_parts)
    shapely.prepare(right_parts)
    result = []
    for part in shapely.get_parts(left):
        indices = tree.query(part)
        if len(indices) == 0:
            if not intersection:
                result.append(part)
            continue
        candidates = right_parts[indices]
        if shapely.covers(candidates, part).any():
            if intersection:
                result.append(part)
            continue
        mask = candidates[0] if len(candidates) == 1 else shapely.MultiPolygon(list(candidates))
        piece = polygonal(shapely.intersection(part, mask) if intersection else shapely.difference(part, mask))
        result.extend(shapely.get_parts(piece))
    combined = shapely.MultiPolygon([p for p in result if not p.is_empty])
    return combined if combined.is_valid else polygonal(shapely.union_all(result))


def calculate_county_native(county, core, allowances):
    code = str(county.GEOID)
    source = ro(RUN / "source_stage.sqlite3")
    boundary = county.geometry
    rows = source.execute("SELECT s.id,s.owner_group,s.opened,s.private,s.military,s.blocked,s.geometry_wkb FROM source_records s JOIN county_sources c ON c.source_id=s.id WHERE c.county_fips=? ORDER BY s.id", (code,)).fetchall()
    source.close()
    buckets = {k: [] for k in ["all", "open", "private", "military", "blocked", 1, 2, 3, 4]}
    evidence = []
    for source_id, group, opened, private, military, blocked, body in rows:
        geometry = shapely.from_wkb(body)
        if not boundary.intersects(geometry):
            continue
        clipped = intersect(geometry, boundary)
        if not clipped.is_valid:
            raise RuntimeError(f"Invalid county intersection {code}/{source_id}")
        if clipped.is_empty or clipped.area == 0:
            continue
        evidence.append((code, source_id, clipped.area))
        buckets["all"].append(clipped)
        if group:
            buckets[group].append(clipped)
        for key, active in [("open", opened), ("private", private), ("military", military), ("blocked", blocked)]:
            if active:
                buckets[key].append(clipped)
    regions = {key: union(value) for key, value in buckets.items()}
    public = union([regions[g] for g in (1, 2, 3, 4)])
    excluded = union([regions[k] for k in ("private", "military", "blocked")])
    before = intersect(public, regions["open"])
    eligible = subtract(before, excluded)
    groups = [regions[g] for g in (1, 2, 3, 4)]
    pivot = max(range(4), key=lambda i: groups[i].area)
    others = [i for i in range(4) if i != pivot and not groups[i].is_empty]
    # Boolean distributivity avoids intersecting the largest owner with almost
    # the same coastline again. Pivot is computational only, never a priority.
    clipped_groups = {i: intersect(groups[i], eligible) for i in others}
    shared = union([intersect(groups[pivot], clipped_groups[i]) for i in others]
                   + [intersect(clipped_groups[i], clipped_groups[j]) for n, i in enumerate(others) for j in others[n+1:]])
    exclusive = [shapely.Polygon() for _ in range(4)]
    exclusive[pivot] = subtract(eligible, union([groups[i] for i in others]))
    for i in others:
        exclusive[i] = subtract(clipped_groups[i], shared)
    denominator = boundary.area
    numeric_tolerance = max(1.0, denominator * 1e-10)
    if not eligible.is_valid or eligible.area > denominator + numeric_tolerance or eligible.area < 0:
        raise RuntimeError(f"Invalid eligible area {code}")
    residual = abs(sum(g.area for g in exclusive) + shared.area - eligible.area)
    excluded_contact = intersect(eligible, excluded).area
    outside_public = subtract(eligible, public).area
    outside_county = subtract(eligible, boundary).area
    allowance = allowances.get(code, {}).get("review_allowance_m2", 0.0)
    sensitivity = allowance / denominator * 100
    open_after_exclusions = subtract(regions["open"], excluded)
    public_after_exclusions = subtract(public, excluded)
    unknown_owner_area = open_after_exclusions.area - eligible.area
    unknown_access_area = public_after_exclusions.area - eligible.area
    # Inclusion/exclusion identities avoid re-overlaying nearly identical
    # coastlines solely to obtain diagnostic areas. Negative roundoff uses an
    # explicit geometric complement; primary eligible percentages are untouched.
    if unknown_owner_area < 0:
        unknown_owner_area = subtract(open_after_exclusions, eligible).area
    if unknown_access_area < 0:
        unknown_access_area = subtract(public_after_exclusions, eligible).area
    removed_area = before.area - eligible.area
    if removed_area < 0:
        removed_area = intersect(before, excluded).area
    unmapped_area = denominator - regions["all"].area
    if unmapped_area < 0:
        unmapped_area = subtract(boundary, regions["all"]).area
    mapped_residual = abs(regions["all"].area + unmapped_area - denominator)
    exclusion_residual = abs(before.area - removed_area - eligible.area)
    combined_sensitivity = (allowance + 8 * numeric_tolerance + residual + mapped_residual + exclusion_residual + excluded_contact + outside_public + outside_county) / denominator * 100
    if combined_sensitivity > 0.01:
        raise RuntimeError(f"County combined accounting sensitivity exceeds budget {code}")
    metrics = (code, str(county.NAME), str(county.STATEFP), int(code in core), denominator, float(county.ALAND), float(county.AWATER),
      eligible.area, eligible.area/ACRE, eligible.area/denominator*100,
      *(g.area for g in exclusive), shared.area, public.area, regions["open"].area,
      regions["private"].area, regions["military"].area, regions["blocked"].area,
      before.area, removed_area, unknown_owner_area, unknown_access_area,
      regions["all"].area, unmapped_area,
      intersect(public, regions["private"]).area, intersect(before, regions["blocked"]).area,
      allowance, sensitivity, len(evidence), "mapped_estimate" if eligible.area else "no_qualifying_mapped_area")
    accounting = (code, numeric_tolerance, residual,
                  excluded_contact, outside_public, outside_county,
                  mapped_residual, exclusion_residual, combined_sensitivity)
    geometries = (code, shapely.to_wkb(eligible), shapely.to_wkb(excluded), shapely.to_wkb(shared), shapely.to_wkb(public))
    return metrics, geometries, evidence, accounting


def calculate_county_v7(county, core, allowances):
    """Preserve the native result; resolve proven ownership-difference failures.

    Exclusive ownership is a scalar output. Inclusion/exclusion computes its
    area without creating another near-coincident difference geometry. Primary
    eligibility and all exclusion/public/shared geometries remain the native
    ones. Independent full source overlay replays are sensitivity diagnostics.
    """
    try:
        return (*calculate_county_native(county, core, allowances), None)
    except RuntimeError as error:
        if 'County combined accounting sensitivity exceeds budget' not in str(error):
            raise
        cursor = error.__traceback__
        state = None
        while cursor:
            if cursor.tb_frame.f_code is calculate_county_native.__code__:
                state = dict(cursor.tb_frame.f_locals)
            cursor = cursor.tb_next
        if state is None or state['residual'] <= state['numeric_tolerance']:
            raise
        failure = str(error)
    groups, eligible, shared = (state[k] for k in ('groups', 'eligible', 'shared'))
    exclusive_areas = [0.0] * 4
    other_union = union([groups[i] for i in state['others']])
    exclusive_areas[state['pivot']] = eligible.area - intersect(eligible, other_union).area
    for i in state['others']:
        clipped = state['clipped_groups'][i]
        exclusive_areas[i] = clipped.area - intersect(clipped, shared).area
    if any(not math.isfinite(a) or a < 0 for a in exclusive_areas):
        raise RuntimeError(f'Invalid ownership area identity {county.GEOID}')
    alternatives = []
    extra_review = 0.0
    for grid in (0.0001, 0.001):
        # Isolated function globals make the bounded threaded replays safe.
        namespace = dict(globals())
        namespace['union'] = lambda items, g=grid: polygonal(shapely.union_all(items, grid_size=g)) if len(items) else shapely.Polygon()
        namespace['intersect'] = lambda a,b,g=grid: polygonal(shapely.intersection(a,b,grid_size=g))
        namespace['subtract'] = lambda a,b,g=grid: polygonal(shapely.difference(a,b,grid_size=g))
        replay = types.FunctionType(calculate_county_native.__code__, namespace)
        metrics, geometry, _, accounting = replay(county, core, allowances)
        category_difference = sum(abs(a-b) for a,b in zip(exclusive_areas+[shared.area], metrics[10:15]))
        spatial_change = shapely.symmetric_difference(eligible, shapely.from_wkb(geometry[1]), grid_size=grid).area
        charge = category_difference + spatial_change
        extra_review = max(extra_review, charge)
        alternatives.append({'grid_m':grid, 'eligible_m2':metrics[7], 'ownership_areas_m2':list(metrics[10:15]),
                             'summed_absolute_category_difference_m2':category_difference,
                             'eligible_symmetric_difference_m2':spatial_change,
                             'combined_sensitivity_pp':accounting[-1]})
    residual = abs(sum(exclusive_areas) + shared.area - eligible.area)
    combined = (state['allowance'] + 8*state['numeric_tolerance'] + residual + extra_review
                + sum(state[k] for k in ('mapped_residual','exclusion_residual','excluded_contact','outside_public','outside_county'))) / state['denominator'] * 100
    if combined > 0.01:
        raise RuntimeError(f'Ownership identity resolution remains material {county.GEOID}: {combined}')
    s=state
    metrics=(s['code'],str(county.NAME),str(county.STATEFP),int(s['code'] in core),s['denominator'],float(county.ALAND),float(county.AWATER),
      eligible.area,eligible.area/ACRE,eligible.area/s['denominator']*100,
      *exclusive_areas,shared.area,s['public'].area,s['regions']['open'].area,
      s['regions']['private'].area,s['regions']['military'].area,s['regions']['blocked'].area,
      s['before'].area,s['removed_area'],s['unknown_owner_area'],s['unknown_access_area'],s['regions']['all'].area,s['unmapped_area'],
      intersect(s['public'],s['regions']['private']).area,intersect(s['before'],s['regions']['blocked']).area,
      s['allowance'],s['sensitivity'],len(s['evidence']),'mapped_estimate' if eligible.area else 'no_qualifying_mapped_area')
    geometries=(s['code'],shapely.to_wkb(eligible),shapely.to_wkb(s['excluded']),shapely.to_wkb(shared),shapely.to_wkb(s['public']))
    accounting=(s['code'],s['numeric_tolerance'],residual,s['excluded_contact'],s['outside_public'],s['outside_county'],s['mapped_residual'],s['exclusion_residual'],combined)
    resolution={'method':'exclusive_ownership_area_inclusion_exclusion', 'failure':failure,
                'native_partition_residual_m2':s['residual'], 'resolved_partition_residual_m2':residual,
                'additional_numeric_review_m2':extra_review,'primary_geometry_changed':False,
                'exclusive_areas_are_calculated_scalars':True,'full_source_precision_replays':alternatives,
                'limits':'Numerical sensitivity evidence, not a bound on all source or floating overlay error.'}
    print(json.dumps({'phase':'county_ownership_resolved','fips':s['code'],'residual_m2':residual,'combined_sensitivity_pp':combined}),flush=True)
    return metrics,geometries,s['evidence'],accounting,resolution


def calculate_county(county, core, allowances):
    """Resolve a demonstrably unstable overlay on derived county geometry only.

    Use explicit 0.1-mm precision through every operation, with a second complete
    source replay at 1 mm. Preserve original inputs and charge all observed
    changes plus an intentionally broad boundary-neighborhood engineering
    reserve. This fallback never weakens the county publication budget.
    """
    try:
        return calculate_county_v7(county, core, allowances)
    except RuntimeError as error:
        if not any(message in str(error) for message in ('Invalid ownership area identity','Ownership identity resolution remains material','County combined accounting sensitivity exceeds budget')):
            raise
        reason=str(error)
    try:
        calculate_county_native(county, core, allowances)
    except RuntimeError as error:
        cursor=error.__traceback__
        native=None
        while cursor:
            if cursor.tb_frame.f_code is calculate_county_native.__code__:
                native=dict(cursor.tb_frame.f_locals)
            cursor=cursor.tb_next
    if native is None or 'eligible' not in native:
        raise RuntimeError(f'No complete native diagnostic for {county.GEOID}')
    results=[]
    reserves=[]
    for grid in (.0001,.001):
        ledger={'operand_perimeter_m':0.0,'operand_coordinates':0,'operations':0}
        namespace=dict(globals())
        def operation(name,*args,grid=grid,ledger=ledger):
            items=args[0] if name=='union' else args
            ledger['operand_perimeter_m']+=sum(g.length for g in items)
            ledger['operand_coordinates']+=sum(int(shapely.get_num_coordinates(g)) for g in items)
            ledger['operations']+=1
            if name=='union':return polygonal(shapely.union_all(items,grid_size=grid)) if len(items) else shapely.Polygon()
            if name=='intersect':return polygonal(shapely.intersection(*args,grid_size=grid))
            return polygonal(shapely.difference(*args,grid_size=grid))
        for name in ('union','intersect','subtract'):
            namespace[name]=lambda *args,name=name:operation(name,*args)
        replay=types.FunctionType(calculate_county_native.__code__,namespace)
        result=replay(county,core,allowances)
        radius=8*grid
        reserve=2*radius*ledger['operand_perimeter_m']+math.pi*radius*radius*ledger['operand_coordinates']
        results.append(result)
        reserves.append({'grid_m':grid,**ledger,'boundary_neighborhood_radius_m':radius,'engineering_reserve_m2':reserve})
    fine,coarse=results
    metrics,geometries,evidence,accounting=fine
    fine_geometry=shapely.from_wkb(geometries[1])
    coarse_geometry=shapely.from_wkb(coarse[1][1])
    primary_change=abs(metrics[7]-native['eligible'].area)+shapely.symmetric_difference(fine_geometry,native['eligible'],grid_size=.0001).area
    replay_change=abs(metrics[7]-coarse[0][7])+shapely.symmetric_difference(fine_geometry,coarse_geometry,grid_size=.0001).area
    category_change=sum(abs(a-b) for a,b in zip(metrics[10:15],coarse[0][10:15]))
    extra=reserves[0]['engineering_reserve_m2']+primary_change+replay_change+category_change
    combined=accounting[-1]+extra/metrics[4]*100
    if not math.isfinite(combined) or combined>.01:
        raise RuntimeError(f'Fixed precision reconciliation remains material {county.GEOID}: {combined}')
    accounting=(*accounting[:-1],combined)
    resolution={'method':'fixed_precision_county_overlay','failure':reason,'grid_m':.0001,
                'additional_numeric_review_m2':extra,'primary_geometry_changed':True,
                'native_primary_m2':native['eligible'].area,'native_primary_sha256':hashlib.sha256(shapely.to_wkb(native['eligible'])).hexdigest(),
                'native_partition_residual_m2':native['residual'],'native_excluded_contact_m2':native['excluded_contact'],
                'native_primary_change_charge_m2':primary_change,'cross_grid_primary_charge_m2':replay_change,
                'cross_grid_category_charge_m2':category_change,'operation_boundary_reserves':reserves,
                'full_source_precision_replays':[{'grid_m':g,'eligible_m2':r[0][7],'ownership_areas_m2':list(r[0][10:15]),'accounting':r[3]} for g,r in zip((.0001,.001),results)],
                'original_source_changed':False,'exclusion_policy_changed':False,
                'limits':'Boundary-neighborhood reserve and same-engine sensitivity are engineering diagnostics, not a theorem bounding all topology/source error or survey accuracy.'}
    print(json.dumps({'phase':'county_fixed_precision_resolved','fips':str(county.GEOID),'combined_sensitivity_pp':combined,'primary_change_charge_m2':primary_change}),flush=True)
    return metrics,geometries,evidence,accounting,resolution


def aggregate():
    manifest = json.loads((RUN / "stage-manifest.json").read_text(encoding="utf-8"))
    if manifest["status"] != "staged" or manifest["policy"] != POLICY:
        raise RuntimeError("Staging not complete or policy differs")
    check(RUN / "source_stage.sqlite3", manifest["stage_database"]["sha256"])
    check(REPAIR / "repair_audit.sqlite3", REPAIR_SHA)
    check(REPAIR / "repair-summary.json", SUMMARY_SHA)
    frame, core, corefile = counties()
    if sha(corefile) != manifest["core_geography"]["sha256"]:
        raise RuntimeError("Core geography changed")
    source = ro(RUN / "source_stage.sqlite3")
    summary = json.loads((REPAIR / "repair-summary.json").read_text())
    allowances = summary["county_review_allowances"]
    result_path = RUN / RESULT_NAME
    output = sqlite3.connect(result_path)
    output.executescript("""
      CREATE TABLE IF NOT EXISTS county_metrics(
        county_fips TEXT PRIMARY KEY CHECK(length(county_fips)=5),county_name TEXT NOT NULL,state_fips TEXT NOT NULL,
        core_county INTEGER NOT NULL,county_area_m2 REAL NOT NULL,census_land_m2 REAL NOT NULL,census_water_m2 REAL NOT NULL,
        eligible_m2 REAL NOT NULL,eligible_acres REAL NOT NULL,eligible_percent REAL NOT NULL,
        federal_exclusive_m2 REAL NOT NULL,state_exclusive_m2 REAL NOT NULL,local_regional_exclusive_m2 REAL NOT NULL,
        territorial_exclusive_m2 REAL NOT NULL,shared_public_m2 REAL NOT NULL,
        public_ownership_m2 REAL NOT NULL,documented_open_m2 REAL NOT NULL,excluded_private_m2 REAL NOT NULL,
        excluded_military_m2 REAL NOT NULL,excluded_restricted_closed_m2 REAL NOT NULL,
        eligible_before_exclusions_m2 REAL NOT NULL,excluded_from_candidate_m2 REAL NOT NULL,
        unknown_ownership_open_m2 REAL NOT NULL,public_without_open_evidence_m2 REAL NOT NULL,
        mapped_footprint_m2 REAL NOT NULL,unmapped_m2 REAL NOT NULL,
        ownership_conflict_m2 REAL NOT NULL,access_conflict_m2 REAL NOT NULL,
        geometry_review_allowance_m2 REAL NOT NULL,geometry_review_sensitivity_pp REAL NOT NULL,
        source_record_count INTEGER NOT NULL,coverage_status TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS county_geometries(county_fips TEXT PRIMARY KEY,eligible_wkb BLOB NOT NULL,
        excluded_wkb BLOB NOT NULL,shared_wkb BLOB NOT NULL,public_wkb BLOB NOT NULL);
      CREATE TABLE IF NOT EXISTS county_accounting(county_fips TEXT PRIMARY KEY,
        numeric_tolerance_m2 REAL NOT NULL,partition_residual_m2 REAL NOT NULL,
        excluded_contact_m2 REAL NOT NULL,outside_public_m2 REAL NOT NULL,outside_county_m2 REAL NOT NULL,
        mapped_residual_m2 REAL NOT NULL,exclusion_residual_m2 REAL NOT NULL,
        combined_review_sensitivity_pp REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS county_source_evidence(county_fips TEXT NOT NULL,source_id INTEGER NOT NULL,
        intersection_m2 REAL NOT NULL,PRIMARY KEY(county_fips,source_id)) WITHOUT ROWID;
      CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS county_resolutions(county_fips TEXT PRIMARY KEY,resolution_json TEXT NOT NULL);
    """)
    identity = {"code_sha256": sha(Path(__file__)), "stage_manifest_sha256": sha(RUN / "stage-manifest.json"),
                "repair_summary_sha256": SUMMARY_SHA, "policy": POLICY, "credits": CREDITS,
                "numeric_accounting": NUMERIC_ACCOUNTING,
                "core_geography_sha256": sha(corefile)}
    previous = output.execute("SELECT value_json FROM metadata WHERE key='aggregation_identity'").fetchone()
    if previous is None:
        if output.execute("SELECT COUNT(*) FROM county_metrics").fetchone()[0]:
            raise RuntimeError("Existing counties lack a resumable calculation identity")
        with output:
            output.execute("INSERT INTO metadata VALUES(?,?)", ("aggregation_identity", json.dumps(identity, sort_keys=True)))
    elif json.loads(previous[0]) != identity:
        raise RuntimeError("Refuse to mix different county calculation/input versions")
    done = {r[0] for r in output.execute("SELECT county_fips FROM county_metrics")}
    pending_counties = iter(row for _, row in frame.sort_values("GEOID").iterrows() if str(row.GEOID) not in done)
    with ThreadPoolExecutor(max_workers=4) as pool:
        active = {}
        for _ in range(4):
            county = next(pending_counties, None)
            if county is not None:
                active[pool.submit(calculate_county, county, core, allowances)] = str(county.GEOID)
        while active:
            finished, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in finished:
                code = active.pop(future)
                metrics, geometries, evidence, accounting, resolution = future.result()
                with output:
                    output.execute("INSERT INTO county_metrics VALUES(" + ",".join("?" for _ in metrics) + ")", metrics)
                    output.execute("INSERT INTO county_geometries VALUES(?,?,?,?,?)", geometries)
                    output.executemany("INSERT INTO county_source_evidence VALUES(?,?,?)", evidence)
                    output.execute("INSERT INTO county_accounting VALUES(?,?,?,?,?,?,?,?,?)", accounting)
                    if resolution is not None:
                        output.execute("INSERT INTO county_resolutions VALUES(?,?)",(code,json.dumps(resolution,sort_keys=True)))
                done.add(code)
                print(json.dumps({"phase": "county", "fips": code, "done": len(done),
                                  "eligible_percent": metrics[9], "records": len(evidence)}), flush=True)
                county = next(pending_counties, None)
                if county is not None:
                    active[pool.submit(calculate_county, county, core, allowances)] = str(county.GEOID)
    if len(done) != 3234:
        raise RuntimeError("County aggregation incomplete")
    with output:
        output.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", ("preparation", json.dumps({"completed_utc": now(), "credits": CREDITS, "policy": POLICY, "numeric_accounting": NUMERIC_ACCOUNTING, "stage_manifest_sha256": sha(RUN / "stage-manifest.json"), "source_code_sha256": sha(Path(__file__))}, sort_keys=True)))
    output.close()
    source.close()
    write(RUN / "aggregation-status.json", {"status": "aggregated", "completed_utc": now(), "counties": len(done), "core_counties": len(core), "runtime_database_built": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", action="store_true")
    parser.add_argument("--aggregate", action="store_true")
    args = parser.parse_args()
    if not args.stage and not args.aggregate:
        parser.error("Choose --stage and/or --aggregate")
    if args.stage:
        stage()
    if args.aggregate:
        aggregate()


if __name__ == "__main__":
    main()
