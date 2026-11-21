"""Read-only original-source diagnosis for Glades Fee FID 77431."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import numpy as np
from osgeo import gdal
import shapely

from investigate_rings import cycles, signed_area
from investigate_upper_snake import digest_file, require
from repair_geometry import parts


def winding_faces(rings):
    edges = np.concatenate([np.column_stack([np.asarray(r)[:-1,:2],np.asarray(r)[1:,:2]]) for r in rings])
    noded = shapely.node(shapely.MultiLineString(rings))
    faces = list(shapely.polygonize(shapely.get_parts(noded)).geoms)
    rows, positive, odd, negative, zero = [], [], [], [], []
    for i, face in enumerate(faces):
        point=face.representative_point()
        x0,y0,x1,y1=edges.T
        left=(x1-x0)*(point.y-y0)-(point.x-x0)*(y1-y0)
        ray=-(int(np.count_nonzero((y0<=point.y)&(y1>point.y)&(left>0)))
              -int(np.count_nonzero((y1<=point.y)&(y0>point.y)&(left<0))))
        ax,ay=x0-point.x,y0-point.y
        bx,by=x1-point.x,y1-point.y
        rotation=-math.fsum(np.arctan2(ax*by-ay*bx,ax*bx+ay*by).tolist())/(2*math.pi)
        require(abs(rotation-round(rotation))<1e-7 and round(rotation)==ray,"Independent winding classifications disagree")
        rows.append({"face":i,"winding":ray,"area_m2":face.area,"bounds":list(face.bounds)})
        if ray>0:
            positive.append(face)
        elif ray<0:
            negative.append(face)
        else:
            zero.append(face)
        if ray%2:
            odd.append(face)
    return noded,rows,shapely.union_all(positive),shapely.union_all(odd),shapely.union_all(negative),shapely.union_all(zero)


def main():
    home=Path(__file__).resolve().parents[1]
    out=home/"working/glades-investigation-2026-10-21"
    destination=home/"verification/glades-diagnosis.json"
    require(not out.exists() and not destination.exists(),"Refuse to overwrite Glades evidence")
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    source_hash=digest_file(archive)
    require(source_hash=="62ed113335e359509ad68a731146c74aa873c5a92da4f3fda572e54bc9b823ab","Original archive changed")
    audit=home/"working/national-repair-nj-v2-2026-10-21/repair_audit.sqlite3"
    audit_hash=digest_file(audit)
    require(audit_hash=="eb3420e454e866f7ebc5280f6687446b9013cd76c29dd007f614eed8f80bfeac","Previous audit changed")
    with sqlite3.connect(audit.resolve().as_uri()+"?mode=ro",uri=True) as connection:
        connection.row_factory=sqlite3.Row
        row=dict(connection.execute("SELECT * FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=77431").fetchone())
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    feature=ds.GetLayerByName("PADUS4_1Fee").GetFeature(77431)
    native=feature.GetGeometryRef()
    native_body=bytes(native.ExportToWkb())
    body=bytes(native.GetLinearGeometry().ExportToWkb())
    require(hashlib.sha256(body).hexdigest()==row["original_sha256"],"Original default geometry differs from audit")
    attrs=json.loads(row["attributes_json"])
    require(all((None if feature.GetField(k) is None else str(feature.GetField(k)))==v for k,v in attrs.items()),"Source attributes differ")
    original=shapely.from_wkb(body)
    candidate=shapely.from_wkb(row["polygon_wkb"])
    full=shapely.from_wkb(row["repaired_wkb"])
    rings=[list(r.coords) for p in original.geoms for r in [p.exterior,*p.interiors]]
    decomposed=[c for r in rings for c in cycles(r)]
    invalid=[{"cycle":i,"signed_area_m2":signed_area(c),"reason":shapely.is_valid_reason(shapely.Polygon(c)),
              "vertices":len(c),"bounds":list(shapely.Polygon(c).bounds)} for i,c in enumerate(decomposed) if not shapely.Polygon(c).is_valid]
    arcs=[]
    component_counts={}
    def visit(g,path):
        kind=g.GetGeometryName()
        component_counts[kind]=component_counts.get(kind,0)+1
        if kind=="CIRCULARSTRING":
            arcs.append({"path":path,"points":g.GetPointCount(),"controls":g.GetPoints()})
        for i in range(g.GetGeometryCount()):
            visit(g.GetGeometryRef(i),path+[i])
    visit(native,[])
    noded,faces,positive,odd,negative,zero=winding_faces(rings)
    source_signed=-math.fsum(signed_area(r) for r in rings)
    weighted=math.fsum(f["winding"]*f["area_m2"] for f in faces)
    face_counts={str(w):sum(f["winding"]==w for f in faces) for w in sorted({f["winding"] for f in faces})}
    polygons,lines,points=parts(full)
    line_union=shapely.union_all(lines)
    promoted=math.fsum(signed_area(list(p.exterior.coords)) for p in original.geoms if signed_area(list(p.exterior.coords))>0)
    wrong_inner=math.fsum(abs(signed_area(list(r.coords))) for p in original.geoms for r in p.interiors if signed_area(list(r.coords))<0)
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"decision":"diagnosis_only_not_accepted","credit":"Margaret",
            "source_layer":"PADUS4_1Fee","source_fid":77431,"source_attributes":attrs,
            "source_archive_sha256":source_hash,"previous_audit_sha256":audit_hash,
            "original_default_wkb_sha256":row["original_sha256"],"native_wkb_sha256":hashlib.sha256(native_body).hexdigest(),
            "candidate_full_wkb_sha256":row["repaired_sha256"],"candidate_polygon_wkb_sha256":row["polygon_sha256"],
            "direct_original_geometry_and_attributes_match":True,"actual_nonlinear_segments":bool(native.HasCurveGeometry(1)),
            "native_component_counts":component_counts,"native_circular_strings":arcs,
            "decoded_polygon_parts":len(original.geoms),"source_rings":len(rings),"exact_repeated_vertex_cycles":len(decomposed),
            "invalid_source_cycles":invalid,"zero_signed_area_cycle_count":sum(signed_area(c)==0 for c in decomposed),
            "clockwise_cycle_count":sum(signed_area(c)<0 for c in decomposed),"counterclockwise_cycle_count":sum(signed_area(c)>0 for c in decomposed),
            "source_face_counts_by_winding":face_counts,"independent_ray_and_angle_winding_agree":True,
            "signed_source_area_m2":source_signed,"weighted_face_area_m2":weighted,"weighted_minus_signed_source_m2":weighted-source_signed,
            "default_positive_winding_area_m2":positive.area,"negative_winding_area_m2":negative.area,
            "default_candidate_vs_positive_symmetric_difference_m2":candidate.symmetric_difference(positive).area,
            "default_candidate_vs_odd_symmetric_difference_m2":candidate.symmetric_difference(odd).area,
            "candidate_area_outside_default_positive_m2":candidate.difference(positive).area,
            "positive_area_absent_from_candidate_m2":positive.difference(candidate).area,
            "candidate_boundary_absent_from_noded_source_m":candidate.boundary.difference(noded).length,
            "candidate_lines_absent_from_noded_source_m":line_union.difference(noded).length,
            "noded_source_absent_from_candidate_boundary_and_lines_m":noded.difference(shapely.union_all([candidate.boundary,line_union])).length,
            "candidate_polygon_parts":len(polygons),"candidate_line_parts":len(lines),"candidate_point_parts":len(points),
            "decoded_area_m2":original.area,"candidate_area_m2":candidate.area,"publisher_shape_area_m2":float(feature.GetField("SHAPE_Area")),
            "promoted_exterior_slot_area_m2":promoted,"wrong_clockwise_interior_area_m2":wrong_inner,
            "decoded_minus_candidate_m2":original.area-candidate.area,
            "decoded_minus_signed_minus_slot_explanation_m2":original.area-source_signed-2*(promoted-wrong_inner),
            "ngo_ownership_remains_excluded":attrs["Own_Type"]=="NGO","repairs_performed":False,"source_faces":faces,"artifacts":{}}
    out.mkdir(parents=True)
    for name,data in [("original-native.wkb",native_body),("original-native.wkt",native.ExportToIsoWkt().encode("utf-8")),
                       ("default-positive-winding-region.wkb",shapely.to_wkb(positive,byte_order=1)),
                       ("default-odd-winding-region.wkb",shapely.to_wkb(odd,byte_order=1)),
                       ("default-negative-winding-region.wkb",shapely.to_wkb(negative,byte_order=1)),
                       ("default-noded-linework.wkb",shapely.to_wkb(noded,byte_order=1))]:
        (out/name).write_bytes(data)
        report["artifacts"][name]={"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}
    require(digest_file(archive)==source_hash and digest_file(audit)==audit_hash,"Preserved input changed")
    report["original_archive_and_previous_audit_unchanged"]=True
    destination.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in {"source_faces","native_circular_strings","artifacts"}}),flush=True)
    print(json.dumps({"native_circular_string_count":len(arcs),"native_arc_control_count":sum(a["points"] for a in arcs)}),flush=True)


if __name__=="__main__":
    main()
