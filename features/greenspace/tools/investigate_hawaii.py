"""Read-only direct-original profile of Hawaii Fee 199053; no accepted repair."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from osgeo import gdal
import shapely

from investigate_rings import cycles, signed_area
from investigate_upper_snake import digest_file, require


def main():
    home=Path(__file__).resolve().parents[1]
    out=home/'working/hawaii-investigation-2026-10-22'
    dest=home/'verification/hawaii-diagnosis.json'
    require(not out.exists() and not dest.exists(),'Refuse to overwrite Hawaii evidence')
    archive=home/'source/padus-4.1/PADUS4_1Geodatabase.zip'
    audit=home/'working/national-prep-eastern-canal-2026-10-22/repair_audit.sqlite3'
    require(digest_file(archive)=='62ed113335e359509ad68a731146c74aa873c5a92da4f3fda572e54bc9b823ab','Original ZIP changed')
    require(digest_file(audit)=='8be0006ae997706efd15682646a949c2cd13afe9a86c4a94fa713f36034db178','Stopped audit changed')
    with sqlite3.connect(audit.resolve().as_uri()+'?mode=ro',uri=True) as connection:
        connection.row_factory=sqlite3.Row
        row=connection.execute("SELECT * FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=199053").fetchone()
    attrs=json.loads(row['attributes_json'])
    gdal.UseExceptions()
    ds=gdal.OpenEx('/vsizip/'+archive.resolve().as_posix()+'/PADUS4_1Geodatabase.gdb',gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=['OpenFileGDB'])
    feature=ds.GetLayerByName('PADUS4_1Fee').GetFeature(199053)
    native=feature.GetGeometryRef()
    native_body=bytes(native.ExportToWkb())
    body=bytes(native.GetLinearGeometry().ExportToWkb())
    require(hashlib.sha256(body).hexdigest()==row['original_sha256'],'Direct source geometry differs')
    require(all((None if feature.GetField(k) is None else str(feature.GetField(k)))==v for k,v in attrs.items()),'Source attributes differ')
    source=shapely.from_wkb(body)
    candidate=shapely.from_wkb(row['polygon_wkb'])
    ring_rows=[]
    all_cycles=[]
    for pi,p in enumerate(source.geoms):
        for ri,r in enumerate([p.exterior,*p.interiors]):
            coords=list(r.coords)
            decomposed=cycles(coords)
            ring_rows.append({'polygon':pi,'ring':ri,'coordinates':len(coords),'signed_area_m2':signed_area(coords),'valid':bool(shapely.Polygon(coords).is_valid),'cycles':len(decomposed),'bounds':list(shapely.Polygon(coords).bounds)})
            for c in decomposed:
                all_cycles.append({'polygon':pi,'ring':ri,'coordinates':len(c),'signed_area_m2':signed_area(c),'valid':bool(shapely.Polygon(c).is_valid),'reason':shapely.is_valid_reason(shapely.Polygon(c)),'bounds':list(shapely.Polygon(c).bounds)})
    arcs=[]
    def visit(g,path):
        if g.GetGeometryName()=='CIRCULARSTRING':
            arcs.append({'path':path,'controls':g.GetPoints()})
        for i in range(g.GetGeometryCount()):
            visit(g.GetGeometryRef(i),path+[i])
    visit(native,[])
    signed=-math.fsum(r['signed_area_m2'] for r in ring_rows)
    promoted=math.fsum(r['signed_area_m2'] for r in ring_rows if r['ring']==0 and r['signed_area_m2']>0)
    wrong_inner=math.fsum(abs(r['signed_area_m2']) for r in ring_rows if r['ring']>0 and r['signed_area_m2']<0)
    report={'created_utc':datetime.now(timezone.utc).isoformat(),'local_date':'2026-10-22','credit':'Margaret',
            'decision':'diagnosis_only_not_accepted','source_layer':'PADUS4_1Fee','source_fid':199053,'source_attributes':attrs,
            'source_archive_sha256':digest_file(archive),'previous_audit_sha256':digest_file(audit),
            'source_native_wkb_sha256':hashlib.sha256(native_body).hexdigest(),'source_original_wkb_sha256':row['original_sha256'],
            'previous_candidate_wkb_sha256':row['repaired_sha256'],'candidate_polygon_wkb_sha256':row['polygon_sha256'],
            'direct_original_source_and_attributes_match':True,'actual_nonlinear_segments':bool(native.HasCurveGeometry(1)),
            'native_circular_strings':arcs,'source_polygon_parts':len(source.geoms),'source_rings':len(ring_rows),'source_coordinates':sum(r['coordinates'] for r in ring_rows),
            'source_cycles':len(all_cycles),'invalid_source_cycles':[r for r in all_cycles if not r['valid']],
            'source_signed_area_m2':signed,'promoted_exterior_slot_area_m2':promoted,'wrong_clockwise_interior_area_m2':wrong_inner,
            'slot_accounting_residual_m2':source.area-signed-2*(promoted-wrong_inner),
            'original_decoded_area_m2':source.area,'candidate_area_m2':candidate.area,'publisher_area_m2':float(attrs['SHAPE_Area']),
            'candidate_minus_signed_m2':candidate.area-signed,'rings':ring_rows,'artifacts':{}}
    out.mkdir(parents=True)
    for name,data in [('original-native.wkb',native_body),('original-native.wkt',native.ExportToIsoWkt().encode()),('original-default.wkb',body)]:
        (out/name).write_bytes(data)
        report['artifacts'][name]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    require(digest_file(archive)==report['source_archive_sha256'] and digest_file(audit)==report['previous_audit_sha256'],'Preserved inputs changed')
    report['original_archive_and_previous_audit_unchanged']=True
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'rings','native_circular_strings','artifacts'}}),flush=True)
    print(json.dumps({'native_circular_string_count':len(arcs)}),flush=True)


if __name__=='__main__':
    main()
