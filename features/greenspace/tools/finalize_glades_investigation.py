"""Record the unresolved Glades method issue and verify preserved inputs."""

from datetime import datetime, timezone
import json
from pathlib import Path

from accepted_adjudications import verify_inputs
from investigate_upper_snake import digest_file, require
from repair_geometry import source_tree


def main():
    home=Path(__file__).resolve().parents[1]
    dest=home/"verification/glades-investigation-outcome.json"
    require(not dest.exists(),"Refuse to overwrite outcome")
    diagnosis=json.loads((home/"verification/glades-diagnosis.json").read_text(encoding="utf-8"))
    curves=json.loads((home/"verification/glades-curve-check.json").read_text(encoding="utf-8"))
    overlay=json.loads((home/"verification/glades-overlay-check.json").read_text(encoding="utf-8"))
    require(not overlay["symmetric_difference_at_least_area_difference"],"Unexpected overlay outcome; review before finalizing")
    previous=json.loads((home/"verification/national-nj-preservation.json").read_text(encoding="utf-8"))
    audits=[]
    for entry in previous["prior_audits_checked"]+[{"run":"national-repair-nj-v2-2026-10-21","sha256":diagnosis["previous_audit_sha256"]}]:
        path=home/"working"/entry["run"]/"repair_audit.sqlite3"
        actual=digest_file(path)
        require(actual==entry["sha256"],"Previous audit changed")
        audits.append({"run":entry["run"],"sha256":actual,"unchanged":True})
    require(digest_file(home/"source/padus-4.1/PADUS4_1Geodatabase.zip")==diagnosis["source_archive_sha256"],"Original archive changed")
    expected=json.loads((home/"working/national-repair-nj-v2-2026-10-21/source-extraction-before.json").read_text(encoding="utf-8"))
    require(source_tree(home/"working/padus-4.1/PADUS4_1Geodatabase.gdb")==expected,"Original extraction changed")
    accepted_inputs=verify_inputs(home)
    evidence={}
    for name in ["glades-diagnosis.json","glades-curve-check.json","glades-overlay-check.json"]:
        path=home/"verification"/name
        evidence[path.relative_to(home).as_posix()]={"bytes":path.stat().st_size,"sha256":digest_file(path)}
    for receipt,folder in [(diagnosis,"glades-investigation-2026-10-21"),(curves,"glades-curves-2026-10-21")]:
        for name,expected_artifact in receipt["artifacts"].items():
            path=home/"working"/folder/name
            require(digest_file(path)==expected_artifact["sha256"] and path.stat().st_size==expected_artifact["bytes"],"New investigation artifact changed")
            evidence[path.relative_to(home).as_posix()]=expected_artifact
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"decision":"stopped_unresolved_numerical_overlay_no_repair_accepted",
            "credit":"Margaret","source_layer":"PADUS4_1Fee","source_fid":77431,"source_attributes":diagnosis["source_attributes"],
            "source_archive_sha256":diagnosis["source_archive_sha256"],"original_archive_and_extraction_unchanged":True,
            "prior_audits":audits,"previous_accepted_inputs_verified":accepted_inputs,
            "geometry_accepted":False,"national_processing_restarted":False,"repair_loader_changed":False,
            "ngo_ownership_remains_excluded":True,"private_military_restricted_closed_unconfirmed_exclusions_unchanged":True,
            "issue":"Fine native-source reconstructions pass validity and independent winding checks, but GEOS overlay violates the area-difference lower bound confirmed with 60-digit Decimal shoelace arithmetic. Cause remains unresolved; no tolerance or coordinate grid is introduced.",
            "absolute_decimal_area_difference_m2":overlay["absolute_decimal_area_difference_m2"],
            "reported_symmetric_difference_m2":overlay["operations"]["symmetric_difference"]["area_m2"],
            "publisher_area_not_used_to_force_a_repair":True,"evidence":evidence,
            "scope":"Read-only source investigation plus separate diagnostic artifacts. Prior candidate and both finer regions remain unaccepted. County/runtime database and UI work remain incomplete."}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in {"previous_accepted_inputs_verified","source_attributes","evidence"}}),flush=True)


if __name__=="__main__":
    main()
