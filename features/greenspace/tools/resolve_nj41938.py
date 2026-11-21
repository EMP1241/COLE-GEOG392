"""Accept a specifically bounded native-curve representation; NGO stays excluded."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from osgeo import gdal
import shapely

from investigate_upper_snake import digest_file, require
from repair_geometry import parts


def main():
    home=Path(__file__).resolve().parents[1]
    dest=home/"verification/nj41938-resolution.json"
    out=home/"working/nj41938-resolution-2026-10-21"
    require(not dest.exists() and not out.exists(),"Refuse to overwrite resolution")
    receipt_paths=[home/"verification"/name for name in ["nj41938-diagnosis.json","nj41938-native-diagnosis.json",
                   "nj41938-curve-diagnosis.json","nj41938-native-region-check.json"]]
    diagnosis,native_diagnosis,curves,check=[json.loads(p.read_text(encoding="utf-8")) for p in receipt_paths]
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    audit=home/"working/national-repair-upper-snake-2026-10-21/repair_audit.sqlite3"
    require(digest_file(archive)==diagnosis["source_archive_sha256"] and digest_file(audit)==diagnosis["previous_audit_sha256"],"Preserved source or audit changed")
    with sqlite3.connect(audit.resolve().as_uri()+"?mode=ro",uri=True) as connection:
        connection.row_factory=sqlite3.Row
        row=connection.execute("SELECT * FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=41938").fetchone()
        old=shapely.from_wkb(row["repaired_wkb"])
        old_polygon=shapely.from_wkb(row["polygon_wkb"])
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    feature=ds.GetLayerByName("PADUS4_1Fee").GetFeature(41938)
    native=feature.GetGeometryRef()
    require(hashlib.sha256(bytes(native.GetLinearGeometry().ExportToWkb())).hexdigest()==row["original_sha256"],"Original default linearization differs")
    require(hashlib.sha256(bytes(native.ExportToWkb())).hexdigest()==native_diagnosis["native_artifacts"]["original-native-geometry.wkb"]["sha256"],"Native curved source differs")
    attributes=json.loads(row["attributes_json"])
    require(attributes==diagnosis["source_attributes"] and all((None if feature.GetField(k) is None else str(feature.GetField(k)))==v for k,v in attributes.items()),"Source attributes differ")
    require(attributes["Own_Type"]=="NGO" and attributes["Own_Name"]=="NGO","Ownership exclusion changed")
    replacement=shapely.from_wkb((home/"working/nj41938-curves-2026-10-21/0.01-degrees-positive-region.wkb").read_bytes())
    independent=shapely.from_wkb((home/"working/nj41938-native-region-2026-10-21/independently-sampled-positive-region.wkb").read_bytes())
    fine_source=shapely.from_wkb((home/"working/nj41938-curves-2026-10-21/0.01-degrees-source.wkb").read_bytes())
    fine_noded=shapely.from_wkb((home/"working/nj41938-curves-2026-10-21/0.01-degrees-noded-linework.wkb").read_bytes())
    zero=shapely.from_wkb((home/"working/nj41938-curves-2026-10-21/0.01-degrees-zero-winding-region.wkb").read_bytes())
    default_positive=shapely.from_wkb((home/"working/nj41938-investigation-2026-10-21/source-positive-winding-region.wkb").read_bytes())
    budget=check["diagnostic_area_budget_m2"]
    require(budget==1e-5 and check["total_curve_area_envelope_bound_m2"]<budget,"Unresolved curve error budget")
    require(replacement.is_valid and independent.is_valid and replacement.geom_type=="MultiPolygon" and len(replacement.geoms)==2,"Invalid replacement")
    difference=replacement.symmetric_difference(independent).area
    require(difference==check["gdal_fine_vs_independently_sampled_symmetric_difference_m2"] and difference<budget,"Independent native curve region differs")
    require(abs(replacement.area-check["analytic_native_positive_area_m2"])<budget,"Analytic native area differs")
    require(replacement.boundary.difference(fine_noded).length==0,"Boundary absent from fine native-source linework")
    require(replacement.intersection(zero).area==0,"Replacement fills a fine-source hole")
    require(check["source_winding_face_counts"]=={"1":2,"0":5,"2":3},"Source winding topology differs")
    # Explain the large invalid-decoded diagnostic independently of publisher area.
    explained=diagnosis["twice_promoted_area_m2"]+2*check["redundant_source_area_m2"]
    require(abs(diagnosis["decoded_minus_candidate_area_m2"]-explained)<1e-5,"Original decoded discrepancy not fully explained")
    _,old_lines,old_points=parts(old)
    require(len(old_lines)==3 and not old_points,"Unexpected collapsed components")
    require(all(line.difference(fine_noded).length==0 for line in old_lines),"Saved collapsed line absent from source")
    full=shapely.GeometryCollection([replacement,*old_lines])
    require(full.is_valid and full.area==replacement.area,"Full replacement differs in area/validity")
    publisher=float(feature.GetField("SHAPE_Area"))
    require(abs(replacement.area-publisher)<1,"Publisher corroboration exceeds existing numeric floor")
    artifacts={}
    input_artifacts={}
    input_locations=[(diagnosis,home/"working/nj41938-investigation-2026-10-21","artifacts"),
                     (native_diagnosis,home/"working/nj41938-native-2026-10-21","native_artifacts"),
                     (curves,home/"working/nj41938-curves-2026-10-21","artifacts"),
                     (check,home/"working/nj41938-native-region-2026-10-21","artifacts")]
    for receipt,folder,key in input_locations:
        for name,expected in receipt[key].items():
            path=folder/name
            require(digest_file(path)==expected["sha256"] and path.stat().st_size==expected["bytes"],"Investigation artifact changed")
            input_artifacts[path.relative_to(home).as_posix()]=expected
    for path in receipt_paths:
        input_artifacts[path.relative_to(home).as_posix()]={"bytes":path.stat().st_size,"sha256":digest_file(path)}
    out.mkdir(parents=True)
    for name,geometry in [("accepted-polygon-replacement.wkb",replacement),("accepted-full-replacement.wkb",full)]:
        data=shapely.to_wkb(geometry,byte_order=1)
        (out/name).write_bytes(data)
        artifacts[name]={"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}
    require(digest_file(archive)==diagnosis["source_archive_sha256"] and digest_file(audit)==diagnosis["previous_audit_sha256"],"Preserved input changed")
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"local_date":"2026-10-21",
            "decision":"accepted_specific_bounded_native_curve_replacement_previous_candidate_rejected",
            "credit":"Margaret","source_layer":"PADUS4_1Fee","source_fid":41938,"source_attributes":attributes,
            "source_archive_sha256":diagnosis["source_archive_sha256"],"previous_audit_sha256":diagnosis["previous_audit_sha256"],
            "source_original_default_linear_wkb_sha256":row["original_sha256"],
            "source_native_wkb_sha256":native_diagnosis["native_artifacts"]["original-native-geometry.wkb"]["sha256"],
            "previous_full_candidate_wkb_sha256":row["repaired_sha256"],"previous_polygon_candidate_wkb_sha256":row["polygon_sha256"],
            "replacement_valid":True,"replacement_polygon_parts":2,"retained_collapsed_line_parts":3,
            "retained_collapsed_line_wkb_sha256":[hashlib.sha256(shapely.to_wkb(g,byte_order=1)).hexdigest() for g in old_lines],
            "source_default_redundant_shell_area_m2":check["redundant_source_area_m2"],
            "source_default_promoted_hole_area_m2":diagnosis["exterior_slot_counterclockwise_area_m2"],
            "unexplained_original_decoded_difference_m2":diagnosis["decoded_minus_candidate_area_m2"]-explained,
            "source_native_circular_arcs":2,"gdal_maximum_curve_angle_degrees":0.01,
            "independent_analytic_arc_sampling_degrees":0.001,
            "independent_native_region_symmetric_difference_m2":difference,
            "analytic_native_positive_area_m2":check["analytic_native_positive_area_m2"],
            "replacement_minus_analytic_native_area_m2":replacement.area-check["analytic_native_positive_area_m2"],
            "curve_area_envelope_bound_m2":check["total_curve_area_envelope_bound_m2"],
            "maximum_curve_boundary_envelope_m":max(b["boundary_distance_bound_m"] for b in check["gdal_fine_curve_envelope_bounds"]),
            "diagnostic_area_budget_m2":budget,"approximation_not_exact_native_arc_geometry":True,
            "replacement_boundary_absent_from_fine_source_linework_m":0.0,"zero_winding_area_filled_m2":0.0,
            "candidate_area_added_m2":replacement.difference(old_polygon).area,
            "candidate_area_removed_m2":old_polygon.difference(replacement).area,
            "candidate_symmetric_difference_m2":replacement.symmetric_difference(old_polygon).area,
            "replacement_area_m2":replacement.area,"publisher_shape_area_m2":publisher,
            "replacement_minus_publisher_area_m2":replacement.area-publisher,
            "original_archive_and_previous_audit_unchanged":True,
            "ownership_eligible_for_greenspace":False,"ownership_exclusion_reason":"NGO/private ownership remains excluded even when Pub_Access=OA",
            "exclusion_mask_role_decided":False,"input_artifacts":input_artifacts,"accepted_artifacts":artifacts,
            "scope":"Exact PADUS4_1Fee FID 41938 native/default-source/attribute identities only; acceptance of geometry representation does not confer eligibility or choose conflict-mask precedence. National processing is not restarted.",
            "limits":"Bounded linear representation of two native arcs, not exact curved geometry or surveyed truth. Native WKB/WKT preserved. No snapping/buffer/simplification or make_valid is used for the replacement. All private/military/restricted/closed/unconfirmed exclusions and Margaret credit remain intact.",
            "documentation":["https://doc.esri.com/en/arcgis-pro/latest/arcpy/get-started/reading-geometries.html",
                             "https://gdal.org/en/stable/doxygen/classOGRGeometry.html",
                             "https://shapely.readthedocs.io/en/2.1.2/reference/shapely.make_valid.html"]}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in {"input_artifacts","source_attributes","accepted_artifacts"}}),flush=True)


if __name__=="__main__":
    main()
