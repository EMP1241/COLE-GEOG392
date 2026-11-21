"""Repair derived geometry only, logging every change and stopping for review.

Use the installed GIS Python environment. The output is an analysis audit and
replacement-geometry copy, not the Greenspace runtime county database.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import pyogrio
import pyproj
import shapely

from source_ring_proof import reconcile
from accepted_adjudications import load as load_adjudication, verify_inputs as verify_adjudication_inputs
from accepted_adjudications import EXPECTED_IDENTITIES
from verified_candidates import VerifiedCandidates
from local_exclusions import load as load_local_exclusion, verify_inputs as verify_local_inputs, IDENTITIES as LOCAL_IDENTITIES
from county_review import CountyReview
from batch_repair import BatchRepair

ACRE_M2 = 4046.8564224
RELATIVE_LIMIT_PERCENT = 0.01
NUMERIC_FLOOR_M2 = 1.0


def fingerprint_file(path):
    with path.open("rb") as stream:
        return {"bytes": path.stat().st_size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}


def source_tree(directory):
    return {p.relative_to(directory).as_posix(): fingerprint_file(p)
            for p in sorted(directory.rglob("*")) if p.is_file() and not p.name.lower().endswith(".lock")}


def json_write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def parts(geometry):
    polygons, lines, points = [], [], []
    stack = [geometry]
    while stack:
        item = stack.pop()
        kind = item.geom_type
        if item.is_empty:
            continue
        if kind == "Polygon":
            polygons.append(item)
        elif kind in {"LineString", "LinearRing"}:
            lines.append(item)
        elif kind == "Point":
            points.append(item)
        elif hasattr(item, "geoms"):
            stack.extend(reversed(list(item.geoms)))
        else:
            raise RuntimeError(f"Unsupported repair component: {kind}")
    return polygons, lines, points


def main():
    home = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=home / "working/controlled-repair-2026-10-21")
    parser.add_argument("--source-ring-check", action="store_true", help="Reconcile raw area flags using exact original-ring reconstruction and stored publisher area")
    parser.add_argument("--accepted-adjudications", action="store_true", help="Load exact investigated Kremmling/Upper Snake/NJ/Eastern Canal replacements; no general exception")
    parser.add_argument("--replay-verified-candidates", action="store_true", help="Reuse pinned prior raw candidates only after fresh source, engine and method checks")
    parser.add_argument("--conservative-local-exclusions", action="store_true", help="Apply the specifically audited Glades exclusion-only mask and county uncertainty allowance")
    parser.add_argument("--batch-county-review",action="store_true",help="Batch routine flags; stop for cumulative county sensitivity above 0.01 percentage points")
    parser.add_argument("--resume-audit",type=Path,help="Passing feature batch audit to reuse after fresh source matching")
    parser.add_argument("--resume-audit-sha256",help="Exact SHA256 of that immutable passing audit")
    args = parser.parse_args()
    if args.batch_county_review and not (args.source_ring_check and args.accepted_adjudications and args.conservative_local_exclusions):
        raise RuntimeError("Batch mode requires source checks, accepted decisions and conservative exclusions")
    if bool(args.resume_audit)!=bool(args.resume_audit_sha256) or (args.resume_audit and not args.batch_county_review):
        raise RuntimeError("Certified resume requires batch mode, audit path and exact hash together")
    output = args.output_dir.resolve()
    if output.exists():
        raise RuntimeError("Refuse to replace an existing repair audit")
    if not output.is_relative_to((home / "working").resolve()):
        raise RuntimeError("Repair output must remain inside this feature's working directory")
    gdb = home / "working/padus-4.1/PADUS4_1Geodatabase.gdb"
    archive = home / "source/padus-4.1/PADUS4_1Geodatabase.zip"
    acquisition = json.loads((archive.parent / "full-inventory-acquisition.json").read_text(encoding="utf-8"))
    archive_before = fingerprint_file(archive)
    if archive_before["sha256"] != acquisition["sha256"]:
        raise RuntimeError("Preserved source archive differs")
    print(json.dumps({"phase": "fingerprint_original_extraction"}), flush=True)
    original_tree = source_tree(gdb)
    adjudication_inputs = verify_adjudication_inputs(home) if args.accepted_adjudications else {}
    local_inputs = verify_local_inputs(home) if args.conservative_local_exclusions else {}
    county_review=CountyReview(home,percentage_only=args.batch_county_review) if args.conservative_local_exclusions else None
    batch_repair=BatchRepair(home) if args.batch_county_review else None
    preflight = json.loads((home / "verification/original-geometry-preflight.json").read_text(encoding="utf-8"))
    thresholds = {
        "feature_absolute_change_m2_above": ACRE_M2,
        "feature_relative_change_percent_above": RELATIVE_LIMIT_PERCENT,
        "relative_check_requires_absolute_change_m2_above": NUMERIC_FLOOR_M2,
        "cumulative_absolute_change_m2_above": ACRE_M2,
        "stop_for_invalid_repair_or_lost_polygon": True,
        "interpretation": "Conservative review triggers chosen before processing; invalid input area is a diagnostic, not ground truth.",
    }
    report = {"started_utc": datetime.now(timezone.utc).isoformat(), "status": "running",
              "credit": "Margaret", "method": "shapely.make_valid(method='linework', keep_collapsed=True)",
              "python_dependencies": {"shapely": shapely.__version__, "geos": shapely.geos_version_string,
                                      "pyogrio": pyogrio.__version__, "gdal": pyogrio.__gdal_version_string__,
                                      "pyproj": pyproj.__version__},
              "source_archive": archive_before, "thresholds": thresholds,
              "source_ring_check_enabled": args.source_ring_check,
              "accepted_adjudications_enabled": args.accepted_adjudications,
              "adjudication_inputs": adjudication_inputs, "accepted_adjudications_loaded": 0,
              "expected_adjudication_identities": [list(key) for key in sorted(EXPECTED_IDENTITIES)] if args.accepted_adjudications else [],
              "loaded_adjudication_identities": [],
              "layers": {}, "repairs_logged": 0, "cumulative_absolute_area_change_m2": 0.0,
              "cumulative_review_absolute_area_change_m2": 0.0, "source_ring_reconciliations": 0,
              "net_area_change_m2": 0.0, "maximum_absolute_area_change_m2": 0.0,
              "maximum_relative_area_change_percent": 0.0, "stop_record": None,
              "area_totals_are_not_eligible_or_unique_land_area": True,
              "runtime_database_built": False}
    report["eastern_canal_adjudication_enabled"] = bool(args.accepted_adjudications and args.conservative_local_exclusions)
    report["batch_county_review_enabled"]=args.batch_county_review
    report["batch_source_reconciliations"]=0
    if args.resume_audit:
        report["certified_batch_resume"]={"audit":args.resume_audit.resolve().relative_to(home).as_posix(),"sha256":args.resume_audit_sha256}
    report.update({"conservative_local_exclusions_enabled":args.conservative_local_exclusions,
                   "conservative_local_inputs":local_inputs,"conservative_local_exclusions_loaded":0,
                   "loaded_conservative_local_identities":[],"county_exclusion_uncertainty_bounds":{},
                   "county_review_allowances":{},
                   "local_materiality_policy":"For audited Glades and accepted Eastern Canal only, use their complete county review allowances in place of invalid-input feature-relative diagnostics. County cumulative caps and ordinary feature gates remain; raw diagnostics stay recorded."})
    replay = VerifiedCandidates(home, report, original_tree) if args.replay_verified_candidates else None
    report["verified_candidate_replay"] = replay.description if replay is not None else None
    report["verified_candidates_replayed"] = 0
    output.mkdir(parents=True)
    if county_review is not None:
        policy = json.loads((home / "working/national-prep-local-exclusions-2026-10-22/preparation-policy.json").read_text(encoding="utf-8"))
        policy.update({
            "authorization": "User approved continuation with the accepted Eastern Canal replacement; stop for possible material county effects.",
            "feature_relative_gate": "Ordinary historical feature-relative gates remain. Only audited Glades FID 77431 and hash-bound accepted Eastern Canal FID 88623 use their complete county allowances in place of invalid-input feature-relative diagnostics. County cumulative caps remain enforced.",
            "eastern_canal_receipt": "verification/eastern-canal-resolution.json",
            "eastern_canal_receipt_sha256": adjudication_inputs.get("verification/eastern-canal-resolution.json"),
            "eastern_canal_allowance_m2": 75.31112267463192,
            "eastern_canal_accounting": "Substitute the accepted full allowance for the old raw diagnostic; no double counting. Original unsupported lobe remains excluded.",
        })
        if args.batch_county_review:
            policy.update({"authorization":"User approved finishing national prep by batching routine repairs and stopping only for meaningful county effects.",
                           "active_county_acreage_limit_m2":None,"legacy_thresholds_are_diagnostics_only":True,
                           "county_cumulative_percentage_point_limit":0.01,
                           "feature_relative_gate":"Diagnostic only; unresolved routine flags are reconciled automatically against original source winding and native curves.",
                           "hwi_receipt_sha256":adjudication_inputs["verification/hawaii-resolution.json"],
                           "batch_policy":"Preserve originals, all prior decisions and exclusions. Carry complete record review allowances cumulatively to all intersecting counties. Stop for >0.01 percentage-point cumulative sensitivity or an unbounded/invalid data issue; one-acre and feature-relative diagnostics do not stop the run."})
        json_write(output / "preparation-policy.json", policy)
        report["preparation_policy"] = fingerprint_file(output / "preparation-policy.json")
    json_write(output / "source-extraction-before.json", original_tree)
    json_write(output / "repair-summary.json", report)
    database = output / "repair_audit.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA foreign_keys=ON;
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
        CREATE TABLE repairs(
            layer TEXT NOT NULL, source_fid INTEGER NOT NULL, name TEXT,
            attributes_json TEXT NOT NULL, original_reason TEXT NOT NULL,
            original_type TEXT NOT NULL, repaired_type TEXT NOT NULL,
            polygon_parts INTEGER NOT NULL, line_parts INTEGER NOT NULL, point_parts INTEGER NOT NULL,
            original_area_m2 REAL, repaired_polygon_area_m2 REAL,
            area_difference_m2 REAL, absolute_difference_m2 REAL, relative_difference_percent REAL,
            original_sha256 TEXT NOT NULL,repaired_sha256 TEXT NOT NULL,polygon_sha256 TEXT NOT NULL,
            original_wkb BLOB NOT NULL,repaired_wkb BLOB NOT NULL,polygon_wkb BLOB NOT NULL,
            repaired_valid INTEGER NOT NULL, polygon_valid INTEGER NOT NULL,
            stop_reasons_json TEXT NOT NULL, PRIMARY KEY(layer,source_fid)
        );
        CREATE TABLE source_ring_reconciliations(
            layer TEXT NOT NULL,source_fid INTEGER NOT NULL,proof_json TEXT NOT NULL,
            reconstructed_wkb BLOB,PRIMARY KEY(layer,source_fid),
            FOREIGN KEY(layer,source_fid) REFERENCES repairs(layer,source_fid)
        );
        CREATE TABLE accepted_adjudications(
            layer TEXT NOT NULL,source_fid INTEGER NOT NULL,proof_json TEXT NOT NULL,
            previous_candidate_wkb BLOB NOT NULL,independent_wkb BLOB NOT NULL,
            PRIMARY KEY(layer,source_fid),
            FOREIGN KEY(layer,source_fid) REFERENCES repairs(layer,source_fid)
        );
        CREATE TABLE conservative_local_exclusions(
            layer TEXT NOT NULL,source_fid INTEGER NOT NULL,proof_json TEXT NOT NULL,
            previous_candidate_wkb BLOB NOT NULL,mask_wkb BLOB NOT NULL,
            PRIMARY KEY(layer,source_fid),FOREIGN KEY(layer,source_fid) REFERENCES repairs(layer,source_fid)
        );
        CREATE TABLE batch_reconciliations(
            layer TEXT NOT NULL,source_fid INTEGER NOT NULL,proof_json TEXT NOT NULL,
            previous_candidate_wkb BLOB NOT NULL,independent_wkb BLOB NOT NULL,
            PRIMARY KEY(layer,source_fid),FOREIGN KEY(layer,source_fid) REFERENCES repairs(layer,source_fid)
        );
    """)
    for key, value in {"source_archive": archive_before, "thresholds": thresholds,
                       "method": report["method"], "credit": "Margaret"}.items():
        connection.execute("INSERT INTO metadata VALUES (?,?)", (key, json.dumps(value, sort_keys=True)))
    connection.commit()
    try:
        with (output / "repairs.jsonl").open("x", encoding="utf-8") as journal:
            for layer, geometry_type in pyogrio.list_layers(gdb):
                if not geometry_type or "Combined_" in layer:
                    continue
                info = pyogrio.read_info(gdb, layer=layer, force_feature_count=True)
                crs = pyproj.CRS(info["crs"])
                if not crs.is_projected or any(axis.unit_name != "metre" for axis in crs.axis_info):
                    raise RuntimeError("Unexpected source CRS or area units")
                stats = {"features": int(info["features"]), "read": 0, "invalid_seen": 0,
                         "repairs_logged": 0, "scan_complete": False, "crs_wkt": crs.to_wkt()}
                report["layers"][layer] = stats
                fields = [f for f in ["Category", "Unit_Nm", "Own_Type", "Own_Name", "Mang_Type", "Mang_Name", "Des_Tp", "Pub_Access", "State_Nm", "SHAPE_Area"] if f in info["fields"]]
                for start in range(0, stats["features"], 10000):
                    frame = pyogrio.read_dataframe(gdb, layer=layer, columns=fields, skip_features=start,
                                                  max_features=10000, fid_as_index=True)
                    if shapely.is_missing(frame.geometry.array).any() or shapely.is_empty(frame.geometry.array).any():
                        raise RuntimeError("Unexpected missing/empty source geometry")
                    bad = ~shapely.is_valid(frame.geometry.array)
                    stats["read"] += len(frame)
                    stats["invalid_seen"] += int(bad.sum())
                    for fid, row in frame.loc[bad].iterrows():
                        original = row.geometry
                        reason = shapely.is_valid_reason(original)
                        print(json.dumps({"phase": "repair", "layer": layer, "fid": int(fid),
                                          "repairs_logged": report["repairs_logged"]}), flush=True)
                        attributes = {key: None if value is None or value != value else str(value)
                                      for key, value in row.items() if key != "geometry"}
                        cached = replay.get(layer, int(fid), original, attributes) if replay is not None else None
                        repaired = cached[0] if cached is not None else shapely.make_valid(original, method="linework", keep_collapsed=True)
                        previous_candidate = repaired
                        if cached is not None:
                            report["verified_candidates_replayed"] += 1
                        adjudication = (load_adjudication(home, layer, int(fid), original, repaired,
                                                        attributes, archive_before["sha256"])
                                        if args.accepted_adjudications else None)
                        if adjudication is not None:
                            repaired, independent, adjudication_proof = adjudication
                            if adjudication_proof.get("county_effect_bounded") and (county_review is None or (layer,int(fid)) not in {("PADUS4_1Fee",88623),("PADUS4_1Fee",199053)}):
                                raise RuntimeError("County-bounded adjudication requires its specific identity and county review mode")
                            report["accepted_adjudications_loaded"] += 1
                            report["loaded_adjudication_identities"].append([layer, int(fid)])
                        local=(load_local_exclusion(home,layer,int(fid),original,repaired,attributes,archive_before["sha256"])
                               if args.conservative_local_exclusions else None)
                        if local is not None:
                            if adjudication is not None:
                                raise RuntimeError("A record cannot have both an exact adjudication and local exclusion")
                            repaired,local_mask,local_proof=local
                            report["conservative_local_exclusions_loaded"]+=1
                            report["loaded_conservative_local_identities"].append([layer,int(fid)])
                            for county in local_proof["county_uncertainty_bounds"]:
                                code=county["county_fips"]
                                if code in report["county_exclusion_uncertainty_bounds"]:
                                    raise RuntimeError("Multiple county mask bounds require explicit cumulative accounting")
                                report["county_exclusion_uncertainty_bounds"][code]=county
                        polygons, lines, points = parts(repaired)
                        polygon_geometry = (polygons[0] if len(polygons) == 1 else
                                            shapely.MultiPolygon(polygons) if polygons else shapely.Polygon())
                        original_area = float(original.area)
                        repaired_area = float(polygon_geometry.area)
                        difference = repaired_area - original_area
                        absolute = abs(difference)
                        finite = all(math.isfinite(number) for number in [original_area, repaired_area, difference, absolute])
                        relative = absolute / abs(original_area) * 100 if finite and original_area else None
                        reasons = []
                        if not repaired.is_valid or not polygon_geometry.is_valid:
                            reasons.append("repair_or_polygon_output_invalid")
                        if not polygons or polygon_geometry.is_empty:
                            reasons.append("polygon_area_lost_or_collapsed")
                        if not finite or original_area < 0:
                            reasons.append("source_area_diagnostic_not_assessable")
                        proof, reconstructed = None, None
                        batch=None
                        review_absolute, review_relative = absolute, relative
                        review_reference = "decoded_original_area"
                        raw_area_flag = finite and (absolute > ACRE_M2 or absolute > NUMERIC_FLOOR_M2 and (relative is None or relative > RELATIVE_LIMIT_PERCENT))
                        if local is not None and not reasons:
                            review_absolute=local_proof["review_absolute_difference_m2"]
                            review_relative=review_absolute/local_mask.area*100
                            review_reference="specific_conservative_exclusion_county_bound"
                        elif adjudication is not None and not reasons:
                            review_absolute = adjudication_proof["review_absolute_difference_m2"]
                            reference = adjudication_proof["comparison_area_m2"]
                            review_relative = review_absolute / reference * 100 if reference else None
                            review_reference = "specific_accepted_source_region"
                        elif args.source_ring_check and raw_area_flag and not reasons:
                            try:
                                publisher_area = float(row["SHAPE_Area"])
                            except (ValueError, TypeError, KeyError):
                                publisher_area = float("nan")
                            proof, reconstructed = reconcile(original, polygon_geometry, publisher_area)
                            if proof["resolved"]:
                                reference = proof["comparison_area_m2"]
                                review_absolute = abs(repaired_area - reference)
                                review_relative = review_absolute / reference * 100 if reference else None
                                review_reference = "independently_reconstructed_source_rings"
                                report["source_ring_reconciliations"] += 1
                        if batch_repair is not None and raw_area_flag and local is None and adjudication is None and not reasons and (proof is None or not proof["resolved"]):
                            certified=replay.batch_certificate(layer,int(fid)) if cached is not None else None
                            region,batch=certified if certified is not None else batch_repair.reconcile(layer,int(fid),original,polygon_geometry,attributes)
                            polygons=parts(region)[0]
                            polygon_geometry=region
                            repaired=shapely.GeometryCollection([region,*lines,*points]) if lines or points else region
                            repaired_area=float(region.area)
                            difference=repaired_area-original_area
                            absolute=abs(difference)
                            relative=absolute/abs(original_area)*100 if original_area else None
                            review_absolute=batch["review_absolute_difference_m2"]
                            review_relative=review_absolute/repaired_area*100 if repaired_area else None
                            review_reference="batch_original_source_winding_and_county_bound"
                            report["batch_source_reconciliations"]+=1
                        if not args.batch_county_review and finite and review_absolute > ACRE_M2:
                            reasons.append("feature_absolute_area_change_above_one_acre")
                        bounded_adjudication = adjudication is not None and adjudication_proof.get("county_effect_bounded",False)
                        if not args.batch_county_review and local is None and not bounded_adjudication and finite and review_absolute > NUMERIC_FLOOR_M2 and (review_relative is None or review_relative > RELATIVE_LIMIT_PERCENT):
                            reasons.append("feature_relative_area_change_above_0_01_percent")
                        if finite:
                            report["cumulative_absolute_area_change_m2"] += absolute
                            report["cumulative_review_absolute_area_change_m2"] += review_absolute
                            report["net_area_change_m2"] += difference
                            report["maximum_absolute_area_change_m2"] = max(report["maximum_absolute_area_change_m2"], absolute)
                            if relative is not None:
                                report["maximum_relative_area_change_percent"] = max(report["maximum_relative_area_change_percent"], relative)
                            if county_review is None and report["cumulative_review_absolute_area_change_m2"] > ACRE_M2:
                                reasons.append("cumulative_absolute_area_change_above_one_acre")
                        allocations=county_review.allocate(original,polygon_geometry,review_absolute) if county_review is not None else []
                        if bounded_adjudication and {a["county_fips"] for a in allocations} != {c["county_fips"] for c in adjudication_proof["county_bounds"]}:
                            raise RuntimeError("Accepted county-bounded adjudication reaches a different county")
                        risk=None
                        if county_review is not None:
                            report["county_review_allowances"]=county_review.totals
                            if county_review.material():
                                reasons.append("county_cumulative_review_allowance_material")
                            if reasons:
                                risk=county_review.unresolved_risk(original,polygon_geometry)
                                if risk["material_change_cannot_be_excluded"]:
                                    reasons.append("unresolved_geometry_could_materially_affect_county")
                        wkb = [shapely.to_wkb(g, byte_order=1) for g in (original, repaired, polygon_geometry)]
                        hashes = [hashlib.sha256(value).hexdigest() for value in wkb]
                        log = {"layer": layer, "source_fid": int(fid), "name": attributes.get("Unit_Nm"),
                               "attributes": attributes, "original_reason": reason,
                               "original_type": original.geom_type, "repaired_type": repaired.geom_type,
                               "polygon_parts": len(polygons), "line_parts": len(lines), "point_parts": len(points),
                               "original_area_m2": original_area if math.isfinite(original_area) else None,
                               "repaired_polygon_area_m2": repaired_area if math.isfinite(repaired_area) else None,
                               "area_difference_m2": difference if finite else None,
                               "absolute_difference_m2": absolute if finite else None,
                               "relative_difference_percent": relative, "original_sha256": hashes[0],
                               "repaired_sha256": hashes[1], "polygon_sha256": hashes[2],
                               "repaired_valid": bool(repaired.is_valid), "polygon_valid": bool(polygon_geometry.is_valid),
                               "stop_reasons": reasons, "review_reference": review_reference,
                               "review_absolute_difference_m2": review_absolute if finite else None,
                               "source_ring_proof": proof,
                               "accepted_adjudication": adjudication_proof if adjudication is not None else None,
                               "conservative_local_exclusion":local_proof if local is not None else None,
                               "batch_source_reconciliation":batch,
                               "county_review_allocations":allocations,"county_materiality_risk":risk,
                               "verified_candidate_replay": cached[1] if cached is not None else None}
                        connection.execute("INSERT INTO repairs VALUES (" + ",".join("?" for _ in range(24)) + ")",
                                           (layer, int(fid), log["name"], json.dumps(attributes), reason,
                                            original.geom_type, repaired.geom_type, len(polygons), len(lines), len(points),
                                            log["original_area_m2"], log["repaired_polygon_area_m2"], log["area_difference_m2"],
                                            log["absolute_difference_m2"], relative, *hashes, *wkb,
                                            int(repaired.is_valid), int(polygon_geometry.is_valid), json.dumps(reasons)))
                        if proof is not None:
                            connection.execute("INSERT INTO source_ring_reconciliations VALUES (?,?,?,?)",
                                               (layer,int(fid),json.dumps(proof,allow_nan=False),
                                                shapely.to_wkb(reconstructed,byte_order=1) if reconstructed is not None else None))
                        if adjudication is not None:
                            connection.execute("INSERT INTO accepted_adjudications VALUES (?,?,?,?,?)",
                                               (layer,int(fid),json.dumps(adjudication_proof,allow_nan=False),
                                                shapely.to_wkb(previous_candidate,byte_order=1),
                                                shapely.to_wkb(independent,byte_order=1)))
                        if local is not None:
                            connection.execute("INSERT INTO conservative_local_exclusions VALUES (?,?,?,?,?)",
                                               (layer,int(fid),json.dumps(local_proof,allow_nan=False),
                                                shapely.to_wkb(previous_candidate,byte_order=1),shapely.to_wkb(local_mask,byte_order=1)))
                        if batch is not None:
                            connection.execute("INSERT INTO batch_reconciliations VALUES (?,?,?,?,?)",
                                               (layer,int(fid),json.dumps(batch,allow_nan=False),
                                                shapely.to_wkb(previous_candidate,byte_order=1),shapely.to_wkb(polygon_geometry,byte_order=1)))
                        connection.commit()
                        journal.write(json.dumps(log, ensure_ascii=False, allow_nan=False) + "\n")
                        journal.flush()
                        stats["repairs_logged"] += 1
                        report["repairs_logged"] += 1
                        if reasons:
                            report["status"] = "stopped_significant_change_or_invalid_output"
                            report["stop_record"] = log
                            break
                    json_write(output / "repair-summary.json", report)
                    if report["stop_record"]:
                        break
                    print(json.dumps({"phase": "scan", "layer": layer, "read": stats["read"],
                                      "features": stats["features"], "repairs_logged": report["repairs_logged"]}), flush=True)
                stats["scan_complete"] = stats["read"] == stats["features"] and not report["stop_record"]
                if report["stop_record"]:
                    break
                if stats["read"] != stats["features"] or stats["invalid_seen"] != preflight["layers"][layer]["invalid"]:
                    raise RuntimeError("Source row/invalid accounting differs from preflight")
            if not report["stop_record"]:
                if args.conservative_local_exclusions and {tuple(k) for k in report["loaded_conservative_local_identities"]}!=LOCAL_IDENTITIES:
                    raise RuntimeError("Expected conservative exclusion was not loaded exactly once")
                if replay is not None and replay.used != replay.keys:
                    raise RuntimeError("Not all verified candidate identities were encountered in fresh source")
                if args.accepted_adjudications and (
                        report["accepted_adjudications_loaded"] != len(EXPECTED_IDENTITIES)
                        or {tuple(key) for key in report["loaded_adjudication_identities"]} != EXPECTED_IDENTITIES):
                    raise RuntimeError("Expected specific adjudications were not loaded exactly once each")
                report["status"] = "completed_within_review_thresholds"
    except Exception as error:
        report["status"] = "stopped_processing_error"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        connection.close()
        if replay is not None:
            try:
                replay.close()
                report["prior_candidate_audit_unchanged"] = True
            except Exception as error:
                report["prior_candidate_audit_unchanged"] = False
                report["candidate_preservation_error"] = {"type":type(error).__name__,"message":str(error)}
        after_tree = source_tree(gdb)
        report["source_extraction_unchanged"] = after_tree == original_tree
        report["source_archive_unchanged"] = fingerprint_file(archive) == archive_before
        try:
            report["conservative_local_inputs_unchanged"]=(verify_local_inputs(home)==local_inputs if args.conservative_local_exclusions else True)
            report["adjudication_inputs_unchanged"] = (verify_adjudication_inputs(home) == adjudication_inputs
                                                      if args.accepted_adjudications else True)
        except Exception as error:
            report["adjudication_inputs_unchanged"] = False
            report["adjudication_preservation_error"] = {"type": type(error).__name__, "message": str(error)}
        if (not report["source_extraction_unchanged"] or not report["source_archive_unchanged"]
                or not report["adjudication_inputs_unchanged"] or report.get("conservative_local_inputs_unchanged") is not True or report.get("prior_candidate_audit_unchanged") is False):
            report["status"] = "stopped_source_preservation_failure"
        report["completed_utc"] = datetime.now(timezone.utc).isoformat()
        report["audit_database"] = fingerprint_file(database)
        json_write(output / "repair-summary.json", report)
        print(json.dumps({"summary": str(output / "repair-summary.json"), "status": report["status"],
                          "repairs_logged": report["repairs_logged"], "stop_record": report["stop_record"]}), flush=True)


if __name__ == "__main__":
    main()
