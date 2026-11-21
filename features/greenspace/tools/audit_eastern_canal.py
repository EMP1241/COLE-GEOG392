"""Read-only source, exact-geometry, county and preservation acceptance audit."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from osgeo import gdal
import pyogrio
import shapely

from accepted_adjudications import verify_inputs as verify_previous
from eastern_canal_geometry import reconstruct, exact_area
from investigate_glades import winding_faces
from investigate_upper_snake import digest_file, require
from local_exclusions import verify_inputs as verify_local
from repair_geometry import source_tree


def main():
    home=Path(__file__).resolve().parents[1]
    dest=home/"verification/eastern-canal-resolution-audit.json"
    require(not dest.exists(),"Refuse to overwrite acceptance audit")
    receipt_path=home/"verification/eastern-canal-resolution.json"
    receipt_hash=digest_file(receipt_path)
    receipt=json.loads(receipt_path.read_text(encoding="utf-8"))
    for relative,item in receipt["input_artifacts"].items():
        require((home/relative).resolve().is_relative_to(home.resolve()),"Input escapes feature")
        require(digest_file(home/relative)==item["sha256"] and (home/relative).stat().st_size==item["bytes"],"Investigation input changed")
    out=home/"working/eastern-canal-resolution-2026-10-22"
    for name,item in receipt["accepted_artifacts"].items():
        require(digest_file(out/name)==item["sha256"] and (out/name).stat().st_size==item["bytes"],"Accepted artifact changed")
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    require(digest_file(archive)==receipt["source_archive_sha256"],"Original archive changed")
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    feature=ds.GetLayerByName("PADUS4_1Fee").GetFeature(88623)
    native=feature.GetGeometryRef()
    require(not native.HasCurveGeometry(1),"Unexpected original native arcs")
    native_body=bytes(native.ExportToWkb())
    body=bytes(native.GetLinearGeometry().ExportToWkb())
    require(hashlib.sha256(native_body).hexdigest()==receipt["source_native_wkb_sha256"],"Original native source differs")
    require(hashlib.sha256(body).hexdigest()==receipt["source_original_wkb_sha256"],"Original decoded source differs")
    require(all((None if feature.GetField(k) is None else str(feature.GetField(k)))==v for k,v in receipt["source_attributes"].items()),"Original attributes differ")
    source=shapely.from_wkb(body)
    reconstructed,lobe,exact_proof=reconstruct(source)
    require(json.loads(json.dumps(exact_proof,allow_nan=False))==receipt["independent_exact_segment_proof"],"Fresh exact arithmetic proof differs")
    accepted=shapely.from_wkb((out/"accepted-source-supported-replacement.wkb").read_bytes())
    require(shapely.to_wkb(reconstructed,byte_order=1)==shapely.to_wkb(accepted,byte_order=1),"Exact reconstruction does not reproduce accepted bytes")
    rings=[list(r.coords) for p in source.geoms for r in [p.exterior,*p.interiors]]
    noded,faces,positive,odd,negative,zero=winding_faces(rings)
    require(accepted.symmetric_difference(positive).is_empty,"Fresh ray/angle source coverage differs")
    require(lobe.symmetric_difference(negative).is_empty and accepted.intersection(zero).area==0,"Source hole/excluded lobe roles differ")
    require(accepted.boundary.difference(noded).length==0,"Output boundary absent from original source linework")
    audit=home/"working/national-prep-local-exclusions-2026-10-22/repair_audit.sqlite3"
    require(digest_file(audit)==receipt["previous_audit_sha256"],"Stopped audit changed")
    with sqlite3.connect(audit.resolve().as_uri()+"?mode=ro",uri=True) as connection:
        row=connection.execute("SELECT repaired_wkb,repaired_sha256 FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=88623").fetchone()
        require(row[1]==receipt["previous_candidate_wkb_sha256"],"Previous candidate identity differs")
        candidate=shapely.from_wkb(row[0])
    require(candidate.symmetric_difference(odd).is_empty,"Old candidate not reproduced by source parity")
    require(accepted.difference(candidate).is_empty,"Accepted replacement adds unsupported area")
    require(candidate.difference(accepted).symmetric_difference(lobe).is_empty,"Removed area is not exactly the unsupported lobe")
    require(candidate.symmetric_difference(accepted).area==receipt["candidate_symmetric_difference_m2"],"Candidate comparison differs")
    county_zip=home/"source/census-2021/tl_2021_us_county.zip"
    require(digest_file(county_zip)=="26890ff92275ff1392cd205c7021ba510b7f63f2ff985ca327b9f5fd5e9ecc90","Census source changed")
    counties=pyogrio.read_dataframe(county_zip,columns=["GEOID","NAME","ALAND"]).to_crs("ESRI:102039")
    maricopa=counties.loc[counties["GEOID"]=="04013"].iloc[0]
    require(accepted.difference(maricopa.geometry).is_empty and lobe.difference(maricopa.geometry).is_empty,"Coverage extends outside reviewed county")
    clipped_before=candidate.intersection(maricopa.geometry)
    clipped_after=accepted.intersection(maricopa.geometry)
    clipped_change=clipped_before.symmetric_difference(clipped_after).area
    clipped_geometry=clipped_before.symmetric_difference(clipped_after)
    require(clipped_geometry.symmetric_difference(lobe).is_empty,"County-clipped change is not the excluded lobe")
    def exact_polygon_area(g):
        return sum(abs(exact_area(list(p.exterior.coords)))-sum(abs(exact_area(list(r.coords))) for r in p.interiors)
                   for p in shapely.get_parts(g) if p.geom_type=="Polygon")
    require(exact_polygon_area(clipped_geometry)==exact_polygon_area(lobe),"Exact county-clipped area differs")
    radius=receipt["county_boundary_guard_m"]
    require(radius==0.001 and exact_proof["intersection_rounding_l1_distance_upper_bound_m"]<radius,"Numerical guard differs")
    numerical=2*radius*(shapely.MultiLineString(rings).length+1)+len(rings)*math.pi*radius**2
    publisher_extra=abs(accepted.area-float(feature.GetField("SHAPE_Area")))
    total=numerical+lobe.area+publisher_extra
    require(total==receipt["total_county_effect_allowance_m2"] and total<4046.8564224,"Total county allowance differs")
    county=receipt["county_bounds"][0]
    denominator=min(float(maricopa["ALAND"]),maricopa.geometry.area)
    require(len(receipt["county_bounds"])==1 and county["county_fips"]=="04013" and county["sensitivity_denominator_m2"]==denominator,"County scope/denominator differs")
    journal=[json.loads(line) for line in (audit.parent/"repairs.jsonl").read_text(encoding="utf-8").splitlines()]
    others=math.fsum(a["allocated_review_allowance_m2"] for log in journal
                    if (log["layer"],log["source_fid"])!=("PADUS4_1Fee",88623)
                    for a in log.get("county_review_allocations",[]) if a["county_fips"]=="04013")
    require(others==county["other_checkpoint_review_allowance_m2"] and total+others==county["prospective_cumulative_allowance_m2"],"Prospective county cumulative accounting differs")
    require(total+others<4046.8564224 and (total+others)/denominator*100<0.01,"Prospective county effect material")
    original_tree=json.loads((audit.parent/"source-extraction-before.json").read_text(encoding="utf-8"))
    require(source_tree(home/"working/padus-4.1/PADUS4_1Geodatabase.gdb")==original_tree,"Original extraction changed")
    prior=json.loads((home/"verification/glades-investigation-outcome.json").read_text(encoding="utf-8"))
    preserved=[]
    for item in prior["prior_audits"]+[{"run":"national-prep-local-exclusions-2026-10-22","sha256":receipt["previous_audit_sha256"]}]:
        path=home/"working"/item["run"]/"repair_audit.sqlite3"
        require(digest_file(path)==item["sha256"],"Earlier audit changed")
        preserved.append(item)
    previous_inputs=verify_previous(home)
    local_inputs=verify_local(home)
    require(digest_file(archive)==receipt["source_archive_sha256"] and digest_file(audit)==receipt["previous_audit_sha256"] and digest_file(receipt_path)==receipt_hash,"Preserved source/receipt changed during audit")
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"local_date":"2026-10-22","status":"passed","discrepancies":[],"credit":"Margaret",
            "acceptance_receipt_sha256":receipt_hash,"direct_original_zip_source_and_attributes_verified":True,
            "fresh_exact_rational_reconstruction_matches_accepted_bytes":True,"fresh_independent_ray_and_angle_coverage_verified":True,
            "county_clipped_candidate_change_m2":clipped_change,"county_effect_allowance_m2":total,"county_effect_allowance_acres":total/4046.8564224,
            "county_clipped_change_exactly_matches_lobe":True,
            "clipped_vs_unclipped_float_area_residual_m2":clipped_change-receipt["candidate_symmetric_difference_m2"],
            "prospective_cumulative_maricopa_allowance_m2":total+others,"maximum_county_percentage_point_effect":total/denominator*100,
            "original_archive_extraction_and_census_unchanged":True,"preserved_prior_audits":preserved,
            "previous_accepted_input_hashes_verified":len(previous_inputs),"glades_local_input_hashes_verified":len(local_inputs),
            "repairs_performed_in_audit":False,"national_processing_restarted":False,
            "accepted_replacement_sha256":receipt["accepted_artifacts"]["accepted-source-supported-replacement.wkb"]["sha256"]}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps(report),flush=True)


if __name__=="__main__":
    main()
