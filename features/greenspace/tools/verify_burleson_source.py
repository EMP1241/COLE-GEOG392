"""Reproduce the Burleson false-contact witness against original source rings."""
import hashlib
import json
import sqlite3
import pyogrio
import shapely
import aggregate_counties as p
from verify_oneida_source import in_polygon


def main():
    root=p.RUN/'county-diagnosis-48051'
    target=root/'source-contact-witness.json'
    if target.exists():
        raise RuntimeError('Preserve existing witness; use a separate output for replay')
    eligible=shapely.from_wkb((root/'eligible.wkb').read_bytes())
    excluded=shapely.from_wkb((root/'excluded.wkb').read_bytes())
    fine=shapely.from_wkb((root/'eligible-grid-0.0001.wkb').read_bytes())
    contact=shapely.intersection(eligible,excluded)
    point=list(contact.representative_point().coords[0])
    report={'native_contact_m2':contact.area,'contact_point':point,
            'ray_in_native_eligible':in_polygon(point,eligible),
            'ray_in_native_excluded':in_polygon(point,excluded),
            'grid_contact_m2':shapely.intersection(eligible,excluded,grid_size=.0001).area,
            'native_to_fine_symmetric_difference_m2':shapely.symmetric_difference(eligible,fine,grid_size=.0001).area,
            'raw_source_witness':[]}
    with p.ro(p.RUN/'source_stage.sqlite3') as db:
        db.row_factory=sqlite3.Row
        rows=db.execute('SELECT s.* FROM source_records s JOIN county_sources c ON c.source_id=s.id WHERE c.county_fips=?',('48051',)).fetchall()
    for layer in sorted({r['layer'] for r in rows}):
        lookup={r['source_fid']:r for r in rows if r['layer']==layer}
        frame=pyogrio.read_dataframe(p.HOME/'working/padus-4.1/PADUS4_1Geodatabase.gdb',layer=layer,fids=list(lookup),fid_as_index=True,columns=p.FIELDS)
        for fid,row in frame.iterrows():
            source=lookup[int(fid)]
            if hashlib.sha256(shapely.to_wkb(row.geometry,byte_order=1)).hexdigest()!=source['original_sha256']:
                raise RuntimeError('Original source changed')
            if in_polygon(point,row.geometry):
                report['raw_source_witness'].append({'layer':layer,'fid':int(fid),'attrs':json.loads(source['attributes_json']),'valid':row.geometry.is_valid})
    report['fresh_original_record_hash_matches']=len(rows)
    if report['ray_in_native_eligible'] or not report['ray_in_native_excluded']:
        raise RuntimeError('False-contact witness was not reproduced')
    if not any(r['attrs']['Pub_Access']=='RA' for r in report['raw_source_witness']):
        raise RuntimeError('Restricted original coverage was not confirmed')
    p.write(target,report)
    print(json.dumps(report),flush=True)


if __name__=='__main__':main()
