"""Read-only original-source audit of the conservative Glades exclusion."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from osgeo import gdal
import shapely

from accepted_adjudications import verify_inputs
from build_glades_local_exclusion import construct, GRID, PADDING, UNCERTAINTY_RADIUS, ACRE
from check_nj41938_native_region import sample_ring
from investigate_glades import winding_faces
from investigate_upper_snake import digest_file, require
from repair_geometry import source_tree


def canonical(g):
    return shapely.to_wkb(shapely.normalize(g),byte_order=1)


def main():
    home=Path(__file__).resolve().parents[1]
    dest=home/"verification/glades-local-exclusion-v2-audit.json"
    require(not dest.exists(),"Refuse to overwrite audit")
    receipt_path=home/"verification/glades-local-exclusion-v2.json"
    receipt=json.loads(receipt_path.read_text(encoding="utf-8"))
    for relative,expected in receipt["input_artifacts"].items():
        require((home/relative).resolve().is_relative_to(home.resolve()),"Input escapes feature")
        require(digest_file(home/relative)==expected["sha256"] and (home/relative).stat().st_size==expected["bytes"],"Exclusion input changed")
    out=home/"working/glades-local-exclusion-v2-2026-10-22"
    for name,expected in receipt["artifacts"].items():
        require(digest_file(out/name)==expected["sha256"],"Exclusion artifact changed")
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    require(digest_file(archive)==receipt["source_archive_sha256"],"Original archive changed")
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    feature=ds.GetLayerByName(receipt["source_layer"]).GetFeature(receipt["source_fid"])
    native=feature.GetGeometryRef()
    require(hashlib.sha256(bytes(native.ExportToWkb())).hexdigest()==receipt["source_native_wkb_sha256"],"Native identity differs")
    require(hashlib.sha256(bytes(native.GetLinearGeometry().ExportToWkb())).hexdigest()==receipt["source_original_wkb_sha256"],"Default identity differs")
    require(all((None if feature.GetField(k) is None else str(feature.GetField(k)))==v for k,v in receipt["source_attributes"].items()),"Attributes differ")
    require(receipt["exclusion_only"] and not receipt["eligible_as_greenspace"] and not receipt["exact_repair_accepted"] and feature.GetField("Own_Type")=="NGO","Exclusion scope differs")
    source=shapely.from_wkb(bytes(native.GetLinearGeometry(0.01).ExportToWkb()))
    rings=[list(r.coords) for p in source.geoms for r in [p.exterior,*p.interiors]]
    _,faces,positive,_,negative,_=winding_faces(rings)
    require(negative.is_empty,"Negative native-source winding")
    print(json.dumps({"phase":"fresh_original_fine_winding","faces":len(faces)}),flush=True)
    independent_rings=[]
    for i in range(native.GetGeometryCount()):
        polygon=native.GetGeometryRef(i)
        independent_rings.extend(sample_ring(polygon.GetGeometryRef(j)) for j in range(polygon.GetGeometryCount()))
    _,_,independent,_,negative,_=winding_faces(independent_rings)
    require(negative.is_empty,"Negative independent native winding")
    print(json.dumps({"phase":"fresh_original_independent_sampling_complete"}),flush=True)
    mask,uncertainty,lines,comparisons=construct(source,positive,independent)
    saved_mask=shapely.from_wkb((out/"conservative-exclusion.wkb").read_bytes())
    saved_band=shapely.from_wkb((out/"boundary-uncertainty-band.wkb").read_bytes())
    require(canonical(mask)==canonical(saved_mask) and canonical(uncertainty)==canonical(saved_band),"Fresh native construction differs")
    require(comparisons==receipt["fixed_precision_comparisons"],"Containment/area accounting differs")
    require(receipt["grid_size_m"]==GRID and receipt["private_boundary_padding_m"]==PADDING and receipt["uncertainty_tube_radius_m"]==UNCERTAINTY_RADIUS,"Construction parameters differ")
    bound=2*UNCERTAINTY_RADIUS*receipt["boundary_length_upper_bound_m"]+receipt["source_ring_count"]*math.pi*UNCERTAINTY_RADIUS**2+receipt["additional_mask_growth_upper_bound_m2"]
    require(bound==receipt["global_area_uncertainty_upper_bound_m2"] and bound<ACRE,"Analytic materiality bound differs")
    require(receipt["county_uncertainty_bounds"] and all(c["maximum_uncertainty_m2"]==bound and c["maximum_percentage_point_uncertainty"]<0.01 for c in receipt["county_uncertainty_bounds"]),"County sensitivity bound differs")
    prior=json.loads((home/"verification/glades-investigation-outcome.json").read_text(encoding="utf-8"))
    for item in prior["prior_audits"]:
        require(digest_file(home/"working"/item["run"]/"repair_audit.sqlite3")==item["sha256"],"Prior audit changed")
    expected=json.loads((home/"working/national-repair-nj-v2-2026-10-21/source-extraction-before.json").read_text(encoding="utf-8"))
    require(source_tree(home/"working/padus-4.1/PADUS4_1Geodatabase.gdb")==expected,"Original extraction changed")
    verify_inputs(home)
    require(digest_file(archive)==receipt["source_archive_sha256"],"Original archive changed during audit")
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"status":"passed","discrepancies":[],"credit":"Margaret",
            "acceptance_receipt_sha256":digest_file(receipt_path),"source_native_identity_verified":True,
            "fresh_original_fine_and_independent_winding_verified":True,"conservative_mask_regenerated_identically":True,
            "analytic_global_uncertainty_upper_bound_m2":bound,"county_bounds":receipt["county_uncertainty_bounds"],
            "original_archive_extraction_and_six_prior_audits_unchanged":True,
            "repairs_performed":False,"exact_repair_accepted":False,"eligible_as_greenspace":False}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps(report),flush=True)


if __name__=="__main__":
    main()
