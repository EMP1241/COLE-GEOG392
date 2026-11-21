"""Construct a specific original-source Hawaii replacement and county bound."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from osgeo import gdal
import pyogrio
import shapely

from hawaii_exact_geometry import reconstruct,county_allowance,exact_polygon_area
from investigate_upper_snake import digest_file,require
from repair_geometry import source_tree

ACRE=4046.8564224


def main():
    home=Path(__file__).resolve().parents[1]
    out=home/'working/hawaii-resolution-2026-10-22'
    dest=home/'verification/hawaii-resolution.json'
    require(not out.exists() and not dest.exists(),'Refuse to overwrite Hawaii resolution')
    diagnosis_path=home/'verification/hawaii-diagnosis.json'
    coverage_path=home/'verification/hawaii-coverage-diagnosis.json'
    diagnosis=json.loads(diagnosis_path.read_text())
    coverage=json.loads(coverage_path.read_text())
    archive=home/'source/padus-4.1/PADUS4_1Geodatabase.zip'
    audit=home/'working/national-prep-eastern-canal-2026-10-22/repair_audit.sqlite3'
    require(digest_file(archive)==diagnosis['source_archive_sha256'] and digest_file(audit)==diagnosis['previous_audit_sha256'],'Preserved source/audit changed')
    with sqlite3.connect(audit.resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        row=c.execute("SELECT * FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=199053").fetchone()
    attrs=json.loads(row['attributes_json'])
    gdal.UseExceptions()
    ds=gdal.OpenEx('/vsizip/'+archive.resolve().as_posix()+'/PADUS4_1Geodatabase.gdb',gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=['OpenFileGDB'])
    feature=ds.GetLayerByName('PADUS4_1Fee').GetFeature(199053)
    native=feature.GetGeometryRef()
    body=bytes(native.ExportToWkb())
    require(not native.HasCurveGeometry(1) and hashlib.sha256(body).hexdigest()==row['original_sha256']==diagnosis['source_native_wkb_sha256'],'Direct native source differs')
    require(all((None if feature.GetField(k) is None else str(feature.GetField(k)))==v for k,v in attrs.items()),'Direct source attributes differ')
    source=shapely.from_wkb(body)
    candidate=shapely.from_wkb(row['polygon_wkb'])
    print(json.dumps({'phase':'exact_source_reconstruction'}),flush=True)
    replacement,excluded,noded,enriched,proof=reconstruct(source,candidate)
    positive=shapely.from_wkb((home/'working/hawaii-coverage-2026-10-22/source-positive-region.wkb').read_bytes())
    negative=shapely.from_wkb((home/'working/hawaii-coverage-2026-10-22/source-negative-region.wkb').read_bytes())
    require(replacement.symmetric_difference(positive).is_empty,'Exact construction differs from ray/angle coverage')
    require(excluded.symmetric_difference(negative).is_empty,'Excluded source lobes differ')
    added=replacement.difference(candidate)
    removed=candidate.difference(replacement)
    require(removed.symmetric_difference(excluded).is_empty,'Removed coverage is not exactly negative winding')
    require(added.area==coverage['positive_minus_candidate_m2'] and removed.area==coverage['candidate_minus_positive_m2'],'Directional change differs')
    require(replacement.boundary.difference(noded).length==0,'Replacement boundary absent from source arrangement')
    allowance=county_allowance(source,candidate,replacement,float(attrs['SHAPE_Area']),proof)
    total=allowance['total_outward_upper_m2']
    county_zip=home/'source/census-2021/tl_2021_us_county.zip'
    require(digest_file(county_zip)=='26890ff92275ff1392cd205c7021ba510b7f63f2ff985ca327b9f5fd5e9ecc90','County source changed')
    counties=pyogrio.read_dataframe(county_zip,columns=['GEOID','NAME','ALAND']).to_crs('ESRI:102039')
    support=shapely.box(*source.bounds)
    journal=[json.loads(line) for line in (audit.parent/'repairs.jsonl').read_text().splitlines()]
    county_bounds=[]
    for _,county in counties.loc[counties.intersects(support)].iterrows():
        code=str(county['GEOID'])
        others=math.fsum(a['allocated_review_allowance_m2'] for log in journal
                         if (log['layer'],log['source_fid'])!=('PADUS4_1Fee',199053)
                         for a in log.get('county_review_allocations',[]) if a['county_fips']==code)
        denominator=min(float(county['ALAND']),county.geometry.area)
        before=candidate.intersection(county.geometry)
        after=replacement.intersection(county.geometry)
        clipped=before.symmetric_difference(after)
        require(total+others<ACRE and (total+others)/denominator*100<0.01,'Prospective cumulative county bound is material')
        county_bounds.append({'county_fips':code,'name':str(county['NAME']),'full_record_allowance_m2':total,
                              'diagnostic_county_clipped_symmetric_difference_m2':float(exact_polygon_area(clipped)),
                              'diagnostic_area_added_m2':float(exact_polygon_area(added.intersection(county.geometry))),
                              'diagnostic_area_removed_m2':float(exact_polygon_area(removed.intersection(county.geometry))),
                              'sensitivity_denominator_m2':denominator,'maximum_percentage_point_effect':total/denominator*100,
                              'other_checkpoint_review_allowance_m2':others,'prospective_cumulative_allowance_m2':total+others,
                              'prospective_cumulative_allowance_acres':(total+others)/ACRE,'prospective_cumulative_percentage_points':(total+others)/denominator*100})
    require({c['county_fips'] for c in county_bounds}=={'15001','15003','15005','15007','15009'},'County scope differs')
    original_tree=json.loads((audit.parent/'source-extraction-before.json').read_text())
    require(source_tree(home/'working/padus-4.1/PADUS4_1Geodatabase.gdb')==original_tree,'Original extraction changed')
    require(digest_file(archive)==diagnosis['source_archive_sha256'] and digest_file(audit)==diagnosis['previous_audit_sha256'],'Preserved inputs changed')
    out.mkdir(parents=True)
    artifacts={}
    for name,g in [('accepted-source-winding-replacement.wkb',replacement),('excluded-negative-winding-lobes.wkb',excluded),('source-exact-noded-linework.wkb',noded),('source-supported-overlap-added.wkb',added)]:
        b=shapely.to_wkb(g,byte_order=1)
        (out/name).write_bytes(b)
        artifacts[name]={'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}
    inputs={}
    for path in [diagnosis_path,coverage_path,home/'tools/hawaii_exact_geometry.py',Path(__file__).resolve()]:
        inputs[path.relative_to(home).as_posix()]={'bytes':path.stat().st_size,'sha256':digest_file(path)}
    for folder,receipt in [('hawaii-investigation-2026-10-22',diagnosis),('hawaii-coverage-2026-10-22',coverage)]:
        for name,item in receipt['artifacts'].items():
            path=home/'working'/folder/name
            require(digest_file(path)==item['sha256'],'Investigation artifact changed')
            inputs[path.relative_to(home).as_posix()]=item
    report={'created_utc':datetime.now(timezone.utc).isoformat(),'local_date':'2026-10-22','credit':'Margaret',
            'decision':'accepted_specific_exact_source_winding_replacement_with_county_bound',
            'source_layer':'PADUS4_1Fee','source_fid':199053,'source_attributes':attrs,
            'source_archive_sha256':diagnosis['source_archive_sha256'],'source_native_wkb_sha256':diagnosis['source_native_wkb_sha256'],
            'source_original_wkb_sha256':row['original_sha256'],'previous_candidate_wkb_sha256':row['repaired_sha256'],
            'previous_audit_sha256':diagnosis['previous_audit_sha256'],'exact_source_proof':proof,'county_allowance_proof':allowance,
            'replacement_area_m2':replacement.area,'replacement_parts':len(list(shapely.get_parts(replacement))),
            'candidate_area_added_m2':added.area,'candidate_area_removed_m2':removed.area,
            'candidate_symmetric_difference_m2':replacement.symmetric_difference(candidate).area,
            'negative_lobes_remain_excluded':True,'county_bounds':county_bounds,'accepted_artifacts':artifacts,'input_artifacts':inputs,
            'source_positive_coverage_symmetric_difference_m2':0.0,'source_archive_extraction_and_stopped_audit_unchanged':True,
            'national_processing_restarted':False,'exclusions_unchanged':True,
            'rejected_intermediate_method':'Smallest-containing-shell assignment is unsuitable here: an original CCW cycle of 999.679119 m2 partly crosses its shell. Use full oriented source winding; no whole-hole shortcut is accepted.',
            'method':'Exact rational segment intersections, no original vertex movement; exact integer ray classifications on original and enriched source agree at all 3273 face witnesses, independently matching floating ray/angle source coverage. Positive winding counted once; negative and zero winding excluded.',
            'allowance_interpretation':'Assign the complete record allowance to every envelope-intersecting county, add earlier allowances without cancellation, and substitute for the stopped raw diagnostic rather than adding both. Includes both exact numerical tube bounds, complete candidate symmetric difference and full publisher-area discrepancy.',
            'limits':'Bounds mapped coverage relative to this original source, not surveyed ownership, access or unknown positional accuracy. Publisher discrepancy is extra caution. Consistent exact county clipping/union/exclusion cannot enlarge symmetric difference; downstream numerical processing still requires its own checks. Final county statistics and runtime remain incomplete.',
            'documentation':['https://doc.esri.com/en/arcgis-pro/latest/arcpy/get-started/reading-geometries.html','https://shapely.readthedocs.io/en/2.1.2/reference/shapely.make_valid.html']}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'exact_source_proof','input_artifacts','accepted_artifacts'}}),flush=True)


if __name__=='__main__':
    main()
