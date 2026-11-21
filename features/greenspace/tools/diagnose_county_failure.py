"""Preserve operands of a new county numerical failure without accepting it."""
import argparse
import json
import math
from pathlib import Path
import types
import shapely
import aggregate_counties as p


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('fips')
    args=parser.parse_args()
    target=p.RUN/f'county-diagnosis-{args.fips}'
    target.mkdir()
    frame,core,_=p.counties()
    county=frame.loc[frame.GEOID==args.fips].iloc[0]
    allowances=json.loads((p.REPAIR/'repair-summary.json').read_text())['county_review_allowances']
    try:
        p.calculate_county_native(county,core,allowances)
    except RuntimeError as error:
        cursor=error.__traceback__
        while cursor:
            if cursor.tb_frame.f_code is p.calculate_county_native.__code__:
                s=dict(cursor.tb_frame.f_locals)
            cursor=cursor.tb_next
    report={'county_fips':args.fips,'code_sha256':p.sha(Path(p.__file__)),'numeric':{k:s[k] for k in ('residual','numeric_tolerance','excluded_contact','outside_public','outside_county','denominator','combined_sensitivity')},'areas':{},'scalar_categories':[],'replays':[]}
    for name in ('eligible','public','excluded','shared','before','boundary'):
        geom=s[name]
        (target/f'{name}.wkb').write_bytes(shapely.to_wkb(geom))
        report['areas'][name]=geom.area
    other=p.union([s['groups'][i] for i in s['others']])
    areas=[0.0]*4
    areas[s['pivot']]=s['eligible'].area-p.intersect(s['eligible'],other).area
    for i in s['others']:
        g=s['clipped_groups'][i]
        areas[i]=g.area-p.intersect(g,s['shared']).area
    report['scalar_categories']=areas
    report['pivot']=s['pivot']
    report['group_areas']=[g.area for g in s['groups']]
    report['clipped_areas']={i:g.area for i,g in s['clipped_groups'].items()}
    report['native_exclusive_areas']=[g.area for g in s['exclusive']]
    p.write(target/'diagnosis.json',report)
    print(json.dumps(report),flush=True)
    for grid in (.0001,.001,.01):
        namespace=dict(vars(p))
        namespace['union']=lambda items,g=grid:p.polygonal(shapely.union_all(items,grid_size=g)) if len(items) else shapely.Polygon()
        namespace['intersect']=lambda a,b,g=grid:p.polygonal(shapely.intersection(a,b,grid_size=g))
        namespace['subtract']=lambda a,b,g=grid:p.polygonal(shapely.difference(a,b,grid_size=g))
        calc=types.FunctionType(p.calculate_county_native.__code__,namespace)
        try:
            metrics,geometries,_,accounting=calc(county,core,allowances)
            (target/f'eligible-grid-{grid}.wkb').write_bytes(geometries[1])
            entry={'grid':grid,'eligible_m2':metrics[7],'categories':list(metrics[10:15]),'accounting':accounting}
        except Exception as error:
            entry={'grid':grid,'error':str(error)}
        report['replays'].append(entry)
        p.write(target/'diagnosis.json',report)
        print(json.dumps(entry),flush=True)


if __name__=='__main__':
    main()
