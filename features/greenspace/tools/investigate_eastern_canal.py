"""Read-only original-source diagnosis of Eastern Canal Bridge Fee 88623."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from osgeo import gdal
import shapely

from investigate_glades import winding_faces
from investigate_rings import cycles, signed_area
from investigate_upper_snake import digest_file, require


def main():
    home=Path(__file__).resolve().parents[1]
    out=home/"working/eastern-canal-investigation-2026-10-22"
    dest=home/"verification/eastern-canal-diagnosis.json"
    require(not out.exists() and not dest.exists(),"Refuse to overwrite Eastern Canal evidence")
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    audit=home/"working/national-prep-local-exclusions-2026-10-22/repair_audit.sqlite3"
    require(digest_file(archive)=="62ed113335e359509ad68a731146c74aa873c5a92da4f3fda572e54bc9b823ab","Original archive changed")
    audit_hash=digest_file(audit)
    require(audit_hash=="76f8ae2050a030f5e4208dd28ef94cf5e77330f1c84f2095654ee2b0123d8f81","Stopped audit changed")
    with sqlite3.connect(audit.resolve().as_uri()+"?mode=ro",uri=True) as connection:
        connection.row_factory=sqlite3.Row
        row=connection.execute("SELECT * FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=88623").fetchone()
        attributes=json.loads(row["attributes_json"])
        candidate=shapely.from_wkb(row["polygon_wkb"])
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    feature=ds.GetLayerByName("PADUS4_1Fee").GetFeature(88623)
    native=feature.GetGeometryRef()
    native_body=bytes(native.ExportToWkb())
    body=bytes(native.GetLinearGeometry().ExportToWkb())
    require(hashlib.sha256(body).hexdigest()==row["original_sha256"],"Original geometry differs")
    require(all((None if feature.GetField(k) is None else str(feature.GetField(k)))==v for k,v in attributes.items()),"Source attributes differ")
    require(not native.HasCurveGeometry(1),"Native nonlinear geometry requires separate review")
    source=shapely.from_wkb(body)
    rings=[list(r.coords) for p in source.geoms for r in [p.exterior,*p.interiors]]
    simple=[c for r in rings for c in cycles(r)]
    noded,faces,positive,odd,negative,zero=winding_faces(rings)
    signed=-math.fsum(signed_area(r) for r in rings)
    weighted=math.fsum(f["winding"]*f["area_m2"] for f in faces)
    promoted=math.fsum(signed_area(list(p.exterior.coords)) for p in source.geoms if signed_area(list(p.exterior.coords))>0)
    wrong_inner=math.fsum(abs(signed_area(list(r.coords))) for p in source.geoms for r in p.interiors if signed_area(list(r.coords))<0)
    added=positive.difference(candidate)
    removed=candidate.difference(positive)
    delta=positive.symmetric_difference(candidate)
    partition=delta.area-added.area-removed.area
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"local_date":"2026-10-22","decision":"diagnosis_only_not_accepted","credit":"Margaret",
            "source_layer":"PADUS4_1Fee","source_fid":88623,"source_attributes":attributes,
            "source_archive_sha256":digest_file(archive),"previous_audit_sha256":audit_hash,
            "source_native_wkb_sha256":hashlib.sha256(native_body).hexdigest(),"source_original_wkb_sha256":row["original_sha256"],
            "previous_candidate_wkb_sha256":row["repaired_sha256"],"candidate_polygon_wkb_sha256":row["polygon_sha256"],
            "direct_original_geometry_and_attributes_match":True,"actual_nonlinear_segments":False,"source_polygon_parts":len(source.geoms),
            "source_rings":len(rings),"source_coordinates":sum(len(r) for r in rings),"exact_repeat_vertex_cycles":len(simple),
            "invalid_source_cycles":[{"cycle":i,"signed_area_m2":signed_area(c),"reason":shapely.is_valid_reason(shapely.Polygon(c))} for i,c in enumerate(simple) if not shapely.Polygon(c).is_valid],
            "source_face_counts_by_winding":{str(w):sum(f["winding"]==w for f in faces) for w in sorted({f["winding"] for f in faces})},
            "source_signed_area_m2":signed,"weighted_face_area_m2":weighted,"weighted_minus_signed_m2":weighted-signed,
            "source_linework_length_m":shapely.MultiLineString(rings).length,
            "promoted_exterior_slot_area_m2":promoted,"wrong_clockwise_interior_area_m2":wrong_inner,
            "decoded_area_m2":source.area,"candidate_area_m2":candidate.area,"positive_source_area_m2":positive.area,
            "positive_valid":bool(positive.is_valid),"positive_parts":len(list(shapely.get_parts(positive))),
            "negative_source_area_m2":negative.area,"zero_winding_area_m2":zero.area,
            "candidate_vs_odd_symmetric_difference_m2":candidate.symmetric_difference(odd).area,
            "positive_minus_candidate_m2":added.area,"candidate_minus_positive_m2":removed.area,"positive_vs_candidate_symmetric_difference_m2":delta.area,
            "overlay_partition_residual_m2":partition,"area_difference_minus_directional_difference_m2":positive.area-candidate.area-added.area+removed.area,
            "positive_boundary_absent_from_noded_source_m":positive.boundary.difference(noded).length,
            "candidate_boundary_absent_from_noded_source_m":candidate.boundary.difference(noded).length,
            "source_slot_area_accounting_residual_m2":source.area-signed-2*(promoted-wrong_inner),
            "publisher_shape_area_m2":float(feature.GetField("SHAPE_Area")),"positive_minus_publisher_m2":positive.area-float(feature.GetField("SHAPE_Area")),
            "face_signed_area_accounting_tolerance_m2":1e-5,"source_faces":faces,"artifacts":{},"repairs_accepted":False}
    require(abs(weighted-signed)<1e-5,"Source integral/face accounting disagrees")
    out.mkdir(parents=True)
    for name,data in [("original-native.wkb",native_body),("original-native.wkt",native.ExportToIsoWkt().encode("utf-8")),
                      ("source-positive-region.wkb",shapely.to_wkb(positive,byte_order=1)),("source-odd-region.wkb",shapely.to_wkb(odd,byte_order=1)),
                      ("source-negative-region.wkb",shapely.to_wkb(negative,byte_order=1)),("source-zero-region.wkb",shapely.to_wkb(zero,byte_order=1)),
                      ("source-noded-linework.wkb",shapely.to_wkb(noded,byte_order=1)),("positive-minus-candidate.wkb",shapely.to_wkb(added,byte_order=1))]:
        (out/name).write_bytes(data)
        report["artifacts"][name]={"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}
    require(digest_file(archive)==report["source_archive_sha256"] and digest_file(audit)==audit_hash,"Preserved input changed")
    report["original_archive_and_previous_audit_unchanged"]=True
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in {"source_faces","artifacts"}}),flush=True)


if __name__=="__main__":
    main()
