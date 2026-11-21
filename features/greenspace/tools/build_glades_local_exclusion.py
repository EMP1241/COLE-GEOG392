"""Create a bounded conservative private-land mask, not an exact repair."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import numpy as np
import pyogrio
import shapely
from osgeo import gdal

from investigate_upper_snake import digest_file, require
from repair_geometry import parts, source_tree

GRID=0.001
PADDING=0.004
UNCERTAINTY_RADIUS=0.011
ACRE=4046.8564224


def construct(source,positive,independent):
    rings=[list(r.coords) for p in source.geoms for r in [p.exterior,*p.interiors]]
    lines=shapely.MultiLineString(rings)
    q=lambda g:shapely.set_precision(g,GRID,mode="valid_output")
    # Only valid diagnostic regions and linework are precision reduced. Raw
    # invalid polygons are never supplied to set_precision.
    edges=np.concatenate([np.column_stack([np.asarray(r)[:-1,:2],np.asarray(r)[1:,:2]]) for r in rings])
    a,b=edges[:,:2],edges[:,2:]
    direction=b-a
    lengths=np.hypot(direction[:,0],direction[:,1])
    u=direction/np.where(lengths==0,1,lengths)[:,None]
    u[lengths==0]=[1,0]
    n=np.column_stack([-u[:,1],u[:,0]])
    corners=np.stack([a-PADDING*u-PADDING*n,a-PADDING*u+PADDING*n,
                      b+PADDING*u+PADDING*n,b+PADDING*u-PADDING*n],axis=1)
    rectangles=q(shapely.polygons(corners))
    require(shapely.is_valid(rectangles).all(),"Invalid source-segment rectangles")
    # Explicit convex rectangles avoid the earlier thin-line buffer anomaly.
    # Preserve the collection, whose additive area overcounts overlap, instead
    # of depending on another nearly coincident union for its upper bound.
    uncertainty=shapely.GeometryCollection(list(rectangles))
    mask=shapely.union_all([q(positive),q(independent),*rectangles],grid_size=GRID)
    require(mask.is_valid and not mask.is_empty and uncertainty.is_valid,"Invalid exclusion output")
    comparisons=[]
    for name,region in [("fine",positive),("independent",independent)]:
        region=q(region)
        missing=shapely.difference(region,mask,grid_size=GRID)
        extra=shapely.difference(mask,region,grid_size=GRID)
        delta=shapely.symmetric_difference(mask,region,grid_size=GRID)
        print(json.dumps({"phase":"local_mask_containment","reference":name,
                          "missing_type":missing.geom_type,"missing_area_m2":missing.area,"missing_length_m":missing.length,
                          "extra_area_m2":extra.area,"rectangle_additive_area_m2":uncertainty.area}),flush=True)
        require(missing.is_empty,"Exclusion fails conservative private containment")
        require(extra.area<=uncertainty.area,"Mask growth exceeds complete primitive-area budget")
        residual=delta.area-missing.area-extra.area
        require(abs(residual)<1e-6,"Fixed-precision area identity failed")
        comparisons.append({"reference":name,"source_private_area_missing_m2":missing.area,
                            "extra_excluded_area_diagnostic_m2":extra.area,
                            "symmetric_difference_partition_residual_m2":residual})
    clearance=shapely.distance(lines,mask.boundary)
    source_covered=bool(shapely.covers(mask,lines))
    print(json.dumps({"phase":"original_source_boundary_clearance","minimum_clearance_m":clearance,"all_source_lines_covered":source_covered}),flush=True)
    require(source_covered and clearance>2*GRID,"Source boundary not safely inside conservative mask")
    for item in comparisons:
        item["minimum_original_fine_boundary_clearance_m"]=clearance
        item["all_original_fine_source_lines_covered"]=source_covered
    return mask,uncertainty,lines,comparisons


def main():
    home=Path(__file__).resolve().parents[1]
    dest=home/"verification/glades-local-exclusion-v2.json"
    out=home/"working/glades-local-exclusion-v2-2026-10-22"
    require(not dest.exists() and not out.exists(),"Refuse to overwrite an exclusion receipt")
    archive=home/"source/padus-4.1/PADUS4_1Geodatabase.zip"
    archive_hash=digest_file(archive)
    require(archive_hash=="62ed113335e359509ad68a731146c74aa873c5a92da4f3fda572e54bc9b823ab","Original archive changed")
    diagnosis=json.loads((home/"verification/glades-diagnosis.json").read_text(encoding="utf-8"))
    curves=json.loads((home/"verification/glades-curve-check.json").read_text(encoding="utf-8"))
    audit=home/"working/national-repair-nj-v2-2026-10-21/repair_audit.sqlite3"
    require(digest_file(audit)==diagnosis["previous_audit_sha256"],"Prior audit changed")
    with sqlite3.connect(audit.resolve().as_uri()+"?mode=ro",uri=True) as connection:
        connection.row_factory=sqlite3.Row
        row=connection.execute("SELECT * FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=77431").fetchone()
        attributes=json.loads(row["attributes_json"])
        old=shapely.from_wkb(row["repaired_wkb"])
    gdal.UseExceptions()
    ds=gdal.OpenEx("/vsizip/"+archive.resolve().as_posix()+"/PADUS4_1Geodatabase.gdb",gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=["OpenFileGDB"])
    feature=ds.GetLayerByName("PADUS4_1Fee").GetFeature(77431)
    native=feature.GetGeometryRef()
    require(hashlib.sha256(bytes(native.ExportToWkb())).hexdigest()==diagnosis["native_wkb_sha256"],"Native source changed")
    require(all((None if feature.GetField(k) is None else str(feature.GetField(k)))==v for k,v in attributes.items()),"Source attributes changed")
    require(attributes["Own_Type"]=="NGO","Private exclusion identity changed")
    input_paths=[home/"verification"/n for n in ["glades-diagnosis.json","glades-curve-check.json","glades-overlay-check.json","glades-overlap-outcome-2026-10-22.json"]]
    root=home/"working/glades-curves-2026-10-21"
    geometry_paths=[root/n for n in ["0.01-degrees-source.wkb","0.01-degrees-positive-region.wkb","independent-positive-region.wkb"]]
    for path in geometry_paths:
        require(digest_file(path)==curves["artifacts"][path.name]["sha256"],"Source diagnostic changed")
    source,positive,independent=[shapely.from_wkb(p.read_bytes()) for p in geometry_paths]
    fresh=shapely.from_wkb(bytes(native.GetLinearGeometry(0.01).ExportToWkb()))
    require(shapely.to_wkb(fresh,byte_order=1)==shapely.to_wkb(source,byte_order=1),"Fine native source differs")
    mask,uncertainty,linework,comparisons=construct(source,positive,independent)
    fine_check=next(v for v in curves["variants"] if v["gdal_requested_angle_degrees"]==0.01)
    native_error=fine_check["maximum_boundary_envelope_m"]
    displacement=GRID/math.sqrt(2)
    # Ample allowance for three successive grid displacements, the polygonal
    # buffer approximation, and the independently calculated native arc error.
    maximum_required_radius=math.sqrt(2)*PADDING+3*displacement+native_error
    require(maximum_required_radius<UNCERTAINTY_RADIUS,"Analytic tube radius insufficient")
    require(comparisons[0]["minimum_original_fine_boundary_clearance_m"]>2*displacement+native_error,"Native curve containment margin insufficient")
    native_arc_length=math.fsum(a["radius_m"]*abs(a["subtended_angle_radians"]) for a in fine_check["arc_bounds"])
    length_upper=linework.length+native_arc_length+1.0
    ring_count=len(linework.geoms)
    # Upper bound over all source boundaries, even those not near public land;
    # independent of the unreliable near-coincident floating overlay area.
    tube_bound=2*UNCERTAINTY_RADIUS*length_upper+ring_count*math.pi*UNCERTAINTY_RADIUS**2
    growth_bound=math.ceil(max(c["extra_excluded_area_diagnostic_m2"] for c in comparisons)*1e6)/1e6+1e-6
    error_upper=tube_bound+growth_bound
    require(uncertainty.area<=tube_bound,"Explicit primitive support exceeds analytic tube bound")
    require(error_upper<ACRE,"Conservative exclusion could affect more than an acre")
    county_zip=home/"source/census-2021/tl_2021_us_county.zip"
    counties=pyogrio.read_dataframe(county_zip,columns=["GEOID","NAME","ALAND","AWATER"]).to_crs("ESRI:102039")
    xmin,xmax,ymin,ymax=native.GetEnvelope()
    envelope=shapely.box(xmin-UNCERTAINTY_RADIUS,ymin-UNCERTAINTY_RADIUS,xmax+UNCERTAINTY_RADIUS,ymax+UNCERTAINTY_RADIUS)
    county_bounds=[]
    for _,county in counties.loc[counties.intersects(envelope)].iterrows():
        denominator=min(float(county["ALAND"]),county.geometry.area)
        require(denominator>0,"County sensitivity denominator missing")
        pp=error_upper/denominator*100
        require(pp<0.01,"Exclusion could materially affect a county percentage")
        county_bounds.append({"county_fips":str(county["GEOID"]),"name":str(county["NAME"]),
                              "maximum_uncertainty_m2":error_upper,"maximum_uncertainty_acres":error_upper/ACRE,
                              "conservative_sensitivity_denominator_m2":denominator,"maximum_percentage_point_uncertainty":pp,
                              "limit":"Entire global tube bound assigned to each envelope-intersecting county; not an adopted denominator or actual county error."})
    _,lines,points=parts(old)
    require(len(lines)==85 and not points,"Prior collapsed components differ")
    full=shapely.GeometryCollection([mask,*lines])
    require(full.is_valid and full.area==mask.area,"Full exclusion/nonarea components invalid")
    out.mkdir(parents=True)
    artifacts={}
    for name,g in [("conservative-exclusion.wkb",mask),("conservative-full.wkb",full),("boundary-uncertainty-band.wkb",uncertainty)]:
        body=shapely.to_wkb(g,byte_order=1)
        (out/name).write_bytes(body)
        artifacts[name]={"bytes":len(body),"sha256":hashlib.sha256(body).hexdigest()}
    inputs={p.relative_to(home).as_posix():{"bytes":p.stat().st_size,"sha256":digest_file(p)} for p in input_paths+geometry_paths}
    expected_tree=json.loads((audit.parent/"source-extraction-before.json").read_text(encoding="utf-8"))
    require(source_tree(home/"working/padus-4.1/PADUS4_1Geodatabase.gdb")==expected_tree,"Original extraction changed")
    require(digest_file(archive)==archive_hash and digest_file(audit)==diagnosis["previous_audit_sha256"],"Original/prior audit changed")
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"local_date":"2026-10-22","credit":"Margaret",
            "decision":"accepted_conservative_private_exclusion_only_not_exact_repair","source_layer":"PADUS4_1Fee","source_fid":77431,
            "source_attributes":attributes,"source_archive_sha256":archive_hash,"source_native_wkb_sha256":diagnosis["native_wkb_sha256"],
            "source_original_wkb_sha256":row["original_sha256"],"previous_candidate_wkb_sha256":row["repaired_sha256"],
            "previous_audit_sha256":diagnosis["previous_audit_sha256"],"exclusion_only":True,"eligible_as_greenspace":False,
            "exact_repair_accepted":False,"grid_size_m":GRID,"private_boundary_padding_m":PADDING,"uncertainty_tube_radius_m":UNCERTAINTY_RADIUS,
            "construction":"Fixed-grid source-positive coverage plus explicit padded convex rectangles around every fine-source segment; no line buffer used.",
            "supersedes":"The preserved v1 thin-line buffer produced a support area exceeding its analytic tube bound and is rejected. Only this separately audited v2 may be loaded.",
            "native_curve_boundary_error_bound_m":native_error,"maximum_grid_displacement_m":displacement,
            "maximum_required_radius_m":maximum_required_radius,"source_linework_length_m":linework.length,"native_arc_length_m":native_arc_length,
            "boundary_length_upper_bound_m":length_upper,"source_ring_count":ring_count,"global_area_uncertainty_upper_bound_m2":error_upper,
            "native_and_precision_tube_upper_bound_m2":tube_bound,"additional_mask_growth_upper_bound_m2":growth_bound,
            "global_area_uncertainty_upper_bound_acres":error_upper/ACRE,"county_uncertainty_bounds":county_bounds,
            "mask_area_m2":mask.area,"boundary_band_diagnostic_area_m2":uncertainty.area,"fixed_precision_comparisons":comparisons,
            "retained_nonarea_line_count":85,"retained_nonarea_line_sha256":[hashlib.sha256(shapely.to_wkb(g,byte_order=1)).hexdigest() for g in lines],
            "materiality_rule":{"maximum_county_cumulative_uncertainty_m2":ACRE,"maximum_county_cumulative_percentage_points":0.01,
                                "interpretation":"Engineering stop bounds for possible county changes, not statistical accuracy. Cumulative county bounds and active threshold ambiguity must remain explicit."},
            "input_artifacts":inputs,"artifacts":artifacts,"original_archive_extraction_and_prior_audit_unchanged":True,
            "scope":"Only exact Glades FID/native/default/candidate/attribute identities; conservative subtraction from otherwise eligible areas. No eligible area may be generated from this mask. Whole-record exact repair remains unaccepted.",
            "documentation":["https://shapely.readthedocs.io/en/2.1.2/reference/shapely.set_precision.html","https://shapely.readthedocs.io/en/2.1.2/reference/shapely.buffer.html"]}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in {"input_artifacts","artifacts","source_attributes","retained_nonarea_line_sha256"}}),flush=True)


if __name__=="__main__":
    main()
