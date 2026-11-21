"""Read-only all-layer local overlap screening; no new geometry repairs."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from osgeo import gdal
import shapely

from investigate_upper_snake import digest_file, require


FIELDS=["Category","Own_Type","Own_Name","Mang_Type","Mang_Name","Des_Tp","Unit_Nm","State_Nm","Pub_Access","SHAPE_Area"]
PUBLIC={"FED","STAT","LOC","DIST","TERR"}


def eligibility(attributes):
    reasons=[]
    if attributes.get("Own_Type") not in PUBLIC:
        reasons.append("public_ownership_not_confirmed")
    if attributes.get("Pub_Access")!="OA":
        reasons.append("not_open_access")
    if attributes.get("Own_Name")=="DOD" or attributes.get("Mang_Name")=="DOD" or attributes.get("Des_Tp")=="MIL":
        reasons.append("military")
    # Missing evidence stays provisional; no SQL NULL comparison can silently
    # discard it or establish a final eligibility verdict.
    if not attributes.get("Own_Name") or not attributes.get("Mang_Name") or not attributes.get("Des_Tp"):
        reasons.append("incomplete_exclusion_attributes")
    return reasons


def main():
    home=Path(__file__).resolve().parents[1]
    dest=home/"verification/glades-overlap-screen-2026-10-22.json"
    out=home/"working/glades-overlap-2026-10-22"
    require(not dest.exists() and not out.exists(),"Refuse to overwrite evidence")
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    archive_hash=digest_file(archive)
    require(archive_hash=="62ed113335e359509ad68a731146c74aa873c5a92da4f3fda572e54bc9b823ab","Original archive changed")
    diagnosis=json.loads((home/"verification/glades-diagnosis.json").read_text(encoding="utf-8"))
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    glades_feature=ds.GetLayerByName("PADUS4_1Fee").GetFeature(77431)
    native=glades_feature.GetGeometryRef()
    require(hashlib.sha256(bytes(native.ExportToWkb())).hexdigest()==diagnosis["native_wkb_sha256"],"Glades source changed")
    xmin,xmax,ymin,ymax=native.GetEnvelope()
    # One-metre padding makes this an inclusive neighborhood screen. It does
    # not alter any source geometry, measure or exclusion mask.
    bounds=[xmin-1,ymin-1,xmax+1,ymax+1]
    representations={"default_positive":home/"working/glades-investigation-2026-10-21/default-positive-winding-region.wkb",
                     "fine_positive":home/"working/glades-curves-2026-10-21/0.01-degrees-positive-region.wkb",
                     "independent_positive":home/"working/glades-curves-2026-10-21/independent-positive-region.wkb"}
    glades={name:shapely.from_wkb(path.read_bytes()) for name,path in representations.items()}
    out.mkdir(parents=True)
    records=[]
    layers={}
    for suffix in ["Fee","Proclamation","Marine","Easement","Designation"]:
        layer=ds.GetLayerByName("PADUS4_1"+suffix)
        require(layer is not None,"Missing original layer")
        require(layer.GetSpatialRef().IsSame(ds.GetLayerByName("PADUS4_1Fee").GetSpatialRef()),"Source layer CRS differs")
        layer.SetSpatialFilterRect(*bounds)
        count=0
        candidates=0
        for feature in layer:
            geometry=feature.GetGeometryRef()
            attrs={k:feature.GetField(k) for k in FIELDS if feature.GetFieldIndex(k)>=0}
            body=bytes(geometry.ExportToWkb())
            linear_body=bytes(geometry.GetLinearGeometry().ExportToWkb())
            shape=shapely.from_wkb(linear_body)
            key=f"{suffix}-{feature.GetFID()}"
            (out/(key+"-native.wkb")).write_bytes(body)
            (out/(key+"-default.wkb")).write_bytes(linear_body)
            excluded=eligibility(attrs)
            row={"layer":layer.GetName(),"source_fid":feature.GetFID(),"attributes":attrs,
                 "native_wkb_sha256":hashlib.sha256(body).hexdigest(),"default_wkb_sha256":hashlib.sha256(linear_body).hexdigest(),
                 "actual_nonlinear_segments":bool(geometry.HasCurveGeometry(1)),"default_valid":bool(shape.is_valid),
                 "default_validity_reason":shapely.is_valid_reason(shape),"provisional_exclusion_reasons":excluded,
                 "otherwise_eligible_attribute_screen":not excluded,"bounds":list(shape.bounds)}
            if not excluded:
                candidates+=1
                row["diagnostic_overlaps"]={}
                for name,region in glades.items():
                    try:
                        intersection=shape.intersection(region)
                        result={"area_m2":intersection.area,"area_acres":intersection.area/4046.8564224,
                                "valid":bool(intersection.is_valid),"empty":bool(intersection.is_empty)}
                        if intersection.area>0:
                            witness=intersection.representative_point()
                            result["witness_xy"]=[witness.x,witness.y]
                            result["witness_public_boundary_clearance_m"]=witness.distance(shape.boundary)
                            result["witness_glades_boundary_clearance_m"]=witness.distance(region.boundary)
                        row["diagnostic_overlaps"][name]=result
                    except Exception as error:
                        row["diagnostic_overlaps"][name]={"error":str(error)}
            records.append(row)
            count+=1
        layers[layer.GetName()]={"bbox_records":count,"otherwise_eligible_attribute_screen":candidates}
        print(json.dumps({"layer":layer.GetName(),**layers[layer.GetName()]}),flush=True)
    require(digest_file(archive)==archive_hash,"Original archive changed")
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"local_date":"2026-10-22","credit":"Margaret",
            "purpose":"Glades overlap/impact screening only; no repair acceptance or county totals",
            "source_archive_sha256":archive_hash,"native_glades_bounds_xyxy":[xmin,ymin,xmax,ymax],"screen_bounds_xyxy":bounds,
            "spatial_filter_documentation":"https://gdal.org/en/stable/doxygen/classOGRLayer.html",
            "spatial_filter_limit":"Inclusive envelope screen; false positives possible. Every returned original record retained. No state/name-only filter.",
            "existing_unaccepted_glades_representations":{name:{"path":path.relative_to(home).as_posix(),"sha256":digest_file(path)} for name,path in representations.items()},
            "layers":layers,"records":records,"original_archive_unchanged":True,"new_repairs_performed":False,
            "repair_accepted":False,"national_processing_restarted":False,
            "limits":"Attribute screening is provisional eligibility before competing exclusions and county/water policy. Existing Glades representations remain unaccepted; overlap results are diagnostics until independently corroborated."}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({"report":str(dest),"eligible_records":[r for r in records if r["otherwise_eligible_attribute_screen"]]}),flush=True)


if __name__=="__main__":
    main()
