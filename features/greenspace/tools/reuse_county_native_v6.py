"""Certify unchanged native calculations when adding a failed-county fallback."""
import ast
import json
from pathlib import Path
import sqlite3
import aggregate_counties as p


def functions(path):
    return {n.name:n for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef)}


def main():
    old_path=p.RUN/'aggregate_counties-overlay-v6.py'
    source_path=p.RUN/'county_results-v6.sqlite3'
    target_path=p.RUN/p.RESULT_NAME
    receipt_path=p.RUN/'native-v6-prefix-reuse.json'
    if target_path.exists() or receipt_path.exists():
        raise RuntimeError('Existing preserved result/certificate')
    old,new=functions(old_path),functions(Path(p.__file__))
    native=new['calculate_county_native']
    native.name='calculate_county'
    if ast.dump(old['calculate_county'],include_attributes=False)!=ast.dump(native,include_attributes=False):
        raise RuntimeError('Native calculation changed')
    for name in set(old)-{'calculate_county','aggregate'}:
        if ast.dump(old[name],include_attributes=False)!=ast.dump(new[name],include_attributes=False):
            raise RuntimeError(f'Native helper changed {name}')
    original_sha=p.sha(source_path)
    p.check(source_path,'5a1d91c46de3239bcd63264dc7d744321b81bf4641ac4a0c2e56c3a9561e61a8')
    with p.ro(source_path) as source, sqlite3.connect(target_path) as dest:
        identity=json.loads(source.execute("SELECT value_json FROM metadata WHERE key='aggregation_identity'").fetchone()[0])
        if identity['code_sha256']!=p.sha(old_path) or identity['policy']!=p.POLICY or identity['credits']!=p.CREDITS:
            raise RuntimeError('Historical identity mismatch')
        source.backup(dest)
        for table in ('county_metrics','county_geometries','county_source_evidence','county_accounting'):
            if source.execute(f'SELECT * FROM {table} ORDER BY 1,2').fetchall()!=dest.execute(f'SELECT * FROM {table} ORDER BY 1,2').fetchall():
                raise RuntimeError(f'Copied output differs {table}')
        receipt={'status':'passed','source_database':source_path.name,'source_database_sha256':original_sha,
                 'native_code_sha256':p.sha(old_path),'current_code_sha256':p.sha(Path(p.__file__)),
                 'county_fips':[r[0] for r in source.execute('SELECT county_fips FROM county_metrics ORDER BY county_fips')],
                 'native_calculation_and_helpers_ast_identical':True,'all_result_tables_identical':True,
                 'source_identity':identity,'new_behavior':'Only formerly failing ownership-accounting results can use logged scalar-area identity fallback.'}
        identity['code_sha256']=p.sha(Path(p.__file__))
        dest.execute("UPDATE metadata SET value_json=? WHERE key='aggregation_identity'",(json.dumps(identity,sort_keys=True),))
        dest.execute('INSERT INTO metadata VALUES(?,?)',('certified_native_v6_prefix',json.dumps(receipt,sort_keys=True)))
    p.check(source_path,original_sha)
    p.write(receipt_path,receipt)
    print(json.dumps({'status':'passed','counties_reused':len(receipt['county_fips'])}),flush=True)


if __name__=='__main__':
    main()
