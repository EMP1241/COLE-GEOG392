"""Apply the owner's contribution labels without changing any numerical tables."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

HOME=Path(__file__).resolve().parents[1]
ROOT=HOME.parents[1]
CREDITS={
    'application_author':'Brandon Santor',
    'concept_functionality_and_logic':'Brandon Santor',
    'baseline_python_code':'Brandon Santor',
    'application_development_and_integration':'Brandon Santor',
    'data_curation_screening_validation':'Brandon Santor',
    'feature_addon_contributor':'Margaret',
    'publishers':['U.S. Geological Survey','U.S. Census Bureau'],
}


def fp(path):
    with path.open('rb') as f:return {'bytes':path.stat().st_size,'sha256':hashlib.file_digest(f,'sha256').hexdigest()}


def write(path,value):path.write_text(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8')


def normalize(value):
    if isinstance(value,dict):return {key:CREDITS if key=='credits' else normalize(item) for key,item in value.items() if key!='preparation_execution'}
    if isinstance(value,list):return [normalize(item) for item in value]
    return value


def main():
    prepared=HOME/'data';runtime=ROOT/'src/cole/features/greenspace/data'
    receipt=HOME/'verification/attribution-revision-2026-10-22.json'
    if receipt.exists():raise RuntimeError('Refuse to replace an attribution revision')
    archive=HOME/'working/attribution-before-2026-10-22';archive.mkdir(exist_ok=False)
    targets=[prepared/'greenspace.sqlite3',prepared/'manifest.json',runtime/'greenspace.sqlite3',runtime/'manifest.json',runtime/'preparation-manifest.json']
    before={p.relative_to(ROOT).as_posix():fp(p) for p in targets}
    for prefix,directory in (('prepared',prepared),('runtime',runtime)):
        shutil.copytree(directory,archive/prefix)
    original=archive/'prepared/greenspace.sqlite3';database=prepared/'greenspace.sqlite3'
    revision={'created_utc':datetime.now(timezone.utc).isoformat(),'parent_database':fp(original),
              'parent_manifest':fp(archive/'prepared/manifest.json'),
              'scope':'Contributor labels only; all numerical, accounting and county-resolution tables unchanged. Embedded contribution labels follow the current owner attribution; original evidence is retained separately.'}
    with sqlite3.connect(database) as c:
        metadata={key:normalize(json.loads(value)) for key,value in c.execute('SELECT key,value_json FROM metadata')}
        metadata['credits']=CREDITS;metadata['attribution_revision']=revision
        c.executemany('UPDATE metadata SET value_json=? WHERE key=?',[(json.dumps(v,sort_keys=True),k) for k,v in metadata.items() if k!='attribution_revision'])
        c.execute('INSERT INTO metadata VALUES(?,?)',('attribution_revision',json.dumps(revision,sort_keys=True)))
        c.execute('DELETE FROM credits')
        c.executemany('INSERT INTO credits VALUES(?,?)',[(k,v if isinstance(v,str) else '; '.join(v)) for k,v in CREDITS.items()])
        c.commit();c.execute('VACUUM')
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Revised SQLite integrity failure')
        with sqlite3.connect(original) as old:
            for table in ('county_metrics','county_accounting','county_resolutions'):
                if c.execute('SELECT * FROM '+table+' ORDER BY county_fips').fetchall()!=old.execute('SELECT * FROM '+table+' ORDER BY county_fips').fetchall():
                    raise RuntimeError('Numerical table changed: '+table)
    manifest=normalize(json.loads((prepared/'manifest.json').read_text(encoding='utf-8')))
    manifest.update(credits=CREDITS,attribution_revision=revision,database={'file':database.name,**fp(database)})
    write(prepared/'manifest.json',manifest)
    shutil.copy2(database,runtime/'greenspace.sqlite3')
    shutil.copy2(prepared/'manifest.json',runtime/'preparation-manifest.json')
    write(runtime/'manifest.json',{**manifest,'feature_version':'1.0.0','preparation_manifest_sha256':fp(prepared/'manifest.json')['sha256']})
    after={p.relative_to(ROOT).as_posix():fp(p) for p in targets}
    write(receipt,{'status':'passed','created_utc':revision['created_utc'],'credits':CREDITS,
                   'archive':archive.relative_to(ROOT).as_posix(),'before':before,'after':after,
                   'numerical_tables_byte_value_identical':True,'original_source_inputs_changed':False})
    print(json.dumps({'status':'passed','database':manifest['database'],'changed_files':list(after)}))


if __name__=='__main__':main()
