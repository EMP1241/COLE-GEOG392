"""Accept only source-supported canal coverage with explicit county bounds."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from osgeo import gdal
import pyogrio
import shapely

from eastern_canal_geometry import reconstruct
from investigate_rings import signed_area
from investigate_upper_snake import digest_file, require
from repair_geometry import source_tree

ACRE=4046.8564224
BOUNDARY_GUARD=0.001


def main():
    home=Path(__file__).resolve().parents[1]
    dest=home/"verification/eastern-canal-resolution.json"
    out=home/"working/eastern-canal-resolution-2026-10-22"
    require(not dest.exists() and not out.exists(),"Refuse to overwrite resolution")
    diagnosis_path=home/"verification/eastern-canal-diagnosis.json"
    diagnosis=json.loads(diagnosis_path.read_text(encoding="utf-8"))
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    audit=home/"working/national-prep-local-exclusions-2026-10-22/repair_audit.sqlite3"
    require(digest_file(archive)==diagnosis["source_archive_sha256"] and digest_file(audit)==diagnosis["previous_audit_sha256"],"Preserved source/audit changed")
    with sqlite3.connect(audit.resolve().as_uri()+"?mode=ro",uri=True) as connection:
        connection.row_factory=sqlite3.Row
        row=connection.execute("SELECT * FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=88623").fetchone()
        source=shapely.from_wkb(row["original_wkb"])
        candidate=shapely.from_wkb(row["polygon_wkb"])
        attributes=json.loads(row["attributes_json"])
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    feature=ds.GetLayerByName("PADUS4_1Fee").GetFeature(88623)
    native=feature.GetGeometryRef()
    require(not native.HasCurveGeometry(1),"Nonlinear source boundary unresolved")
    require(hashlib.sha256(bytes(native.GetLinearGeometry().ExportToWkb())).hexdigest()==row["original_sha256"],"Original source geometry differs")
    require(hashlib.sha256(bytes(native.ExportToWkb())).hexdigest()==diagnosis["source_native_wkb_sha256"],"Native source geometry differs")
    require(all((None if feature.GetField(k) is None else str(feature.GetField(k)))==v for k,v in attributes.items()),"Original source attributes differ")
    replacement,lobe,proof=reconstruct(source)
    root=home/"working/eastern-canal-investigation-2026-10-22"
    for name,item in diagnosis["artifacts"].items():
        require(digest_file(root/name)==item["sha256"],"Investigation artifact changed")
    positive=shapely.from_wkb((root/"source-positive-region.wkb").read_bytes())
    negative=shapely.from_wkb((root/"source-negative-region.wkb").read_bytes())
    noded=shapely.from_wkb((root/"source-noded-linework.wkb").read_bytes())
    require(replacement.is_valid and len(replacement.geoms)==10,"Replacement structure invalid")
    require(replacement.symmetric_difference(positive).is_empty,"Exact reconstruction differs from independently classified source coverage")
    require(lobe.symmetric_difference(negative).is_empty and replacement.intersection(lobe).area==0,"Unconfirmed lobe accounting differs")
    require(replacement.difference(candidate).is_empty,"Replacement adds unverified area")
    removed=candidate.difference(replacement)
    require(removed.symmetric_difference(lobe).is_empty,"Change is not exactly the unconfirmed lobe")
    require(replacement.boundary.difference(noded).length==0,"Replacement boundary absent from original source segments")
    require(proof["geos_vs_correctly_rounded_exact_intersection_distance_m"]==0,"Floating intersection differs from exact rational construction")
    require(proof["intersection_rounding_l1_distance_upper_bound_m"]<BOUNDARY_GUARD,"Numerical node error exceeds guard")
    # Reconcile the large invalid-decoded area by exact source role accounting.
    explained=2*diagnosis["promoted_exterior_slot_area_m2"]-2*lobe.area
    require(abs((source.area-candidate.area)-explained)<1e-5,"Raw decoded discrepancy remains unexplained")
    rings=[list(r.coords) for p in source.geoms for r in [p.exterior,*p.interiors]]
    length=shapely.MultiLineString(rings).length
    numerical_tube=2*BOUNDARY_GUARD*(length+1)+len(rings)*math.pi*BOUNDARY_GUARD**2
    publisher=float(feature.GetField("SHAPE_Area"))
    publisher_allowance=abs(replacement.area-publisher)
    # Full tube + omitted unconfirmed lobe + complete publisher discrepancy;
    # includes redundant allowances intentionally, never net cancellation.
    total=numerical_tube+lobe.area+publisher_allowance
    require(total<ACRE,"Replacement cannot be bounded below one acre")
    county_zip=home/"source/census-2021/tl_2021_us_county.zip"
    require(digest_file(county_zip)=="26890ff92275ff1392cd205c7021ba510b7f63f2ff985ca327b9f5fd5e9ecc90","County source changed")
    counties=pyogrio.read_dataframe(county_zip,columns=["GEOID","NAME","ALAND"]).to_crs("ESRI:102039")
    xmin,xmax,ymin,ymax=native.GetEnvelope()
    support=shapely.box(xmin-BOUNDARY_GUARD,ymin-BOUNDARY_GUARD,xmax+BOUNDARY_GUARD,ymax+BOUNDARY_GUARD)
    journal=[json.loads(line) for line in (audit.parent/"repairs.jsonl").read_text(encoding="utf-8").splitlines()]
    county_bounds=[]
    for _,county in counties.loc[counties.intersects(support)].iterrows():
        code=str(county["GEOID"])
        prior_other=math.fsum(a["allocated_review_allowance_m2"] for log in journal
                              if (log["layer"],log["source_fid"])!=("PADUS4_1Fee",88623)
                              for a in log.get("county_review_allocations",[]) if a["county_fips"]==code)
        denominator=min(float(county["ALAND"]),county.geometry.area)
        require(total+prior_other<ACRE and (total+prior_other)/denominator*100<0.01,"County cumulative bound material")
        county_bounds.append({"county_fips":code,"name":str(county["NAME"]),"effect_allowance_m2":total,"effect_allowance_acres":total/ACRE,
                              "sensitivity_denominator_m2":denominator,"maximum_percentage_point_effect":total/denominator*100,
                              "other_checkpoint_review_allowance_m2":prior_other,"prospective_cumulative_allowance_m2":prior_other+total,
                              "prospective_cumulative_allowance_acres":(prior_other+total)/ACRE,
                              "prospective_cumulative_percentage_points":(prior_other+total)/denominator*100})
    require([c["county_fips"] for c in county_bounds]==["04013"],"Unexpected county scope")
    out.mkdir(parents=True)
    artifacts={}
    for name,g in [("accepted-source-supported-replacement.wkb",replacement),("excluded-unconfirmed-lobe.wkb",lobe)]:
        body=shapely.to_wkb(g,byte_order=1)
        (out/name).write_bytes(body)
        artifacts[name]={"bytes":len(body),"sha256":hashlib.sha256(body).hexdigest()}
    original_tree=json.loads((audit.parent/"source-extraction-before.json").read_text(encoding="utf-8"))
    require(source_tree(home/"working/padus-4.1/PADUS4_1Geodatabase.gdb")==original_tree,"Original extraction changed")
    require(digest_file(archive)==diagnosis["source_archive_sha256"] and digest_file(audit)==diagnosis["previous_audit_sha256"],"Originals/audit changed")
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"local_date":"2026-10-22","credit":"Margaret",
            "decision":"accepted_specific_source_supported_replacement_unconfirmed_lobe_excluded_county_effect_bounded",
            "source_layer":"PADUS4_1Fee","source_fid":88623,"source_attributes":attributes,
            "source_archive_sha256":diagnosis["source_archive_sha256"],"source_native_wkb_sha256":diagnosis["source_native_wkb_sha256"],
            "source_original_wkb_sha256":row["original_sha256"],"previous_candidate_wkb_sha256":row["repaired_sha256"],
            "previous_audit_sha256":diagnosis["previous_audit_sha256"],"source_diagnosis_sha256":digest_file(diagnosis_path),
            "independent_exact_segment_proof":proof,"replacement_area_m2":replacement.area,"replacement_polygon_parts":10,
            "candidate_area_added_m2":0.0,"candidate_area_removed_m2":removed.area,"candidate_symmetric_difference_m2":removed.area,
            "replacement_boundary_absent_from_noded_source_m":0.0,"source_positive_region_symmetric_difference_m2":0.0,
            "decoded_difference_explanation_m2":explained,"unexplained_decoded_difference_m2":source.area-candidate.area-explained,
            "county_boundary_guard_m":BOUNDARY_GUARD,"source_linework_length_m":length,"numerical_tube_allowance_m2":numerical_tube,
            "publisher_shape_area_m2":publisher,"additional_publisher_discrepancy_allowance_m2":publisher_allowance,
            "total_county_effect_allowance_m2":total,"total_county_effect_allowance_acres":total/ACRE,"county_bounds":county_bounds,
            "exclusions_unchanged":True,"national_processing_restarted":False,"input_artifacts":{
                diagnosis_path.relative_to(home).as_posix():{"bytes":diagnosis_path.stat().st_size,"sha256":digest_file(diagnosis_path)},
                **{(root/name).relative_to(home).as_posix():item for name,item in diagnosis["artifacts"].items()}},
            "accepted_artifacts":artifacts,"original_archive_extraction_and_stopped_audit_unchanged":True,
            "scope":"Only exact native/default/candidate/attribute identities for Fee 88623. Source-supported area representation; the unsupported negative-winding lobe remains excluded. Future preparation must substitute this reviewed allowance for the stopped raw diagnostic, not add both.",
            "limits":"Bound concerns computed mapped coverage relative to this source and candidate, not surveyed ownership/access or unknown source positional errors. Publisher area discrepancy is retained and added as extra caution; it is not treated as external ground truth or a bound on source accuracy. County clipping/union/subtraction cannot enlarge the area change from this record under consistent set operations. Competing exclusions and final county denominators remain required.",
            "method":"Exact rational source-segment intersection; no original vertex moves; oriented source loops with one contained hole; tiny uncontained counterclockwise lobe excluded. Independent ray/angle planar-face coverage agrees. No buffer, snapping, simplification, or make_valid is used for this replacement.",
            "documentation":["https://doc.esri.com/en/arcgis-pro/latest/arcpy/get-started/reading-geometries.html","https://pro.arcgis.com/en/pro-app/3.6/tool-reference/data-management/repair-geometry.htm"]}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in {"input_artifacts","source_attributes","independent_exact_segment_proof"}}),flush=True)


if __name__=="__main__":
    main()
