"""Classify original Hawaii linework by independent ray/angle winding rules."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import shapely

from investigate_glades import winding_faces
from investigate_rings import signed_area
from investigate_upper_snake import digest_file, require


def main():
    home=Path(__file__).resolve().parents[1]
    out=home/'working/hawaii-coverage-2026-10-22'
    dest=home/'verification/hawaii-coverage-diagnosis.json'
    require(not out.exists() and not dest.exists(),'Refuse to overwrite coverage evidence')
    diagnosis=json.loads((home/'verification/hawaii-diagnosis.json').read_text())
    source_path=home/'working/hawaii-investigation-2026-10-22/original-default.wkb'
    require(digest_file(source_path)==diagnosis['source_original_wkb_sha256'],'Source artifact changed')
    source=shapely.from_wkb(source_path.read_bytes())
    audit=home/'working/national-prep-eastern-canal-2026-10-22/repair_audit.sqlite3'
    require(digest_file(audit)==diagnosis['previous_audit_sha256'],'Stopped audit changed')
    with sqlite3.connect(audit.resolve().as_uri()+'?mode=ro',uri=True) as c:
        candidate=shapely.from_wkb(c.execute("SELECT polygon_wkb FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=199053").fetchone()[0])
    rings=[list(r.coords) for p in source.geoms for r in [p.exterior,*p.interiors]]
    print(json.dumps({'phase':'original_winding_faces','rings':len(rings)}),flush=True)
    noded,faces,positive,odd,negative,zero=winding_faces(rings)
    print(json.dumps({'phase':'compare_candidate','faces':len(faces)}),flush=True)
    added=positive.difference(candidate)
    removed=candidate.difference(positive)
    symmetric=positive.symmetric_difference(candidate)
    report={'created_utc':datetime.now(timezone.utc).isoformat(),'credit':'Margaret','decision':'coverage_diagnosis_only_not_accepted',
            'source_layer':'PADUS4_1Fee','source_fid':199053,'diagnosis_sha256':digest_file(home/'verification/hawaii-diagnosis.json'),
            'source_original_wkb_sha256':diagnosis['source_original_wkb_sha256'],'previous_audit_sha256':diagnosis['previous_audit_sha256'],
            'source_face_counts_by_winding':{str(w):sum(f['winding']==w for f in faces) for w in sorted({f['winding'] for f in faces})},
            'source_signed_area_m2':-math.fsum(signed_area(r) for r in rings),
            'weighted_face_area_m2':math.fsum(f['area_m2']*f['winding'] for f in faces),
            'positive_area_m2':positive.area,'positive_valid':bool(positive.is_valid),'positive_parts':len(list(shapely.get_parts(positive))),
            'negative_area_m2':negative.area,'zero_area_m2':zero.area,'odd_area_m2':odd.area,
            'candidate_vs_odd_symmetric_difference_m2':candidate.symmetric_difference(odd).area,
            'positive_minus_candidate_m2':added.area,'candidate_minus_positive_m2':removed.area,'candidate_symmetric_difference_m2':symmetric.area,
            'overlay_partition_residual_m2':symmetric.area-added.area-removed.area,
            'overlay_area_accounting_residual_m2':positive.area-candidate.area-added.area+removed.area,
            'positive_boundary_absent_from_noded_source_m':positive.boundary.difference(noded).length,
            'source_linework_length_m':shapely.MultiLineString(rings).length,'faces':faces,'artifacts':{}}
    out.mkdir(parents=True)
    for name,g in [('source-positive-region.wkb',positive),('source-negative-region.wkb',negative),('source-zero-region.wkb',zero),('source-odd-region.wkb',odd),('source-noded-linework.wkb',noded),('positive-minus-candidate.wkb',added),('candidate-minus-positive.wkb',removed)]:
        b=shapely.to_wkb(g,byte_order=1)
        (out/name).write_bytes(b)
        report['artifacts'][name]={'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'faces','artifacts'}}),flush=True)


if __name__=='__main__':
    main()
