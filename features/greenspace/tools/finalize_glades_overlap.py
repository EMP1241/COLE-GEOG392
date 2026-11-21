"""Corroborate local impact, locate counties, and fingerprint preserved evidence."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pyogrio
import shapely

from accepted_adjudications import verify_inputs
from investigate_upper_snake import digest_file, require
from repair_geometry import source_tree


def canonical_digest(g):
    return hashlib.sha256(shapely.to_wkb(shapely.normalize(g),byte_order=1)).hexdigest()


def main():
    home=Path(__file__).resolve().parents[1]
    dest=home/"verification/glades-overlap-outcome-2026-10-22.json"
    require(not dest.exists(),"Refuse to overwrite outcome")
    screen_path=home/"verification/glades-overlap-screen-2026-10-22.json"
    need_path=home/"verification/glades-exclusion-need-2026-10-22.json"
    screen=json.loads(screen_path.read_text(encoding="utf-8"))
    need=json.loads(need_path.read_text(encoding="utf-8"))
    require(digest_file(screen_path)==need["screen_receipt_sha256"],"Screen changed")
    root=home/"working/glades-exclusion-need-2026-10-22"
    for name,item in need["artifacts"].items():
        require(digest_file(root/name)==item["sha256"] and (root/name).stat().st_size==item["bytes"],"Impact artifact changed")
    relevant=[]
    for row in screen["records"]:
        if not row["otherwise_eligible_attribute_screen"] or not row["default_valid"] or row["actual_nonlinear_segments"]:
            continue
        if not any(v.get("area_m2",0)>0 for v in row["diagnostic_overlaps"].values()):
            continue
        suffix=row["layer"].removeprefix("PADUS4_1")
        public=shapely.from_wkb((home/f"working/glades-overlap-2026-10-22/{suffix}-{row['source_fid']}-default.wkb").read_bytes())
        hashes={}
        for name,item in screen["existing_unaccepted_glades_representations"].items():
            require(digest_file(home/item["path"])==item["sha256"],"Glades diagnostic changed")
            glades=shapely.from_wkb((home/item["path"]).read_bytes())
            hashes[name]=canonical_digest(public.intersection(glades))
        relevant.append({"layer":row["layer"],"source_fid":row["source_fid"],"name":row["attributes"]["Unit_Nm"],
                         "overlap_m2":row["diagnostic_overlaps"]["fine_positive"]["area_m2"],
                         "overlap_acres":row["diagnostic_overlaps"]["fine_positive"]["area_acres"],
                         "normalized_intersection_sha256_by_representation":hashes,
                         "all_three_intersections_byte_identical_after_normalization":len(set(hashes.values()))==1})
    county_zip=home/"source/census-2021/tl_2021_us_county.zip"
    counties=pyogrio.read_dataframe(county_zip,columns=["GEOID","NAME","STATEFP","ALAND","AWATER"]).to_crs("ESRI:102039")
    tree=shapely.STRtree(counties.geometry.array)
    witnesses=[]
    for entry in need["independent_source_winding_witnesses"]:
        point=shapely.Point(entry["xy"])
        rows=[]
        for i in tree.query(point,predicate="within"):
            county=counties.iloc[i]
            rows.append({"county_fips":str(county["GEOID"]),"county_name":str(county["NAME"]),"state_fips":str(county["STATEFP"])})
        require(len(rows)==1,"Witness county attribution ambiguous")
        witnesses.append({**entry,"county":rows[0]})
    preserve=json.loads((home/"verification/glades-investigation-outcome.json").read_text(encoding="utf-8"))
    for item in preserve["prior_audits"]:
        require(digest_file(home/"working"/item["run"]/"repair_audit.sqlite3")==item["sha256"],"Prior audit changed")
    expected_tree=json.loads((home/"working/national-repair-nj-v2-2026-10-21/source-extraction-before.json").read_text(encoding="utf-8"))
    require(source_tree(home/"working/padus-4.1/PADUS4_1Geodatabase.gdb")==expected_tree,"Original extraction changed")
    require(digest_file(home/"source/padus-4.1/PADUS4_1Geodatabase.zip")==screen["source_archive_sha256"],"Original archive changed")
    inputs=verify_inputs(home)
    evidence={}
    for path in [screen_path,need_path]:
        evidence[path.relative_to(home).as_posix()]={"bytes":path.stat().st_size,"sha256":digest_file(path)}
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"local_date":"2026-10-22","credit":"Margaret",
            "decision":"overlap_affects_exclusion_accounting_record_not_safely_discardable_no_repair_accepted",
            "source_archive_sha256":screen["source_archive_sha256"],"all_five_source_layers_screened":True,
            "bbox_records":len(screen["records"]),"provisionally_public_open_records":need["all_otherwise_eligible_attribute_records"],
            "valid_straight_public_records_screened":need["valid_straight_public_records"],"positive_overlap_valid_public_records":relevant,
            "unique_valid_public_subset_overlap_m2":need["diagnostic_variants"]["fine_positive"]["valid_straight_public_overlap_m2"],
            "unique_valid_public_subset_overlap_acres":need["diagnostic_variants"]["fine_positive"]["overlap_acres"],
            "other_valid_straight_exclusion_records":need["other_valid_straight_exclusion_records"],
            "covered_by_other_valid_straight_exclusions_m2":need["diagnostic_variants"]["fine_positive"]["covered_by_other_valid_straight_masks_m2"],
            "remaining_invalid_public_records":sum(r["otherwise_eligible_attribute_screen"] and not r["default_valid"] for r in screen["records"]),
            "unresolved_other_exclusion_records":need["unresolved_other_masks"],"independent_source_winding_witnesses_with_county":witnesses,
            "all_original_and_prior_audit_preservation_checks_passed":True,"prior_audit_databases_checked":len(preserve["prior_audits"]),
            "previous_accepted_inputs_verified_count":len(inputs),"evidence":evidence,"census_county_archive_sha256":digest_file(county_zip),
            "exclusions_unchanged":True,"new_repairs_performed":False,"repair_accepted":False,"national_processing_restarted":False,
            "interpretation":"The small raw repair-area discrepancy and public/private overlap are different quantities. Exact private boundaries remain needed at demonstrated conflicts unless a conservative exclusion/uncertainty policy is adopted. No sub-acre blanket tolerance is inferred from the user's request to inspect the impact.",
            "recommendation":"Retain this as a relevant private exclusion; defer whole-record precision work. A bounded local conservative exclusion approach could be considered before any further repair. Do not count contested private/public overlaps as eligible or silently skip invalid neighbors.",
            "limits":"These are overlap-screening results from unaccepted Glades representations. The valid-public subset is incomplete, so its area is not a certified total, formal lower bound, or county percentage. Identical normalized overlap geometry across three Glades variants and independent directed source-ring point checks corroborate relevance, not complete repair validity or ownership truth. Other invalid exclusions remain unresolved; point samples do not prove their full coverage. No final conflict-precedence rule, runtime database or county total is adopted."}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in {"evidence","independent_source_winding_witnesses_with_county","unresolved_other_exclusion_records"}}),flush=True)


if __name__=="__main__":
    main()
