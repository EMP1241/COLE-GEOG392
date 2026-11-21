"""Bound Glades native curves and compare independent source reconstructions."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from osgeo import gdal
import shapely

from check_nj41938_native_region import sample_ring
from investigate_glades import winding_faces
from investigate_nj41938_curves import arc_parameters, native_ring_area, angle_minus_sine
from investigate_rings import signed_area
from investigate_upper_snake import digest_file, require


def analyse(rings):
    noded, faces, positive, odd, negative, zero = winding_faces(rings)
    require(negative.is_empty, "Negative source winding requires review")
    weighted = math.fsum(f["winding"] * f["area_m2"] for f in faces)
    signed = -math.fsum(signed_area(r) for r in rings)
    require(abs(weighted-signed)<1e-6, "Source integral and face winding disagree")
    return positive, {"area_m2":positive.area,"weighted_face_area_m2":weighted,
                      "signed_source_area_m2":signed,"weighted_minus_signed_m2":weighted-signed,
                      "duplicate_coverage_area_m2":math.fsum((f["winding"]-1)*f["area_m2"] for f in faces if f["winding"]>1),
                      "face_counts":{str(w):sum(f["winding"]==w for f in faces) for w in sorted({f["winding"] for f in faces})},
                      "valid":bool(positive.is_valid),"negative_area_m2":negative.area,
                      "vertex_count":sum(len(r) for r in rings)}


def main():
    home=Path(__file__).resolve().parents[1]
    out=home/"working/glades-curves-2026-10-21"
    dest=home/"verification/glades-curve-check.json"
    require(not out.exists() and not dest.exists(),"Refuse to overwrite evidence")
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    audit=home/"working/national-repair-nj-v2-2026-10-21/repair_audit.sqlite3"
    archive_hash=digest_file(archive)
    audit_hash=digest_file(audit)
    require(archive_hash=="62ed113335e359509ad68a731146c74aa873c5a92da4f3fda572e54bc9b823ab","Archive changed")
    require(audit_hash=="eb3420e454e866f7ebc5280f6687446b9013cd76c29dd007f614eed8f80bfeac","Audit changed")
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    feature=ds.GetLayerByName("PADUS4_1Fee").GetFeature(77431)
    native=feature.GetGeometryRef()
    require(hashlib.sha256(bytes(native.ExportToWkb())).hexdigest()=="6170e1b13d5f971cb88cec3c8a076000a69ea84812c1bae7213483f0a6eb9ce2","Native source changed")
    require(feature.GetField("Own_Type")=="NGO","Ownership changed")
    native_rings=[]
    arcs=[]
    for i in range(native.GetGeometryCount()):
        p=native.GetGeometryRef(i)
        for j in range(p.GetGeometryCount()):
            ring=p.GetGeometryRef(j)
            native_rings.append(ring)
            segments=[ring.GetGeometryRef(k) for k in range(ring.GetGeometryCount())] if ring.GetGeometryName()=="COMPOUNDCURVE" else [ring]
            for k,segment in enumerate(segments):
                if segment.GetGeometryName()=="CIRCULARSTRING":
                    arcs.append((segment,{"path":[i,j,k],**arc_parameters(segment.GetPoints())}))
    require(len(arcs)==53,"Unexpected arc count")
    native_signed=-math.fsum(native_ring_area(r) for r in native_rings)
    print(json.dumps({"arc_count":len(arcs),"native_weighted_integral_m2":native_signed,"maximum_arc_radius_m":max(a[1]["radius_m"] for a in arcs)}),flush=True)
    out.mkdir(parents=True)
    artifacts={}
    def save(name,geometry):
        body=shapely.to_wkb(geometry,byte_order=1)
        (out/name).write_bytes(body)
        artifacts[name]={"bytes":len(body),"sha256":hashlib.sha256(body).hexdigest()}
    variants=[]
    regions=[]
    for angle in [0.1,0.01]:
        source=shapely.from_wkb(bytes(native.GetLinearGeometry(angle).ExportToWkb()))
        rings=[list(r.coords) for p in source.geoms for r in [p.exterior,*p.interiors]]
        region,item=analyse(rings)
        bounds=[]
        for arc,info in arcs:
            points=arc.GetLinearGeometry(angle).GetPoints()
            first=arc.GetPoints()[0]
            center=info["center_offset_from_first_control_m"]
            vectors=[(p[0]-first[0]-center[0],p[1]-first[1]-center[1]) for p in points]
            steps=[math.atan2(u[0]*v[1]-u[1]*v[0],u[0]*v[0]+u[1]*v[1]) for u,v in zip(vectors,vectors[1:])]
            require(all(t*info["subtended_angle_radians"]>0 for t in steps),"Stroke reverses along native arc")
            require(abs(math.fsum(steps)-info["subtended_angle_radians"])<1e-8,"Stroke does not cover original arc")
            residue=max(abs(math.hypot(*v)-info["radius_m"]) for v in vectors)+1e-9
            length=info["radius_m"]*abs(info["subtended_angle_radians"])
            bounds.append({**info,"stroke_segments":len(steps),"maximum_radial_residue_with_margin_m":residue,
                           "boundary_envelope_m":2*info["radius_m"]*math.sin(max(abs(t) for t in steps)/4)**2+residue,
                           "area_envelope_m2":info["radius_squared_m2"]/2*math.fsum(angle_minus_sine(abs(t)) for t in steps)+4*length*residue+4*math.pi*residue**2})
        item.update({"gdal_requested_angle_degrees":angle,"arc_bounds":bounds,
                     "curve_area_envelope_m2":math.fsum(b["area_envelope_m2"] for b in bounds),
                     "maximum_boundary_envelope_m":max(b["boundary_envelope_m"] for b in bounds),
                     "source_integral_minus_native_m2":item["signed_source_area_m2"]-native_signed})
        if regions:
            item["symmetric_difference_from_previous_m2"]=region.symmetric_difference(regions[-1]).area
        variants.append(item)
        regions.append(region)
        save(f"{angle:g}-degrees-positive-region.wkb",region)
        save(f"{angle:g}-degrees-source.wkb",source)
        print(json.dumps({k:v for k,v in item.items() if k!="arc_bounds"}),flush=True)
    independent_rings=[sample_ring(r) for r in native_rings]
    independent,independent_info=analyse(independent_rings)
    independent_info["sampling_maximum_angle_degrees"]=0.001
    independent_info["source_integral_minus_native_m2"]=independent_info["signed_source_area_m2"]-native_signed
    difference=regions[-1].symmetric_difference(independent).area
    save("independent-positive-region.wkb",independent)
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"decision":"diagnostic_only_not_accepted","credit":"Margaret",
            "source_fid":77431,"source_layer":"PADUS4_1Fee","source_archive_sha256":archive_hash,
            "previous_audit_sha256":audit_hash,"native_weighted_integral_m2":native_signed,
            "publisher_shape_area_m2":feature.GetField("SHAPE_Area"),"variants":variants,"independent_native_sampling":independent_info,
            "fine_vs_independent_symmetric_difference_m2":difference,"artifacts":artifacts,
            "ngo_remains_excluded":True,"repairs_performed":False}
    require(digest_file(archive)==archive_hash and digest_file(audit)==audit_hash,"Preserved inputs changed")
    report["original_archive_and_previous_audit_unchanged"]=True
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({"independent":independent_info,"fine_vs_independent_symmetric_difference_m2":difference}),flush=True)


if __name__=="__main__":
    main()
