"""Read-only original-source, exact-geometry, county and preservation audit."""

from datetime import datetime,timezone
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from osgeo import gdal
import pyogrio
import shapely

from accepted_adjudications import verify_inputs as verify_previous
from hawaii_exact_geometry import reconstruct,county_allowance,exact_polygon_area
from investigate_glades import winding_faces
from investigate_upper_snake import digest_file,require
from local_exclusions import verify_inputs as verify_local
from repair_geometry import source_tree


def main():
    home=Path(__file__).resolve().parents[1]
    dest=home/'verification/hawaii-resolution-audit.json'
    require(not dest.exists(),'Refuse to overwrite Hawaii acceptance audit')
    receipt_path=home/'verification/hawaii-resolution.json'
    receipt_hash=digest_file(receipt_path)
    receipt=json.loads(receipt_path.read_text())
    require(receipt['decision']=='accepted_specific_exact_source_winding_replacement_with_county_bound'
            and receipt['source_layer']=='PADUS4_1Fee' and receipt['source_fid']==199053
            and receipt['credit']=='Margaret' and receipt['negative_lobes_remain_excluded'],'Acceptance identity differs')
    root=home/'working/hawaii-resolution-2026-10-22'
    for relative,item in receipt['input_artifacts'].items():
        path=(home/relative).resolve()
        require(path.is_relative_to(home.resolve()),'Input path escapes feature')
        require(digest_file(path)==item['sha256'] and path.stat().st_size==item['bytes'],'Resolution input changed: '+relative)
    for name,item in receipt['accepted_artifacts'].items():
        path=(root/name).resolve()
        require(path.is_relative_to(root.resolve()),'Accepted path escapes resolution')
        require(digest_file(path)==item['sha256'] and path.stat().st_size==item['bytes'],'Accepted artifact changed')
    archive=home/'source/padus-4.1/PADUS4_1Geodatabase.zip'
    require(digest_file(archive)==receipt['source_archive_sha256']=='62ed113335e359509ad68a731146c74aa873c5a92da4f3fda572e54bc9b823ab','Original source changed')
    gdal.UseExceptions()
    ds=gdal.OpenEx('/vsizip/'+archive.resolve().as_posix()+'/PADUS4_1Geodatabase.gdb',gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=['OpenFileGDB'])
    feature=ds.GetLayerByName('PADUS4_1Fee').GetFeature(199053)
    native=feature.GetGeometryRef()
    body=bytes(native.ExportToWkb())
    require(not native.HasCurveGeometry(1),'Unexpected native curves')
    require(hashlib.sha256(body).hexdigest()==receipt['source_native_wkb_sha256']==receipt['source_original_wkb_sha256']=='7b27f4186b668f1b485e9fda382c6da639351e30ffd292b3114debb7c1b39580','Original geometry identity differs')
    require(all((None if feature.GetField(k) is None else str(feature.GetField(k)))==v for k,v in receipt['source_attributes'].items()),'Source attributes differ')
    source=shapely.from_wkb(body)
    audit=home/'working/national-prep-eastern-canal-2026-10-22/repair_audit.sqlite3'
    require(digest_file(audit)==receipt['previous_audit_sha256']=='8be0006ae997706efd15682646a949c2cd13afe9a86c4a94fa713f36034db178','Stopped audit changed')
    with sqlite3.connect(audit.resolve().as_uri()+'?mode=ro',uri=True) as c:
        row=c.execute("SELECT original_wkb,repaired_wkb,attributes_json FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=199053").fetchone()
    require(row[0]==body and json.loads(row[2])==receipt['source_attributes'],'Stored original differs')
    require(hashlib.sha256(row[1]).hexdigest()==receipt['previous_candidate_wkb_sha256'],'Prior candidate differs')
    candidate=shapely.from_wkb(row[1])
    print(json.dumps({'phase':'fresh_exact_reconstruction'}),flush=True)
    reconstructed,negative,noded,enriched,proof=reconstruct(source,candidate)
    accepted=shapely.from_wkb((root/'accepted-source-winding-replacement.wkb').read_bytes())
    require(shapely.to_wkb(reconstructed,byte_order=1)==shapely.to_wkb(accepted,byte_order=1),'Fresh exact source construction does not reproduce accepted bytes')
    require(json.loads(json.dumps(proof))==receipt['exact_source_proof'],'Exact source proof differs')
    require(accepted.is_valid and len(list(shapely.get_parts(accepted)))==2952,'Accepted geometry invalid or wrong part count')
    require(accepted.area==receipt['replacement_area_m2'],'Accepted area differs')
    require(shapely.to_wkb(negative,byte_order=1)==(root/'excluded-negative-winding-lobes.wkb').read_bytes(),'Excluded lobes differ')
    print(json.dumps({'phase':'fresh_independent_ray_angle_coverage'}),flush=True)
    rings=[list(r.coords) for p in source.geoms for r in [p.exterior,*p.interiors]]
    raw_noded,faces,positive,odd,excluded,zero=winding_faces(rings)
    require(accepted.symmetric_difference(positive).is_empty,'Fresh ray/angle coverage differs')
    require(candidate.symmetric_difference(odd).is_empty and negative.symmetric_difference(excluded).is_empty,'Odd/negative source coverage differs')
    require(accepted.intersection(zero).area==0 and accepted.intersection(excluded).area==0,'Unconfirmed or hole coverage included')
    require(accepted.boundary.difference(raw_noded).length==0,'Boundary absent from original linework')
    added=accepted.difference(candidate)
    removed=candidate.difference(accepted)
    require(removed.symmetric_difference(negative).is_empty,'Removed area not exactly unsupported negative winding')
    require(added.area==receipt['candidate_area_added_m2'] and removed.area==receipt['candidate_area_removed_m2'],'Directional coverage change differs')
    allowance=county_allowance(source,candidate,accepted,float(feature.GetField('SHAPE_Area')),proof)
    require(allowance==receipt['county_allowance_proof'],'Exact numerical/coverage allowance differs')
    total=allowance['total_outward_upper_m2']
    require(Fraction.from_float(total)>=Fraction(allowance['total_exact_m2']) and total<4046.8564224,'Outward bound invalid')
    census=home/'source/census-2021/tl_2021_us_county.zip'
    require(digest_file(census)=='26890ff92275ff1392cd205c7021ba510b7f63f2ff985ca327b9f5fd5e9ecc90','Census source changed')
    counties=pyogrio.read_dataframe(census,columns=['GEOID','NAME','ALAND']).to_crs('ESRI:102039')
    selected=counties.loc[counties.intersects(shapely.box(*source.bounds))]
    require(set(selected['GEOID'])=={c['county_fips'] for c in receipt['county_bounds']}=={'15001','15003','15005','15007','15009'},'County scope differs')
    journal=[json.loads(line) for line in (audit.parent/'repairs.jsonl').read_text().splitlines()]
    recomputed=[]
    for row in receipt['county_bounds']:
        code=row['county_fips']
        county=selected.loc[selected['GEOID']==code].iloc[0]
        other=math.fsum(a['allocated_review_allowance_m2'] for log in journal if (log['layer'],log['source_fid'])!=('PADUS4_1Fee',199053)
                        for a in log.get('county_review_allocations',[]) if a['county_fips']==code)
        denominator=min(float(county['ALAND']),county.geometry.area)
        clipped=candidate.intersection(county.geometry).symmetric_difference(accepted.intersection(county.geometry))
        require(float(exact_polygon_area(clipped))==row['diagnostic_county_clipped_symmetric_difference_m2'],'Clipped county change differs')
        require(float(exact_polygon_area(added.intersection(county.geometry)))==row['diagnostic_area_added_m2']
                and float(exact_polygon_area(removed.intersection(county.geometry)))==row['diagnostic_area_removed_m2'],'County directional area differs')
        require(other==row['other_checkpoint_review_allowance_m2'] and total+other==row['prospective_cumulative_allowance_m2'],'Cumulative county allowance differs')
        require(denominator==row['sensitivity_denominator_m2'] and total/denominator*100==row['maximum_percentage_point_effect'],'County denominator/sensitivity differs')
        require(total+other<4046.8564224 and (total+other)/denominator*100<0.01,'County review cap crossed')
        recomputed.append({'county_fips':code,'cumulative_m2':total+other,'percentage_points':(total+other)/denominator*100})
    coverage_change=candidate.symmetric_difference(accepted)
    require(coverage_change.difference(shapely.union_all(selected.geometry.array)).is_empty,'Change extends outside reviewed Census counties')
    original_tree=json.loads((audit.parent/'source-extraction-before.json').read_text())
    require(source_tree(home/'working/padus-4.1/PADUS4_1Geodatabase.gdb')==original_tree,'Original extraction changed')
    previous=json.loads((home/'verification/national-eastern-canal-preservation-final-2026-10-22.json').read_text())
    preserved=[]
    for item in previous['preserved_prior_audits']+[{'run':'national-prep-eastern-canal-2026-10-22','sha256':receipt['previous_audit_sha256']}]:
        path=home/'working'/item['run']/'repair_audit.sqlite3'
        require(digest_file(path)==item['sha256'],'Previous audit database changed')
        preserved.append({'run':item['run'],'sha256':item['sha256'],'unchanged':True})
    previous_inputs=verify_previous(home)
    local_inputs=verify_local(home)
    require(digest_file(archive)==receipt['source_archive_sha256'] and digest_file(audit)==receipt['previous_audit_sha256']
            and digest_file(receipt_path)==receipt_hash,'Source/receipt changed during audit')
    report={'created_utc':datetime.now(timezone.utc).isoformat(),'local_date':'2026-10-22','status':'passed','discrepancies':[],
            'credit':'Margaret','acceptance_receipt_sha256':receipt_hash,'direct_original_zip_geometry_and_attributes_verified':True,
            'fresh_exact_source_reconstruction_matches_accepted_bytes':True,'fresh_independent_ray_angle_region_matches':True,
            'exact_source_and_enriched_witnesses_checked':proof['exact_ray_witnesses_checked'],
            'whole_record_county_effect_allowance_m2':total,'whole_record_county_effect_allowance_acres':total/4046.8564224,
            'counties_recomputed':recomputed,'all_counties_within_both_cumulative_review_caps':True,
            'coverage_change_outside_reviewed_counties_m2':0.0,'original_archive_extraction_and_census_unchanged':True,
            'preserved_prior_audits':preserved,'previous_accepted_input_hashes_verified':len(previous_inputs),
            'glades_local_input_hashes_verified':len(local_inputs),'national_processing_restarted':False,'runtime_database_built':False,
            'source_repairs_or_prior_audit_changes_performed':False,
            'accepted_replacement_sha256':receipt['accepted_artifacts']['accepted-source-winding-replacement.wkb']['sha256']}
    with dest.open('x',encoding='utf-8') as stream:
        stream.write(json.dumps(report,indent=2,allow_nan=False)+'\n')
    print(json.dumps(report),flush=True)


if __name__=='__main__':
    main()
