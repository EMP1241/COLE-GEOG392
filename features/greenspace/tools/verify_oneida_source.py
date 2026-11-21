"""Check the missing state parcel against untouched PAD-US source records."""
import hashlib
import json
import numpy as np
import pyogrio
import shapely
import aggregate_counties as prep


def in_ring(point, coords):
    x,y=point
    a=np.asarray(coords)
    left,right=a[:-1],a[1:]
    crossings=(left[:,1]>y)!=(right[:,1]>y)
    left,right=left[crossings],right[crossings]
    return bool(np.count_nonzero(x < (right[:,0]-left[:,0])*(y-left[:,1])/(right[:,1]-left[:,1])+left[:,0])%2)


def in_polygon(point, geometry):
    return any(in_ring(point,p.exterior.coords) and not any(in_ring(point,r.coords) for r in p.interiors)
               for p in shapely.get_parts(geometry))


def main():
    target=prep.RUN/'oneida-investigation'
    if (target/'original-source-witness.json').exists():
        raise RuntimeError('Preserve the source witness receipt; use a separate output for replay')
    report=json.loads((target/'source-partition.json').read_text())
    point=report['lost_state_parts'][0]['point']
    with prep.ro(prep.RUN/'source_stage.sqlite3') as db:
        db.row_factory=__import__('sqlite3').Row
        rows=db.execute('SELECT s.* FROM source_records s JOIN county_sources c ON c.source_id=s.id WHERE c.county_fips=?',('16071',)).fetchall()
    witness=[]
    original_matches=0
    for layer in sorted({r['layer'] for r in rows}):
        subset={r['source_fid']:r for r in rows if r['layer']==layer}
        raw=pyogrio.read_dataframe(prep.HOME/'working/padus-4.1/PADUS4_1Geodatabase.gdb',layer=layer,fids=list(subset),fid_as_index=True,columns=prep.FIELDS)
        for fid,row in raw.iterrows():
            s=subset[int(fid)]
            body=shapely.to_wkb(row.geometry,byte_order=1)
            if hashlib.sha256(body).hexdigest()!=s['original_sha256']:
                raise RuntimeError('Original source mismatch')
            original_matches+=1
            if in_polygon(point,row.geometry):
                witness.append({'layer':layer,'source_fid':int(fid),'original_sha256':s['original_sha256'],
                                'attributes':json.loads(s['attributes_json']),'original_valid':row.geometry.is_valid,
                                'derived_repair_used':bool(s['repaired'])})
    owned=[r for r in witness if prep.flags(r['layer'],r['attributes'])[0]==2]
    blocked=[r for r in witness if any(prep.flags(r['layer'],r['attributes'])[2:])]
    opened=[r for r in witness if prep.flags(r['layer'],r['attributes'])[1]]
    if not owned or not opened or blocked:
        raise RuntimeError('Source witness does not confirm public open state land')
    result={'status':'passed','original_records_freshly_hash_matched':original_matches,
            'independent_even_odd_source_ring_witness':point,'containing_original_records':witness,
            'state_ownership_confirmed':True,'open_access_confirmed':True,'hard_exclusion_present':False,
            'limits':'Interior witness corroborates source membership; it is not a proof of every boundary or a global area-error bound.'}
    prep.write(target/'original-source-witness.json',result)
    print(json.dumps(result),flush=True)


if __name__=='__main__':
    main()
