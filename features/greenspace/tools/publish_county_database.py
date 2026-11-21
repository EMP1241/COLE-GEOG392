"""Audit completed county data and publish a small feature-owned SQLite copy.

This performs data accounting required for publication, not application tests.
The full source evidence and polygon derivations remain in the analysis folder.
"""

from collections import Counter
from datetime import datetime, timezone
import json
import hashlib
import math
from pathlib import Path
import sqlite3

import shapely

from aggregate_counties import (HOME, RUN, RESULT_NAME, REPAIR, CREDITS, POLICY, NUMERIC_ACCOUNTING, ACRE, ARCHIVE_SHA,
                               REPAIR_SHA, CENSUS_SHA, SUMMARY_SHA, AUDIT_SHA,
                               check, counties, ro, sha, write, intersect, subtract)


def main():
    output_dir = HOME / "data"
    target = output_dir / "greenspace.sqlite3"
    manifest_target = output_dir / "manifest.json"
    receipt_path = HOME / "verification/county-database-preparation-2026-10-22.json"
    if target.exists() or manifest_target.exists() or receipt_path.exists():
        raise RuntimeError("Refuse to replace an existing published county database/receipt")
    status = json.loads((RUN / "aggregation-status.json").read_text())
    if status["status"] != "aggregated":
        raise RuntimeError("County aggregation is incomplete")
    primary_report=json.loads((RUN/'primary-overlay-acceptance.json').read_text())
    if primary_report['status']!='passed' or primary_report['compared_counties']!=3234 or primary_report['flagged_counties']:
        raise RuntimeError('Full-reference primary numerical replay is incomplete or has unresolved discrepancies')
    stage = json.loads((RUN / "stage-manifest.json").read_text())
    if stage["status"] != "staged" or stage["policy"] != POLICY or stage["credits"] != CREDITS:
        raise RuntimeError("Stage policy/credit identity differs")
    stage_snapshot = RUN / "aggregate_counties-stage-v2.py"
    stage_code = stage_snapshot if stage_snapshot.exists() else Path(__file__).with_name("aggregate_counties.py")
    check(stage_code, stage["source_code"]["aggregate_counties.py"])
    inputs = {}
    for relative, expected in {
        "source/padus-4.1/PADUS4_1Geodatabase.zip": ARCHIVE_SHA,
        "source/census-2021/tl_2021_us_county.zip": CENSUS_SHA,
        "working/national-batch-prep-v2-2026-10-22/repair_audit.sqlite3": REPAIR_SHA,
        "working/national-batch-prep-v2-2026-10-22/repair-summary.json": SUMMARY_SHA,
        "verification/national-batch-final-audit-2026-10-22.json": AUDIT_SHA,
        RUN.relative_to(HOME).as_posix() + "/source_stage.sqlite3": stage["stage_database"]["sha256"],
    }.items():
        inputs[relative] = check(HOME / relative, expected)
    print(json.dumps({"phase": "preservation_checked"}), flush=True)
    from repair_geometry import source_tree
    if source_tree(HOME / "working/padus-4.1/PADUS4_1Geodatabase.gdb") != stage["source_extraction"]:
        raise RuntimeError("Original extraction changed")
    completion = json.loads((HOME / "verification/national-geometry-preparation-complete-2026-10-22.json").read_text())
    preserved = []
    for prior in completion["preserved_prior_audits"]:
        path = HOME / "working" / prior["run"] / "repair_audit.sqlite3"
        check(path, prior["sha256"])
        preserved.append({"run": prior["run"], "sha256": prior["sha256"], "unchanged": True})
    source = ro(RUN / RESULT_NAME)
    source.row_factory = sqlite3.Row
    if source.execute("PRAGMA integrity_check").fetchall()[0][0] != "ok":
        raise RuntimeError("Analysis SQLite integrity fails")
    frame, core, corefile = counties()
    expected = {str(row.GEOID): row for _, row in frame.iterrows()}
    identity = json.loads(source.execute("SELECT value_json FROM metadata WHERE key='aggregation_identity'").fetchone()[0])
    if (identity["code_sha256"] != sha(Path(__file__).with_name("aggregate_counties.py"))
            or identity["stage_manifest_sha256"] != sha(RUN / "stage-manifest.json")
            or identity["policy"] != POLICY or identity["credits"] != CREDITS
            or identity["numeric_accounting"] != NUMERIC_ACCOUNTING):
        raise RuntimeError("Completed calculation identity differs")
    rows = source.execute("SELECT * FROM county_metrics ORDER BY county_fips").fetchall()
    if {r["county_fips"] for r in rows} != set(expected) or len(rows) != 3234:
        raise RuntimeError("County key coverage differs")
    if {r["county_fips"] for r in rows if r["core_county"]} != core:
        raise RuntimeError("Core county coverage differs")
    check(RUN/RESULT_NAME,primary_report['identity']['native_database_sha256'])
    check(RUN/'primary-overlay-sensitivity.sqlite3',primary_report['identity']['replay_database_sha256'])
    check(RUN/'primary-overlay-acceptance.sqlite3',primary_report['comparison_database_sha256'])
    with ro(RUN/'primary-overlay-sensitivity.sqlite3') as diagnostic:
        diagnostic_identity=json.loads(diagnostic.execute("SELECT value_json FROM metadata WHERE key='identity'").fetchone()[0])
        expected_diagnostic_identity={'script_sha256':sha(HOME/primary_report['identity']['replay_script']),
            'stage_manifest_sha256':sha(RUN/'stage-manifest.json'),'grid_m':.0001,'census_sha256':CENSUS_SHA}
        if diagnostic_identity!=expected_diagnostic_identity or diagnostic_identity!=primary_report['identity']['replay_identity']:
            raise RuntimeError('Primary diagnostic source identity differs')
    with ro(RUN/'primary-overlay-acceptance.sqlite3') as replay:
        replay_identity=json.loads(replay.execute("SELECT value_json FROM metadata WHERE key='identity'").fetchone()[0])
        if replay_identity['comparison_script_sha256']!=sha(Path(__file__).with_name('finalize_primary_overlay_audit.py')) or replay_identity['stage_manifest_sha256']!=sha(RUN/'stage-manifest.json'):
            raise RuntimeError('Primary replay identity differs')
        comparisons={r[0]:r for r in replay.execute('SELECT * FROM comparisons')}
        if set(comparisons)!=set(expected) or any(comparisons[r['county_fips']][1]!=r['eligible_m2'] or comparisons[r['county_fips']][8] for r in rows):
            raise RuntimeError('Primary replay no longer matches accepted values')
    counts = Counter()
    denominator_diagnostics = []
    repair_summary = json.loads((REPAIR / "repair-summary.json").read_text())
    accounting_rows = source.execute("SELECT * FROM county_accounting ORDER BY county_fips").fetchall()
    accounting_by_key = {r["county_fips"]: r for r in accounting_rows}
    resolution_rows=source.execute('SELECT * FROM county_resolutions ORDER BY county_fips').fetchall()
    resolutions={r['county_fips']:json.loads(r['resolution_json']) for r in resolution_rows}
    if set(accounting_by_key) != set(expected):
        raise RuntimeError("County accounting coverage differs")
    spatial_replays = []
    representative = {"02013", "02016", "02050", "02063", "02185", "02282", "06037", "06075",
                      "09001", "12086", "15005", "17031", "22071", "36061", "41013", "48201", "51059"}
    for row in rows:
        code = row["county_fips"]
        if sum(counts.values()) % 100 == 0:
            print(json.dumps({"phase": "county_publication_accounting", "completed": sum(counts.values()), "next": code}), flush=True)
        boundary = expected[code].geometry
        denominator = boundary.area
        reference_area = float(expected[code].ALAND + expected[code].AWATER)
        if reference_area <= 0:
            raise RuntimeError(f"Missing Census reference area {code}")
        denominator_diagnostics.append({"county_fips": code,
            "projected_vs_reference_relative_percent": (denominator / reference_area - 1) * 100,
            "alternative_denominator_percentage_point_difference": abs(row["eligible_percent"] - row["eligible_m2"] / reference_area * 100)})
        tolerance = max(1.0, denominator * 1e-10)
        accounting = accounting_by_key[code]
        comparison=comparisons[code]
        if json.loads(comparison[11])!=list(accounting) or comparison[6]!=accounting['combined_review_sensitivity_pp']:
            raise RuntimeError(f'Stale primary comparison accounting {code}')
        if any(not math.isfinite(comparison[i]) or comparison[i]<0 for i in (3,4,5,12,13)):
            raise RuntimeError(f'Invalid primary comparison charge {code}')
        primary_combined=accounting['combined_review_sensitivity_pp']+(comparison[3]+comparison[4]+comparison[12]+comparison[13])/denominator*100
        if not math.isclose(primary_combined,comparison[7],rel_tol=1e-12,abs_tol=1e-14) or primary_combined>.01:
            raise RuntimeError(f'Primary sensitivity budget fails {code}')
        if accounting["numeric_tolerance_m2"] != tolerance:
            raise RuntimeError(f"County numeric tolerance differs {code}")
        additional_review=resolutions.get(code,{}).get('additional_numeric_review_m2',0.0)
        if not math.isfinite(additional_review) or additional_review<0:
            raise RuntimeError(f'Invalid numerical resolution allowance {code}')
        combined = (row["geometry_review_allowance_m2"] + 8 * tolerance + additional_review
                    + accounting["partition_residual_m2"] + accounting["mapped_residual_m2"]
                    + accounting["exclusion_residual_m2"] + accounting["excluded_contact_m2"]
                    + accounting["outside_public_m2"] + accounting["outside_county_m2"]) / denominator * 100
        if not math.isclose(accounting["combined_review_sensitivity_pp"],combined,rel_tol=1e-12,abs_tol=1e-14) or combined > 0.01:
            raise RuntimeError(f"County combined review budget fails {code}")
        for key in ("partition_residual_m2", "excluded_contact_m2", "outside_public_m2", "outside_county_m2", "mapped_residual_m2", "exclusion_residual_m2"):
            if not math.isfinite(accounting[key]) or accounting[key] < 0:
                raise RuntimeError(f"Invalid county numeric discrepancy {code}/{key}")
        for key in row.keys():
            if key.endswith(("_m2", "_acres", "_percent", "_pp")):
                if not math.isfinite(row[key]) or row[key] < 0:
                    raise RuntimeError(f"Nonfinite/negative metric {code}/{key}")
        if row["county_area_m2"] != denominator or row["eligible_percent"] > 100:
            raise RuntimeError(f"County denominator/percentage fails {code}")
        if not math.isclose(row["eligible_percent"], row["eligible_m2"] / denominator * 100, abs_tol=1e-12):
            raise RuntimeError(f"County percentage arithmetic fails {code}")
        if not math.isclose(row["eligible_acres"], row["eligible_m2"] / ACRE, abs_tol=1e-10):
            raise RuntimeError(f"County acreage arithmetic fails {code}")
        allowance = repair_summary["county_review_allowances"].get(code, {}).get("review_allowance_m2", 0.0)
        if (row["geometry_review_allowance_m2"] != allowance
                or not math.isclose(row["geometry_review_sensitivity_pp"], allowance / denominator * 100, abs_tol=1e-14)):
            raise RuntimeError(f"County repair allowance arithmetic fails {code}")
        # Match the producer's grouping: Python's compensated sum over all five
        # values can differ by an ULP from sum(four exclusives) + shared.
        partition = sum(row[k] for k in ("federal_exclusive_m2", "state_exclusive_m2", "local_regional_exclusive_m2", "territorial_exclusive_m2")) + row["shared_public_m2"]
        if abs(partition-row["eligible_m2"]) != accounting["partition_residual_m2"]:
            raise RuntimeError(f"County saved partition discrepancy differs {code}")
        if row["geometry_review_sensitivity_pp"] > 0.01:
            raise RuntimeError(f"Ownership partition/review budget fails {code}")
        geo = source.execute("SELECT * FROM county_geometries WHERE county_fips=?", (code,)).fetchone()
        if hashlib.sha256(geo['eligible_wkb']).hexdigest()!=comparison[9]:
            raise RuntimeError(f'Stale primary comparison geometry {code}')
        eligible, excluded, shared, public = [shapely.from_wkb(geo[k]) for k in ("eligible_wkb", "excluded_wkb", "shared_wkb", "public_wkb")]
        if not all(g.is_valid for g in (eligible, excluded, shared, public)) or abs(eligible.area-row["eligible_m2"]) > tolerance:
            raise RuntimeError(f"County saved geometry accounting fails {code}")
        observed = {"mapped_residual_m2": abs(row["mapped_footprint_m2"]+row["unmapped_m2"]-denominator),
                    "exclusion_residual_m2": abs(row["eligible_before_exclusions_m2"]-row["excluded_from_candidate_m2"]-row["eligible_m2"])}
        precision_grid=resolutions.get(code,{}).get('grid_m')
        local_intersect=(lambda a,b: shapely.intersection(a,b,grid_size=precision_grid)) if precision_grid else intersect
        local_subtract=(lambda a,b: shapely.difference(a,b,grid_size=precision_grid)) if precision_grid else subtract
        replay = code in representative or code in resolutions or any(accounting[key] > tolerance for key in ("excluded_contact_m2", "outside_public_m2", "outside_county_m2"))
        if replay:
            print(json.dumps({"phase": "saved_geometry_replay", "county_fips": code, "step": "start"}), flush=True)
            observed.update({"excluded_contact_m2": local_intersect(eligible, excluded).area,
                             "outside_county_m2": local_subtract(eligible, boundary).area,
                             "outside_public_m2": local_subtract(eligible, public).area})
            spatial_replays.append(code)
        if any(not math.isclose(amount, accounting[key], rel_tol=1e-10, abs_tol=1e-7) for key, amount in observed.items()):
            raise RuntimeError(f"County numeric accounting reproduction differs {code}")
        if replay and local_subtract(shared, eligible).area / denominator * 100 + combined > 0.01:
            raise RuntimeError(f"County saved exclusion/extent containment fails {code}")
        if replay:
            print(json.dumps({"phase": "saved_geometry_replay", "county_fips": code, "step": "complete"}), flush=True)
        count = source.execute("SELECT COUNT(*) FROM county_source_evidence WHERE county_fips=?", (code,)).fetchone()[0]
        if count != row["source_record_count"]:
            raise RuntimeError(f"County source evidence accounting fails {code}")
        counts[row["coverage_status"]] += 1
    print(json.dumps({"phase": "county_data_accounting_passed", "counties": len(rows)}), flush=True)
    # Publish scalar data only; the analysis SQLite retains complete spatial lineage.
    output_dir.mkdir(exist_ok=True)
    pending = output_dir / "greenspace.pending.sqlite3"
    if pending.exists():
        raise RuntimeError("An unfinished publication exists; preserve for inspection")
    dest = sqlite3.connect(pending)
    create = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='county_metrics'").fetchone()[0]
    dest.execute(create)
    dest.execute(source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='county_accounting'").fetchone()[0])
    dest.execute(source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='county_resolutions'").fetchone()[0])
    dest.executemany('INSERT INTO county_resolutions VALUES(?,?)',[tuple(r) for r in resolution_rows])
    dest.executemany("INSERT INTO county_accounting VALUES(?,?,?,?,?,?,?,?,?)", [tuple(r) for r in accounting_rows])
    dest.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value_json TEXT NOT NULL)")
    dest.execute("CREATE TABLE credits(role TEXT PRIMARY KEY,credit TEXT NOT NULL)")
    dest.execute("CREATE VIEW cole_county_metrics AS SELECT * FROM county_metrics WHERE core_county=1")
    dest.execute("PRAGMA user_version=1")
    dest.execute("PRAGMA application_id=1129270343")
    dest.executemany("INSERT INTO county_metrics VALUES(" + ",".join("?" for _ in rows[0]) + ")", [tuple(r) for r in rows])
    dest.executemany("INSERT INTO credits VALUES(?,?)", [(key, value if isinstance(value, str) else "; ".join(value)) for key, value in CREDITS.items()])
    artifacts = ["source_stage.sqlite3", RESULT_NAME, "stage-manifest.json", "aggregation-status.json"]
    artifacts.extend(['primary-overlay-sensitivity.sqlite3','primary-overlay-sensitivity.json','primary-overlay-acceptance.sqlite3','primary-overlay-acceptance.json'])
    artifacts.extend(p.name for p in sorted(RUN.glob("aggregate_counties-*.py")))
    artifacts.extend(p.name for p in sorted(RUN.glob('audit_primary_overlay-v*.py')))
    artifacts.extend(p.name for p in sorted(RUN.glob('finalize_primary_overlay_audit-v*.py')))
    if (RUN / "native-prefix-reuse.json").exists():
        artifacts.append("native-prefix-reuse.json")
    if (RUN / 'native-v6-prefix-reuse.json').exists():
        artifacts.append('native-v6-prefix-reuse.json')
    if (RUN / 'native-v7-prefix-reuse.json').exists():
        artifacts.append('native-v7-prefix-reuse.json')
    analysis = {name: {"bytes": (RUN / name).stat().st_size, "sha256": sha(RUN / name)} for name in artifacts}
    metadata = {"schema_version": 1, "dataset": "PAD-US 4.1", "county_vintage": 2021, "credits": CREDITS,
                "area_crs": "ESRI:102039", "acre_m2": ACRE,
                "policy": POLICY, "numeric_accounting": NUMERIC_ACCOUNTING, "inputs": inputs, "analysis_directory": RUN.relative_to(HOME).as_posix(),
                "analysis_artifacts": analysis, "core_geography_sha256": sha(corefile),
                "limitations": ["Inventory is incomplete; no qualifying mapped area is not a known true zero",
                                "Percent of whole county including water, not vegetation or land-only park coverage",
                                "Exclusion and unknown/conflict diagnostic areas overlap and are not additive",
                                "Repair sensitivity is an engineering allowance, not a statistical confidence interval"],
                "scripts": {name: sha(Path(__file__).with_name(name)) for name in ("aggregate_counties.py", "publish_county_database.py", "audit_primary_overlay.py", "finalize_primary_overlay_audit.py")}}
    metadata["publication_audit"] = {"scalar_accounting_and_saved_geometry_validity_counties": len(rows),
                                    "spatial_overlay_replays": spatial_replays,
                                    "full_source_primary_precision_replays": primary_report,
                                    "all_county_spatial_checks_performed_in_preparation": True}
    certified_prefix = source.execute("SELECT value_json FROM metadata WHERE key='certified_native_prefix'").fetchone()
    if certified_prefix is not None:
        certificate = json.loads(certified_prefix[0])
        check(RUN / certificate["source_database"], certificate["source_database_sha256"])
        metadata["certified_native_prefix"] = certificate
    certified_v6=source.execute("SELECT value_json FROM metadata WHERE key='certified_native_v6_prefix'").fetchone()
    if certified_v6 is not None:
        certificate=json.loads(certified_v6[0])
        check(RUN/certificate['source_database'],certificate['source_database_sha256'])
        metadata['certified_native_v6_prefix']=certificate
    certified_v7=source.execute("SELECT value_json FROM metadata WHERE key='certified_native_v7_prefix'").fetchone()
    if certified_v7 is not None:
        certificate=json.loads(certified_v7[0])
        check(RUN/certificate['source_database'],certificate['source_database_sha256'])
        metadata['certified_native_v7_prefix']=certificate
    metadata['numerical_resolutions']={'counties':len(resolutions),'county_fips':sorted(resolutions),
        'methods':dict(Counter(r['method'] for r in resolutions.values())),
        'method':'Logged equivalent ownership area identities or 0.1-mm derived county overlays; both require complete source precision sensitivity replays.',
        'additional_review_allowance':'Absolute category and primary spatial/area changes without cancellation; fixed-precision county corrections also carry an operation-boundary neighborhood engineering reserve.'}
    metadata["census_area_reference_diagnostics"] = {
        "interpretation": "Comparison with ALAND+AWATER reference attributes; actual numerator/denominator consistently use the full projected county polygon",
        "largest_relative_difference": max(denominator_diagnostics, key=lambda row: abs(row["projected_vs_reference_relative_percent"])),
        "largest_alternative_percentage_difference": max(denominator_diagnostics, key=lambda row: row["alternative_denominator_percentage_point_difference"])}
    prefix_path = RUN / "county_results-v3.sqlite3"
    if prefix_path.exists():
        with ro(prefix_path) as prefix:
            previous = {r[0]: r[1] for r in prefix.execute("SELECT county_fips,eligible_percent FROM county_metrics")}
        differences = [{"county_fips": row["county_fips"], "percentage_point_difference": abs(row["eligible_percent"]-previous[row["county_fips"]])} for row in rows if row["county_fips"] in previous]
        if not differences or max(row["percentage_point_difference"] for row in differences) > 0.01:
            raise RuntimeError("Material difference from preserved preliminary county calculation")
        metadata["preliminary_algorithm_comparison"] = {"prefix_database_sha256": sha(prefix_path),
            "common_counties": len(differences), "maximum_difference": max(differences, key=lambda row: row["percentage_point_difference"]),
            "interpretation": "Comparison with interrupted preliminary computation, not an independent reference dataset"}
    dest.executemany("INSERT INTO metadata VALUES(?,?)", [(key, json.dumps(value, sort_keys=True)) for key, value in metadata.items()])
    dest.commit()
    dest.execute("VACUUM")
    if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("Published SQLite integrity fails")
    copied = dest.execute("SELECT * FROM county_metrics ORDER BY county_fips").fetchall()
    if copied != [tuple(row) for row in rows]:
        raise RuntimeError("Runtime copy differs from audited analysis scalars")
    if dest.execute("SELECT * FROM county_accounting ORDER BY county_fips").fetchall() != [tuple(row) for row in accounting_rows]:
        raise RuntimeError("Runtime accounting copy differs from analysis")
    if dest.execute('SELECT * FROM county_resolutions ORDER BY county_fips').fetchall()!=[tuple(row) for row in resolution_rows]:
        raise RuntimeError('Runtime numerical resolution copy differs from analysis')
    dest.close()
    source.close()
    pending.rename(target)
    manifest = {"status": "county_database_prepared_runtime_ui_not_integrated", "created_utc": datetime.now(timezone.utc).isoformat(),
                **metadata, "county_rows": len(rows), "core_county_rows": len(core), "coverage_status_counts": dict(counts),
                "database": {"file": target.name, "bytes": target.stat().st_size, "sha256": sha(target)}}
    write(manifest_target, manifest)
    samples = {code: dict(next(row for row in rows if row["county_fips"] == code)) for code in ["06037", "12086", "15005", "02185", "09001"]}
    receipt = {"status": "passed", "created_utc": manifest["created_utc"], "credits": CREDITS,
               "county_rows": len(rows), "core_county_rows": len(core), "all_county_data_accounting_passed": True,
               "source_archive_extraction_and_repair_database_unchanged": True, "preserved_prior_audits": preserved,
               "database": manifest["database"], "manifest_sha256": sha(manifest_target),
               "coverage_status_counts": dict(counts), "samples": samples,
               "maximum_geometry_review_sensitivity_pp": max(row["geometry_review_sensitivity_pp"] for row in rows),
               "maximum_combined_review_sensitivity_pp": max(row["combined_review_sensitivity_pp"] for row in accounting_rows),
               "maximum_observed_numeric_discrepancy_m2": max(max(row[key] for key in ("partition_residual_m2", "excluded_contact_m2", "outside_public_m2", "outside_county_m2", "mapped_residual_m2", "exclusion_residual_m2")) for row in accounting_rows),
               "application_runtime_integrated": False, "portable_build_produced": False,
               "publication_audit": metadata["publication_audit"],
               "limits": metadata["limitations"], "scripts": metadata["scripts"]}
    receipt["census_area_reference_diagnostics"] = metadata["census_area_reference_diagnostics"]
    receipt['numerical_resolutions']=metadata['numerical_resolutions']
    if "preliminary_algorithm_comparison" in metadata:
        receipt["preliminary_algorithm_comparison"] = metadata["preliminary_algorithm_comparison"]
    write(receipt_path, receipt)
    print(json.dumps({"phase": "published", "database": str(target), "rows": len(rows), "bytes": target.stat().st_size}), flush=True)


if __name__ == "__main__":
    main()
