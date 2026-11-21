"""Reproduce native Oneida failure, preserve operands, and compare precision models."""
import json
import math
from pathlib import Path
import time
import shapely
import aggregate_counties as prep
import importlib.util

current = prep
spec = importlib.util.spec_from_file_location('oneida_native_v6', current.RUN/'aggregate_counties-overlay-v6.py')
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)
prep.HOME, prep.RUN, prep.REPAIR = current.HOME, current.RUN, current.REPAIR


def main():
    target = prep.RUN / 'oneida-investigation'
    target.mkdir()
    frame, core, _ = prep.counties()
    county = frame.loc[frame.GEOID == '16071'].iloc[0]
    allowances = json.loads((prep.REPAIR / 'repair-summary.json').read_text())['county_review_allowances']
    try:
        prep.calculate_county(county, core, allowances)
        raise RuntimeError('Expected historical failure was not reproduced')
    except RuntimeError as error:
        if 'combined accounting sensitivity' not in str(error):
            raise
        cursor = error.__traceback__
        while cursor:
            if cursor.tb_frame.f_code.co_name == 'calculate_county':
                native = dict(cursor.tb_frame.f_locals)
            cursor = cursor.tb_next
    operands = {k: native[k] for k in ('boundary', 'public', 'before', 'eligible', 'excluded', 'shared')}
    operands.update({f'group-{i}': g for i, g in enumerate(native['groups'])})
    operands.update({f'exclusive-{i}': g for i, g in enumerate(native['exclusive'])})
    report = {'county_fips': '16071', 'native_code_sha256': prep.sha(Path(prep.__file__)),
              'versions': {'shapely': shapely.__version__, 'geos': shapely.geos_version_string},
              'native': {}, 'precision_comparisons': []}
    for name, g in operands.items():
        (target / (name+'.wkb')).write_bytes(shapely.to_wkb(g))
        report['native'][name] = {'area_m2': g.area, 'valid': g.is_valid, 'coordinates': int(shapely.get_num_coordinates(g))}
    report['native']['signed_partition_residual_m2'] = sum(g.area for g in native['exclusive']) + native['shared'].area - native['eligible'].area
    prep.write(target / 'investigation.json', report)
    print(json.dumps({'phase':'native', **report['native']}), flush=True)
    for grid in (0.0001, 0.001, 0.01):
        prep.union = lambda items, grid=grid: prep.polygonal(shapely.union_all(items, grid_size=grid)) if len(items) else shapely.Polygon()
        prep.intersect = lambda a,b,grid=grid: prep.polygonal(shapely.intersection(a,b,grid_size=grid))
        prep.subtract = lambda a,b,grid=grid: prep.polygonal(shapely.difference(a,b,grid_size=grid))
        start=time.monotonic()
        result = prep.calculate_county(county, core, allowances)
        metrics, geometries, evidence, accounting = result
        g = shapely.from_wkb(geometries[1])
        (target / f'eligible-grid-{grid}.wkb').write_bytes(geometries[1])
        entry = {'grid_m':grid,'seconds':time.monotonic()-start,'eligible_m2':metrics[7], 'eligible_percent':metrics[9],
                 'ownership_areas_m2':list(metrics[10:15]), 'accounting':list(accounting),
                 'main_area_difference_m2':metrics[7]-native['eligible'].area,
                 'native_eligible_symmetric_difference_m2':shapely.symmetric_difference(g,native['eligible'],grid_size=grid).area}
        report['precision_comparisons'].append(entry)
        prep.write(target / 'investigation.json', report)
        print(json.dumps({'phase':'precision',**entry}),flush=True)


if __name__ == '__main__':
    main()
