"""Full-reference primary coverage sensitivity replay from staged source polygons.

Uses explicit 0.1-mm overlay precision for diagnostic comparison only. This is
not an independent source survey; the original inputs and accepted coordinates
are not modified. The replay is resumable and keeps every candidate geometry.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import sqlite3
from pathlib import Path
import time
import shapely
import aggregate_counties as p

GRID=0.0001
TARGET=p.RUN/'primary-overlay-sensitivity.sqlite3'


def primary(county):
    started=time.monotonic()
    code=str(county.GEOID)
    boundary=county.geometry
    xmin,ymin,xmax,ymax=boundary.bounds
    with p.ro(p.RUN/'source_stage.sqlite3') as source:
        rows=source.execute('SELECT s.owner_group,s.opened,s.private,s.military,s.blocked,s.geometry_wkb FROM source_records s JOIN county_sources c ON c.source_id=s.id WHERE c.county_fips=? ORDER BY s.id',(code,)).fetchall()
    owned,opened,excluded=[],[],[]
    for group,oa,private,military,blocked,body in rows:
        if not(group or oa or private or military or blocked):
            continue
        original=shapely.from_wkb(body)
        # Omit only complete polygon components whose bounding boxes cannot
        # reach this county, even after diagnostic snap displacement. Keep all
        # holes and every coordinate of retained components. This is not the
        # rejected per-component Boolean overlay: the single exact county
        # intersection below still operates on their full MultiPolygon.
        parts=shapely.get_parts(original)
        if len(parts)>1:
            bounds=shapely.bounds(parts)
            selected=parts[(bounds[:,2]>=xmin-2*GRID)&(bounds[:,0]<=xmax+2*GRID)&(bounds[:,3]>=ymin-2*GRID)&(bounds[:,1]<=ymax+2*GRID)]
            if len(selected)==0:
                continue
            original=selected[0] if len(selected)==1 else shapely.MultiPolygon(list(selected))
        part=p.polygonal(shapely.intersection(original,boundary,grid_size=GRID))
        if part.is_empty or part.area==0:
            continue
        if group: owned.append(part)
        if oa: opened.append(part)
        if private or military or blocked: excluded.append(part)
    def dissolve(items):
        return p.polygonal(shapely.union_all(items,grid_size=GRID)) if items else shapely.Polygon()
    public,open_area,exclusion=[dissolve(items) for items in (owned,opened,excluded)]
    candidate=p.polygonal(shapely.intersection(public,open_area,grid_size=GRID))
    eligible=p.polygonal(shapely.difference(candidate,exclusion,grid_size=GRID))
    if not eligible.is_valid:
        raise RuntimeError(f'Invalid diagnostic replay {code}')
    elapsed=time.monotonic()-started
    if elapsed>10:
        print(json.dumps({'phase':'primary_replay_slow_county','county':code,'seconds':elapsed}),flush=True)
    return code,eligible.area,shapely.to_wkb(eligible),boundary.area


def compare(db):
    with p.ro(p.RUN/p.RESULT_NAME) as accepted:
        metrics=accepted.execute('SELECT m.county_fips,m.eligible_m2,g.eligible_wkb,a.combined_review_sensitivity_pp FROM county_metrics m JOIN county_geometries g USING(county_fips) JOIN county_accounting a USING(county_fips)').fetchall()
    flags=[]
    done=0
    for code,area,body,allowance in metrics:
        row=db.execute('SELECT area_m2,geometry_wkb,county_area_m2 FROM primary_replays WHERE county_fips=?',(code,)).fetchone()
        if row is None:
            continue
        replay_area,replay_body,denominator=row
        difference=shapely.symmetric_difference(shapely.from_wkb(body),shapely.from_wkb(replay_body),grid_size=GRID).area
        combined=allowance+difference/denominator*100
        db.execute('INSERT OR REPLACE INTO comparisons VALUES(?,?,?,?,?,?,?)',(code,area,replay_area,abs(area-replay_area),difference,combined,int(combined>0.01)))
        if combined>0.01:
            flags.append({'county_fips':code,'native_m2':area,'alternative_m2':replay_area,'symmetric_difference_m2':difference,'combined_sensitivity_pp':combined})
        done+=1
    db.commit()
    report={'created_utc':p.now(),'status':'material_discrepancies' if flags else ('passed' if done==3234 else 'partial'),
            'compared_counties':done,'expected_counties':3234,'grid_m':GRID,'flagged_counties':flags,
            'maximum_observed_symmetric_difference_pp':db.execute('SELECT MAX(c.symmetric_difference_m2/r.county_area_m2*100) FROM comparisons c JOIN primary_replays r USING(county_fips)').fetchone()[0],
            'limits':'Numerical sensitivity replay using a different overlay order/precision in the same engine; not independent survey evidence or a complete error bound.'}
    p.write(p.RUN/'primary-overlay-sensitivity.json',report)
    print(json.dumps(report),flush=True)


def main():
    frame,_,_=p.counties()
    with sqlite3.connect(TARGET) as db:
        db.executescript('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value_json TEXT NOT NULL); CREATE TABLE IF NOT EXISTS primary_replays(county_fips TEXT PRIMARY KEY,area_m2 REAL NOT NULL,geometry_wkb BLOB NOT NULL,county_area_m2 REAL NOT NULL); CREATE TABLE IF NOT EXISTS comparisons(county_fips TEXT PRIMARY KEY,native_m2 REAL,alternative_m2 REAL,absolute_area_difference_m2 REAL,symmetric_difference_m2 REAL,combined_sensitivity_pp REAL,flag INTEGER);')
        identity={'script_sha256':p.sha(Path(__file__)),'stage_manifest_sha256':p.sha(p.RUN/'stage-manifest.json'),'grid_m':GRID,'census_sha256':p.CENSUS_SHA}
        old=db.execute("SELECT value_json FROM metadata WHERE key='identity'").fetchone()
        if old and json.loads(old[0])!=identity:
            raise RuntimeError('Existing replay identity differs')
        db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)',('identity',json.dumps(identity,sort_keys=True)))
        db.commit()
        complete={r[0] for r in db.execute('SELECT county_fips FROM primary_replays')}
        tasks=[row for _,row in frame.sort_values('GEOID').iterrows() if str(row.GEOID) not in complete]
        with ThreadPoolExecutor(max_workers=4) as pool:
            for future in as_completed([pool.submit(primary,row) for row in tasks]):
                result=future.result()
                db.execute('INSERT INTO primary_replays VALUES(?,?,?,?)',result)
                db.commit()
                complete.add(result[0])
                if len(complete)%50==0:
                    print(json.dumps({'phase':'primary_replay','counties':len(complete),'last':result[0]}),flush=True)
        # Final comparisons belong to finalize_primary_overlay_audit.py, which
        # binds current accepted WKB/accounting and charges precision changes.
        p.write(p.RUN/'primary-overlay-sensitivity.json',{'status':'primary_replays_complete',
            'primary_replay_counties':len(complete),'grid_m':GRID,'comparison':'Run finalize_primary_overlay_audit.py after aggregation and replay finish.'})
        print(json.dumps({'phase':'primary_replays_complete','counties':len(complete)}),flush=True)


if __name__=='__main__':
    main()
