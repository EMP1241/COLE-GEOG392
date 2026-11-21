"""Assess Glades' exclusion role without accepting or repairing geometries."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import shapely
from osgeo import gdal

from investigate_upper_snake import digest_file, require


def rings_of(g):
    return [list(r.coords) for p in shapely.get_parts(g) for r in [p.exterior,*p.interiors]]


def winding(rings,point):
    edges=np.concatenate([np.column_stack([np.asarray(r)[:-1,:2],np.asarray(r)[1:,:2]]) for r in rings])
    x0,y0,x1,y1=edges.T
    left=(x1-x0)*(point.y-y0)-(point.x-x0)*(y1-y0)
    ray=-(int(np.count_nonzero((y0<=point.y)&(y1>point.y)&(left>0)))-int(np.count_nonzero((y1<=point.y)&(y0>point.y)&(left<0))))
    ax,ay=x0-point.x,y0-point.y
    bx,by=x1-point.x,y1-point.y
    rotation=-math.fsum(np.arctan2(ax*by-ay*bx,ax*bx+ay*by).tolist())/(2*math.pi)
    require(abs(rotation-round(rotation))<1e-7 and round(rotation)==ray,"Independent point winding checks disagree")
    return ray


def main():
    home=Path(__file__).resolve().parents[1]
    dest=home/"verification/glades-exclusion-need-2026-10-22.json"
    out=home/"working/glades-exclusion-need-2026-10-22"
    require(not dest.exists() and not out.exists(),"Refuse to overwrite evidence")
    screen_path=home/"verification/glades-overlap-screen-2026-10-22.json"
    screen=json.loads(screen_path.read_text(encoding="utf-8"))
    saved=home/"working/glades-overlap-2026-10-22"
    shapes={}
    for row in screen["records"]:
        key=(row["layer"],row["source_fid"])
        suffix=row["layer"].removeprefix("PADUS4_1")
        path=saved/f"{suffix}-{row['source_fid']}-default.wkb"
        require(digest_file(path)==row["default_wkb_sha256"],"Screen source geometry changed")
        shapes[key]=shapely.from_wkb(path.read_bytes())
    publics=[r for r in screen["records"] if r["otherwise_eligible_attribute_screen"]]
    valid_public=[r for r in publics if r["default_valid"] and not r["actual_nonlinear_segments"]]
    # This deliberately does not treat DESG/unknown ownership as private land.
    masks=[r for r in screen["records"] if (r["layer"],r["source_fid"])!=("PADUS4_1Fee",77431)
           and (r["attributes"].get("Own_Type") in {"NGO","PVT"} or r["attributes"].get("Pub_Access") in {"RA","XA"}
                or r["attributes"].get("Own_Name")=="DOD" or r["attributes"].get("Mang_Name")=="DOD" or r["attributes"].get("Des_Tp")=="MIL")]
    certain_masks=[r for r in masks if r["default_valid"] and not r["actual_nonlinear_segments"]]
    unresolved_masks=[r for r in masks if r not in certain_masks]
    public_union=shapely.union_all([shapes[r["layer"],r["source_fid"]] for r in valid_public])
    other_mask_union=shapely.union_all([shapes[r["layer"],r["source_fid"]] for r in certain_masks])
    require(public_union.is_valid and other_mask_union.is_valid,"Invalid union of already valid straight sources")
    out.mkdir(parents=True)
    artifacts={}
    def save(name,g):
        body=shapely.to_wkb(g,byte_order=1)
        (out/name).write_bytes(body)
        artifacts[name]={"bytes":len(body),"sha256":hashlib.sha256(body).hexdigest()}
    save("valid-straight-public-union.wkb",public_union)
    save("other-valid-straight-exclusion-union.wkb",other_mask_union)
    variants={}
    residuals={}
    for name,entry in screen["existing_unaccepted_glades_representations"].items():
        path=home/entry["path"]
        require(digest_file(path)==entry["sha256"],"Existing Glades diagnostic changed")
        glades=shapely.from_wkb(path.read_bytes())
        overlap=public_union.intersection(glades)
        residue=overlap.difference(other_mask_union)
        covered=overlap.intersection(other_mask_union)
        variants[name]={"valid_straight_public_overlap_m2":overlap.area,"overlap_acres":overlap.area/4046.8564224,
                        "covered_by_other_valid_straight_masks_m2":covered.area,"remaining_after_other_valid_masks_m2":residue.area,
                        "remaining_acres":residue.area/4046.8564224,
                        "partition_area_residual_m2":overlap.area-covered.area-residue.area,
                        "remaining_valid":bool(residue.is_valid)}
        save(name+"-valid-public-overlap.wkb",overlap)
        save(name+"-after-other-valid-masks.wkb",residue)
        residuals[name]=residue
        print(json.dumps({name:variants[name]}),flush=True)
    # Independent raw directed-ring checks at points in each residual polygon:
    # no make_valid, polygon repair, precision grid or new Glades reconstruction.
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    require(digest_file(archive)==screen["source_archive_sha256"],"Original archive changed")
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    source_rings={}
    fine_source={}
    for row in masks+[next(r for r in screen["records"] if r["layer"]=="PADUS4_1Fee" and r["source_fid"]==77431)]:
        key=(row["layer"],row["source_fid"])
        feature=ds.GetLayerByName(key[0]).GetFeature(key[1])
        native=feature.GetGeometryRef()
        require(hashlib.sha256(bytes(native.ExportToWkb())).hexdigest()==row["native_wkb_sha256"],"Original native source differs")
        fine=shapely.from_wkb(bytes(native.GetLinearGeometry(0.01).ExportToWkb()))
        source_rings[key]=rings_of(fine)
        fine_source[key]=shapely.MultiLineString(source_rings[key])
    points=[]
    for polygon in shapely.get_parts(residuals["fine_positive"]):
        if polygon.geom_type!="Polygon" or polygon.area<1:
            continue
        point=polygon.representative_point()
        glades_key=("PADUS4_1Fee",77431)
        rows=[]
        for row in masks:
            key=(row["layer"],row["source_fid"])
            w=winding(source_rings[key],point)
            if w!=0:
                rows.append({"layer":key[0],"source_fid":key[1],"winding":w,"native_fine_source_boundary_clearance_m":point.distance(fine_source[key]),
                             "source_default_valid":row["default_valid"],"source_has_native_curves":row["actual_nonlinear_segments"]})
        points.append({"xy":[point.x,point.y],"diagnostic_residual_component_area_m2":polygon.area,
                       "glades_raw_fine_ray_and_angle_winding":winding(source_rings[glades_key],point),
                       "glades_native_fine_boundary_clearance_m":point.distance(fine_source[glades_key]),
                       "public_valid_straight_sources":[r["source_fid"] for r in valid_public if shapes[r["layer"],r["source_fid"]].contains(point)],
                       "other_explicit_exclusion_sources_with_nonzero_raw_winding":rows})
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"local_date":"2026-10-22","credit":"Margaret",
            "screen_receipt_sha256":digest_file(screen_path),"source_archive_sha256":screen["source_archive_sha256"],
            "valid_straight_public_records":len(valid_public),"all_otherwise_eligible_attribute_records":len(publics),
            "other_explicit_exclusion_records":len(masks),"other_valid_straight_exclusion_records":len(certain_masks),
            "unresolved_other_masks":[{"layer":r["layer"],"source_fid":r["source_fid"],"name":r["attributes"]["Unit_Nm"],"valid":r["default_valid"],"curves":r["actual_nonlinear_segments"]} for r in unresolved_masks],
            "diagnostic_variants":variants,"independent_source_winding_witnesses":points,"artifacts":artifacts,
            "repair_accepted":False,"new_repairs_performed":False,"original_archive_unchanged":digest_file(archive)==screen["source_archive_sha256"],
            "limits":"Existing unaccepted Glades regions are used for screening only. Invalid public sources are omitted from this subset, not from final eligibility. Exclusion redundancy is not proved by invalid other masks or point samples. No county totals or final conflict-precedence policy is adopted."}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="artifacts"}),flush=True)


if __name__=="__main__":
    main()
