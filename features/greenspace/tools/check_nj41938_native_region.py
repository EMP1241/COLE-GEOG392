"""Compare GDAL approximation with independently sampled native circular arcs."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from osgeo import gdal
import shapely

from investigate_nj41938_curves import arc_parameters, linear_source_region
from investigate_upper_snake import digest_file, require


def sample_arc(points,step_degrees=0.001):
    info=arc_parameters(points)
    start=points[0]
    center=info["center_offset_from_first_control_m"]
    vectors=[[p[0]-start[0]-center[0],p[1]-start[1]-center[1]] for p in points]
    result=[]
    for index,(u,v) in enumerate(zip(vectors,vectors[1:])):
        theta=math.atan2(u[0]*v[1]-u[1]*v[0],u[0]*v[0]+u[1]*v[1])
        intervals=math.ceil(abs(theta)/math.radians(step_degrees))
        if not result:
            result.append(tuple(points[index][:2]))
        for k in range(1,intervals):
            angle=theta*k/intervals
            cosine,sine=math.cos(angle),math.sin(angle)
            result.append((start[0]+center[0]+u[0]*cosine-u[1]*sine,
                           start[1]+center[1]+u[0]*sine+u[1]*cosine))
        result.append(tuple(points[index+1][:2]))
    return result


def sample_ring(ring):
    segments=[ring.GetGeometryRef(i) for i in range(ring.GetGeometryCount())] if ring.GetGeometryName()=="COMPOUNDCURVE" else [ring]
    result=[]
    for segment in segments:
        coordinates=segment.GetPoints()
        points=sample_arc(coordinates) if segment.GetGeometryName()=="CIRCULARSTRING" else [tuple(p[:2]) for p in coordinates]
        result.extend(points[1:] if result and points[0]==result[-1] else points)
    return result


def main():
    home=Path(__file__).resolve().parents[1]
    out=home/"working/nj41938-native-region-2026-10-21"
    dest=home/"verification/nj41938-native-region-check.json"
    require(not out.exists() and not dest.exists(),"Refuse to overwrite native region check")
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    original_hash=digest_file(archive)
    require(original_hash=="62ed113335e359509ad68a731146c74aa873c5a92da4f3fda572e54bc9b823ab","Original archive changed")
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    feature=ds.GetLayerByName("PADUS4_1Fee").GetFeature(41938)
    native=feature.GetGeometryRef()
    polygons=[]
    for i in range(native.GetGeometryCount()):
        polygon=native.GetGeometryRef(i)
        rings=[sample_ring(polygon.GetGeometryRef(j)) for j in range(polygon.GetGeometryCount())]
        polygons.append(shapely.Polygon(rings[0],rings[1:]))
    independent_source=shapely.MultiPolygon(polygons)
    _,independent_region,noded,zero,counts=linear_source_region(independent_source)
    fine=shapely.from_wkb((home/"working/nj41938-curves-2026-10-21/0.01-degrees-positive-region.wkb").read_bytes())
    curves=json.loads((home/"verification/nj41938-curve-diagnosis.json").read_text(encoding="utf-8"))
    native_signed=curves["native_signed_ring_areas"]
    # Three tiny clockwise parts repeat area already positively covered by
    # the larger shells. Their native boundaries are straight and unchanged.
    redundant=[5,6,7]
    for i in redundant:
        require(polygons[i].difference(independent_region).area==0,"Redundant shell is not fully in positive source area")
    duplicate_area=math.fsum(abs(native_signed[i]["signed_area_m2"]) for i in redundant)
    analytic_positive=-math.fsum(r["signed_area_m2"] for r in native_signed)-duplicate_area
    curve_variant=next(v for v in curves["variants"] if v["maximum_angle_degrees"]==0.01)
    bounds=[]
    for arc,detail in zip(curves["circular_arcs"],curve_variant["arc_chord_diagnostics"]):
        residue=detail["maximum_vertex_radial_residual_m"]+1e-9
        length=arc["radius_m"]*abs(arc["subtended_angle_radians"])
        area_bound=detail["sum_sector_minus_chord_area_m2"]+4*length*residue+4*math.pi*residue**2
        bounds.append({"arc_path":arc["path"],"area_envelope_bound_m2":area_bound,
                       "boundary_distance_bound_m":detail["maximum_chord_sagitta_m"]+residue})
    total_bound=math.fsum(b["area_envelope_bound_m2"] for b in bounds)
    difference=fine.symmetric_difference(independent_region).area
    # The 1e-5 m² diagnostic budget is smaller than existing 1 m² numerical
    # floor; it does not relax significance or ground-truth accuracy rules.
    budget=1e-5
    require(total_bound<budget and difference<budget and abs(fine.area-analytic_positive)<budget,
            "Native curve region is not resolved within the explicit diagnostic budget")
    require(counts=={"1":2,"0":5,"2":3},"Native-sampled topology differs")
    require(fine.is_valid and independent_region.is_valid,"Invalid native source region")
    out.mkdir(parents=True)
    artifacts={}
    for name,geometry in [("independently-sampled-source.wkb",independent_source),
                          ("independently-sampled-positive-region.wkb",independent_region)]:
        data=shapely.to_wkb(geometry,byte_order=1)
        (out/name).write_bytes(data)
        artifacts[name]={"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}
    require(digest_file(archive)==original_hash,"Original archive changed")
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"decision":"native_region_check_only_not_accepted","credit":"Margaret",
            "source_archive_sha256":original_hash,"analytic_native_positive_area_m2":analytic_positive,
            "redundant_clockwise_source_parts":redundant,"redundant_source_area_m2":duplicate_area,
            "independent_sampling_maximum_angle_degrees":0.001,"independent_region_area_m2":independent_region.area,
            "gdal_fine_region_area_m2":fine.area,"gdal_fine_vs_analytic_area_difference_m2":fine.area-analytic_positive,
            "gdal_fine_vs_independently_sampled_symmetric_difference_m2":difference,
            "gdal_fine_curve_envelope_bounds":bounds,"total_curve_area_envelope_bound_m2":total_bound,
            "diagnostic_area_budget_m2":budget,"source_winding_face_counts":counts,
            "approximation_not_exact_native_arc_geometry":True,"artifacts":artifacts,
            "original_archive_unchanged":True,"repairs_performed":False}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps(report),flush=True)


if __name__=="__main__":
    main()
