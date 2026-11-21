"""Compare equivalent ownership identities against preserved source geometry."""
import inspect
import json
import shapely
from investigate_oneida import prep


def main():
    target = prep.RUN / 'oneida-investigation'
    if (target/'source-partition.json').exists():
        raise RuntimeError('Preserve the recorded investigation; use a separate output for replay')
    frame, core, _ = prep.counties()
    county = frame.loc[frame.GEOID == '16071'].iloc[0]
    allowances = json.loads((prep.REPAIR / 'repair-summary.json').read_text())['county_review_allowances']
    try:
        prep.calculate_county(county, core, allowances)
    except RuntimeError as error:
        cursor = error.__traceback__
        while cursor:
            if cursor.tb_frame.f_code.co_name == 'calculate_county':
                scope = dict(cursor.tb_frame.f_locals)
            cursor = cursor.tb_next
    groups, regions, eligible, excluded = [scope[k] for k in ('groups','regions','eligible','excluded')]
    # Independently establish each owner's eligible footprint from ownership,
    # open-access evidence, and hard exclusions, before pairwise overlap.
    owner_eligible = [prep.subtract(prep.intersect(g, regions['open']), excluded) for g in groups]
    shared = prep.union([prep.intersect(a,b) for i,a in enumerate(owner_eligible) for b in owner_eligible[i+1:]])
    exclusive = [prep.subtract(g, shared) for g in owner_eligible]
    report = {'method':'Each ownership group intersect OA minus hard exclusions; pairwise shared; exclusive subtracts shared',
              'eligible_m2':eligible.area,'owner_eligible_m2':[g.area for g in owner_eligible],
              'shared_m2':shared.area,'exclusive_m2':[g.area for g in exclusive],
              'signed_partition_residual_m2':sum(g.area for g in exclusive)+shared.area-eligible.area,
              'owner_union_vs_eligible_symmetric_difference_grid_0_0001_m2':shapely.symmetric_difference(prep.union(owner_eligible),eligible,grid_size=.0001).area}
    native_clipped = scope['clipped_groups']
    scalar_exclusive = [g.area for g in scope['exclusive']]
    scalar_details = []
    for i in scope['others']:
        original = native_clipped[i]
        contact = prep.intersect(original,scope['shared'])
        scalar_exclusive[i] = original.area-contact.area
        scalar_details.append({'group':i,'clipped_area_m2':original.area,'shared_contact_m2':contact.area,
                               'native_difference_m2':scope['exclusive'][i].area,
                               'expected_difference_by_area_identity_m2':scalar_exclusive[i]})
    report['native_scalar_details'] = scalar_details
    report['native_scalar_partition_residual_m2'] = sum(scalar_exclusive)+scope['shared'].area-eligible.area
    report['native_scalar_exclusive_m2'] = scalar_exclusive
    correct_state = shapely.difference(native_clipped[1],scope['shared'],grid_size=.0001)
    lost = shapely.difference(correct_state,scope['exclusive'][1],grid_size=.0001)
    (target/'state-missing-native.wkb').write_bytes(shapely.to_wkb(lost))
    report['lost_state_parts'] = [{'area_m2':p.area,'point':list(p.representative_point().coords[0])} for p in shapely.get_parts(lost) if p.area>1]
    report['state_missing_native'] = {'area_m2':lost.area,'parts':len(shapely.get_parts(lost)), 'bounds':list(lost.bounds)}
    for i,g in enumerate(exclusive):
        (target/f'exclusive-source-{i}.wkb').write_bytes(shapely.to_wkb(g))
    prep.write(target/'source-partition.json',report)
    print(json.dumps(report),flush=True)


if __name__=='__main__':
    main()
