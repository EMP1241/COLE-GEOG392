"""Bind the national sensitivity comparison to exact final county geometries.

Preserves the earlier diagnostic outputs. Charges both absolute area difference
and grid-based symmetric difference, with the current county accounting ledger.
"""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import shapely
import aggregate_counties as p


def main():
    target=p.RUN/'primary-overlay-acceptance.sqlite3'
    report_path=p.RUN/'primary-overlay-acceptance.json'
    if target.exists() or report_path.exists():
        raise RuntimeError('Existing final comparison receipt must be preserved')
    if json.loads((p.RUN/'aggregation-status.json').read_text())['status']!='aggregated':
        raise RuntimeError('National county calculation is not complete')
    with p.ro(p.RUN/'primary-overlay-sensitivity.sqlite3') as replay:
        if replay.execute('SELECT COUNT(*) FROM primary_replays').fetchone()[0]!=3234:
            raise RuntimeError('National primary replay incomplete')
        replay_identity=json.loads(replay.execute("SELECT value_json FROM metadata WHERE key='identity'").fetchone()[0])
        script=Path(__file__).with_name('audit_primary_overlay.py')
        if p.sha(script)!=replay_identity['script_sha256']:
            script=p.RUN/'audit_primary_overlay-v2.py'
        expected_identity={'script_sha256':p.sha(script),
                           'stage_manifest_sha256':p.sha(p.RUN/'stage-manifest.json'),
                           'grid_m':.0001,'census_sha256':p.CENSUS_SHA}
        if replay_identity!=expected_identity:
            raise RuntimeError('National primary replay source/script/grid identity differs')
    print(json.dumps({'phase':'final_comparison_input_hashes'}),flush=True)
    identity={'native_database_sha256':p.sha(p.RUN/p.RESULT_NAME),
              'replay_database_sha256':p.sha(p.RUN/'primary-overlay-sensitivity.sqlite3'),
              'aggregation_code_sha256':p.sha(Path(p.__file__)),
              'comparison_script_sha256':p.sha(Path(__file__)),
              'stage_manifest_sha256':p.sha(p.RUN/'stage-manifest.json'), 'grid_m':.0001,
              'replay_identity':replay_identity,'replay_script':str(script.relative_to(p.HOME))}
    flags=[]
    count=0
    maximum=None
    with p.ro(p.RUN/p.RESULT_NAME) as native, p.ro(p.RUN/'primary-overlay-sensitivity.sqlite3') as replay, sqlite3.connect(target) as dest:
        dest.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value_json TEXT NOT NULL); CREATE TABLE comparisons(county_fips TEXT PRIMARY KEY,native_m2 REAL,alternative_m2 REAL,absolute_area_difference_m2 REAL,symmetric_difference_m2 REAL,review_charge_m2 REAL,current_accounting_pp REAL,combined_sensitivity_pp REAL,flag INTEGER,native_geometry_sha256 TEXT,replay_geometry_sha256 TEXT,accounting_json TEXT,comparison_precision_reserve_m2 REAL,input_snapping_area_charge_m2 REAL);')
        dest.execute('INSERT INTO metadata VALUES(?,?)',('identity',json.dumps(identity,sort_keys=True)))
        for code,area,denominator,body in native.execute('SELECT m.county_fips,m.eligible_m2,m.county_area_m2,g.eligible_wkb FROM county_metrics m JOIN county_geometries g USING(county_fips) ORDER BY m.county_fips'):
            result=replay.execute('SELECT area_m2,geometry_wkb,county_area_m2 FROM primary_replays WHERE county_fips=?',(code,)).fetchone()
            if result is None or result[2]!=denominator:
                raise RuntimeError(f'Replay identity mismatch {code}')
            alt_area,alt_body,_=result
            accounting=native.execute('SELECT * FROM county_accounting WHERE county_fips=?',(code,)).fetchone()
            absolute=abs(area-alt_area)
            original,alternative=shapely.from_wkb(body),shapely.from_wkb(alt_body)
            normalized=[shapely.set_precision(g,.0001) for g in (original,alternative)]
            if not all(g.is_valid for g in normalized):raise RuntimeError(f'Invalid comparison precision copy {code}')
            symmetric=shapely.symmetric_difference(*normalized,grid_size=.0001).area
            snapping=sum(abs(g.area-n.area) for g,n in zip((original,alternative),normalized))
            radius=8*.0001
            reserve=2*radius*(original.length+alternative.length)+math.pi*radius*radius*sum(int(shapely.get_num_coordinates(g)) for g in (original,alternative))
            charge=absolute+symmetric+snapping+reserve
            if any(not math.isfinite(v) or v<0 for v in (absolute,symmetric,snapping,reserve,charge)):
                raise RuntimeError(f'Invalid comparison discrepancy {code}')
            combined=accounting[-1]+charge/denominator*100
            entry={'county_fips':code,'absolute_area_difference_m2':absolute,'symmetric_difference_m2':symmetric,'additional_review_pp':charge/denominator*100,'combined_sensitivity_pp':combined}
            if maximum is None or entry['combined_sensitivity_pp']>maximum['combined_sensitivity_pp']:maximum=entry
            if combined>.01:flags.append(entry)
            dest.execute('INSERT INTO comparisons VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (code,area,alt_area,absolute,symmetric,charge,accounting[-1],combined,int(combined>.01),hashlib.sha256(body).hexdigest(),hashlib.sha256(alt_body).hexdigest(),json.dumps(accounting),reserve,snapping))
            count+=1
            if count%100==0:
                dest.commit()
                print(json.dumps({'phase':'final_primary_comparison','counties':count,'flags':len(flags)}),flush=True)
        dest.commit()
        if count!=3234 or dest.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise RuntimeError('Final comparison coverage/integrity differs')
    p.check(p.RUN/p.RESULT_NAME,identity['native_database_sha256'])
    p.check(p.RUN/'primary-overlay-sensitivity.sqlite3',identity['replay_database_sha256'])
    report={'status':'material_discrepancies' if flags else 'passed','created_utc':p.now(),
            'compared_counties':count,'flagged_counties':flags,'maximum_combined_sensitivity':maximum,
            'identity':identity,'comparison_database_sha256':p.sha(target),
            'charge':'Current county sensitivity plus absolute area difference, explicitly grid-normalized symmetric difference, absolute input snapping area changes and eight-grid boundary-neighborhood engineering reserve, without cancellation',
            'limits':'Same-engine alternative precision/order sensitivity, not independent source accuracy or a complete error bound.'}
    p.write(report_path,report)
    print(json.dumps(report),flush=True)


if __name__=='__main__':
    main()
