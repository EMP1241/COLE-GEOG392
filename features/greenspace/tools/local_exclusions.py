"""Specific hash-bound conservative exclusions; never eligible geometries."""

import hashlib
import json

import shapely


RECEIPT="verification/glades-local-exclusion-v2.json"
AUDIT="verification/glades-local-exclusion-v2-audit.json"
PINNED={RECEIPT:"8173ce92918832d68a76c2b3ba9412fe88b48f776c0cd4a35701593216d1833c",AUDIT:"d78c41ffd4ba9fd7028d0ee6aedaa3632ed4a4fd76e16aa1edbeda7d043a9e49"}
IDENTITIES={("PADUS4_1Fee",77431)}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def verify_inputs(home):
    result=dict(PINNED)
    for relative,expected in PINNED.items():
        if digest(home/relative)!=expected:
            raise RuntimeError("Conservative local exclusion receipt/audit differs")
    receipt=json.loads((home/RECEIPT).read_text(encoding="utf-8"))
    audit=json.loads((home/AUDIT).read_text(encoding="utf-8"))
    if audit["status"]!="passed" or audit["discrepancies"] or audit["acceptance_receipt_sha256"]!=PINNED[RECEIPT]:
        raise RuntimeError("Conservative local exclusion lacks passing pinned audit")
    for relative,item in receipt["input_artifacts"].items():
        if not (home/relative).resolve().is_relative_to(home.resolve()):
            raise RuntimeError("Exclusion input escapes feature")
        result[relative]=item["sha256"]
    root="working/glades-local-exclusion-v2-2026-10-22/"
    result.update({root+name:item["sha256"] for name,item in receipt["artifacts"].items()})
    for relative,expected in result.items():
        if digest(home/relative)!=expected:
            raise RuntimeError("Conservative exclusion evidence changed: "+relative)
    return result


def load(home,layer,fid,original,candidate,attributes,archive_sha256):
    if (layer,fid) not in IDENTITIES:
        return None
    from osgeo import gdal
    inputs=verify_inputs(home)
    receipt=json.loads((home/RECEIPT).read_text(encoding="utf-8"))
    hash_geometry=lambda g:hashlib.sha256(shapely.to_wkb(g,byte_order=1)).hexdigest()
    if (receipt["source_archive_sha256"]!=archive_sha256 or receipt["source_attributes"]!=attributes
            or receipt["source_original_wkb_sha256"]!=hash_geometry(original)
            or receipt["previous_candidate_wkb_sha256"]!=hash_geometry(candidate)
            or attributes.get("Own_Type")!="NGO" or not receipt["exclusion_only"]
            or receipt["eligible_as_greenspace"] or receipt["exact_repair_accepted"]):
        raise RuntimeError("Glades conservative exclusion source/scope identity differs")
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    feature=ds.GetLayerByName(layer).GetFeature(fid)
    native=feature.GetGeometryRef()
    if hashlib.sha256(bytes(native.ExportToWkb())).hexdigest()!=receipt["source_native_wkb_sha256"]:
        raise RuntimeError("Original Glades native geometry differs")
    if any((None if feature.GetField(k) is None else str(feature.GetField(k)))!=v for k,v in attributes.items()):
        raise RuntimeError("Original Glades attributes differ")
    root=home/"working/glades-local-exclusion-v2-2026-10-22"
    full=shapely.from_wkb((root/"conservative-full.wkb").read_bytes())
    mask=shapely.from_wkb((root/"conservative-exclusion.wkb").read_bytes())
    lines=[g for g in full.geoms if g.geom_type=="LineString"]
    if (not full.is_valid or not mask.is_valid or mask.is_empty or full.area!=mask.area or mask.area!=receipt["mask_area_m2"]
            or [hash_geometry(g) for g in lines]!=receipt["retained_nonarea_line_sha256"] or len(lines)!=85):
        raise RuntimeError("Conservative mask/nonarea preservation differs")
    return full,mask,{"method":"specific_conservative_private_exclusion_only","receipt":RECEIPT,"input_sha256":inputs,
                      "credit":"Margaret","exact_repair_accepted":False,"eligible_as_greenspace":False,
                      "comparison_area_m2":mask.area,"review_absolute_difference_m2":receipt["global_area_uncertainty_upper_bound_m2"],
                      "county_uncertainty_bounds":receipt["county_uncertainty_bounds"],"exclusion_only":True,
                      "minimum_source_boundary_clearance_m":receipt["fixed_precision_comparisons"][0]["minimum_original_fine_boundary_clearance_m"],
                      "scope":"Exact Glades source identity only. Must be subtracted, never counted as eligible. Earlier exact repair remains unaccepted."}
