"""Preserve native curved geometry and diagnose its linearization; no repairs."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from osgeo import gdal

from investigate_upper_snake import digest_file, require


def main():
    home=Path(__file__).resolve().parents[1]
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    archive_hash=digest_file(archive)
    require(archive_hash=="62ed113335e359509ad68a731146c74aa873c5a92da4f3fda572e54bc9b823ab","Original archive changed")
    dest=home/"verification/nj41938-native-diagnosis.json"
    out=home/"working/nj41938-native-2026-10-21"
    require(not dest.exists() and not out.exists(),"Refuse to overwrite native investigation")
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    feature=ds.GetLayerByName("PADUS4_1Fee").GetFeature(41938)
    native=feature.GetGeometryRef()
    components=[]
    def walk(g,path):
        entry={"path":path,"type":g.GetGeometryName(),"children":g.GetGeometryCount(),"points":g.GetPointCount(),"area_m2":g.GetArea()}
        if g.GetGeometryName()=="CIRCULARSTRING":
            entry["control_points"]=g.GetPoints()
        components.append(entry)
        for i in range(g.GetGeometryCount()):
            walk(g.GetGeometryRef(i),path+[i])
    walk(native,[])
    out.mkdir(parents=True)
    artifacts={}
    for name,data in [("original-native-geometry.wkb",bytes(native.ExportToWkb())),("original-native-geometry.wkt",native.ExportToIsoWkt().encode("utf-8"))]:
        (out/name).write_bytes(data)
        artifacts[name]={"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}
    variants=[]
    for angle in [None,4.0,1.0,0.1,0.01]:
        linear=native.GetLinearGeometry() if angle is None else native.GetLinearGeometry(angle)
        data=bytes(linear.ExportToWkb())
        name="linear-default.wkb" if angle is None else f"linear-{angle:g}-degrees.wkb"
        (out/name).write_bytes(data)
        variants.append({"maximum_angle_degrees":angle,"linear_area_m2":linear.GetArea(),"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest(),"artifact":name})
    require(digest_file(archive)==archive_hash,"Original archive changed during inspection")
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"decision":"native_diagnosis_only_not_accepted","credit":"Margaret",
            "source_layer":"PADUS4_1Fee","source_fid":41938,"source_archive_sha256":archive_hash,
            "native_type":native.GetGeometryName(),"actual_nonlinear_segments":bool(native.HasCurveGeometry(1)),
            "native_area_m2":native.GetArea(),"publisher_shape_area_m2":feature.GetField("SHAPE_Area"),
            "components":components,"linearization_variants":variants,"native_artifacts":artifacts,
            "original_archive_unchanged":True,"repairs_performed":False}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps(report),flush=True)


if __name__=="__main__":
    main()
