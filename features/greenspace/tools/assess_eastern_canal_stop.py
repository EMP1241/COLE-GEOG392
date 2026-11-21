"""Tighten county-risk screening at the stop; do not repair or accept it."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from osgeo import gdal
import pyogrio
import shapely

from investigate_upper_snake import digest_file, require


def main():
    home=Path(__file__).resolve().parents[1]
    dest=home/"verification/eastern-canal-county-risk-2026-10-22.json"
    require(not dest.exists(),"Refuse to overwrite risk assessment")
    run=home/"working/national-prep-local-exclusions-2026-10-22"
    summary=json.loads((run/"repair-summary.json").read_text(encoding="utf-8"))
    stopped=summary["stop_record"]
    require(stopped["source_fid"]==88623 and stopped["layer"]=="PADUS4_1Fee","Unexpected stop identity")
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    require(digest_file(archive)==summary["source_archive"]["sha256"],"Original archive differs")
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    feature=ds.GetLayerByName(stopped["layer"]).GetFeature(88623)
    native=feature.GetGeometryRef()
    default=shapely.from_wkb(bytes(native.GetLinearGeometry().ExportToWkb()))
    require(hashlib.sha256(shapely.to_wkb(default,byte_order=1)).hexdigest()==stopped["original_sha256"],"Original source differs")
    require(all((None if feature.GetField(k) is None else str(feature.GetField(k)))==v for k,v in stopped["attributes"].items()),"Original attributes differ")
    components=[native.GetGeometryRef(i) for i in range(native.GetGeometryCount())] if native.GetGeometryName() in {"MULTIPOLYGON","MULTISURFACE"} else [native]
    boxes=[]
    for component in components:
        a,b,c,d=component.GetEnvelope()
        boxes.append(shapely.box(a,c,b,d))
    footprint=shapely.union_all(boxes)
    counties=pyogrio.read_dataframe(home/"source/census-2021/tl_2021_us_county.zip",columns=["GEOID","NAME","ALAND"]).to_crs("ESRI:102039")
    risks=[]
    for _,county in counties.loc[counties.intersects(footprint)].iterrows():
        area=county.geometry.intersection(footprint).area
        denominator=min(float(county["ALAND"]),county.geometry.area)
        risks.append({"county_fips":str(county["GEOID"]),"name":str(county["NAME"]),"native_component_envelope_upper_bound_m2":area,
                      "upper_bound_acres":area/4046.8564224,"upper_percentage_point_sensitivity":area/denominator*100,
                      "material_effect_cannot_be_excluded":area>4046.8564224 or area/denominator*100>0.01})
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"local_date":"2026-10-22","credit":"Margaret",
            "source_layer":stopped["layer"],"source_fid":88623,"name":stopped["name"],"source_attributes":stopped["attributes"],
            "source_archive_sha256":summary["source_archive"]["sha256"],"source_native_wkb_sha256":hashlib.sha256(bytes(native.ExportToWkb())).hexdigest(),
            "actual_nonlinear_segments":bool(native.HasCurveGeometry(1)),"native_components":len(components),"county_risks":risks,
            "candidate_area_m2":stopped["repaired_polygon_area_m2"],"candidate_area_acres":stopped["repaired_polygon_area_m2"]/4046.8564224,
            "raw_decoded_area_difference_m2":stopped["absolute_difference_m2"],"raw_decoded_area_difference_acres":stopped["absolute_difference_m2"]/4046.8564224,
            "decision":"retain_stop_possible_material_public_county_effect_not_bounded_below_one_acre",
            "repairs_performed":False,"repair_accepted":False,"national_processing_restarted":False,
            "limits":"Union of native part envelopes is a tighter conservative support bound than the entire feature envelope. It is not measured lost land or actual repair error. The sub-acre raw area difference does not bound geometry ambiguity. No further source-ring or curve repair investigation is performed under the user's stop instruction."}
    require(digest_file(archive)==summary["source_archive"]["sha256"],"Original archive changed")
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps(report),flush=True)


if __name__=="__main__":
    main()
