"""Read-only audit of the specific excluded NJ native-curve representation."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from osgeo import gdal
import shapely

from check_nj41938_native_region import sample_ring
from investigate_nj41938_curves import linear_source_region, native_ring_area, source_region
from investigate_upper_snake import digest_file, require
from repair_geometry import parts, source_tree


def main():
    home=Path(__file__).resolve().parents[1]
    dest=home/"verification/nj41938-resolution-audit.json"
    require(not dest.exists(),"Refuse to replace read-only audit")
    receipt_path=home/"verification/nj41938-resolution.json"
    receipt_hash=digest_file(receipt_path)
    receipt=json.loads(receipt_path.read_text(encoding="utf-8"))
    errors=[]
    def check(condition,message):
        if not condition:
            errors.append(message)
    for relative,expected in receipt["input_artifacts"].items():
        path=home/relative
        check(path.stat().st_size==expected["bytes"] and digest_file(path)==expected["sha256"],f"Investigation artifact changed: {relative}")
    out=home/"working/nj41938-resolution-2026-10-21"
    for name,expected in receipt["accepted_artifacts"].items():
        path=out/name
        check(path.stat().st_size==expected["bytes"] and digest_file(path)==expected["sha256"],f"Accepted artifact changed: {name}")
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    check(digest_file(archive)==receipt["source_archive_sha256"],"Original archive changed")
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    feature=ds.GetLayerByName("PADUS4_1Fee").GetFeature(41938)
    native=feature.GetGeometryRef()
    check(hashlib.sha256(bytes(native.ExportToWkb())).hexdigest()==receipt["source_native_wkb_sha256"],"Native source differs")
    check(hashlib.sha256(bytes(native.GetLinearGeometry().ExportToWkb())).hexdigest()==receipt["source_original_default_linear_wkb_sha256"],"Default source differs")
    check(all((None if feature.GetField(k) is None else str(feature.GetField(k)))==v for k,v in receipt["source_attributes"].items()),"Original attributes differ")
    check(feature.GetField("Own_Type")=="NGO" and receipt["ownership_eligible_for_greenspace"] is False,"NGO exclusion changed")
    polygon=shapely.from_wkb((out/"accepted-polygon-replacement.wkb").read_bytes())
    full=shapely.from_wkb((out/"accepted-full-replacement.wkb").read_bytes())
    fine_source,fine_region,fine_noded,zero,counts=source_region(native,0.01)
    check(polygon.is_valid and full.is_valid and polygon.area==full.area==receipt["replacement_area_m2"],"Replacement validity/area differs")
    check(polygon.symmetric_difference(fine_region).area==0,"Replacement differs from regenerated fine native source region")
    check(polygon.boundary.difference(fine_noded).length==0 and polygon.intersection(zero).area==0,"Fine source boundary/hole roles violated")
    native_polygons=[]
    analytic_signed=[]
    for i in range(native.GetGeometryCount()):
        p=native.GetGeometryRef(i)
        sampled=[sample_ring(p.GetGeometryRef(j)) for j in range(p.GetGeometryCount())]
        native_polygons.append(shapely.Polygon(sampled[0],sampled[1:]))
        analytic_signed.extend(native_ring_area(p.GetGeometryRef(j)) for j in range(p.GetGeometryCount()))
    _,independent_region,_,_,independent_counts=linear_source_region(shapely.MultiPolygon(native_polygons))
    duplicate_area=math.fsum(abs(analytic_signed[i]) for i in [5,6,7])
    analytic_area=-math.fsum(analytic_signed)-duplicate_area
    check(counts==independent_counts=={"1":2,"0":5,"2":3},"Native source topology differs")
    check(all(native_polygons[i].difference(independent_region).area==0 for i in [5,6,7]),"Redundant shells not fully in positive region")
    check(polygon.symmetric_difference(independent_region).area==receipt["independent_native_region_symmetric_difference_m2"],"Independent native region difference changed")
    check(abs(polygon.area-analytic_area)<receipt["diagnostic_area_budget_m2"]==1e-5,"Analytic native area exceeds budget")
    check(receipt["curve_area_envelope_bound_m2"]<1e-5 and receipt["maximum_curve_boundary_envelope_m"]<1e-6,"Recorded curve bounds exceed budget")
    audit=home/"working/national-repair-upper-snake-2026-10-21/repair_audit.sqlite3"
    with sqlite3.connect(audit.resolve().as_uri()+"?mode=ro",uri=True) as connection:
        original_body,old_body,old_polygon_body=connection.execute("SELECT original_wkb,repaired_wkb,polygon_wkb FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=41938").fetchone()
    check(hashlib.sha256(old_body).hexdigest()==receipt["previous_full_candidate_wkb_sha256"],"Earlier full candidate differs")
    check(hashlib.sha256(old_polygon_body).hexdigest()==receipt["previous_polygon_candidate_wkb_sha256"],"Earlier polygon candidate differs")
    old_polygon=shapely.from_wkb(old_polygon_body)
    _,old_lines,_=parts(shapely.from_wkb(old_body))
    polys,new_lines,new_points=parts(full)
    old_hashes=[hashlib.sha256(shapely.to_wkb(g,byte_order=1)).hexdigest() for g in old_lines]
    new_hashes=[hashlib.sha256(shapely.to_wkb(g,byte_order=1)).hexdigest() for g in new_lines]
    check(old_hashes==new_hashes==receipt["retained_collapsed_line_wkb_sha256"] and len(new_lines)==3 and not new_points,"Collapsed source lines changed/lost")
    check(len(polys)==2,"Polygon count changed")
    check(all(g.difference(fine_noded).length==0 for g in new_lines),"Retained line absent from source")
    check(polygon.symmetric_difference(old_polygon).area==receipt["candidate_symmetric_difference_m2"],"Candidate change accounting differs")
    original=shapely.from_wkb(original_body)
    diagnosis=json.loads((home/"verification/nj41938-diagnosis.json").read_text(encoding="utf-8"))
    explained=2*diagnosis["exterior_slot_counterclockwise_area_m2"]+2*duplicate_area
    check(abs(original.area-old_polygon.area-explained)<1e-5,"Large decoded discrepancy not explained")
    expected_tree=json.loads((home/"working/national-repair-upper-snake-2026-10-21/source-extraction-before.json").read_text(encoding="utf-8"))
    check(source_tree(home/"working/padus-4.1/PADUS4_1Geodatabase.gdb")==expected_tree,"Original extraction changed")
    prior={"controlled-repair-2026-10-21":"ca6c54ba464da4e6db7104715b56e549fd4f0f0b54db5c0962b12d0e90e151f9",
           "source-checked-repair-2026-10-21":"c4efe7103c22ac9520c5e180e0c80b1b5759998b99da428f0f6e15ec931ca42e",
           "national-repair-2026-10-21":"6d62bb98a9a01afcdc059cf42392470a32cd41d51758ca7a43168305b0eaf215",
           "national-repair-upper-snake-2026-10-21":receipt["previous_audit_sha256"]}
    for name,expected in prior.items():
        check(digest_file(home/"working"/name/"repair_audit.sqlite3")==expected,f"Earlier audit changed: {name}")
    check(digest_file(archive)==receipt["source_archive_sha256"] and digest_file(receipt_path)==receipt_hash,"Archive/receipt changed during read-only audit")
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"status":"passed" if not errors else "failed","discrepancies":errors,
            "credit":"Margaret","source_layer":"PADUS4_1Fee","source_fid":41938,
            "accepted_polygon_sha256":receipt["accepted_artifacts"]["accepted-polygon-replacement.wkb"]["sha256"],
            "accepted_full_sha256":receipt["accepted_artifacts"]["accepted-full-replacement.wkb"]["sha256"],
            "acceptance_receipt_sha256":receipt_hash,"fine_native_source_region_symmetric_difference_m2":polygon.symmetric_difference(fine_region).area,
            "independent_native_arc_region_symmetric_difference_m2":polygon.symmetric_difference(independent_region).area,
            "analytic_native_area_difference_m2":polygon.area-analytic_area,"collapsed_lines_preserved_byte_identically":old_hashes==new_hashes,
            "ngo_ownership_remains_excluded":True,"original_archive_extraction_and_four_prior_audits_unchanged":not errors,
            "repairs_performed_in_audit":False,"national_processing_restarted":False,"runtime_database_built":False}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps(report),flush=True)
    require(not errors,"NJ record data audit failed")


if __name__=="__main__":
    main()
