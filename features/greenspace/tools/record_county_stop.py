"""Record the material Oneida county stop and recheck preserved inputs only."""

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

import aggregate_counties as preparation
from repair_geometry import source_tree


def main():
    receipt_path = preparation.HOME / "verification/county-aggregation-stop-16071-2026-10-22.json"
    if receipt_path.exists():
        raise RuntimeError("Refuse to replace a stop receipt")
    events = []
    for path in preparation.RUN.glob("operation-timing-*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("phase") == "county_error" and event.get("county_fips") == "16071":
                events.append((event["monotonic"],path,event))
    _, log_path, event = max(events)
    context = event["context"]
    if context["combined_sensitivity"] <= 0.01:
        raise RuntimeError("This is not the documented material stop")
    frame, core, _ = preparation.counties()
    county = frame.loc[frame.GEOID == "16071"].iloc[0]
    with preparation.ro(preparation.RUN / preparation.RESULT_NAME) as connection:
        completed = connection.execute("SELECT county_fips FROM county_metrics ORDER BY county_fips").fetchall()
        identity = json.loads(connection.execute("SELECT value_json FROM metadata WHERE key='aggregation_identity'").fetchone()[0])
        if connection.execute("SELECT COUNT(*) FROM county_metrics WHERE county_fips='16071'").fetchone()[0]:
            raise RuntimeError("Failed county was committed")
    if identity["code_sha256"] != preparation.sha(Path(preparation.__file__)):
        raise RuntimeError("Failed calculation source differs")
    stage = json.loads((preparation.RUN / "stage-manifest.json").read_text(encoding="utf-8"))
    print(json.dumps({"phase": "check_preserved_inputs"}),flush=True)
    inputs = {}
    for relative, expected in {
        "source/padus-4.1/PADUS4_1Geodatabase.zip": preparation.ARCHIVE_SHA,
        "source/census-2021/tl_2021_us_county.zip": preparation.CENSUS_SHA,
        "working/national-batch-prep-v2-2026-10-22/repair_audit.sqlite3": preparation.REPAIR_SHA,
        "working/national-batch-prep-v2-2026-10-22/repair-summary.json": preparation.SUMMARY_SHA,
        preparation.RUN.relative_to(preparation.HOME).as_posix()+"/source_stage.sqlite3": stage["stage_database"]["sha256"],
    }.items():
        inputs[relative] = preparation.check(preparation.HOME / relative, expected)
    if source_tree(preparation.HOME / "working/padus-4.1/PADUS4_1Geodatabase.gdb") != stage["source_extraction"]:
        raise RuntimeError("Original extraction differs")
    previous = json.loads((preparation.HOME / "verification/national-geometry-preparation-complete-2026-10-22.json").read_text(encoding="utf-8"))
    prior_audits = []
    for prior in previous["preserved_prior_audits"]:
        preparation.check(preparation.HOME / "working" / prior["run"] / "repair_audit.sqlite3", prior["sha256"])
        prior_audits.append({"run":prior["run"],"sha256":prior["sha256"],"unchanged":True})
    certificate = json.loads((preparation.RUN / "native-prefix-reuse.json").read_text(encoding="utf-8"))
    preparation.check(preparation.RUN / certificate["source_database"], certificate["source_database_sha256"])
    with preparation.ro(preparation.RUN / "source_stage.sqlite3") as connection:
        rows = connection.execute("SELECT s.id,s.layer,s.source_fid,s.attributes_json,s.original_sha256,s.geometry_sha256 FROM source_records s JOIN county_sources c ON c.source_id=s.id WHERE c.county_fips='16071' ORDER BY s.id").fetchall()
    candidates = [{"source_id":row[0],"layer":row[1],"source_fid":row[2],"attributes":json.loads(row[3]),
                   "original_wkb_sha256":row[4],"derived_wkb_sha256":row[5]} for row in rows]
    receipt = {"status":"stopped_material_county_accounting_issue","created_utc":datetime.now(timezone.utc).isoformat(),
        "county_fips":"16071","county_name":str(county.NAME),"state":"Idaho","credits":preparation.CREDITS,
        "failure":"Ownership exclusive/shared subtotals do not reconcile with eligible area within the county review budget",
        "context":context,"absolute_partition_discrepancy_acres":context["residual"]/preparation.ACRE,
        "partition_discrepancy_percentage_points":context["residual"]/context["denominator"]*100,
        "review_budget_percentage_points":0.01,"completed_reference_counties":len(completed),
        "completed_core_counties":sum(code[0] in core for code in completed),"reference_counties":3234,"core_counties":len(core),
        "completed_county_fips":[code[0] for code in completed],"failed_county_committed":False,
        "source_records_staged":stage["source_records"],"repairs_freshly_matched":stage["repairs_freshly_matched"],
        "analysis_database":{"file":preparation.RESULT_NAME,"bytes":(preparation.RUN/preparation.RESULT_NAME).stat().st_size,
                             "sha256":preparation.sha(preparation.RUN/preparation.RESULT_NAME)},
        "operation_log":{"file":log_path.name,"sha256":preparation.sha(log_path)},"inputs":inputs,
        "source_extraction_unchanged":True,"prior_audits_preserved":prior_audits,"native_prefix_unchanged":True,
        "exclusion_rules_unchanged":True,"new_repair_accepted":False,
        "final_runtime_database_published":(preparation.HOME/"data/greenspace.sqlite3").exists(),
        "source_code_sha256":preparation.sha(Path(preparation.__file__)),"stop_script_sha256":preparation.sha(Path(__file__)),
        "candidate_source_records":{"purpose":"Envelope candidates for a future investigation, not verified positive intersections",
                                    "count":len(candidates),"by_layer":dict(Counter(row["layer"] for row in candidates)),"records":candidates},
        "limits":["Absolute ownership-accounting discrepancy; direction and cause are unresolved",
                  "Not yet established as an error in the main eligible county percentage",
                  "Engineering review sensitivity is not a statistical confidence interval or complete source-error bound",
                  "Component-indexed prototype was rejected; only native whole-geometry results are retained in this stopped run"]}
    preparation.write(receipt_path,receipt)
    print(json.dumps({"status":receipt["status"],"completed":len(completed),"discrepancy_acres":receipt["absolute_partition_discrepancy_acres"],
                      "runtime_database_published":receipt["final_runtime_database_published"],"receipt":str(receipt_path)}),flush=True)


if __name__ == "__main__":
    main()
