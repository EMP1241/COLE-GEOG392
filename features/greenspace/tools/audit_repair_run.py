"""Read-only data audit of a completed or stopped controlled preparation run."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import shapely

from accepted_adjudications import verify_inputs, load as load_adjudication
from accepted_adjudications import EXPECTED_IDENTITIES
from verified_candidates import VerifiedCandidates
from repair_geometry import fingerprint_file, source_tree,parts
from local_exclusions import load as load_local_exclusion, verify_inputs as verify_local_inputs
from county_review import CountyReview
from batch_repair import BatchRepair


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    home = Path(__file__).resolve().parents[1]
    run = args.run.resolve()
    output = args.output.resolve()
    if output.exists() or not output.is_relative_to((home / "verification").resolve()):
        raise RuntimeError("Audit output must be new and inside feature verification")
    if not run.is_relative_to((home / "working").resolve()):
        raise RuntimeError("Audit input must be inside feature working")
    summary = json.loads((run / "repair-summary.json").read_text(encoding="utf-8"))
    if summary["status"] == "running":
        raise RuntimeError("Do not audit an active preparation run")
    errors = []

    def require(condition, message):
        if not condition:
            errors.append(message)

    database = run / "repair_audit.sqlite3"
    before = fingerprint_file(database)
    require(before == summary["audit_database"], "Audit database differs from completed run receipt")
    journal = [json.loads(line) for line in (run / "repairs.jsonl").read_text(encoding="utf-8").splitlines()]
    keys = [(entry["layer"], entry["source_fid"]) for entry in journal]
    require(len(keys) == len(set(keys)), "Duplicate journal identities")
    logs = dict(zip(keys, journal))
    raw_total, review_total = [], []
    county_totals={}
    county_validator=CountyReview(home,percentage_only=summary.get("batch_county_review_enabled",False)) if summary.get("conservative_local_exclusions_enabled") else None
    batch_validator=BatchRepair(home) if summary.get("batch_county_review_enabled") else None
    batch_count=0
    proof_count, adjudication_count, local_count = 0, 0, 0
    expected_tree = json.loads((run / "source-extraction-before.json").read_text(encoding="utf-8"))
    replay = VerifiedCandidates(home, summary, expected_tree) if summary.get("verified_candidate_replay") else None
    if replay is not None:
        require(replay.description == summary["verified_candidate_replay"], "Verified candidate replay description differs")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        require(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity failure")
        require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "SQLite foreign-key failure")
        row_count = connection.execute("SELECT COUNT(*) FROM repairs").fetchone()[0]
        require(row_count == summary["repairs_logged"] == len(journal), "Repair count differs")
        stored_keys = set(connection.execute("SELECT layer,source_fid FROM repairs").fetchall())
        require({tuple(key) for key in stored_keys} == set(keys), "Journal/database identities differ")
        # Keep large WKB blobs bounded to one record while auditing nationally.
        for row in connection.execute("SELECT * FROM repairs ORDER BY layer,source_fid"):
            key = (row["layer"], row["source_fid"])
            log = logs[key]
            geometries = {}
            for prefix in ("original", "repaired", "polygon"):
                body = row[prefix + "_wkb"]
                digest = hashlib.sha256(body).hexdigest()
                require(digest == row[prefix + "_sha256"] == log[prefix + "_sha256"], f"WKB hash mismatch {key} {prefix}")
                geometries[prefix] = shapely.from_wkb(body)
            original, repaired, polygon = (geometries[prefix] for prefix in ("original", "repaired", "polygon"))
            require(not original.is_valid, f"Unexpected valid original {key}")
            require(repaired.is_valid and polygon.is_valid and not polygon.is_empty, f"Invalid/lost repair {key}")
            require(polygon.geom_type in {"Polygon", "MultiPolygon"}, f"Nonpolygon output {key}")
            require(original.area == row["original_area_m2"] and polygon.area == row["repaired_polygon_area_m2"], f"Stored area mismatch {key}")
            require(polygon.area - original.area == row["area_difference_m2"], f"Stored difference mismatch {key}")
            require(json.loads(row["stop_reasons_json"]) == log["stop_reasons"], f"Stop reasons mismatch {key}")
            require(json.loads(row["attributes_json"]) == log["attributes"], f"Attributes mismatch {key}")
            raw_total.append(abs(polygon.area - original.area))
            review = log["review_absolute_difference_m2"]
            require(math.isfinite(review) and review >= 0, f"Invalid review difference {key}")
            review_total.append(review)
            if county_validator is not None:
                expected=county_validator.allocate(original,polygon,review)
                require(sorted(expected,key=lambda a:a["county_fips"])==sorted(log.get("county_review_allocations",[]),key=lambda a:a["county_fips"]),f"Fresh county geometry allocation differs {key}")
            for allocation in log.get("county_review_allocations",[]):
                require(allocation["allocated_review_allowance_m2"]==review,f"County allocation amount differs {key}")
                county_totals.setdefault(allocation["county_fips"],[]).append(review)
            if log.get("verified_candidate_replay") is not None:
                require(replay is not None, f"Unexpected replay record {key}")
                cached = replay.get(*key, original, json.loads(row["attributes_json"]))
                require(cached is not None and cached[1] == log["verified_candidate_replay"], f"Verified candidate provenance differs {key}")
                if cached is not None:
                    prior_body = connection.execute("SELECT previous_candidate_wkb FROM accepted_adjudications WHERE layer=? AND source_fid=?",key).fetchone()
                    if prior_body is None and log.get("conservative_local_exclusion") is not None:
                        prior_body=connection.execute("SELECT previous_candidate_wkb FROM conservative_local_exclusions WHERE layer=? AND source_fid=?",key).fetchone()
                    if prior_body is None and log.get("batch_source_reconciliation") is not None:
                        prior_body=connection.execute("SELECT previous_candidate_wkb FROM batch_reconciliations WHERE layer=? AND source_fid=?",key).fetchone()
                    raw_body = prior_body[0] if prior_body else row["repaired_wkb"]
                    require(hashlib.sha256(raw_body).hexdigest() == cached[1]["raw_candidate_wkb_sha256"], f"Replayed raw candidate changed {key}")
            if log["source_ring_proof"] is not None:
                proof_row = connection.execute("SELECT * FROM source_ring_reconciliations WHERE layer=? AND source_fid=?", key).fetchone()
                require(proof_row is not None and json.loads(proof_row["proof_json"]) == log["source_ring_proof"], f"Source proof mismatch {key}")
                if log["source_ring_proof"]["resolved"]:
                    proof_count += 1
                    independent = shapely.from_wkb(proof_row["reconstructed_wkb"])
                    require(independent.is_valid and independent.symmetric_difference(polygon).area == 0, f"Resolved source region mismatch {key}")
                    require(abs(independent.area - polygon.area) == review, f"Resolved review reference mismatch {key}")
            if log.get("accepted_adjudication") is not None:
                adjudication_count += 1
                accepted = connection.execute("SELECT * FROM accepted_adjudications WHERE layer=? AND source_fid=?", key).fetchone()
                require(accepted is not None and json.loads(accepted["proof_json"]) == log["accepted_adjudication"], f"Adjudication receipt mismatch {key}")
                independent = shapely.from_wkb(accepted["independent_wkb"])
                require(independent.is_valid and independent.symmetric_difference(polygon).area == 0, f"Accepted independent region mismatch {key}")
                require(key in EXPECTED_IDENTITIES, f"Unexpected adjudication {key}")
                previous_candidate = shapely.from_wkb(accepted["previous_candidate_wkb"])
                loaded = load_adjudication(home, *key, original, previous_candidate,
                                          json.loads(row["attributes_json"]), summary["source_archive"]["sha256"])
                require(loaded is not None and loaded[0].symmetric_difference(polygon).area == 0, f"Pinned adjudication recheck differs {key}")
                require(loaded is not None and hashlib.sha256(shapely.to_wkb(loaded[0],byte_order=1)).hexdigest() == row["repaired_sha256"], f"Pinned full adjudication hash differs {key}")
                require(loaded is not None and loaded[2]["review_absolute_difference_m2"] == review, f"Adjudication review reference differs {key}")
                if log["accepted_adjudication"].get("county_effect_bounded"):
                    require(key in {("PADUS4_1Fee",88623),("PADUS4_1Fee",199053)} and summary.get("eastern_canal_adjudication_enabled") and county_validator is not None, f"Unexpected county-bounded adjudication {key}")
                    saved=log["accepted_adjudication"]
                    require(verify_inputs(home,saved["input_sha256"])==saved["input_sha256"],f"Historical accepted input subset differs {key}")
                    require(loaded is not None and {k:v for k,v in loaded[2].items() if k!="input_sha256"} == {k:v for k,v in saved.items() if k!="input_sha256"}, f"County-bounded accepted proof differs {key}")
                    require({a["county_fips"] for a in log["county_review_allocations"]} == {c["county_fips"] for c in saved["county_bounds"]}, f"Accepted county scope differs {key}")
            if log.get("conservative_local_exclusion") is not None:
                local_count+=1
                saved=connection.execute("SELECT * FROM conservative_local_exclusions WHERE layer=? AND source_fid=?",key).fetchone()
                require(saved is not None and json.loads(saved["proof_json"])==log["conservative_local_exclusion"],f"Local exclusion provenance differs {key}")
                if saved is not None:
                    previous=shapely.from_wkb(saved["previous_candidate_wkb"])
                    loaded=load_local_exclusion(home,*key,original,previous,json.loads(row["attributes_json"]),summary["source_archive"]["sha256"])
                    require(loaded is not None and hashlib.sha256(shapely.to_wkb(loaded[0],byte_order=1)).hexdigest()==row["repaired_sha256"],f"Pinned local full exclusion differs {key}")
                    require(loaded is not None and hashlib.sha256(shapely.to_wkb(loaded[1],byte_order=1)).hexdigest()==row["polygon_sha256"],f"Pinned local mask differs {key}")
                    require(loaded is not None and loaded[2]==log["conservative_local_exclusion"] and loaded[2]["review_absolute_difference_m2"]==review,f"Local review allowance differs {key}")
            if log.get("batch_source_reconciliation") is not None:
                batch_count+=1
                stored=connection.execute("SELECT * FROM batch_reconciliations WHERE layer=? AND source_fid=?",key).fetchone()
                require(stored is not None and json.loads(stored["proof_json"])==log["batch_source_reconciliation"],f"Batch proof differs {key}")
                if stored is not None:
                    previous=shapely.from_wkb(stored["previous_candidate_wkb"])
                    _,prior_lines,prior_points=parts(previous)
                    _,actual_lines,actual_points=parts(repaired)
                    require([shapely.to_wkb(g,byte_order=1) for g in prior_lines+prior_points]==[shapely.to_wkb(g,byte_order=1) for g in actual_lines+actual_points],f"Batch collapsed non-area components changed {key}")
                    source_parts=[]
                    stack=[previous]
                    while stack:
                        item=stack.pop()
                        if item.geom_type=='Polygon':source_parts.append(item)
                        elif hasattr(item,'geoms'):stack.extend(reversed(list(item.geoms)))
                    raw_polygon=source_parts[0] if len(source_parts)==1 else shapely.MultiPolygon(source_parts)
                    certified=replay.batch_certificate(*key) if log.get("verified_candidate_replay") is not None else None
                    region,fresh=certified if certified is not None else batch_validator.reconcile(*key,original,raw_polygon,json.loads(row["attributes_json"]))
                    require(json.loads(json.dumps(fresh))==log["batch_source_reconciliation"] and fresh["review_absolute_difference_m2"]==review,f"Fresh batch source proof differs {key}")
                    require(region.symmetric_difference(polygon).is_empty and shapely.from_wkb(stored["independent_wkb"]).symmetric_difference(polygon).is_empty,f"Batch accepted region differs {key}")
        require(proof_count == summary["source_ring_reconciliations"], "Source proof count mismatch")
        require(adjudication_count == summary["accepted_adjudications_loaded"], "Adjudication count mismatch")
        require(local_count==summary.get("conservative_local_exclusions_loaded",0),"Local exclusion count mismatch")
        require(batch_count==summary.get("batch_source_reconciliations",0),"Batch reconciliation count differs")
    replay_count = len(replay.used) if replay is not None else 0
    require(replay_count == summary.get("verified_candidates_replayed",0), "Replayed candidate count differs")
    if replay is not None:
        replay.close()
    for values, field in ((raw_total, "cumulative_absolute_area_change_m2"), (review_total, "cumulative_review_absolute_area_change_m2")):
        require(math.isclose(math.fsum(values), summary[field], rel_tol=1e-12, abs_tol=1e-6), f"Cumulative area mismatch {field}")
    if summary.get("conservative_local_exclusions_enabled"):
        require(set(county_totals)==set(summary["county_review_allowances"]),"County review identities differ")
        for code,values in county_totals.items():
            require(math.isclose(math.fsum(values),summary["county_review_allowances"][code]["review_allowance_m2"],rel_tol=1e-12,abs_tol=1e-6),f"County allowance total differs {code}")
            require(county_validator.totals[code]["conservative_sensitivity_denominator_m2"]==summary["county_review_allowances"][code]["conservative_sensitivity_denominator_m2"],f"County sensitivity denominator differs {code}")
        policy=run/"preparation-policy.json"
        require(policy.exists(),"County preparation policy receipt missing")
        if policy.exists():
            contract=json.loads(policy.read_text(encoding="utf-8"))
            require(contract["county_cumulative_review_limit_m2"]==4046.8564224 and contract["county_cumulative_percentage_point_limit"]==0.01,"County review policy differs")
            if summary.get("batch_county_review_enabled"):
                require(contract.get("active_county_acreage_limit_m2") is None and contract.get("legacy_thresholds_are_diagnostics_only") is True,"Batch review policy differs")
            if summary.get("eastern_canal_adjudication_enabled"):
                require(fingerprint_file(policy)==summary.get("preparation_policy"),"Current county preparation policy fingerprint differs")
                require(contract.get("eastern_canal_allowance_m2")==75.31112267463192 and contract.get("eastern_canal_receipt_sha256")==summary["adjudication_inputs"]["verification/eastern-canal-resolution.json"],"Eastern Canal policy differs")
    require(bool(summary["stop_record"]) == any(log["stop_reasons"] for log in journal), "Stop accounting mismatch")
    if summary["stop_record"]:
        require(journal[-1] == summary["stop_record"], "Run continued beyond first stop")
    archive = home / "source/padus-4.1/PADUS4_1Geodatabase.zip"
    require(fingerprint_file(archive) == summary["source_archive"], "Original archive changed")
    require(source_tree(home / "working/padus-4.1/PADUS4_1Geodatabase.gdb") == expected_tree, "Original extraction changed")
    if summary["accepted_adjudications_enabled"]:
        require(verify_inputs(home, summary["adjudication_inputs"]) == summary["adjudication_inputs"], "Adjudication inputs changed")
    if summary.get("conservative_local_exclusions_enabled"):
        require(verify_local_inputs(home)==summary["conservative_local_inputs"],"Local exclusion evidence changed")
    require(fingerprint_file(database) == before, "Read-only audit modified its input")
    report = {"created_utc": datetime.now(timezone.utc).isoformat(), "status": "passed" if not errors else "failed",
              "discrepancies": errors, "run": run.relative_to(home).as_posix(),
              "run_status": summary["status"], "repairs_checked": len(journal),
              "resolved_source_ring_proofs_checked": proof_count,
              "accepted_specific_adjudications_checked": adjudication_count,
              "conservative_local_exclusions_checked":local_count,
              "batch_source_reconciliations_checked":batch_count,
              "verified_replayed_candidates_checked": replay_count,
              "original_archive_and_extraction_unchanged": not any("archive changed" in e or "extraction changed" in e for e in errors),
              "runtime_database_built": False, "repairs_performed_in_audit": False,
              "credit": "Margaret", "audit_database": before}
    if summary.get("conservative_local_exclusions_enabled"):
        report["preparation_policy"]=fingerprint_file(run/"preparation-policy.json")
        report["county_allocations_recomputed_from_preserved_census"]=True
        report["census_source_sha256"]=county_validator.source_sha256
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report), flush=True)
    if errors:
        raise RuntimeError("Repair data audit failed")


if __name__ == "__main__":
    main()
