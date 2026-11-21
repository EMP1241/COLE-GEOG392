"""Carry exact passing v7 rows into v8, with historical identities retained."""
import ast
import json
from pathlib import Path
import sqlite3
import aggregate_counties as p


def main():
    old_path=p.RUN/'aggregate_counties-overlay-v7.py'
    source_path=p.RUN/'county_results-v7.sqlite3'
    target_path=p.RUN/p.RESULT_NAME
    receipt_path=p.RUN/'native-v7-prefix-reuse.json'
    if target_path.exists() or receipt_path.exists():raise RuntimeError('Existing result/certificate')
    def functions(path):return {n.name:n for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef)}
    old,new=functions(old_path),functions(Path(p.__file__))
    new['calculate_county_v7'].name='calculate_county'
    for name,node in old.items():
        compare=new['calculate_county_v7'] if name=='calculate_county' else new[name]
        if ast.dump(node,include_attributes=False)!=ast.dump(compare,include_attributes=False):
            raise RuntimeError(f'Existing execution changed {name}')
    original_sha=p.sha(source_path)
    with p.ro(source_path) as source, sqlite3.connect(target_path) as dest:
        identity=json.loads(source.execute("SELECT value_json FROM metadata WHERE key='aggregation_identity'").fetchone()[0])
        if identity['code_sha256']!=p.sha(old_path) or identity['policy']!=p.POLICY or identity['credits']!=p.CREDITS:raise RuntimeError('Historical identity mismatch')
        source.backup(dest)
        for table in ('county_metrics','county_geometries','county_source_evidence','county_accounting','county_resolutions'):
            left,right=source.execute(f'SELECT * FROM {table} ORDER BY 1,2'),dest.execute(f'SELECT * FROM {table} ORDER BY 1,2')
            while True:
                a,b=left.fetchmany(100),right.fetchmany(100)
                if a!=b:raise RuntimeError(f'Copied rows differ {table}')
                if not a:break
        receipt={'status':'passed','source_database':source_path.name,'source_database_sha256':original_sha,
                 'native_code_sha256':p.sha(old_path),'current_code_sha256':p.sha(Path(p.__file__)),
                 'county_fips':[r[0] for r in source.execute('SELECT county_fips FROM county_metrics ORDER BY county_fips')],
                 'existing_execution_ast_identical':True,'all_result_tables_identical':True,'source_identity':identity}
        identity['code_sha256']=p.sha(Path(p.__file__))
        dest.execute("UPDATE metadata SET value_json=? WHERE key='aggregation_identity'",(json.dumps(identity,sort_keys=True),))
        dest.execute('INSERT INTO metadata VALUES(?,?)',('certified_native_v7_prefix',json.dumps(receipt,sort_keys=True)))
    p.check(source_path,original_sha)
    p.write(receipt_path,receipt)
    print(json.dumps({'status':'passed','counties_reused':len(receipt['county_fips'])}),flush=True)


if __name__=='__main__':main()
