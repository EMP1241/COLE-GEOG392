"""Quantify native circular arcs and independent source-winding regions."""

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import math
from pathlib import Path

from osgeo import gdal
import shapely

from investigate_rings import cycles, signed_area
from investigate_upper_snake import digest_file, require


def angle_minus_sine(theta):
    return math.fsum((-1)**(n+1)*theta**(2*n+1)/math.factorial(2*n+1) for n in range(1,10))


def arc_parameters(points):
    require(len(points)==3,"Unexpected circular arc point count")
    with localcontext() as context:
        context.prec=60
        a,b,c=[[Decimal.from_float(float(v)) for v in p[:2]] for p in points]
        bx,by=b[0]-a[0],b[1]-a[1]
        cx,cy=c[0]-a[0],c[1]-a[1]
        determinant=2*(bx*cy-by*cx)
        require(determinant!=0,"Collinear circular controls require review")
        ux=((bx*bx+by*by)*cy-(cx*cx+cy*cy)*by)/determinant
        uy=(bx*(cx*cx+cy*cy)-cx*(bx*bx+by*by))/determinant
        radius2=ux*ux+uy*uy
        vectors=[(-ux,-uy),(bx-ux,by-uy),(cx-ux,cy-uy)]
        angles=[]
        for u,v in zip(vectors,vectors[1:]):
            angles.append(math.atan2(float(u[0]*v[1]-u[1]*v[0]),float(u[0]*v[0]+u[1]*v[1])))
        require(angles[0]*angles[1]>0 and abs(sum(angles))<math.pi,"Unexpected long/ambiguous circular arc")
        correction=float(radius2)/2*math.fsum(angle_minus_sine(t) for t in angles)
        return {"center_offset_from_first_control_m":[float(ux),float(uy)],"radius_m":math.sqrt(float(radius2)),
                "radius_squared_m2":float(radius2),"subtended_angle_radians":sum(angles),
                "control_polyline_to_true_arc_signed_area_correction_m2":correction}


def native_ring_area(ring):
    pieces=[ring.GetGeometryRef(i) for i in range(ring.GetGeometryCount())] if ring.GetGeometryName()=="COMPOUNDCURVE" else [ring]
    points=[]
    corrections=[]
    for piece in pieces:
        coordinates=[tuple(p[:2]) for p in piece.GetPoints()]
        points.extend(coordinates[1:] if points and coordinates[0]==points[-1] else coordinates)
        if piece.GetGeometryName()=="CIRCULARSTRING":
            corrections.append(arc_parameters(coordinates)["control_polyline_to_true_arc_signed_area_correction_m2"])
        else:
            require(piece.GetGeometryName() in {"LINESTRING","LINEARRING"},"Unsupported native ring segment")
    return signed_area(points)+math.fsum(corrections)


def source_region(native,angle):
    linear=native.GetLinearGeometry(angle)
    source=shapely.from_wkb(bytes(linear.ExportToWkb()))
    return linear_source_region(source)


def linear_source_region(source):
    rings=[list(r.coords) for p in source.geoms for r in [p.exterior,*p.interiors]]
    simple=[c for r in rings for c in cycles(r)]
    shells=[shapely.Polygon(c) for c in simple if signed_area(c)<0]
    holes=[shapely.Polygon(c) for c in simple if signed_area(c)>0]
    require(len(shells)==5 and len(holes)==5 and all(g.is_valid for g in shells+holes),"Source role/cycle structure changed")
    st,ht=shapely.STRtree(shells),shapely.STRtree(holes)
    noded=shapely.node(shapely.MultiLineString(rings))
    faces=list(shapely.polygonize(shapely.get_parts(noded)).geoms)
    positive=[]
    counts={}
    zero=[]
    for face in faces:
        point=face.representative_point()
        winding=len(st.query(point,predicate="within"))-len(ht.query(point,predicate="within"))
        require(winding>=0,"Negative source winding requires review")
        counts[str(winding)]=counts.get(str(winding),0)+1
        (positive if winding>0 else zero).append(face)
    region=shapely.union_all(positive)
    difference=shapely.union_all(shells).difference(shapely.union_all(holes))
    require(region.is_valid and region.symmetric_difference(difference).area==0,"Independent source regions disagree")
    return source,region,noded,shapely.union_all(zero),counts


def main():
    home=Path(__file__).resolve().parents[1]
    dest=home/"verification/nj41938-curve-diagnosis.json"
    out=home/"working/nj41938-curves-2026-10-21"
    require(not dest.exists() and not out.exists(),"Refuse to overwrite curve investigation")
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    archive_hash=digest_file(archive)
    require(archive_hash=="62ed113335e359509ad68a731146c74aa873c5a92da4f3fda572e54bc9b823ab","Original archive changed")
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    feature=ds.GetLayerByName("PADUS4_1Fee").GetFeature(41938)
    native=feature.GetGeometryRef().Clone()
    signed_native=[]
    arcs=[]
    for i in range(native.GetGeometryCount()):
        polygon=native.GetGeometryRef(i)
        for j in range(polygon.GetGeometryCount()):
            ring=polygon.GetGeometryRef(j)
            signed_native.append({"polygon":i,"ring":j,"signed_area_m2":native_ring_area(ring)})
            if ring.GetGeometryName()=="COMPOUNDCURVE":
                for k in range(ring.GetGeometryCount()):
                    arc=ring.GetGeometryRef(k)
                    if arc.GetGeometryName()=="CIRCULARSTRING":
                        parameters=arc_parameters(arc.GetPoints())
                        arcs.append({"path":[i,j,k],"controls":[p[:2] for p in arc.GetPoints()],**parameters})
    out.mkdir(parents=True)
    variants=[]
    regions=[]
    artifacts={}
    for angle in [4.0,0.1,0.01,0.001]:
        source,region,noded,zero,counts=source_region(native,angle)
        bounds=[]
        for info in arcs:
            curve=native.GetGeometryRef(info["path"][0]).GetGeometryRef(info["path"][1]).GetGeometryRef(info["path"][2])
            linear=curve.GetLinearGeometry(angle)
            points=linear.GetPoints()
            center=[info["controls"][0][axis]+info["center_offset_from_first_control_m"][axis] for axis in range(2)]
            steps=[]
            radial=[]
            for a,b in zip(points,points[1:]):
                u=[a[k]-center[k] for k in range(2)]
                v=[b[k]-center[k] for k in range(2)]
                steps.append(abs(math.atan2(u[0]*v[1]-u[1]*v[0],u[0]*v[0]+u[1]*v[1])))
            for p in points:
                radial.append(abs(math.hypot(p[0]-center[0],p[1]-center[1])-info["radius_m"]))
            radius=info["radius_m"]
            # Sector-minus-chord areas bound changed area of the circular
            # boundary. Radial coordinate residue is reported separately.
            bounds.append({"arc_path":info["path"],"linear_segments":len(steps),"maximum_step_radians":max(steps),
                           "maximum_chord_sagitta_m":2*radius*math.sin(max(steps)/4)**2,
                           "sum_sector_minus_chord_area_m2":info["radius_squared_m2"]/2*math.fsum(angle_minus_sine(t) for t in steps),
                           "maximum_vertex_radial_residual_m":max(radial)})
        item={"maximum_angle_degrees":angle,"region_area_m2":region.area,"region_parts":len(region.geoms),
              "source_winding_face_counts":counts,"arc_chord_diagnostics":bounds,
              "total_sector_minus_chord_area_m2":math.fsum(v["sum_sector_minus_chord_area_m2"] for v in bounds)}
        if regions:
            item["symmetric_difference_from_previous_angle_region_m2"]=region.symmetric_difference(regions[-1]).area
        variants.append(item)
        regions.append(region)
        for suffix,g in [("source",source),("positive-region",region),("noded-linework",noded),("zero-winding-region",zero)]:
            name=f"{angle:g}-degrees-{suffix}.wkb"
            data=shapely.to_wkb(g,byte_order=1)
            (out/name).write_bytes(data)
            artifacts[name]={"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}
    require(digest_file(archive)==archive_hash,"Original archive changed")
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"decision":"curve_diagnosis_only_not_accepted","credit":"Margaret",
            "source_archive_sha256":archive_hash,"native_signed_ring_areas":signed_native,"circular_arcs":arcs,
            "variants":variants,"artifacts":artifacts,"original_archive_unchanged":True,"repairs_performed":False}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="artifacts"}),flush=True)


if __name__=="__main__":
    main()
