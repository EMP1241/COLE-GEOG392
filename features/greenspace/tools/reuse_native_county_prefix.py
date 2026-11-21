"""Certify reuse of native results after changing only accounting stop policy.

Old county metrics/geometries remain byte-for-byte copied. Only the derived
combined review ledger is recalculated, adding every stored discrepancy.
"""

import ast
import json
from pathlib import Path
import sqlite3

import aggregate_counties as preparation


def functions(path):
    return {node.name: node for node in ast.parse(path.read_text(encoding="utf-8")).body if isinstance(node, ast.FunctionDef)}


def calculation(node):
    # Ignore raising validation gates and the combined accounting assignment;
    # preserve every geometry, classification, area and output expression.
    node.body = [statement for statement in node.body
                 if not (isinstance(statement, ast.If) and len(statement.body) == 1 and isinstance(statement.body[0], ast.Raise))
                 and not (isinstance(statement, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "combined_sensitivity" for t in statement.targets))]
    return ast.dump(node, include_attributes=False)


def main():
    source_path = preparation.RUN / "county_results-v5.sqlite3"
    output_path = preparation.RUN / preparation.RESULT_NAME
    receipt_path = preparation.RUN / "native-prefix-reuse.json"
    snapshot = preparation.RUN / "aggregate_counties-overlay-v5.py"
    current = Path(preparation.__file__)
    if output_path.exists() or receipt_path.exists():
        raise RuntimeError("Refuse to overwrite a county calculation or reuse certificate")
    old_functions, new_functions = functions(snapshot), functions(current)
    if set(old_functions) != set(new_functions):
        raise RuntimeError("Function inventory changed")
    for name in old_functions:
        if name == "calculate_county":
            if calculation(old_functions[name]) != calculation(new_functions[name]):
                raise RuntimeError("County geometry/metric calculation changed; prefix cannot be reused")
        elif ast.dump(old_functions[name], include_attributes=False) != ast.dump(new_functions[name], include_attributes=False):
            raise RuntimeError(f"Other preparation function changed: {name}")
    original_sha = preparation.sha(source_path)
    source = preparation.ro(source_path)
    old_identity = json.loads(source.execute("SELECT value_json FROM metadata WHERE key='aggregation_identity'").fetchone()[0])
    if (old_identity["code_sha256"] != preparation.sha(snapshot)
            or old_identity["stage_manifest_sha256"] != preparation.sha(preparation.RUN / "stage-manifest.json")
            or old_identity["repair_summary_sha256"] != preparation.SUMMARY_SHA
            or old_identity["policy"] != preparation.POLICY or old_identity["credits"] != preparation.CREDITS):
        raise RuntimeError("Native prefix identity differs")
    _, _, corefile = preparation.counties()
    if old_identity["core_geography_sha256"] != preparation.sha(corefile):
        raise RuntimeError("County reference changed")
    target = sqlite3.connect(output_path)
    source.backup(target)
    rows = target.execute("SELECT a.*,m.geometry_review_allowance_m2,m.county_area_m2 FROM county_accounting a JOIN county_metrics m USING(county_fips) ORDER BY county_fips").fetchall()
    for row in rows:
        code,tolerance,partition,contact,outside_public,outside_county,mapped,exclusion,old_combined,repair,denominator = row
        combined = (repair + 8*tolerance + partition + mapped + exclusion + contact + outside_public + outside_county)/denominator*100
        if combined > 0.01:
            raise RuntimeError(f"Native prefix exceeds combined county budget {code}")
        target.execute("UPDATE county_accounting SET combined_review_sensitivity_pp=? WHERE county_fips=?", (combined,code))
    new_identity = {**old_identity, "code_sha256": preparation.sha(current), "numeric_accounting": preparation.NUMERIC_ACCOUNTING}
    receipt = {"status": "passed", "source_database": source_path.name, "source_database_sha256": original_sha,
               "original_code_sha256": preparation.sha(snapshot), "current_code_sha256": preparation.sha(current),
               "county_fips": [row[0] for row in rows], "counties_reused": len(rows),
               "geometry_classification_and_metric_expressions_unchanged": True,
               "changed_fields": ["county_accounting.combined_review_sensitivity_pp", "metadata.aggregation_identity"],
               "source_database_unchanged": preparation.sha(source_path) == original_sha,
               "certificate_script_sha256": preparation.sha(Path(__file__))}
    target.execute("UPDATE metadata SET value_json=? WHERE key='aggregation_identity'", (json.dumps(new_identity, sort_keys=True),))
    target.execute("INSERT INTO metadata VALUES(?,?)", ("certified_native_prefix",json.dumps(receipt,sort_keys=True)))
    target.commit()
    for table in ("county_metrics", "county_geometries", "county_source_evidence"):
        if source.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall() != target.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall():
            raise RuntimeError(f"Native prefix scalar/geometry copy changed: {table}")
    target.close()
    source.close()
    if not receipt["source_database_unchanged"]:
        raise RuntimeError("Native prefix changed")
    preparation.write(receipt_path, receipt)
    print(json.dumps({"status": "passed", "counties_reused": len(rows)}), flush=True)


if __name__ == "__main__":
    main()
